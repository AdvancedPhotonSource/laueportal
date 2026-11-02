"""The entity tags' code identity (``core.etag.versions``, ``core.provenance``): a deployment with other portal
source or another lauelab commit never matches an old tag."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from laueportal.core import etag, provenance

PINNED = {"lauelab_version": "0.1.0", "lauelab_commit": "a" * 40}


@pytest.fixture
def fresh_versions() -> Iterator[None]:
    etag.versions.cache_clear()
    yield
    etag.versions.cache_clear()  # the next use reads the real code again


@pytest.mark.parametrize(
    ("change", "same"),
    [
        pytest.param("source", False, id="portal-source-edited"),
        pytest.param("commit", False, id="lauelab-commit-moved"),
        pytest.param("bytecode", True, id="bytecode-written"),
    ],
)
def test_a_tag_changes_with_the_portals_source_and_lauelabs_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_versions: None, change: str, same: bool
) -> None:
    source = tmp_path / "laueportal"
    (source / "core").mkdir(parents=True)
    (source / "core" / "etag.py").write_text("A = 1\n")
    monkeypatch.setattr(provenance, "PORTAL_SOURCE", source)
    monkeypatch.setattr(provenance, "lauelab_provenance", lambda: PINNED)
    tag = etag.entity_tag("map", 1)

    if change == "source":
        (source / "core" / "etag.py").write_text("A = 2\n")
    elif change == "commit":
        monkeypatch.setattr(provenance, "lauelab_provenance", lambda: {**PINNED, "lauelab_commit": "b" * 40})
    else:
        (source / "core" / "__pycache__").mkdir()
        (source / "core" / "__pycache__" / "etag.cpython-312.pyc").write_bytes(b"\x00")
    etag.versions.cache_clear()  # a restart or another API process

    assert (etag.entity_tag("map", 1) == tag) is same


def test_lauelab_is_identified_by_its_pinned_commit_else_by_its_installed_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = provenance.lauelab_provenance()
    assert provenance.lauelab_identity() == installed["lauelab_commit"]  # make setup pins a commit

    monkeypatch.setattr(provenance, "lauelab_provenance", lambda: {"lauelab_version": "0.1.0"})  # an editable install
    version, digest = provenance.lauelab_identity().split("+")
    assert (version, len(digest)) == ("0.1.0", 32)
