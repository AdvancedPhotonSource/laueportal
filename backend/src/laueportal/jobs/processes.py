"""Compute process cleanup shared by local and standalone executors.

The runner calls :func:`terminate_descendants` itself; ``jobs.lifecycle`` re-exports it for the executor. The live
computes end their pools' workers with :func:`end_pool_workers`. It imports neither the database nor the settings
(import-linter rule 3).
"""

from __future__ import annotations

import multiprocessing.connection
import time
from concurrent.futures import ProcessPoolExecutor

import psutil

_HELPER_MARKERS = ("multiprocessing.resource_tracker", "multiprocessing.semaphore_tracker")
WORKER_END_SECONDS = 5.0
"""How long :func:`end_pool_workers` gives a pool's workers to end after SIGTERM before SIGKILL (and again after it)."""


def is_helper_process(process: psutil.Process) -> bool:
    """Python's multiprocessing trackers are not compute work and exit with their parent (``jobs.lifecycle``'s
    sweep lets them)."""

    try:
        command = " ".join(process.cmdline())
    except psutil.Error:
        return False
    return any(marker in command for marker in _HELPER_MARKERS)


def terminate_descendants(grace_seconds: float = 5.0) -> int:
    """Terminate every descendant of this process; returns how many had to be dealt with."""

    try:
        children = [child for child in psutil.Process().children(recursive=True) if not is_helper_process(child)]
    except psutil.Error:
        return 0
    if not children:
        return 0
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(children, timeout=grace_seconds)
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=grace_seconds)
    return len(children)


def end_pool_workers(pool: ProcessPoolExecutor, *, grace_seconds: float | None = None) -> int:
    """End ``pool``'s workers now, abandoning their tasks, and wait until they are gone (SIGKILL for one still there
    after ``grace_seconds``, ``WORKER_END_SECONDS`` by default), so none writes anything afterwards; return how many are
    still there (only a process in uninterruptible I/O outlives SIGKILL). A live compute calls it first at a run-level
    error. It ends them the way lauelab's own schedulers end a broken pool (``_terminate``).
    ``ProcessPoolExecutor`` has no public way to stop running calls before Python 3.14: its process table is the only
    handle on the workers (``None`` once the pool has shut down: none left; a pool without one, none). Not
    :func:`terminate_descendants`, which would also end the job process's other children (a DM call in flight)."""
    grace = WORKER_END_SECONDS if grace_seconds is None else grace_seconds
    processes = list((getattr(pool, "_processes", None) or {}).values())
    sentinels = {process.sentinel: process for process in processes}
    for process in processes:
        process.terminate()
    pool.shutdown(wait=False, cancel_futures=True)
    for last in (False, True):
        deadline = time.monotonic() + grace
        while sentinels and (left := deadline - time.monotonic()) > 0:
            for ended in multiprocessing.connection.wait(list(sentinels), left):
                del sentinels[ended]
        if last or not sentinels:
            break
        for process in sentinels.values():
            process.kill()
    return len(sentinels)
