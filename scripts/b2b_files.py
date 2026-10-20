"""B2B's attachments: zip what is sent, and unzip what arrives without trusting it
(docs/b2b.md#attachments). Stdlib only.

A message carries at most one archive: the files are zipped together, the zip is
sealed (`b2b_crypto.seal_file`) and goes to the relay as one blob. What comes
back is data from another brain, so `extract_zip` refuses a path that leaves its
folder (zip-slip), a link, and an archive that unpacks to far more than it
weighs (zip-bomb), and counts the bytes it actually writes rather than the ones
the archive claims.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath

#: What the relay accepts is 50 MiB per file; the zip leaves room for the seal.
ARCHIVE_MAX = 50 * 1024 * 1024 - 1024
FILES_MAX = 20
#: What an archive may unpack to, in bytes and in entries.
EXTRACT_MAX = 200 * 1024 * 1024
EXTRACT_FILES_MAX = 200
#: An entry bigger than `RATIO_FLOOR` that unpacks to more than `RATIO_MAX` times
#: its compressed size is a bomb, not a document.
RATIO_FLOOR = 1024 * 1024
RATIO_MAX = 200
CHUNK = 64 * 1024


class BadArchive(ValueError):
    """An archive that was not built, or will not be unpacked."""


def zip_files(paths: list[str]) -> tuple[bytes, list[str]]:
    """The files at `paths` as one zip, flat (by file name; a repeated name gets
    a number), and the names in it. Raises `BadArchive`."""
    if not paths:
        raise BadArchive("no files")
    if len(paths) > FILES_MAX:
        raise BadArchive(f"at most {FILES_MAX} files in one message")
    files = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.is_file():
            raise BadArchive(f"{raw} is not a file")
        files.append(p)
    if sum(p.stat().st_size for p in files) > ARCHIVE_MAX * 4:
        raise BadArchive(f"the files are far over {ARCHIVE_MAX // (1024 * 1024)} MB")
    buf = io.BytesIO()
    names: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            name = _unique(p.name, names)
            names.append(name)
            with p.open("rb") as src, z.open(zipfile.ZipInfo(name), "w") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            if buf.tell() > ARCHIVE_MAX:
                raise BadArchive(f"the zip is over {ARCHIVE_MAX // (1024 * 1024)} MB; the "
                                 "relay takes 50 MB per message")
    return buf.getvalue(), names


def _unique(name: str, taken: list[str]) -> str:
    """`name`, or `name` with a number before its extension when taken."""
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        cand = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
        if cand not in taken:
            return cand
        n += 1


def _entry_path(name: str, dest: Path) -> Path:
    """Where an entry goes inside `dest`, or `BadArchive` when it would leave."""
    if not name or "\\" in name or "\x00" in name or name.startswith("/") \
            or (len(name) > 1 and name[1] == ":"):
        raise BadArchive(f"unsafe path in the archive: {name!r}")
    parts = PurePosixPath(name).parts
    if any(part in ("..", "") for part in parts):
        raise BadArchive(f"unsafe path in the archive: {name!r}")
    target = (dest / PurePosixPath(*parts)).resolve()
    if dest.resolve() not in target.parents:
        raise BadArchive(f"unsafe path in the archive: {name!r}")
    return target


def extract_zip(data: bytes, dest: Path) -> list[str]:
    """Unpack `data` into the new folder `dest`, and return the files written
    (paths relative to it). All or nothing: on any refusal `dest` does not exist.
    Raises `BadArchive`."""
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")
    written: list[str] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > EXTRACT_FILES_MAX:
                raise BadArchive(f"the archive has more than {EXTRACT_FILES_MAX} entries")
            if sum(i.file_size for i in infos) > EXTRACT_MAX:
                raise BadArchive("the archive unpacks to more than "
                                 f"{EXTRACT_MAX // (1024 * 1024)} MB")
            tmp.mkdir(parents=True, mode=0o700)
            for info in infos:
                kind = stat.S_IFMT(info.external_attr >> 16)   # 0 when the zip says none
                if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
                    raise BadArchive(f"{info.filename!r} is a link or a special file")
                target = _entry_path(info.filename.rstrip("/") or info.filename, tmp)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if info.file_size > RATIO_FLOOR and \
                        info.file_size > RATIO_MAX * max(1, info.compress_size):
                    raise BadArchive(f"{info.filename!r} unpacks to far more than it weighs")
                target.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out, z.open(info) as src:
                    while chunk := src.read(CHUNK):
                        total += len(chunk)
                        if total > EXTRACT_MAX:
                            raise BadArchive("the archive unpacks to more than "
                                             f"{EXTRACT_MAX // (1024 * 1024)} MB")
                        out.write(chunk)
                written.append(str(target.relative_to(tmp.resolve())))
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(dest)
    except zipfile.BadZipFile as e:
        raise BadArchive(f"not a zip: {e}") from None
    except (OSError, RuntimeError, NotImplementedError, EOFError) as e:
        raise BadArchive(f"cannot unpack: {e}") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return written
