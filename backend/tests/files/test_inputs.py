"""Tests for linear-time workflow input resolution."""

import os

import pytest

from laueportal.files import inputs as files
from laueportal.files.patterns import FilenameTemplateMatch


def test_wire_resolution_lists_once_and_sorts_by_filename_not_by_requested_order(tmp_path, monkeypatch):
    for filename in ("wire_1.h5", "wire_2.h5", "ignore.txt"):
        (tmp_path / filename).write_text("")

    real_listdir = os.listdir
    calls = []

    def tracked_listdir(directory):
        calls.append(directory)
        return real_listdir(directory)

    monkeypatch.setattr(files.os, "listdir", tracked_listdir)
    resolved = files.resolve_input_files(
        tmp_path,
        ["wire_%d"],
        [2, 1],
        append_suffix_wildcard=True,
    )

    assert calls == [os.fspath(tmp_path)]
    assert resolved == (  # sorted, not in the requested order [2, 1]
        os.path.join(tmp_path, "wire_1.h5"),
        os.path.join(tmp_path, "wire_2.h5"),
    )


def test_indexing_resolution_supports_wildcards_and_two_indices(tmp_path):
    for filename in (
        "sample_A_1_0.h5",
        "sample_B_1_0.h5",
        "sample_A_1_1.h5",
        "sample_A_2_0.h5",
        "sample_A_2_1.h5",
    ):
        (tmp_path / filename).write_text("")

    resolved = files.resolve_input_files(
        tmp_path,
        ["sample_*_%d_%d.h5"],
        [2, 1],
        depth_points=[1, 0],
    )

    assert tuple(os.path.basename(path) for path in resolved) == (  # natural filename order
        "sample_A_1_0.h5",
        "sample_A_1_1.h5",
        "sample_A_2_0.h5",
        "sample_A_2_1.h5",
        "sample_B_1_0.h5",
    )


def test_resolution_fails_when_a_requested_index_has_no_file(tmp_path):
    (tmp_path / "wire_1.h5").write_text("")

    with pytest.raises(files.FileResolutionError, match="for 2"):
        files.resolve_input_files(tmp_path, ["wire_%d.h5"], [1, 2])


def test_resolution_combines_multiple_filename_templates(tmp_path):
    for filename in ("left_1.h5", "right_1.h5"):
        (tmp_path / filename).write_text("")

    resolved = files.resolve_input_files(
        tmp_path,
        ["left_%d.h5", "right_%d.h5"],
        [1],
    )

    assert tuple(os.path.basename(path) for path in resolved) == ("left_1.h5", "right_1.h5")


def test_resolution_performs_one_match_per_directory_entry(monkeypatch):
    entry_count = 100_000
    filenames = [f"image_{index}.h5" for index in range(entry_count)]
    match_calls = 0
    list_calls = 0

    class CountingMatcher:
        def match(self, filename):
            nonlocal match_calls
            match_calls += 1
            index = int(filename.removeprefix("image_").removesuffix(".h5"))
            return FilenameTemplateMatch(0, (index,))

    def listdir(directory):
        nonlocal list_calls
        list_calls += 1
        return filenames

    monkeypatch.setattr(files, "compile_filename_templates", lambda *args, **kwargs: CountingMatcher())
    monkeypatch.setattr(files.os, "listdir", listdir)

    resolved = files.resolve_input_files("/network/scan", ["image_%d.h5"], range(entry_count))

    assert list_calls == 1
    assert match_calls == entry_count
    assert len(resolved) == entry_count


def test_resolution_reports_matching_progress(monkeypatch):
    monkeypatch.setattr(files.os, "listdir", lambda directory: ["image_1.h5", "image_2.h5"])
    updates = []

    files.resolve_input_files(
        "/network/scan",
        ["image_%d.h5"],
        [1, 2],
        progress_callback=lambda scanned, total: updates.append((scanned, total)),
        progress_interval=1,
    )

    assert updates == [(0, 2), (1, 2), (2, 2)]


