"""The roots-restricted file browser (``files.browser``): typed listings, selections and reads."""

import os
from datetime import UTC, datetime

import pytest

from laueportal.files import browser
from laueportal.files.browser import (
    READ_FACTOR,
    InvalidSelection,
    list_directory,
    read_selected_file,
    validate_selection,
)
from laueportal.files.inputs import natural_key
from laueportal.files.roots import PathNotAllowed, PathNotFound
from laueportal.files.schemas import DirectoryListing, EntryKind, FileEntry
from tests.settings_support import make_settings


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    return root


def _settings(tmp_path, root, **files):
    return make_settings(tmp_path, files={"roots": {"data": root}, **files})


def test_list_directory_filters_files_and_sorts_directories_first(tmp_path, root):
    folder = root / "archive"
    folder.mkdir()
    (root / "scan_b.xml").write_text("b")
    (root / "scan_a.XML").write_text("a")
    (root / "notes.txt").write_text("ignored")

    listing = list_directory(root, _settings(tmp_path, root), extensions=[".xml"])

    assert isinstance(listing, DirectoryListing)
    assert (listing.root, listing.path, listing.truncated) == ("data", str(root), False)
    assert [entry.name for entry in listing.entries] == ["archive", "scan_a.XML", "scan_b.xml"]
    assert [entry.kind for entry in listing.entries] == [EntryKind.DIRECTORY, EntryKind.FILE, EntryKind.FILE]


def test_entries_are_typed_with_raw_size_and_utc_time(tmp_path, root):
    scan_log = root / "scan.xml"
    scan_log.write_bytes(b"x" * 1234)
    os.utime(scan_log, (1_700_000_000, 1_700_000_000))
    (root / "folder").mkdir()

    folder, log = list_directory(root, _settings(tmp_path, root)).entries

    assert log == FileEntry(
        name="scan.xml",
        path=str(scan_log),
        kind=EntryKind.FILE,
        size=1234,
        modified=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
    )
    assert log.modified is not None and log.modified.tzinfo is not None
    assert (folder.kind, folder.size) == (EntryKind.DIRECTORY, None)
    assert log.model_dump(mode="json")["modified"] == "2023-11-14T22:13:20Z"  # no preformatted local date


def test_listing_order_is_natural_and_hidden_entries_are_left_out(tmp_path, root):
    for name in ("frame_10.h5", "frame_2.h5", "Frame_3.h5", ".hidden.h5", "frame_1.h5"):
        (root / name).write_text("")
    (root / ".cache").mkdir()

    names = [entry.name for entry in list_directory(root, _settings(tmp_path, root)).entries]

    assert names == ["frame_1.h5", "frame_2.h5", "Frame_3.h5", "frame_10.h5"]


def test_listing_is_capped_by_the_setting_and_reports_truncation(tmp_path, root):
    for index in range(3):
        (root / f"scan_{index}.xml").write_text("scan")

    capped = list_directory(root, _settings(tmp_path, root, listing_cap=2), extensions=[".xml"])
    exact = list_directory(root, _settings(tmp_path, root, listing_cap=3), extensions=[".xml"])

    assert ([entry.name for entry in capped.entries], capped.truncated) == (["scan_0.xml", "scan_1.xml"], True)
    assert (len(exact.entries), exact.truncated) == (3, False)  # exactly the cap is not truncated


@pytest.fixture
def sorted_names(monkeypatch):
    """The names a listing computes a natural-order key for: what it sorts (``browser.natural_key``, counted)."""
    names: list[str] = []

    def counting(name):
        names.append(name)
        return natural_key(name)

    monkeypatch.setattr(browser, "natural_key", counting)
    return names


def test_a_listing_keeps_every_folder_and_sorts_at_most_ten_times_the_cap_of_files(tmp_path, root, sorted_names):
    """Typing and natural-sorting every entry before the cap applies takes 2 s for 200,000 entries, and bounding the
    entries read instead hides the folders past the bound. So every folder is kept, and only the first READ_FACTOR x
    listing_cap files the file system gives are sorted; the listing says it is truncated."""
    assert READ_FACTOR == 10  # the API's description of `truncated` says "ten times"
    folders = [f"sub_{index:02d}" for index in range(20)]
    # Half the folders before the files and half after: one half lands past the files whether the file system
    # lists the oldest entries first or the newest (tmpfs).
    for name in folders[:10]:
        (root / name).mkdir()
    for index in range(500):
        (root / f"frame_{index:03d}.h5").write_text("")
    for name in folders[10:]:
        (root / name).mkdir()
    (root / ".hidden").mkdir()
    order = [entry.name for entry in os.scandir(root)]  # the file system's own order
    first_files = [name for name in order if name.startswith("frame_")][:300]
    if not any(order.index(name) >= 300 for name in folders):  # a listing that read 300 entries would miss one
        pytest.skip("this file system lists every folder within its first 300 entries")

    listing = list_directory(root, _settings(tmp_path, root, listing_cap=30))

    assert len(sorted_names) == 20 + 300  # every folder, and 300 of the 500 files
    expected = sorted(folders, key=natural_key) + sorted(first_files, key=natural_key)[:10]
    assert [entry.name for entry in listing.entries] == expected
    assert [entry.kind for entry in listing.entries] == [EntryKind.DIRECTORY] * 20 + [EntryKind.FILE] * 10
    assert listing.truncated is True


