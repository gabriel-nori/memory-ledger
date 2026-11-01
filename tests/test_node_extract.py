"""scripts/_node.py unpacks a tarball on every Python MIN_PY allows.

    python -m pytest tests/test_node_extract.py
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

import _node


def _tar(*members: tuple[str, bytes | None, dict]) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data, extra in members:
            info = tarfile.TarInfo(name)
            for k, v in extra.items():
                setattr(info, k, v)
            if data is not None:
                info.size = len(data)
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    buf.seek(0)
    return tarfile.open(fileobj=buf)


@pytest.fixture
def no_data_filter(monkeypatch):
    """Python 3.11.0-3 has no tarfile.data_filter, and extractall(filter=) is a TypeError there."""
    monkeypatch.setattr(_node, "_HAS_TAR_FILTER", False)


def test_a_plain_tarball_unpacks_without_the_filter_keyword(no_data_filter, tmp_path: Path):
    _node._extract_tar(_tar(("node/bin/node", b"x", {})), tmp_path)
    assert (tmp_path / "node" / "bin" / "node").read_bytes() == b"x"


@pytest.mark.parametrize("member", [
    ("../escape", b"x", {}),
    ("/abs-escape", b"x", {}),
    ("link", None, {"type": tarfile.SYMTYPE, "linkname": "../../outside"}),
    ("dev", None, {"type": tarfile.CHRTYPE}),
])
def test_a_member_that_leaves_the_folder_is_refused_on_old_pythons_too(no_data_filter, tmp_path: Path, member):
    with pytest.raises(tarfile.TarError):
        _node._extract_tar(_tar(member), tmp_path / "dest")
    assert not (tmp_path / "escape").exists()