# ---------------------------------------------------------------------------
# Deterministic order (natural sort by filename, then frame/scan index)


def test_natural_key_orders_mixed_widths_case_and_leading_zeros():
    names = ["f10.h5", "F11.h5", "f2.h5", "f02.h5", "f1.h5", "g1.h5", "f2a.h5", "f2.H5", "10f.h5", "9f.h5"]

    assert sorted(names, key=files.natural_key) == [
        "9f.h5",
        "10f.h5",
        "f1.h5",
        "f02.h5",  # equal numbers: plain string order breaks the tie ("0" < "2")
        "f2.H5",  # equal ignoring case: plain string order ("H" < "h")
        "f2.h5",
        "f2a.h5",
        "f10.h5",
        "F11.h5",
        "g1.h5",
    ]


@pytest.mark.parametrize("listing_order", [sorted, lambda names: sorted(names, reverse=True)])
def test_resolution_order_does_not_depend_on_the_listing_or_the_requested_order(tmp_path, monkeypatch, listing_order):
    names = ["frame_10.h5", "frame_2.h5", "frame_1.h5", "frame_1_b.h5", "frame_100.h5"]
    for name in names:
        (tmp_path / name).write_text("")
    real_listdir = os.listdir
    monkeypatch.setattr(files.os, "listdir", lambda directory: listing_order(real_listdir(directory)))

    resolved = files.resolve_inputs(tmp_path, ["frame_%d"], [100, 2, 10, 1], append_suffix_wildcard=True)

    assert [os.path.basename(item.path) for item in resolved] == [
        "frame_1.h5",
        "frame_1_b.h5",  # same index, more files (suffix wildcard): still by filename
        "frame_2.h5",
        "frame_10.h5",
        "frame_100.h5",
    ]
    assert [item.indices for item in resolved] == [(1,), (1,), (2,), (10,), (100,)]


def test_several_templates_interleave_by_filename(tmp_path):
    for name in ("b_1.h5", "b_2.h5", "a_2.h5", "a_10.h5"):
        (tmp_path / name).write_text("")

    resolved = files.resolve_inputs(tmp_path, ["b_%d.h5", "a_*.h5"], [1, 2])

    assert [(os.path.basename(item.path), item.template_index) for item in resolved] == [
        ("a_2.h5", 1),
        ("a_10.h5", 1),
        ("b_1.h5", 0),
        ("b_2.h5", 0),
    ]


def test_ties_on_filename_order_by_index_then_directory():
    """Frames of one point file share the path (order by depth); equal names in several directories order by
    index first, then by directory in natural order."""

    entries = [
        files.ResolvedInput("/run/points/p2.h5", 0, (2, 1), point_id="p2", depth_index=1),
        files.ResolvedInput("/run/points/p10.h5", 0, (10, 0), point_id="p10", depth_index=0),
        files.ResolvedInput("/run/points/p2.h5", 0, (2, 0), point_id="p2", depth_index=0),
        files.ResolvedInput("/data/scan10/img_1.h5", 0, (1,)),
        files.ResolvedInput("/data/scan9/img_1.h5", 0, (1,)),
        files.ResolvedInput("/data/scan10/img_1.h5", 1, (0,)),
        files.ResolvedInput("/data/Scan9/img_1.h5", 0, (1,)),
    ]

    ordered = files.sort_inputs(entries)

    assert [(item.path, item.indices, item.template_index) for item in ordered] == [
        ("/data/scan10/img_1.h5", (0,), 1),  # same filename: lowest index first
        ("/data/Scan9/img_1.h5", (1,), 0),  # then directory: scan9 < scan10 (natural), "Scan9" < "scan9"
        ("/data/scan9/img_1.h5", (1,), 0),
        ("/data/scan10/img_1.h5", (1,), 0),
        ("/run/points/p2.h5", (2, 0), 0),  # p2 before p10; within the point file by depth index
        ("/run/points/p2.h5", (2, 1), 0),
        ("/run/points/p10.h5", (10, 0), 0),
    ]
    assert files.sort_inputs(list(reversed(entries))) == ordered
