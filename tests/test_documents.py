"""The document reader of the Agents tab (scripts/documents.py), against a THROWAWAY home.

    python -m pytest tests/test_documents.py
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

import documents

UTF8 = "utf-8"
DOCS_DIR = "documents"
CHARSET_SUFFIX = "; charset=utf-8"
ZIP_BYTES = b"zipbytes"
ZIP_B64 = base64.b64encode(ZIP_BYTES).decode()


@pytest.fixture
def home(brain_home: Path) -> Path:
    """A brain with one of each kind of document under exports/documents."""
    docs = brain_home / "exports" / DOCS_DIR
    docs.mkdir(parents=True)
    (docs / "a.md").write_text("# Hi", encoding=UTF8)
    (docs / "b.html").write_text("<p>x</p>", encoding=UTF8)
    (docs / "c.pdf").write_bytes(b"%PDF-1.4")
    (docs / "d.exe").write_bytes(b"MZ")
    (docs / "p.png").write_bytes(b"\x89PNG")
    (brain_home / "secret.md").write_text("nope", encoding=UTF8)
    return brain_home


def _docs(home: Path) -> Path:
    return home / "exports" / DOCS_DIR


def test_markdown_comes_back_as_text(home):
    r = documents.read("documents/a.md")
    assert r["ok"] and r["kind"] == "md" and r["text"] == "# Hi"


def test_html_comes_back_as_text(home):
    assert documents.read("documents/b.html")["kind"] == "html"


@pytest.mark.parametrize("name, kind", [("c.pdf", "pdf"), ("p.png", "img")])
def test_a_binary_document_has_no_text_only_its_kind(home, name, kind):
    r = documents.read(f"documents/{name}")
    assert r["ok"] and r["kind"] == kind and r["text"] is None


def test_unknown_type_is_refused(home):
    assert not documents.read("documents/d.exe")["ok"]


def test_missing_file_is_refused(home):
    assert not documents.read("documents/zzz.md")["ok"]


def test_parent_traversal_is_refused(home):
    assert not documents.read("../secret.md")["ok"]


def test_absolute_path_is_refused(home):
    assert not documents.read(str(home / "secret.md"))["ok"]


def test_symlink_out_of_exports_is_refused(home):
    try:
        (_docs(home) / "link.md").symlink_to(home / "secret.md")
    except (OSError, NotImplementedError):
        pytest.skip("this platform or account cannot create symlinks")
    assert not documents.read("documents/link.md")["ok"]


def test_empty_path_is_refused(home):
    assert not documents.read("")["ok"]


@pytest.mark.parametrize("name", ["a.md", "b.html"])
def test_text_documents_are_served_as_utf8(home, name):
    assert documents.media_type(_docs(home) / name).endswith(CHARSET_SUFFIX)


def test_an_image_is_served_with_its_own_media_type(home):
    assert documents.media_type(_docs(home) / "p.png") == "image/png"


def test_an_attachment_is_saved_under_home_attachments(home):
    a = documents.save_attachment("../../my report (1).zip", ZIP_B64)
    assert a["ok"]
    assert (home / "attachments" / a["path"]).read_bytes() == ZIP_BYTES


def test_the_saved_name_drops_the_path_and_keeps_the_extension(home):
    a = documents.save_attachment("../../my report (1).zip", ZIP_B64)
    assert a["name"] == "my report _1_.zip" or a["name"].endswith(".zip")
    assert "/" not in a["name"] and ".." not in a["name"]


def test_an_attachment_of_any_type_is_readable_as_a_file(home):
    a = documents.save_attachment("x.zip", ZIP_B64)
    assert documents.read(a["path"], "attachments")["kind"] == "file"


def test_an_attachment_is_not_readable_as_an_export(home):
    a = documents.save_attachment("x.zip", ZIP_B64)
    assert not documents.read(a["path"])["ok"]


def test_two_files_of_one_name_do_not_collide(home):
    first = documents.save_attachment("x.png", ZIP_B64)["path"]
    second = documents.save_attachment("x.png", ZIP_B64)["path"]
    assert first != second


def test_an_empty_file_is_refused(home):
    assert not documents.save_attachment("e.txt", "")["ok"]


def test_traversal_out_of_attachments_is_refused(home):
    assert not documents.read("../exports/documents/a.md", "attachments")["ok"]


def test_an_unknown_area_falls_back_to_exports(home):
    assert documents.root("zzz") == documents.root("exports")


def test_generated_images_are_readable_and_linked(tmp_path, monkeypatch):
    import agents
    import graph
    monkeypatch.setattr(graph, "home_dir", lambda: tmp_path)
    (tmp_path / "images").mkdir()
    img = tmp_path / "images" / "hf-1.webp"
    img.write_bytes(b"x")
    assert documents.resolve("hf-1.webp", "images") == img.resolve()
    assert agents.image_link(str(img)) == "[hf-1.webp](img:hf-1.webp)"
    assert agents.image_link(str(tmp_path / "other.webp")) == ""
