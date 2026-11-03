#!/usr/bin/env python3
"""Image generation on a Hugging Face Space (docs/agents.md#hugging-face-space).

A Space is a Gradio app. This calls its public API over plain HTTP (no dependency):
`POST /gradio_api/call/<endpoint>` returns an event id, `GET` on it streams the result.
The saved Hugging Face token rides in `Authorization`, so a ZeroGPU Space spends the
account's daily GPU quota, not Inference Providers credits (that is what answers 402).

Two rules live here in code, not in a prompt:
  - the wrapper (`generate`) asks for the minimum viable image: the smallest size the Space
    offers for the aspect wanted, and the fewest steps for the quality wanted;
  - the models list is free: Space metadata is public and listing it never starts a GPU job.

The Space is a third party's: the prompt is sent to its server. Run as a script it is the
wrapper an agent calls:  python3 hf_space.py spaces | generate --prompt "..." [--aspect 16:9]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import graph
import providers

KIND = "hfspace"
DEFAULT_SPACE = "a-test-user-in-the-wrold/qwen-image-21-uncensored"
API = "https://huggingface.co/api"
IMAGES_DIR = "images"
# fewest steps that still reads as the thing asked for; the Space's own slider bounds clamp them
QUALITY_STEPS = {"draft": 12, "standard": 20, "final": 28}
DEFAULT_QUALITY = "draft"
ASPECTS = {"square": 1.0, "wide": 16 / 9, "tall": 9 / 16, "landscape": 4 / 3, "portrait": 3 / 4}
SIZE_RE = re.compile(r"(\d{2,5})\s*[x×]\s*(\d{2,5})")


class SpaceError(RuntimeError):
    pass


# ---------------------------------------------------------------- http
def _req(url: str, *, data: Any = None, token: str = "", timeout: float = 30.0) -> urllib.request.Request:
    h = {"User-Agent": "memory-ledger"}
    if data is not None:
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), headers=h)


def _get_json(url: str, token: str = "", timeout: float = 15.0) -> Any:
    try:
        with urllib.request.urlopen(_req(url, token=token), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SpaceError(providers.redact(f"{url.split('?')[0]} answered {e.code}", "huggingface")) from None
    except (OSError, ValueError) as e:
        raise SpaceError(providers.redact(f"could not reach {url.split('?')[0]}: {e}", "huggingface")) from None


# ---------------------------------------------------------------- what is available (free)
def space_info(space: str) -> dict[str, Any]:
    """Public metadata of one Space: stage, hardware, host. Costs nothing."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", space or ""):
        raise SpaceError("a Space is written owner/name")
    d = _get_json(f"{API}/spaces/{space}")
    rt = d.get("runtime") or {}
    hw = (rt.get("hardware") or {}).get("current") or ""
    return {"id": d.get("id") or space, "host": d.get("host") or "", "stage": rt.get("stage") or "",
            "hardware": hw, "sdk": d.get("sdk") or "", "likes": d.get("likes") or 0,
            "free": hw.startswith("zero") or hw.startswith("cpu"),
            "ready": rt.get("stage") == "RUNNING" and d.get("sdk") == "gradio"}


def list_spaces(limit: int = 12) -> list[dict[str, Any]]:
    """Text-to-image Gradio Spaces that are running on ZeroGPU or CPU hardware (the ones a
    token's free quota covers), most liked first. Public metadata only: no GPU job starts."""
    q = urllib.parse.urlencode({"filter": "text-to-image", "sdk": "gradio", "sort": "likes",
                                "limit": max(limit * 3, 20)})
    ids = [s["id"] for s in _get_json(f"{API}/spaces?{q}") if s.get("id")]
    if DEFAULT_SPACE not in ids:
        ids.insert(0, DEFAULT_SPACE)

    def one(i: str) -> dict[str, Any] | None:
        try:
            return space_info(i)
        except SpaceError:
            return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = [r for r in ex.map(one, ids) if r and r["ready"] and r["free"]]
    rows.sort(key=lambda r: (r["id"] != DEFAULT_SPACE, -r["likes"]))
    return rows[:limit]


# ---------------------------------------------------------------- the wrapper's choices
def pick_size(choices: list[str], aspect: str = "") -> str:
    """The smallest of `choices` (strings like '1344x768 (16:9)') for the aspect wanted.
    `aspect` is 'auto'/'' (smallest of all), a name in ASPECTS or 'W:H'. Of the choices
    whose ratio is within 5% of the closest, the one with the fewest pixels."""
    sizes = []
    for c in choices:
        m = SIZE_RE.search(str(c))
        if m:
            sizes.append((int(m[1]), int(m[2]), str(c)))
    if not sizes:
        return ""
    want = _ratio(aspect)
    if want is None:
        return min(sizes, key=lambda s: s[0] * s[1])[2]
    best = min(abs(w / h - want) / want for w, h, _ in sizes)
    near = [s for s in sizes if abs(s[0] / s[1] - want) / want <= best + 0.05]
    return min(near, key=lambda s: s[0] * s[1])[2]