def test_files_the_extension_filter_leaves_out_do_not_count_toward_the_bound(tmp_path, root, sorted_names):
    for index in range(5):
        (root / f"scan_{index}.xml").write_text("")
    for index in range(2000):
        (root / f"notes_{index}.txt").write_text("")

    listing = list_directory(root, _settings(tmp_path, root, listing_cap=10), extensions=[".xml"])

    assert [entry.name for entry in listing.entries] == [f"scan_{index}.xml" for index in range(5)]
    assert (listing.truncated, len(sorted_names)) == (False, 5)


def test_symlinks_out_of_the_roots_are_not_listed(tmp_path, root):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.h5").write_text("")
    runs = tmp_path / "runs_root"
    runs.mkdir()
    (root / "escape").symlink_to(outside)
    (root / "secret.h5").symlink_to(outside / "secret.h5")
    (root / "dangling.h5").symlink_to(outside / "missing.h5")
    (root / "runs").symlink_to(runs)
    (root / "kept.h5").write_text("")
    settings = make_settings(tmp_path, files={"roots": {"data": root, "runs": runs}})

    entries = list_directory(root, settings).entries

    assert [(entry.name, entry.path) for entry in entries] == [("runs", str(runs)), ("kept.h5", str(root / "kept.h5"))]


def test_listing_refuses_paths_outside_the_roots(tmp_path, root):
    settings = _settings(tmp_path, root)
    outside = tmp_path / "outside"
    outside.mkdir()

    for path in (outside, tmp_path, f"{root}/..", "~", "data"):
        with pytest.raises(PathNotAllowed):
            list_directory(path, settings)
    with pytest.raises(PathNotAllowed, match="No file roots are configured"):
        list_directory(root, make_settings(tmp_path))


def test_missing_and_non_directory_paths(tmp_path, root):
    settings = _settings(tmp_path, root)
    regular_file = root / "scan.xml"
    regular_file.write_text("scan")

    with pytest.raises(PathNotFound, match="not available on this host") as missing:
        list_directory(root / "missing", settings)
    assert missing.value.status == 404
    with pytest.raises(InvalidSelection, match="not a directory") as not_directory:
        list_directory(regular_file, settings)
    assert not_directory.value.status == 422


def test_validate_selection_supports_file_and_directory_modes(tmp_path, root):
    settings = _settings(tmp_path, root)
    scan_log = root / "scan.xml"
    scan_log.write_bytes(b"<scanLog />")

    assert validate_selection(root, settings, mode="directory") == root
    assert validate_selection(scan_log, settings, extensions=[".xml"]) == scan_log
    with pytest.raises(InvalidSelection, match="not a directory"):
        validate_selection(scan_log, settings, mode="directory")
    with pytest.raises(InvalidSelection, match="not a regular file"):
        validate_selection(root, settings)
    with pytest.raises(PathNotFound):
        validate_selection(root / "missing.xml", settings)


def test_validate_selection_rejects_wrong_extension_and_large_file(tmp_path, root):
    settings = _settings(tmp_path, root)
    text_file = root / "scan.txt"
    text_file.write_text("not xml")
    large_file = root / "large.xml"
    large_file.write_bytes(b"x" * 1050)

    with pytest.raises(InvalidSelection, match="extensions"):
        validate_selection(text_file, settings, extensions=[".xml"])
    with pytest.raises(InvalidSelection, match="exceeds"):
        validate_selection(large_file, settings, extensions=[".xml"], max_file_size_mb=0.001)


def test_validate_selection_refuses_paths_outside_the_roots(tmp_path, root):
    secret = tmp_path / "secret.xml"
    secret.write_text("<scanLog />")
    (root / "link.xml").symlink_to(secret)
    settings = _settings(tmp_path, root)

    for path in (secret, root / "link.xml", f"{root}/../secret.xml"):
        with pytest.raises(PathNotAllowed):
            validate_selection(path, settings, extensions=[".xml"])


def test_read_selected_file_revalidates_and_reads_bytes(tmp_path, root):
    settings = _settings(tmp_path, root)
    scan_log = root / "scan.xml"
    scan_log.write_bytes(b"<scanLog />")

    assert read_selected_file(scan_log, settings, extensions=[".xml"], max_file_size_mb=1) == b"<scanLog />"

    scan_log.unlink()
    with pytest.raises(PathNotFound):
        read_selected_file(scan_log, settings, extensions=[".xml"], max_file_size_mb=1)
    # Swapped for a link out of the roots after it was selected: refused when read.
    secret = tmp_path / "secret.xml"
    secret.write_bytes(b"<secret />")
    scan_log.symlink_to(secret)
    with pytest.raises(PathNotAllowed):
        read_selected_file(scan_log, settings, extensions=[".xml"], max_file_size_mb=1)
