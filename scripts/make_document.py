#!/usr/bin/env python3
"""Save a document the agent wrote so the Agents tab can show it.

    <content> | python3 scripts/make_document.py --format html|md|txt|pdf --title "Q3 report"

Reads the content on stdin and writes `<home>/exports/documents/<stamp>-<slug>.<ext>`
(for `pdf` the content is HTML, printed to PDF with headless Chrome or Chromium).
Prints the line to put in the reply, which the chat turns into an expandable card:

    [Q3 report](doc:documents/20261007-1030-q3-report.html)

Headless runs cannot write artifacts; this is how they hand over a document.
No model call.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import documents
import graph

EXT = {"html": ".html", "md": ".md", "txt": ".txt", "pdf": ".pdf"}
CHROMES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "document"


def chrome() -> str | None:
    for c in CHROMES:
        if Path(c).is_file() or shutil.which(c):
            return c
    return None


def to_pdf(html: str, out: Path) -> None:
    exe = chrome()
    if not exe:
        raise SystemExit("no Chrome or Chromium found to print the PDF; save it as html instead")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.html"
        src.write_text(html, encoding="utf-8")
        subprocess.run([exe, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={out}", src.as_uri()],
                       check=True, capture_output=True, timeout=60)


def save(fmt: str, title: str, content: str) -> str:
    """Write the document; returns its path relative to <home>/exports."""
    folder = documents.root() / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{datetime.now():%Y%m%d-%H%M}-{slug(title)}{EXT[fmt]}"
    if fmt == "pdf":
        to_pdf(content, out)
    else:
        out.write_text(content, encoding="utf-8")
    return out.relative_to(documents.root()).as_posix()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--format", required=True, choices=sorted(EXT))
    ap.add_argument("--title", required=True)
    a = ap.parse_args()
    content = sys.stdin.read()
    if not content.strip():
        raise SystemExit("nothing on stdin")
    rel = save(a.format, a.title, content)
    print(f"[{a.title.replace(']', ')').replace('[', '(')}](doc:{rel})")


if __name__ == "__main__":
    main()
