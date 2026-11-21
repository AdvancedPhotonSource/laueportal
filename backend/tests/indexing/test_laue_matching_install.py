"""Installing forward caches and the orientation database into a remote target's data folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from laueportal.indexing.laue_matching import cache
from laueportal.indexing.laue_matching.install import InstallError, install_cache, install_database
from tests.indexing.test_laue_matching import NAME, parameters, write_cache


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "portal"
    write_cache(root, parameters(root))
    return root


def test_a_cache_and_the_database_are_installed_whole_and_again_is_harmless(source, tmp_path):
    target = tmp_path / "shared" / "laue-matching"
    messages: list[str] = []

    install_database(source, target, log=messages.append)
    digest = install_cache(source, target, NAME, log=messages.append)

    for name in (cache.FORWARD_NAME, cache.HKL_NAME, cache.RECORD_NAME):
        assert (target / NAME / name).read_bytes() == (source / NAME / name).read_bytes()
    assert (target / cache.DATABASE_NAME).read_bytes() == (source / cache.DATABASE_NAME).read_bytes()
    assert sorted(path.name for path in target.iterdir()) == [cache.DATABASE_NAME, NAME]  # no partial, no lock
    assert install_cache(source, target, NAME, log=messages.append) == digest
    assert messages[-1].endswith("is already installed in " + str(target))


def test_another_cache_of_the_same_name_is_never_replaced(source, tmp_path):
    target = tmp_path / "target"
    install_cache(source, target, NAME, log=lambda _: None)
    (target / NAME / cache.HKL_NAME).write_text("1 1 1\n")

    with pytest.raises(InstallError, match="holds another cache"):
        install_cache(source, target, NAME, log=lambda _: None)
    assert (target / NAME / cache.HKL_NAME).read_text() == "1 1 1\n"


def test_an_unfinished_cache_is_not_installed(source, tmp_path):
    (source / NAME / cache.FORWARD_NAME).write_bytes(b"\0" * 8)  # the record says 244 bytes

    with pytest.raises(InstallError, match="is 8 bytes, and its record says 244"):
        install_cache(source, tmp_path / "target", NAME, log=lambda _: None)
    assert not (tmp_path / "target" / NAME).exists()


def test_a_running_or_interrupted_install_holds_its_lock(source, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / f".{NAME}.install.lock").write_text("1")

    with pytest.raises(InstallError, match="another install is running"):
        install_cache(source, target, NAME, log=lambda _: None)
