#!/usr/bin/env python3
"""memory-ledger setup — everything a machine needs, on macOS, Linux or Windows.

    python3 scripts/setup.py [--voice] [--piper] [--stt-model medium]
                             [--launcher] [--skip-venv] [--skip-ui] [--json]

`scripts/setup.sh` and `scripts/setup.ps1` are thin wrappers that find a Python
and run this. Every step is idempotent: run it again after an upgrade and it
does only what is missing.

1. Python: 3.11 or newer, the one running this.
2. The server's venv (what `serve.py` builds on first start), from
   `scripts/requirements-server.txt`.
3. The UI: `npm ci` and `npm run build` in `frontend/` (a portable Node LTS is
   downloaded and checked against nodejs.org's SHA-256 when npm is missing; skipped
   when the bundle is already built, as in a release).
4. Voice, with `--voice`: a separate venv (`_platform.voice_venv()`) with
   faster-whisper, and the STT model (`--stt-model`, default `medium`)
   downloaded into it once; `--piper` adds Piper there too. Without it, the
   TTS engine and ffmpeg are only detected and reported.
5. The brain: `graph.py init`, which creates it (with the built-in skills) or
   tops up what is missing.
6. `doctor.py`, whose verdict is the exit code.

Stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _node  # noqa: E402
import _platform  # noqa: E402
import _textio  # noqa: E402

MIN_PY = (3, 11)
VOICE_PACKAGES = ["faster-whisper", "numpy"]

steps: list[dict] = []
QUIET = False


def say(msg: str) -> None:
    if not QUIET:
        print(msg, flush=True)


def step(name: str, ok: bool | None, detail: str = "") -> dict:
    """Record one step. ok None is skipped: not a failure."""
    row = {"step": name, "ok": ok, "detail": detail}
    steps.append(row)
    mark = "ok  " if ok else ("skip" if ok is None else "FAIL")
    say(f"[{mark}] {name}" + (f": {detail}" if detail else ""))
    return row


def run(cmd: list, cwd: Path | None = None) -> tuple[bool, str]:
    """Run a toolchain command, streaming its output unless --json."""
    try:
        if QUIET:
            r = _textio.run_text([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                                 stdin=subprocess.DEVNULL)
            out = (r.stdout or "") + (r.stderr or "")
        else:
            r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                               env=_textio.child_env(), stdin=subprocess.DEVNULL)
            out = ""
    except OSError as e:
        return False, f"{cmd[0]} could not start: {e}"
    if r.returncode != 0:
        return False, f"{Path(str(cmd[0])).name} exited {r.returncode}" + (f": {out.strip()[-300:]}" if out else "")
    return True, ""


@contextlib.contextmanager
def stdout_to_stderr():
    """With --json, stdout carries the report only: what serve.py and its
    children (pip, npm) print goes to stderr, file descriptor and all."""
    if not QUIET:
        yield
        return
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


# ---------------------------------------------------------------- steps
def check_python() -> bool:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PY
    step("python", ok, f"{v.major}.{v.minor}.{v.micro} at {sys.executable}"
         + ("" if ok else f" — {MIN_PY[0]}.{MIN_PY[1]} or newer is needed"))
    return ok


def server_venv() -> None:
    import serve
    d = serve.venv_dir()
    if (d / serve.READY).exists() or os.environ.get("MEMORY_LEDGER_VENV", "").strip():
        step("server venv", _platform.venv_python(d).exists(), str(d))
        return
    with stdout_to_stderr():
        why = serve._build_venv(d)
    step("server venv", not why, why or str(d))


def ui() -> None:
    import serve
    if (serve.INDEX.exists() and not serve.ui_stale()):
        step("ui", True, f"already built in {serve.DIST}")
        return
    _platform.use_portable_node()
    if not shutil.which("npm"):
        why = _node.portable_node()
        if why:
            step("ui", None, f"no npm and no portable Node ({why}); the API runs without the page")
            return
        _platform.use_portable_node()
    with stdout_to_stderr():
        why = serve.ensure_ui(auto=True)
    step("ui", not why, why or f"built in {serve.DIST}")


def launcher() -> None:
    import launcher as launcher_mod
    ok, detail = launcher_mod.install()
    step("launcher", ok, detail)


def voice_venv(stt_model: str, piper: bool) -> None:
    d = _platform.voice_venv()
    py = _platform.venv_python(d)
    if not py.exists():
        say(f"creating the voice venv in {d}")
        ok, err = run([sys.executable, "-m", "venv", str(d)])
        if not ok:
            step("voice venv", False, err + " (on Debian/Ubuntu: apt install python3-venv)")
            return
    pkgs = VOICE_PACKAGES + (["piper-tts"] if piper else [])
    ok, err = run([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                   "-q", "--upgrade", *pkgs])
    step("voice venv", ok, err or f"{', '.join(pkgs)} in {d}")
    if not ok:
        return
    # Loading the model once is what downloads it; later loads are offline.
    say(f"downloading the STT model {stt_model!r} (once; this can take a while)")
    ok, err = run([py, "-c", "import sys; from faster_whisper import WhisperModel; "
                   "WhisperModel(sys.argv[1], device='cpu', compute_type='int8')", stt_model])
    step("stt model", ok, err or f"{stt_model} (set BRAIN_STT_MODEL={stt_model} if not medium)")


def tts_report() -> None:
    import voice
    eng = voice.engine()
    why = eng.problem()
    # no engine is not a failure: the brain works without speech
    step("tts engine", True if not why else None,
         f"{eng.name}" + (f" — {why}" if why else f", {len(eng.voices())} voice(s)"))
    ff = shutil.which("ffmpeg")
    if eng.name == "say":
        # optional like the engine itself: without it `say` cannot write WAV, but the brain works
        step("ffmpeg", True if ff else None, ff or "needed by `say` to write WAV: brew install ffmpeg")
    else:
        step("ffmpeg", True if ff else None, ff or f"not needed by {eng.name}")


def stt_report() -> None:
    import voice
    py = voice._voice_python()
    step("stt", True if py else None,
         " ".join(py) if py else "no Python with faster-whisper: run setup with --voice")


def brain() -> None:
    r = _textio.run_text([sys.executable, str(HERE / "graph.py"), "init"], stdin=subprocess.DEVNULL)
    try:
        out = json.loads(r.stdout or "{}")
    except ValueError:
        out = {}
    sk = out.get("skills") or {}
    step("brain", r.returncode == 0 and out.get("ok") is True,
         (f"{out.get('db')}; built-in skills: "
          + (sk.get("skipped") or (f"{sk.get('ingested')} ingested" if sk.get("ingested") else "all present")))
         if out.get("ok") else (r.stderr or r.stdout).strip()[-300:])


def doctor() -> bool:
    r = _textio.run_text([sys.executable, str(HERE / "doctor.py"), "--json"], stdin=subprocess.DEVNULL)
    try:
        rep = json.loads(r.stdout or "{}")
    except ValueError:
        rep = {}
    ok = r.returncode == 0 and rep.get("ok") is True
    step("doctor", ok, "healthy" if ok else "; ".join(rep.get("problems") or [r.stderr.strip()[-300:]]))
    return ok


def main(argv=None) -> int:
    global QUIET
    ap = argparse.ArgumentParser(prog="setup", description=__doc__.split("\n\n")[0])
    ap.add_argument("--voice", action="store_true",
                    help="install faster-whisper in a voice venv and download the STT model")
    ap.add_argument("--piper", action="store_true", help="with --voice: also install Piper (TTS on any OS)")
    ap.add_argument("--stt-model", default=os.environ.get("BRAIN_STT_MODEL", "medium"),
                    help="faster-whisper model to download (default medium)")
    ap.add_argument("--launcher", action="store_true",
                    help="also put a launcher on the Desktop that starts everything")
    ap.add_argument("--skip-venv", action="store_true", help="do not build the server's venv")
    ap.add_argument("--skip-ui", action="store_true", help="do not build the UI")
    ap.add_argument("--json", action="store_true", help="print one JSON report instead of progress")
    a = ap.parse_args(argv)
    QUIET = a.json

    say(f"memory-ledger setup ({sys.platform}) in {ROOT}")
    if check_python():
        if a.skip_venv:
            step("server venv", None, "--skip-venv")
        else:
            server_venv()
        if a.skip_ui:
            step("ui", None, "--skip-ui")
        else:
            ui()
        if a.voice:
            voice_venv(a.stt_model, a.piper)
        tts_report()
        stt_report()
        brain()
        doctor()
        if a.launcher:
            launcher()
    failed = [s["step"] for s in steps if s["ok"] is False]
    if a.json:
        print(json.dumps({"ok": not failed, "platform": sys.platform, "steps": steps,
                          "failed": failed}, indent=2, ensure_ascii=False))
    else:
        say("\n" + ("ready" if not failed else f"not ready: {', '.join(failed)} failed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