def _ratio(aspect: str) -> float | None:
    a = (aspect or "").strip().lower()
    if a in ASPECTS:
        return ASPECTS[a]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[:/x]\s*(\d+(?:\.\d+)?)", a)
    if m and float(m[2]) > 0 and float(m[1]) > 0:
        return float(m[1]) / float(m[2])
    return None


def pick_steps(quality: str, lo: int | None = None, hi: int | None = None) -> int:
    n = QUALITY_STEPS.get(quality or DEFAULT_QUALITY)
    if n is None:
        raise SpaceError(f"quality is one of {', '.join(QUALITY_STEPS)}")
    if lo is not None:
        n = max(n, int(lo))
    return min(n, int(hi)) if hi is not None else n


# ---------------------------------------------------------------- the Gradio call
def _endpoint(host: str, name: str, token: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """(parameters of `name`, component props by parameter name) from /gradio_api/info and /config."""
    info = _get_json(f"{host}/gradio_api/info", token)
    ep = (info.get("named_endpoints") or {}).get("/" + name.lstrip("/"))
    if not ep:
        raise SpaceError(f"the Space has no endpoint /{name.lstrip('/')} "
                         f"(it has {', '.join(sorted(info.get('named_endpoints') or {})) or 'none'})")
    params = ep.get("parameters") or []
    props: dict[str, dict[str, Any]] = {}
    try:
        cfg = _get_json(f"{host}/config", token)
        comps = {c["id"]: c for c in cfg.get("components") or []}
        deps = [d for d in cfg.get("dependencies") or []
                if d.get("api_name") == name.lstrip("/")]
        if deps:
            for pname, cid in zip([p["parameter_name"] for p in params], deps[0].get("inputs") or []):
                props[pname] = (comps.get(cid) or {}).get("props") or {}
    except SpaceError:
        pass
    return params, props


def _stream(host: str, name: str, data: list[Any], token: str, timeout: float) -> list[Any]:
    try:
        with urllib.request.urlopen(_req(f"{host}/gradio_api/call/{name}", data={"data": data}, token=token),
                                    timeout=30) as r:
            event_id = json.loads(r.read().decode("utf-8"))["event_id"]
    except urllib.error.HTTPError as e:
        raise SpaceError(providers.redact(f"the Space answered {e.code}: "
                                          f"{e.read().decode('utf-8', 'replace')[:300]}", "huggingface")) from None
    except (OSError, ValueError, KeyError) as e:
        raise SpaceError(providers.redact(f"could not start the call: {e}", "huggingface")) from None
    event, deadline = "", time.time() + timeout
    try:
        with urllib.request.urlopen(_req(f"{host}/gradio_api/call/{name}/{event_id}", token=token),
                                    timeout=timeout) as r:
            for raw in r:
                if time.time() > deadline:
                    raise SpaceError("the Space took too long")
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event in ("complete", "error"):
                    body = line[5:].strip()
                    if event == "error":
                        raise SpaceError(providers.redact(_quota_hint(body), "huggingface"))
                    return json.loads(body)
    except SpaceError:
        raise
    except (OSError, ValueError) as e:
        raise SpaceError(providers.redact(f"the call broke off: {e}", "huggingface")) from None
    raise SpaceError("the Space ended without an image")


def _quota_hint(body: str) -> str:
    low = body.lower()
    if "quota" in low or "zerogpu" in low or "gpu" in low and "limit" in low:
        return "ZeroGPU quota used up for this account (resets daily; PRO has more): " + body[:200]
    return "the Space reported an error: " + body[:300]


def _download(url: str, token: str, dest: Path) -> None:
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        with urllib.request.urlopen(_req(url, token=token if host.endswith(".hf.space") else ""),
                                    timeout=60) as r:
            dest.write_bytes(r.read())
    except (OSError, ValueError) as e:
        raise SpaceError(providers.redact(f"could not download the image: {e}", "huggingface")) from None


def generate(prompt: str, *, space: str = "", aspect: str = "auto", quality: str = DEFAULT_QUALITY,
             steps: int = 0, seed: int | None = None, endpoint: str = "generate",
             out_dir: Path | None = None, timeout: float = 300.0) -> dict[str, Any]:
    """One image. Asks for the smallest size the Space offers for `aspect` and the fewest steps
    for `quality`. Returns {ok, path, space, size, steps, seed}. Raises SpaceError with the key masked."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise SpaceError("say what the image should show")
    token = providers.load_key("huggingface")
    if not token:
        raise SpaceError("save an HF_TOKEN first (Settings > AI providers): without it the "
                         "anonymous GPU quota is tiny")
    info = space_info(space or DEFAULT_SPACE)
    if not info["ready"]:
        raise SpaceError(f"{info['id']} is not running (stage {info['stage'] or 'unknown'})")
    host = info["host"]
    params, props = _endpoint(host, endpoint, token)
    values: dict[str, Any] = {"prompt": prompt}
    if "size" in {p["parameter_name"] for p in params}:
        choices = [c[0] if isinstance(c, list) else c for c in (props.get("size") or {}).get("choices") or []]
        values["size"] = pick_size(choices, aspect) or next(
            (p["parameter_default"] for p in params if p["parameter_name"] == "size"), "")
    if "steps" in {p["parameter_name"] for p in params}:
        p = props.get("steps") or {}
        values["steps"] = int(steps) if steps else pick_steps(quality, p.get("minimum"), p.get("maximum"))
    if "seed" in {p["parameter_name"] for p in params}:
        values["seed"] = int(seed) if seed is not None else 0
        values["randomize_seed"] = seed is None
    data = [values.get(p["parameter_name"], p.get("parameter_default")) for p in params]
    result = _stream(host, endpoint.lstrip("/"), data, token, timeout)
    img = next((x for x in result if isinstance(x, dict) and (x.get("url") or x.get("path"))), None)
    if not img:
        raise SpaceError("the Space answered without an image")
    url = img.get("url") or f"{host}/gradio_api/file={img['path']}"
    out = out_dir or (graph.home_dir() / IMAGES_DIR)
    out.mkdir(parents=True, exist_ok=True)
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    dest = out / f"hf-{time.strftime('%Y%m%d-%H%M%S')}{ext if ext in ('.png', '.jpg', '.jpeg', '.webp') else '.png'}"
    _download(url, token, dest)
    used = next((x for x in result if isinstance(x, int) and not isinstance(x, bool)), values.get("seed"))
    return {"ok": True, "path": str(dest), "space": info["id"], "size": values.get("size", ""),
            "steps": values.get("steps"), "seed": used}


def ping() -> dict[str, Any]:
    """Free check for the Test call button: the token is valid and the default Space is up.
    No GPU job starts, so it spends no quota."""
    token = providers.load_key("huggingface")
    if not token:
        return {"ok": False, "error": "save an HF_TOKEN first (Settings > AI providers)"}
    try:
        who = _get_json(f"{API}/whoami-v2", token)
        info = space_info(DEFAULT_SPACE)
    except SpaceError as e:
        return {"ok": False, "error": str(e)}
    if not info["ready"]:
        return {"ok": False, "error": f"{info['id']} is not running (stage {info['stage'] or 'unknown'})"}
    return {"ok": True, "answer": f"token ok ({who.get('name', '?')}{', PRO' if who.get('isPro') else ''}); "
                                  f"{info['id']} running on {info['hardware']}"}


# ---------------------------------------------------------------- the runner for a connector turn
def brief_options(brief: str) -> tuple[str, dict[str, str]]:
    """A delegated brief may start with `aspect=16:9 quality=draft` lines; the rest is the prompt."""
    opts: dict[str, str] = {}
    rest = []
    for line in brief.strip().splitlines():
        m = re.fullmatch(r"\s*(aspect|quality)\s*[=:]\s*(\S+)\s*", line, re.I)
        if m and not rest:
            opts[m[1].lower()] = m[2]
        else:
            rest.append(line)
    return "\n".join(rest).strip(), opts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("spaces", help="free text-to-image Spaces that are running now")
    g = sub.add_parser("generate", help="one image, at the minimum size and steps that fit")
    g.add_argument("--prompt", required=True)
    g.add_argument("--space", default="", help=f"owner/name, default {DEFAULT_SPACE}")
    g.add_argument("--aspect", default="auto", help="square, wide, tall, landscape, portrait, W:H or auto (smallest)")
    g.add_argument("--quality", default=DEFAULT_QUALITY, choices=list(QUALITY_STEPS))
    g.add_argument("--seed", type=int)
    a = ap.parse_args(argv)
    try:
        res = ({"ok": True, "spaces": list_spaces()} if a.cmd == "spaces" else
               generate(a.prompt, space=a.space, aspect=a.aspect, quality=a.quality, seed=a.seed))
    except SpaceError as e:
        res = {"ok": False, "error": str(e)}
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
