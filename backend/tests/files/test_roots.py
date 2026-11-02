"""``files.roots.resolve_inside_roots``: the single check for every path from a request, and how it resolves a path
through ``files.probe`` (``real_path``: one mount at a time)."""

import os
from pathlib import Path

import pytest

from laueportal.core.problems import ProblemError
from laueportal.files import probe, roots
from laueportal.files.probe import NotResponding
from laueportal.files.roots import (
    MAX_LINKS,
    MAX_NAME_BYTES,
    MAX_PATH_BYTES,
    PathNotAllowed,
    is_inside_roots,
    real_path,
    resolve_inside_roots,
)
from tests.probe_support import hang, install_probe
from tests.settings_support import make_settings


@pytest.fixture
def tree(tmp_path):
    """Two roots (``data`` and ``runs``) and a directory outside both."""

    data = tmp_path / "data"
    runs = tmp_path / "runs"
    outside = tmp_path / "outside"
    for directory in (data / "scan_1", runs, outside, tmp_path / "data2"):
        directory.mkdir(parents=True)
    (data / "scan_1" / "frame_1.h5").write_text("")
    (outside / "secret.txt").write_text("")
    (tmp_path / "data2" / "frame.h5").write_text("")
    settings = make_settings(tmp_path, files={"roots": {"data": data, "runs": runs}})
    return settings, tmp_path


def _rejected(path, settings, match):
    with pytest.raises(PathNotAllowed, match=match) as raised:
        resolve_inside_roots(path, settings)
    assert raised.value.status == 400 and isinstance(raised.value, ProblemError)
    assert not is_inside_roots(path, settings)


def test_paths_inside_a_root_resolve_with_the_root_name(tree):
    settings, base = tree
    frame = base / "data" / "scan_1" / "frame_1.h5"

    assert resolve_inside_roots(str(frame), settings) == ("data", frame)
    assert resolve_inside_roots(frame, settings) == ("data", frame)  # PathLike
    assert resolve_inside_roots(base / "data", settings) == ("data", base / "data")  # the root itself
    assert resolve_inside_roots(f"  {base / 'runs'}/  ", settings) == ("runs", base / "runs")  # pasted whitespace
    # A path need not exist (e.g. an output folder to create), but it must still be inside a root.
    assert resolve_inside_roots(base / "runs" / "new" / "index_1", settings) == (
        "runs",
        base / "runs" / "new" / "index_1",
    )


def test_outside_every_root(tree):
    settings, base = tree
    _rejected(base / "outside" / "secret.txt", settings, "outside the allowed roots")
    _rejected("/etc/passwd", settings, r"outside the allowed roots \(data, runs\)")
    _rejected("/", settings, "outside the allowed roots")


def test_dot_dot_is_refused_before_any_stat(tree, monkeypatch):
    """A ``..`` component is refused, even one that stays inside: the walk would stat what
    lies before it, on a mount the path's text does not name."""
    settings, base = tree
    monkeypatch.setattr(os, "lstat", lambda *args, **kwargs: pytest.fail("stat"))
    for path in (
        f"{base}/data/scan_1/../../outside/secret.txt",
        f"{base}/data/..",
        f"{base}/data/scan_1/../scan_1/frame_1.h5",
        f"{base}/outside/../data/scan_1",
    ):
        _rejected(path, settings, "has a '..' component")


def test_a_sibling_with_a_common_prefix_is_not_inside(tree):
    settings, base = tree
    _rejected(base / "data2" / "frame.h5", settings, "outside the allowed roots")


def test_symlinks_are_resolved_before_the_check(tree):
    settings, base = tree
    data = base / "data"
    (data / "escape").symlink_to(base / "outside")
    (data / "secret_link.txt").symlink_to(base / "outside" / "secret.txt")
    (data / "to_runs").symlink_to(base / "runs")
    (data / "dangling").symlink_to(base / "outside" / "missing")

    _rejected(data / "escape", settings, "outside the allowed roots")
    _rejected(data / "escape" / "secret.txt", settings, "outside the allowed roots")
    _rejected(data / "secret_link.txt", settings, "outside the allowed roots")
    _rejected(data / "dangling", settings, "outside the allowed roots")  # a dangling link is resolved too
    assert resolve_inside_roots(data / "to_runs", settings) == ("runs", base / "runs")  # reported under its target


def test_a_root_that_is_a_symlink(tmp_path):
    real = tmp_path / "mnt" / "data"
    (real / "scan").mkdir(parents=True)
    link = tmp_path / "data"
    link.symlink_to(real)
    settings = make_settings(tmp_path, files={"roots": {"data": link}})

    assert resolve_inside_roots(link / "scan", settings) == ("data", real / "scan")
    assert resolve_inside_roots(real / "scan", settings) == ("data", real / "scan")
    _rejected(tmp_path / "mnt", settings, "outside the allowed roots")


