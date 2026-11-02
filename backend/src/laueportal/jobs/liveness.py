"""Whether a process a row records still runs on this host: a job process, which the dispatcher
records with its host, pid and start since boot (``jobs.dispatch``; the reconciler's check), and the worker, whose
row records its host, pid and ``started_at`` (``jobs.worker``; the status page's and ``/health``'s check). This module
imports neither the worker nor the run lifecycle, so the API may use it (import contract 4).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime

import psutil

SAME_PROCESS_SECONDS = 1.5
"""How far a live process's start may be from the recorded one for it to be the recorded job process (a reused
pid starts later). Both are seconds since boot (``since_boot``), which a step of the system clock does not move;
/proc's boot time is whole seconds, read twice, so a value may be one second off. The worker's check allows as much
after its row's ``started_at``."""


@dataclass(frozen=True)
class ProcessRecord:
    """A job process as its row records it: host, pid and start in seconds since boot."""

    host: str
    pid: int
    since_boot: float


def since_boot(process: psutil.Process) -> float:
    """When ``process`` started, in seconds since the host booted (psutil's ``create_time`` less the boot time:
    a step of the system clock moves both)."""
    return process.create_time() - psutil.boot_time()


def process_record(pid: int) -> ProcessRecord:
    """What a row records for the process ``pid`` of this host."""
    return ProcessRecord(socket.gethostname(), pid, since_boot(psutil.Process(pid)))


def recorded_process_alive(host: str | None, pid: int | None, started: float | None) -> bool | None:
    """Whether the recorded job process still runs: None when nothing usable is recorded or it ran on another host
    (this host cannot look at it); False when it is gone (a zombie too) or the pid is another process's now."""
    if host is None or pid is None or started is None or host != socket.gethostname():
        return None
    try:
        process = psutil.Process(pid)
        return process.status() != psutil.STATUS_ZOMBIE and abs(since_boot(process) - started) < SAME_PROCESS_SECONDS
    except psutil.Error:
        return False


def worker_process_alive(host: str, pid: int, started_at: datetime | None) -> bool | None:
    """Whether the worker a row records still runs: None on another host (this host cannot look at it); False when
    no process has its pid (a zombie too) or, given ``started_at``, the pid's process started more than
    ``SAME_PROCESS_SECONDS`` after it (a reused pid: the worker writes its row after it starts). Without
    ``started_at``, the pid alone counts: the row records the start by the wall clock, so a clock stepped forward
    since then makes the worker's own process look later."""
    if host != socket.gethostname():
        return None
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
        return started_at is None or process.create_time() <= started_at.timestamp() + SAME_PROCESS_SECONDS
    except psutil.Error:
        return False
