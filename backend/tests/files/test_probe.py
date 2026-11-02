"""Bounded file checks (``files.probe``): a check of a hung mount answers ``NotResponding`` within the probe's timeout,
the mount is remembered so that no more threads pile up behind it, the threads stuck in the kernel are at most the
probe's own, and once they all are a check answers at once; the checks of paths requests give leave the recorded paths'
checks threads of their own. The hang is a stat that blocks on an event (``tests.probe_support``); a hung mount's cap of
checks in flight is tested on the routes (``tests/test_files_on_net``)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from laueportal.files import probe
from laueportal.files.probe import FileProbe, NotResponding, mount_of, read_mounts
from tests.probe_support import hang, start_threads
from tests.settings_support import make_settings

pytestmark = pytest.mark.usefixtures("gc_disabled")  # each check starts within the probe's short timeout


def table(*points: str, autofs: tuple[str, ...] = ()) -> tuple[tuple[str, bool], ...]:
    return tuple((point, point in autofs) for point in ("/", *points))


def test_a_check_answers_with_its_value_or_its_error(tmp_path: Path) -> None:
    files = FileProbe(timeout=5, mounts=lambda: table())

    assert files.run(tmp_path, lambda: os.stat(tmp_path).st_ino) == os.stat(tmp_path).st_ino
    with pytest.raises(FileNotFoundError):
        files.run(tmp_path / "absent", lambda: os.stat(tmp_path / "absent"))


def test_a_hung_mount_answers_within_the_timeout_and_is_remembered_until_its_check_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net, local = tmp_path / "net", tmp_path / "local"
    net.mkdir(), local.mkdir()
    hung = hang(monkeypatch, net)
    files = start_threads(FileProbe(timeout=0.2, hung_seconds=0, mounts=lambda: table(str(net), str(local))))

    with pytest.raises(NotResponding) as answer:
        files.run(net / "run", lambda: os.stat(net / "run"))
    assert not hung.released.is_set() and hung.calls == 1  # answered while the stat is still in the "kernel"
    assert answer.value.status == 503 and answer.value.headers["Retry-After"] == "1"  # the memory, a second at least

    ran = threading.Event()
    with pytest.raises(NotResponding):  # the mount is remembered: the check is not run
        files.run(net / "other", ran.set)
    assert (hung.calls, ran.is_set(), files.hung(net / "x")) == (1, False, True)
    assert files.run(local, lambda: os.stat(local).st_ino) == os.stat(local).st_ino  # another mount answers

    hung.released.set()  # the mount comes back: its stuck check returns
    for _ in range(200):
        if not files.hung(net):
            break
        time.sleep(0.01)
    assert files.run(net, lambda: os.stat(net).st_ino) == os.stat(net).st_ino


def test_threads_stuck_in_the_kernel_are_the_probes_own_and_once_all_are_a_check_answers_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads, two hung mounts: both threads are stuck. A third mount's check is not responding without
    waiting: it never runs, and its mount is not remembered."""
    first, second, third = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for folder in (first, second, third):
        folder.mkdir()
    hangs = [hang(monkeypatch, first), hang(monkeypatch, second)]
    files = start_threads(FileProbe(threads=2, timeout=0.2, mounts=lambda: table(str(first), str(second), str(third))))
    ran = threading.Event()
    try:
        for folder in (first, second):
            with pytest.raises(NotResponding):
                files.run(folder, lambda folder=folder: os.stat(folder))
        with pytest.raises(NotResponding):
            files.run(third, ran.set)
        threads = [thread for thread in threading.enumerate() if thread.name.startswith(f"{files.name}-")]
        remembered = (files.hung(first), files.hung(second), files.hung(third))
    finally:
        for hung in hangs:
            hung.released.set()

    assert len(threads) == 2 and all(hung.calls == 1 for hung in hangs)
    assert (ran.is_set(), remembered) == (False, (True, True, False))


def test_the_api_bounds_its_checks_by_its_settings_and_the_cli_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_configured", probe._configured)
    monkeypatch.setattr(probe, "_default", None)
    files = make_settings(
        tmp_path, files={"probe_timeout_seconds": 2.5, "hung_mount_seconds": 12, "probe_threads": 3}
    ).files

    probe.configure(files)
    api = probe.default_probe()
    probe.configure(bound=False)
    cli = probe.default_probe()

    assert (api.timeout, api.threads, api.not_responding("/net/x").headers["Retry-After"]) == (2.5, 3, "12")
    assert cli.timeout is None and cli.run("/net/x", threading.current_thread) is threading.current_thread()


def test_a_path_is_remembered_by_its_mount(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw\n"
        "40 22 0:35 / /net rw,relatime shared:20 - autofs /etc/auto.net rw,fd=7\n"
        "41 40 0:36 / /net/fileserver/export rw,relatime - nfs4 fileserver:/export rw\n"
        "42 22 0:37 / /data\\040one rw,relatime - nfs s:/d rw\n"
    )
    mounts = read_mounts(str(mountinfo))

    assert mounts == (("/", False), ("/net", True), ("/net/fileserver/export", False), ("/data one", False))
    assert mount_of("/net/fileserver/export/runs/rec_1", mounts) == "/net/fileserver/export"
    assert mount_of("/net/storage/data-b/Run2023-1", mounts) == "/net/storage"  # not mounted yet: its host
    assert mount_of("/data one/x", mounts) == "/data one"
    assert mount_of("/home/user/runs/../x", mounts) == "/home/user"  # the root file system: never "/" itself
    assert mount_of("/net/storage/data-b", None) == "/net/storage"  # without /proc: two components
    files = FileProbe(threads=2, timeout=1, mounts=lambda: mounts)
    apart = {folder: files.keys_apart(folder) for folder in ("/net", "/net/fileserver", "/home", "/home/user", "/")}
    assert [apart["/net"]("storage"), apart["/net/fileserver"]("export"), apart["/net/fileserver"]("other")] == [
        True,
        True,
        False,
    ]
    assert [apart["/home"]("user"), apart["/home/user"]("runs"), apart["/"]("data one"), apart["/"]("usr")] == [
        True,
        False,
        True,
        True,
    ]


def test_requested_checks_leave_the_recorded_paths_two_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Checks of paths requests give share ``threads - RECORDED_THREADS``: eight of them on four hung mounts hold
    six threads, and the next answers at once; a recorded path's check on a healthy mount still runs."""
    dead = [tmp_path / f"dead{index}" for index in range(4)]
    healthy = tmp_path / "healthy"
    for folder in (*dead, healthy):
        folder.mkdir()
    hangs = [hang(monkeypatch, folder) for folder in dead]
    files = start_threads(FileProbe(threads=8, timeout=0.3, mounts=lambda: table(*map(str, dead), str(healthy))))
    answers: list[str] = []

    def request(folder: Path) -> None:
        try:
            files.run(folder, lambda: os.stat(folder), requested=True)
        except NotResponding:
            answers.append("not responding")

    threads = [threading.Thread(target=request, args=(folder,)) for folder in dead for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stuck = (files._stuck_total, files._requested_stuck)
        started = time.monotonic()
        with pytest.raises(NotResponding):
            files.run(healthy, lambda: "requested", requested=True)
        refused_in = time.monotonic() - started
        recorded = files.run(healthy, lambda: "recorded")
    finally:
        for hung in hangs:
            hung.released.set()

    assert (answers, stuck, files.request_threads) == (["not responding"] * 8, (6, 6), 6)
    assert refused_in < 0.05 and recorded == "recorded"
