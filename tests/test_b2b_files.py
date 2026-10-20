"""B2B attachments: zipping what is sent, unzipping what arrives without trusting it."""

import io
import os
import stat
import zipfile
from pathlib import Path

import pytest

import b2b_files
from b2b_files import BadArchive, extract_zip, zip_files


def make_zip(members: dict[str, bytes], symlink: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    return buf.getvalue()


class TestZip:
    def test_files_round_trip_flat(self, tmp_path):
        (tmp_path / "a.txt").write_text("one", encoding="utf-8")
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "a.txt").write_text("two", encoding="utf-8")
        data, names = zip_files([str(tmp_path / "a.txt"), str(tmp_path / "d" / "a.txt")])
        assert names == ["a.txt", "a-2.txt"]
        out = tmp_path / "out"
        assert sorted(extract_zip(data, out)) == ["a-2.txt", "a.txt"]
        assert (out / "a.txt").read_text(encoding="utf-8") == "one" and (out / "a-2.txt").read_text(encoding="utf-8") == "two"

    @pytest.mark.parametrize("paths", [[], ["/no/such/file"]])
    def test_nothing_or_not_a_file_is_refused(self, paths):
        with pytest.raises(BadArchive):
            zip_files(paths)

    def test_too_many_files_are_refused(self, tmp_path):
        (tmp_path / "f").write_text("x", encoding="utf-8")
        with pytest.raises(BadArchive):
            zip_files([str(tmp_path / "f")] * (b2b_files.FILES_MAX + 1))

    def test_a_zip_over_the_relays_limit_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(b2b_files, "ARCHIVE_MAX", 1_000)
        (tmp_path / "r").write_bytes(os.urandom(5_000))
        with pytest.raises(BadArchive):
            zip_files([str(tmp_path / "r")])


class TestExtract:
    @pytest.mark.parametrize("name", ["../evil", "a/../../evil", "/abs", "C:/x", "a\\b", ""])
    def test_a_path_that_leaves_the_folder_is_refused_and_nothing_is_left(self, tmp_path, name):
        dest = tmp_path / "out"
        with pytest.raises(BadArchive):
            extract_zip(make_zip({"ok.txt": b"fine", name or "\x00": b"x"}), dest)
        assert not dest.exists()
        assert not (tmp_path / "evil").exists()
        assert [p.name for p in tmp_path.iterdir()] == []

    def test_a_link_is_refused(self, tmp_path):
        with pytest.raises(BadArchive, match="link"):
            extract_zip(make_zip({"a": b"x"}, symlink="l"), tmp_path / "out")

    def test_a_ratio_bomb_is_refused(self, tmp_path):
        with pytest.raises(BadArchive, match="far more"):
            extract_zip(make_zip({"z": b"\0" * (30 * 1024 * 1024)}), tmp_path / "out")
        assert not (tmp_path / "out").exists()

    def test_an_archive_that_unpacks_to_too_much_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(b2b_files, "EXTRACT_MAX", 10_000)
        data = make_zip({"a": os.urandom(6_000), "b": os.urandom(6_000)})
        with pytest.raises(BadArchive, match="more than"):
            extract_zip(data, tmp_path / "out")

    def test_the_bytes_written_count_not_the_size_the_archive_claims(self, tmp_path, monkeypatch):
        monkeypatch.setattr(b2b_files, "EXTRACT_MAX", 500)
        data = bytearray(make_zip({"a": b"x" * 400, "b": b"y" * 400}))
        # lie in both headers: each entry claims to be 10 bytes
        for sig in (b"PK\x03\x04", b"PK\x01\x02"):
            i = 0
            while (i := bytes(data).find(sig, i)) != -1:
                off = i + (22 if sig == b"PK\x03\x04" else 24)
                data[off:off + 4] = (10).to_bytes(4, "little")
                i += 4
        with pytest.raises(BadArchive):
            extract_zip(bytes(data), tmp_path / "out")
        assert not (tmp_path / "out").exists()

    def test_too_many_entries_are_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(b2b_files, "EXTRACT_FILES_MAX", 2)
        with pytest.raises(BadArchive, match="entries"):
            extract_zip(make_zip({"a": b"1", "b": b"2", "c": b"3"}), tmp_path / "out")

    def test_something_that_is_not_a_zip_is_refused(self, tmp_path):
        with pytest.raises(BadArchive, match="not a zip"):
            extract_zip(b"not a zip at all", tmp_path / "out")

    def test_files_are_private(self, tmp_path):
        out = tmp_path / "out"
        extract_zip(make_zip({"a.txt": b"x"}), out)
        assert stat.S_IMODE((out / "a.txt").stat().st_mode) == 0o600
        assert isinstance(out, Path)
