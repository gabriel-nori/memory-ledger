"""Updates from GitHub Releases: ask what is newest, and install it.

`check()` reads the latest release of REPO (at most once per CHECK_EVERY_S; the
page asks on load and every 8 hours), and reports whether it is newer than the
`version` in `.claude-plugin/plugin.json`. `apply()` downloads that release's
zip, verifies it against the SHA-256 published beside it, swaps the code in and
leaves the restart to `serve.py`, which re-execs when its own files change.

Nothing outside the checkout is touched: the brain (`~/.second_brain`) is
never read or written here. A checkout that is a git clone is not updated this
way (`git pull` is the update); neither is a server started with --no-write.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import _net

ROOT = Path(__file__).resolve().parent.parent.parent
REPO = "gabriel-nori/memory-ledger"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
CHECK_EVERY_S = 8 * 3600
TIMEOUT_S = 30
MAX_ZIP_BYTES = 300 * 1024 * 1024
KEEP = {".git"}                 # never replaced, whatever the release holds
OLD_DIR, NEW_DIR = ".update-old", ".update-new"

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "latest": None, "error": ""}


def current_version() -> str:
    try:
        return str(json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text("utf-8")).get("version", ""))
    except (OSError, ValueError):
        return ""


def parse_version(v: str) -> tuple:
    """'v1.2.3' -> (1, 2, 3); anything unparseable is (), which is never newer."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", (v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _get(url: str, limit: int = MAX_ZIP_BYTES) -> bytes:
    # _net.get, not urlopen: a python.org Python on macOS has no CA bundle
    return _net.get(url, timeout=TIMEOUT_S, limit=limit,
                    headers={"User-Agent": "memory-ledger-updater",
                             "Accept": "application/vnd.github+json"})


def _release() -> dict:
    return json.loads(_get(API, 2_000_000).decode("utf-8"))


def is_checkout() -> bool:
    return (ROOT / ".git").exists()


def check(force: bool = False) -> dict:
    """What the page shows: the running version, the latest, and whether it can be installed."""
    with _lock:
        if force or time.time() - _cache["at"] > CHECK_EVERY_S:
            try:
                rel = _release()
                _cache.update(latest={"tag": rel.get("tag_name", ""), "notes": (rel.get("body") or "")[:4000],
                                      "url": rel.get("html_url", ""), "assets": rel.get("assets") or []},
                              error="")
            except (OSError, ValueError) as e:
                _cache.update(latest=None, error=f"{type(e).__name__}: {e}")
            _cache["at"] = time.time()
        latest, error = _cache["latest"], _cache["error"]
    cur = current_version()
    tag = (latest or {}).get("tag", "")
    available = bool(latest) and parse_version(tag) > parse_version(cur)
    why = ""
    if available and is_checkout():
        why = "this is a git checkout: update it with git pull"
    return {"ok": True, "current": cur, "latest": tag, "available": available,
            "notes": (latest or {}).get("notes", ""), "url": (latest or {}).get("url", ""),
            "installable": available and not why, "why_not": why, "error": error}


def _asset(latest: dict, name: str) -> str:
    for a in latest["assets"]:
        if a.get("name") == name:
            url = a.get("browser_download_url", "")
            if url.startswith(DOWNLOAD_PREFIX):
                return url
    raise ValueError(f"the release has no {name}")


def _safe_extract(blob: bytes, dest: Path) -> Path:
    """Unpack the zip into `dest` and return its single top-level directory."""
    zpath = dest / "release.zip"
    zpath.write_bytes(blob)
    root = dest.resolve()
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            if not (root / n).resolve().is_relative_to(root):
                raise ValueError(f"unsafe path in the release: {n}")
        z.extractall(dest / "x")
    zpath.unlink()
    tops = [p for p in (dest / "x").iterdir()]
    if len(tops) != 1 or not tops[0].is_dir():
        raise ValueError("the release zip must hold exactly one top-level directory")
    return tops[0]


def apply() -> dict:
    """Install the latest release over this checkout. `ok` False carries `error`."""
    info = check(force=True)
    if not info["available"]:
        return {"ok": False, "error": info["error"] or "already up to date"}
    if not info["installable"]:
        return {"ok": False, "error": info["why_not"]}
    tag = info["latest"]
    with _lock:
        latest = _cache["latest"]
    old, new = ROOT / OLD_DIR, ROOT / NEW_DIR
    try:
        name = f"memory-ledger-{tag}.zip"
        blob = _get(_asset(latest, name))
        want = _get(_asset(latest, name + ".sha256"), 4096).decode("utf-8").split()[0].lower()
        if hashlib.sha256(blob).hexdigest() != want:
            return {"ok": False, "error": f"checksum mismatch for {name}: nothing was changed"}
        for d in (old, new):
            shutil.rmtree(d, ignore_errors=True)
        new.mkdir()
        src = _safe_extract(blob, new)
        old.mkdir()
        moved = []
        try:
            for item in sorted(src.iterdir()):
                if item.name in KEEP:
                    continue
                target = ROOT / item.name
                if target.exists():
                    shutil.move(str(target), str(old / item.name))
                moved.append(item.name)
                shutil.move(str(item), str(target))
        except OSError:
            for n in moved:                     # put the previous version back
                shutil.rmtree(ROOT / n, ignore_errors=True)
                if (old / n).exists():
                    shutil.move(str(old / n), str(ROOT / n))
            raise
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(new, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    with _lock:
        _cache["at"] = 0.0
    return {"ok": True, "installed": tag, "restarting": True}
