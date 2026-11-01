"""A portable Node.js LTS for machines without npm.

Downloaded from nodejs.org into `_platform.node_home()` and checked against the
SHA-256 it publishes. Used by `setup.py` and by `serve.py` on start, so the first
start can build the UI with Python as the only prerequisite.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import _net
import _platform

NODE_MAJOR = 20                 # the oldest LTS the UI builds on; the newest LTS is fetched


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _node_target() -> tuple[str, str, str]:
    """(nodejs.org platform, arch, archive extension) for this machine."""
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(), "x64")
    if _platform.WINDOWS:
        return "win", arch, "zip"
    return ("darwin" if sys.platform == "darwin" else "linux"), arch, "tar.xz"


_HAS_TAR_FILTER = hasattr(tarfile, "data_filter")


def _extract_tar(t: tarfile.TarFile, dest: Path) -> None:
    """`extractall(filter="data")` where the Python has it. Python 3.11.0-3
    (which MIN_PY allows) predates `filter=` and raises TypeError, so there
    the same refusals are made by hand: nothing outside `dest`, no device
    nodes, and links only to somewhere inside `dest`."""
    if _HAS_TAR_FILTER:
        t.extractall(dest, filter="data")
        return
    root = dest.resolve()

    def inside(p: Path) -> bool:
        return p == root or root in p.parents

    for m in t.getmembers():
        if m.isdev() or not inside((root / m.name).resolve()):
            raise tarfile.TarError(f"refusing to unpack {m.name!r}")
        if m.islnk() and not inside((root / m.linkname).resolve()):
            raise tarfile.TarError(f"refusing to unpack {m.name!r}: link leaves the folder")
        if m.issym() and not inside((root / m.name).parent.joinpath(m.linkname).resolve()):
            raise tarfile.TarError(f"refusing to unpack {m.name!r}: link leaves the folder")
    t.extractall(dest)


def portable_node() -> str:
    """Download the newest Node LTS into `_platform.node_home()`, checked against
    the SHA-256 nodejs.org publishes. Returns "" when done, else why not."""
    try:
        index = json.loads(_net.get("https://nodejs.org/dist/index.json"))
        rel = next(r for r in index if r.get("lts") and int(r["version"].lstrip("v").split(".")[0]) >= NODE_MAJOR)
        ver = rel["version"]
        osn, arch, ext = _node_target()
        name = f"node-{ver}-{osn}-{arch}"
        root = _platform.node_home()
        dest = root / ver
        if not dest.exists():
            say(f"downloading Node.js {ver} ({name}.{ext}) into {root}")
            blob = _net.get(f"https://nodejs.org/dist/{ver}/{name}.{ext}")
            sums = _net.get(f"https://nodejs.org/dist/{ver}/SHASUMS256.txt").decode("utf-8")
            want = next(line.split()[0] for line in sums.splitlines() if line.endswith(f"{name}.{ext}"))
            if hashlib.sha256(blob).hexdigest() != want:
                return f"checksum mismatch for {name}.{ext}: refusing to unpack it"
            root.mkdir(parents=True, exist_ok=True)
            tmp = root / f".{ver}.partial"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            archive = tmp / f"node.{ext}"
            archive.write_bytes(blob)
            if ext == "zip":
                with zipfile.ZipFile(archive) as z:
                    z.extractall(tmp)
            else:
                with tarfile.open(archive) as t:
                    _extract_tar(t, tmp)
            archive.unlink()
            (tmp / name).rename(dest)
            shutil.rmtree(tmp, ignore_errors=True)
        link = root / "current"
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            os.rmdir(link)              # a Windows junction: removes the link, not the Node behind it
        _platform.link_dir(link, dest)
        return ""
    except (OSError, ValueError, StopIteration, KeyError, tarfile.TarError, zipfile.BadZipFile) as e:
        return f"{type(e).__name__}: {e}"