def test_nested_roots_name_the_innermost(tmp_path):
    (tmp_path / "all" / "runs" / "rec_1").mkdir(parents=True)
    settings = make_settings(tmp_path, files={"roots": {"runs": tmp_path / "all" / "runs", "all": tmp_path / "all"}})

    assert resolve_inside_roots(tmp_path / "all" / "runs" / "rec_1", settings)[0] == "runs"
    assert resolve_inside_roots(tmp_path / "all", settings)[0] == "all"


@pytest.mark.parametrize("path", ["~", "~/data", "~user/.ssh/id_rsa", "~nobody"])
def test_home_expansion_is_refused(tree, path):
    settings, _ = tree
    _rejected(path, settings, "'~' is not expanded")


@pytest.mark.parametrize("path", ["data/scan_1", "./data", "../outside", "scan_1/frame_1.h5"])
def test_relative_paths_are_refused(tree, monkeypatch, path):
    settings, base = tree
    monkeypatch.chdir(base)  # even where they would land inside a root
    _rejected(path, settings, "is not an absolute path")


@pytest.mark.parametrize(("path", "match"), [("", "Enter a path"), ("   ", "Enter a path"), ("/data/\x00x", "NUL")])
def test_empty_and_malformed_paths(tree, path, match):
    settings, _ = tree
    _rejected(path, settings, match)


@pytest.mark.parametrize("path", ["/data/\ud800", "\udfff", "/data/x\ud83d"])
def test_a_path_with_a_lone_surrogate_is_refused(tree, path):
    """JSON can spell one ("\\ud800"), and resolving it raised UnicodeEncodeError (a 500)."""
    settings, _ = tree
    _rejected(path, settings, "lone surrogate")


def test_a_surrogate_escape_from_the_file_system_still_resolves(tree):
    """``os`` names a Latin-1 byte with a surrogate escape (\\udcfc for ü): that path can be encoded."""
    settings, base = tree
    assert resolve_inside_roots(f"{base}/data/M\udcfcller", settings)[0] == "data"


