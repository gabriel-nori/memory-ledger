#!/usr/bin/env python3
"""Documents the agents write for the owner (HTML, Markdown, PDF, plain text,
images) and the files the owner attaches to a message.

Headless runs cannot write artifacts, so the document skills (skills/document-*)
write a file under `<home>/exports/documents/` and the reply links it as
`[name](doc:<path relative to exports>)`. The Agents tab shows that link as a
collapsed card (the name and an expand button) that opens the file in an overlay.

Attachments the owner adds in the chat are saved under `<home>/attachments/`
(any type, up to 25 MB) and shown as `[name](attach:<path relative to attachments>)`.

This module is the only reader of those files. It serves nothing outside
`<home>/exports` and `<home>/attachments`: a path must be relative, resolve
inside its area (symlinks included), be a known kind (exports only), and fit
the size cap. No model call.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
from datetime import datetime
from pathlib import Path

import graph

KINDS = {
    ".md": "md", ".markdown": "md",
    ".txt": "txt", ".log": "txt", ".csv": "txt", ".json": "txt",
    ".html": "html", ".htm": "html",
    ".pdf": "pdf",
    ".png": "img", ".jpg": "img", ".jpeg": "img", ".gif": "img", ".webp": "img", ".svg": "img",
}
TEXT_KINDS = {"md", "txt", "html"}
MAX_BYTES = 5 * 1024 * 1024
ATTACH_MAX_BYTES = 25 * 1024 * 1024
# Text is always UTF-8 (make_document.py writes it so); naming the charset keeps a
# browser from guessing the locale's code page and rendering mojibake.
MEDIA = {"md": "text/markdown; charset=utf-8", "txt": "text/plain; charset=utf-8",
         "html": "text/html; charset=utf-8", "pdf": "application/pdf"}
IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
AREAS = ("exports", "attachments", "images")


def root(area: str = "exports") -> Path:
    return (graph.home_dir() / (area if area in AREAS else "exports")).resolve()


def kind_of(target: Path) -> str:
    """The kind of a file; an attachment of any other type is a plain `file`."""
    return KINDS.get(target.suffix.lower(), "file")


def resolve(rel: str, area: str = "exports") -> Path:
    """The file `rel` names in `area`, or ValueError saying why it cannot be read."""
    if not rel or rel.startswith(("/", "~")) or "\x00" in rel:
        raise ValueError(f"a document path is relative to <home>/{area}")
    base = root(area)
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"path leaves <home>/{area}")
    attached = area == "attachments"
    if not attached and target.suffix.lower() not in KINDS:
        raise ValueError(f"unsupported type: {target.suffix or 'none'}")
    if not target.is_file():
        raise ValueError("no such document")
    if target.stat().st_size > (ATTACH_MAX_BYTES if attached else MAX_BYTES):
        raise ValueError(f"document is over {25 if attached else 5} MB")
    return target


def read(rel: str, area: str = "exports") -> dict:
    """Name, kind and size; the text too for md, txt and html (a PDF or an image is fetched raw)."""
    try:
        target = resolve(rel, area)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    kind = kind_of(target)
    out = {"ok": True, "name": target.name, "path": rel, "kind": kind,
           "size": target.stat().st_size, "text": None}
    if kind in TEXT_KINDS:
        out["text"] = target.read_text(encoding="utf-8", errors="replace")
    return out


def media_type(target: Path) -> str:
    suffix = target.suffix.lower()
    return IMAGE_MEDIA.get(suffix) or MEDIA.get(KINDS.get(suffix, ""), "application/octet-stream")


def _safe_name(name: str) -> str:
    base = re.sub(r"[^\w.\- ]+", "_", Path(name or "").name).strip(" .")
    return (base or "file")[:120]


def save_attachment(name: str, data_b64: str) -> dict:
    """Store one file the owner attached, under `<home>/attachments/`. The name
    gets a timestamp and a random tag, so two files with one name never collide."""
    try:
        raw = base64.b64decode(data_b64 or "", validate=False)
    except (binascii.Error, ValueError):
        return {"ok": False, "error": "the file is not valid base64"}
    if not raw:
        return {"ok": False, "error": "the file is empty"}
    if len(raw) > ATTACH_MAX_BYTES:
        return {"ok": False, "error": "a file can be at most 25 MB"}
    clean = _safe_name(name)
    rel = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}-{clean}"
    base = root("attachments")
    base.mkdir(parents=True, exist_ok=True)
    (base / rel).write_bytes(raw)
    return {"ok": True, "name": clean, "path": rel, "kind": kind_of(base / rel), "size": len(raw)}