def test_no_roots_configured_allows_nothing(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.files.roots == {}
    _rejected(tmp_path, settings, "No file roots are configured")
    _rejected(Path(os.sep), settings, "No file roots are configured")


def test_a_path_outside_every_root_by_its_text_is_refused_before_any_stat(tree, monkeypatch):
    """Outside every root's text and real path: refused without a stat of it, even a link from outside into a root
    (which resolves inside) and a path on a hung mount; a root's real path is inside (a root that is a link)."""
    settings, base = tree
    (base / "outside" / "into_data").symlink_to(base / "data" / "scan_1")
    (base / "data_link").symlink_to(base / "data")
    linked = make_settings(base, files={"roots": {"data": base / "data_link"}})
    install_probe(monkeypatch, mounts=(base / "outside",))
    hung = hang(monkeypatch, base / "outside")
    try:
        _rejected(base / "outside" / "into_data" / "frame_1.h5", settings, "outside the allowed roots")
        _rejected(base / "outside" / "x", settings, "outside the allowed roots")
        inside = resolve_inside_roots(base / "data" / "scan_1", linked)  # the root's real path, not its text
    finally:
        hung.released.set()
    assert hung.calls == 0 and inside == ("data", base / "data" / "scan_1")


@pytest.mark.usefixtures("gc_disabled")  # the check starts within the probe's short timeout
def test_a_link_to_a_hung_mount_marks_that_mount_never_the_roots(tree, monkeypatch):
    """The walk stats each component under its own mount's key, a link's target under the target's: a link inside
    a root to a hung mount marks the hung mount, and the root's own mount keeps answering."""
    settings, base = tree
    dead = base / "dead"
    dead.mkdir()
    (base / "data" / "trap").symlink_to(dead / "x")
    files = install_probe(monkeypatch, mounts=(base / "data", dead), timeout=0.2)
    hung = hang(monkeypatch, dead)
    try:
        with pytest.raises(NotResponding):
            resolve_inside_roots(base / "data" / "trap" / "y", settings)
        marked = (files.hung(base / "data" / "scan_1"), files.hung(dead / "x"))
        healthy = resolve_inside_roots(base / "data" / "scan_1", settings)
    finally:
        hung.released.set()
    assert (marked, healthy, hung.calls) == ((False, True), ("data", base / "data" / "scan_1"), 1)


def test_a_root_is_resolved_on_each_check(tmp_path):
    """A root link pointed elsewhere counts at once: its new target is inside, its old one no longer."""
    old, new, link = tmp_path / "old", tmp_path / "new", tmp_path / "root"
    old.mkdir()
    new.mkdir()
    link.symlink_to(old)
    settings = make_settings(tmp_path, files={"roots": {"data": link}})
    assert resolve_inside_roots(old / "x", settings) == ("data", old / "x")
    link.unlink()
    link.symlink_to(new)
    _rejected(old / "x", settings, "outside the allowed roots")
    assert resolve_inside_roots(new / "x", settings) == ("data", new / "x")


@pytest.mark.usefixtures("gc_disabled")  # the check starts within the probe's short timeout
@pytest.mark.parametrize("root", ["plain", "link and .."])
def test_a_root_that_does_not_answer_never_holds_a_path(tmp_path, monkeypatch, root):
    """A root that never answered holds nothing, by its text neither (``<t>/link/../data`` with ``link`` on the
    hung mount is ``dead/data``, not ``<t>/data``): a path inside it is not responding, and so is one no root holds."""
    dead = tmp_path / "dead"
    (dead / "data").mkdir(parents=True)
    (dead / "deep").mkdir()
    (tmp_path / "data" / "secret").mkdir(parents=True)
    (tmp_path / "link").symlink_to(dead / "deep")
    text = dead / "data" if root == "plain" else Path(f"{tmp_path}/link/../data")
    settings = make_settings(tmp_path, files={"roots": {"data": text}})
    install_probe(monkeypatch, mounts=(dead,), timeout=0.2)
    monkeypatch.setattr(roots, "_kept_roots", {})
    hung = hang(monkeypatch, dead)
    try:
        for path in (dead / "data" / "x", tmp_path / "data" / "secret"):
            with pytest.raises(NotResponding):
                resolve_inside_roots(path, settings)
    finally:
        hung.released.set()
    install_probe(monkeypatch)  # the mount answers again (a probe that does not remember it)
    _rejected(tmp_path / "data" / "secret", settings, "outside the allowed roots")


def test_the_walk_resolves_as_realpath_does(tmp_path):
    """``real_path`` is ``os.path.realpath`` (links relative and absolute, ``..`` in a target, a loop, a broken
    link, a file in the way, a folder it may not open), and refuses a path that needs more than ``MAX_LINKS``."""
    (tmp_path / "r" / "a").mkdir(parents=True)
    (tmp_path / "r" / "a" / "f").write_text("x")
    (tmp_path / "closed").mkdir()
    links = {
        "out": tmp_path / "a",
        "up": "..",
        "rel": "a/../a/f",
        "loop1": tmp_path / "r" / "loop2",
        "loop2": tmp_path / "r" / "loop1",
        "broken": tmp_path / "nowhere",
        "chain": "up/r/up/r/a",
    }
    for name, target in links.items():
        (tmp_path / "r" / name).symlink_to(target)
    for index in range(MAX_LINKS + 1):
        (tmp_path / f"l{index}").symlink_to(tmp_path / f"l{index + 1}")
    (tmp_path / "closed").chmod(0)
    try:
        paths = [f"{tmp_path}/r/{tail}" for tail in ("a/f", "out/x", "up/r/a", "rel", "loop1/x", "broken/x", "chain/f")]
        paths += [
            f"{tmp_path}/r//a/./f/../f",
            f"{tmp_path}/r/a/f/x",
            f"{tmp_path}/closed/x",
            f"{tmp_path}/l{MAX_LINKS}",
        ]
        walked = [str(real_path(path)) for path in paths]
        with pytest.raises(PathNotAllowed, match="symbolic links"):
            real_path(tmp_path / "l0")
    finally:
        (tmp_path / "closed").chmod(0o700)
    assert walked == [os.path.realpath(path) for path in paths]


@pytest.mark.parametrize("recorded", [False, True])
def test_a_path_over_the_length_limits_is_refused_before_any_stat(tree, monkeypatch, recorded):
    """Longer than ``MAX_PATH_BYTES`` or with a name longer than ``MAX_NAME_BYTES`` (in bytes: ``é`` is two) is
    refused by its text, a request's path as a run's output's; up to the limits, a path resolves."""
    settings, base = tree
    inside = f"{base}/data/"
    longest = inside + "a/" * ((MAX_PATH_BYTES - len(os.fsencode(inside))) // 2)
    longest += "a" * (MAX_PATH_BYTES - len(os.fsencode(longest)))
    allowed = [longest, inside + "n" * MAX_NAME_BYTES, inside + "é" * (MAX_NAME_BYTES // 2)]
    assert len(os.fsencode(longest)) == MAX_PATH_BYTES
    assert all(resolve_inside_roots(path, settings, recorded=recorded)[0] == "data" for path in allowed)

    monkeypatch.setattr(os, "lstat", lambda *args, **kwargs: pytest.fail("stat"))
    for path, match in [
        (longest + "a", "is longer than 4,096 bytes"),
        (inside + "/a" * 4000, "is longer than 4,096 bytes"),
        (inside + "n" * (MAX_NAME_BYTES + 1) + "/x", "has a name longer than 255 bytes"),
        (inside + "é" * (MAX_NAME_BYTES // 2 + 1), "has a name longer than 255 bytes"),
    ]:
        with pytest.raises(PathNotAllowed, match=match):
            resolve_inside_roots(path, settings, recorded=recorded)


def test_a_long_resolution_looks_up_a_bounded_number_of_mounts_a_component(tmp_path, monkeypatch):
    """A component's mount is looked up among the prefixes no deeper than the deepest mount point, so a resolution costs
    a bounded number of mount-table lookups per component (it was quadratic in the depth, so a resolution was cubic). A
    request inside the limits walks about 2,000 components, half of them through a link whose target holds 1,000 (the
    most a walk within ``MAX_PATH_BYTES`` holds; a longer walk stops, the next test): it resolves as ``realpath`` does,
    within the probe's default timeout, marking nothing."""
    looked: list[int] = []

    class Counted(probe.MountTable):
        def get(self, key, default=None):
            looked.append(len(key))
            return super().get(key, default)

    real_points = probe.mount_points
    monkeypatch.setattr(probe, "mount_points", lambda mounts: Counted(real_points(mounts)))
    root = tmp_path / "root"
    root.mkdir()
    (root / "deep").symlink_to("/".join(["a"] * 1000))  # 1,999 bytes
    files = install_probe(monkeypatch, mounts=(root,), timeout=probe.PROBE_TIMEOUT_SECONDS)
    settings = make_settings(tmp_path, files={"roots": {"data": root}})
    text = f"{root}/deep"
    text += "/a" * ((MAX_PATH_BYTES - len(os.fsencode(text)) - 1999) // 2)

    resolved = resolve_inside_roots(text, settings)[1]
    components = len(resolved.parts)
    deepest = len(root.parts)

    assert resolved == Path(os.path.realpath(text)) and len(os.fsencode(str(resolved))) <= MAX_PATH_BYTES
    assert components >= 1900
    assert len(looked) <= 3 * deepest * components  # a quadratic lookup made 8,006,050 lookups for 4,000 components
    assert max(looked) <= len(str(root)) + 2  # never a prefix deeper than the root's mount and one name
    assert not files.hung(root)


def test_a_walk_stops_once_its_path_passes_the_length_limit(tmp_path, monkeypatch):
    """40 nested links planted in a root (each target a link to the next and 4 KB of names) made a 4 KB request walk
    about 84,000 components (51 s). The walk stops once the path it has walked passes ``MAX_PATH_BYTES`` (no stat could
    open a longer one): a 400 for a request, unavailable for a recorded path, after at most about one stat a component
    of 4 KB, marking no mount."""
    root = tmp_path / "root"
    root.mkdir()
    for index in range(MAX_LINKS):  # L0 -> L1/a/a/..., ..., L39 -> a/a/...: each target 4,095 bytes
        head = [f"L{index + 1}"] if index + 1 < MAX_LINKS else []
        names = head + ["a"] * ((4095 - sum(len(name) + 1 for name in head)) // 2)
        (root / f"L{index}").symlink_to("/".join(names))
    text = f"{root}/L0"
    text += "/a" * ((MAX_PATH_BYTES - len(os.fsencode(text))) // 2)
    files = install_probe(monkeypatch, mounts=(root,), timeout=probe.PROBE_TIMEOUT_SECONDS)
    settings = make_settings(tmp_path, files={"roots": {"data": root}})
    stats: list[str] = []
    lstat = os.lstat
    monkeypatch.setattr(os, "lstat", lambda path, *args, **kwargs: stats.append(path) or lstat(path, *args, **kwargs))

    for recorded in (False, True):
        with pytest.raises(PathNotAllowed, match="resolves to a path longer than 4,096 bytes"):
            resolve_inside_roots(text, settings, recorded=recorded)
    with pytest.raises(RuntimeError, match="resolves to a path longer than 4,096 bytes"):
        roots.recorded_real_path(text)

    assert len(stats) <= 3 * MAX_PATH_BYTES  # three walks of at most about 2,000 components each
    assert not files.hung(root)
