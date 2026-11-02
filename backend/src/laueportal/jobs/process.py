"""A job process: ``python -m laueportal.jobs.process run <job ID> [--cores <n>]`` or
``convert <indexing ID>``, started by the worker's dispatcher (``jobs.dispatch``) in a session of its own, never
forked. ``--cores`` is a live run's share of the machine's cores (``jobs.dispatch.live_cores``), which
``execute_run`` gives its live compute as ``RunRequest.cores`` to size its pools; a batch item has none.

Its first act is its handling of the service stop's signal (``STOP_SIGNAL``, ``lifecycle.SHUTDOWN_SIGNAL``; this
module imports only the standard library at its top, so nothing slow comes before it). Until then the signal is
ignored: the worker ignores it itself and a job process inherits that across exec (``jobs.worker``), so a stop
cannot kill a job process that is still starting, and the worker sends it again on each poll of its stop. A run's
job process records the request (``STOP``): the run drains and ends Failed/interrupted like any stopped run, even
when the stop came before it began work (``execute_run`` then starts already stopping). A conversion's keeps
ignoring it and finishes. Once the item has run, the signal is ignored again: a late one never ends the process.

It then reads the settings (``$LAUEPORTAL_CONFIG``) and opens its own engine, waits for the dispatcher's go line on its
stdin (sent once the row records this process; end of file when the worker died first, or when the worker's service stop
let it go unrecorded, ``Dispatcher.let_go_unrecorded``), and runs nothing unless the row names its pid on this host:
then ``execute_run`` (the run's claim is the dispatcher's) or the conversion. It arms its own timeout: SIGALRM raises
``lifecycle.RunTimeout`` inside the item after ``execution.job_timeout_seconds`` for a run (the executor records it as a
failure) or ``indexing.conversion.CONVERSION_TIMEOUT_SECONDS`` for a conversion; a live run has none (``jobs.live``:
only its Stop or the stale-heartbeat rule ends it). Exit status 0 when the item ran (whatever its outcome), 1 when it
raised, 2 for a usage or settings error, 3 when the row does not name this process (nothing ran).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Sequence
from types import FrameType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from laueportal.core.settings import Settings

logger = logging.getLogger(__name__)

NOT_MINE = 3

STOP_SIGNAL = signal.SIGUSR1
"""The service stop's signal, ``lifecycle.SHUTDOWN_SIGNAL`` (named here: nothing is imported before it is handled)."""

STOP = threading.Event()
"""Set when a run's job process gets ``STOP_SIGNAL``, at any time after its first act; ``execute_run`` takes it."""
_stop_recorded = False


def _record_stop(signum: int, frame: FrameType | None) -> None:
    """A run's handler of ``STOP_SIGNAL``. Only its first call sets ``STOP``: the worker sends the signal again on
    each poll of its stop, CPython runs a handler again when its signal comes while it runs, and a second
    ``Event.set()`` inside the first would wait for the event's lock, which the first holds (the flag is set before
    the event, and taking it needs no lock)."""
    global _stop_recorded
    if _stop_recorded:
        return
    _stop_recorded = True
    STOP.set()


def handle_stop(kind: str) -> None:
    """The job process's handling of the service stop: a run's records it (``STOP``); a conversion's ignores it."""
    if kind == "run":
        signal.signal(STOP_SIGNAL, _record_stop)
    else:
        signal.signal(STOP_SIGNAL, signal.SIG_IGN)  # a conversion cannot stop part way: it finishes


def _timeout(seconds: int) -> None:
    from laueportal.jobs.lifecycle import RunTimeout

    def expired(signum: int, frame: FrameType | None) -> None:
        raise RunTimeout(f"Task exceeded maximum timeout value ({seconds} seconds)")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(max(1, int(seconds)))


def names_this_process(engine: Engine, kind: str, key: int) -> bool:
    """Whether the claimed row records this process (host and pid) and is still being run."""
    from sqlalchemy.orm import Session

    from laueportal.indexing.models import Conversion
    from laueportal.jobs.dispatch import CONVERSION_RUNNING
    from laueportal.jobs.models import Job
    from laueportal.jobs.state import JobStatus

    with Session(engine) as session:
        if kind == "run":
            row: Job | Conversion | None = session.get(Job, key)
            running = row is not None and row.status == JobStatus.RUNNING
        else:
            row = session.get(Conversion, key)
            running = row is not None and row.status == CONVERSION_RUNNING
        return (
            running and row is not None and row.process_pid == os.getpid() and row.process_host == socket.gethostname()
        )


def _live(engine: Engine, job_id: int) -> bool:
    """Whether the run is a live run (``jobs.live.is_live_job``: its queue identity), which has no time limit."""
    from sqlalchemy.orm import Session

    from laueportal.jobs.live import is_live_job

    with Session(engine) as session:
        return is_live_job(session, job_id)


def run(kind: str, key: int, settings: Settings, *, cores: int | None = None) -> int:
    from laueportal.core.db import make_engine

    engine = make_engine(settings, create=False)
    try:
        sys.stdin.buffer.readline()  # the dispatcher's go line, or end of file
        if not names_this_process(engine, kind, key):
            logger.warning("Job process %s: the row of %s %s does not name it; nothing runs", os.getpid(), kind, key)
            return NOT_MINE
        if kind == "run":
            from laueportal.jobs.executor import execute_run
            from laueportal.jobs.queue import RunPolicy

            policy = RunPolicy.from_settings(settings.execution)
            if not _live(engine, key):
                _timeout(policy.job_timeout_seconds)
            execute_run(key, engine=engine, policy=policy, shutdown=STOP, live_settings=settings.live, cores=cores)
        else:
            from laueportal.indexing.conversion import CONVERSION_TIMEOUT_SECONDS, run_conversion

            _timeout(CONVERSION_TIMEOUT_SECONDS)
            run_conversion(key, engine=engine, settings=settings)
        return 0
    except Exception:
        logger.exception("Job process %s: %s %s raised", os.getpid(), kind, key)
        return 1
    finally:
        signal.alarm(0)
        signal.signal(STOP_SIGNAL, signal.SIG_IGN)  # done: a late stop must not end the exiting process
        engine.dispose()


def _arguments(args: Sequence[str]) -> tuple[str, int, int | None] | None:
    """``run|convert <id>``, or ``run <id> --cores <n>`` (n at least 1); None for anything else."""
    if len(args) not in (2, 4) or args[0] not in ("run", "convert") or not args[1].isdigit():
        return None
    if len(args) == 2:
        return args[0], int(args[1]), None
    if args[0] != "run" or args[2] != "--cores" or not args[3].isdigit() or int(args[3]) < 1:
        return None
    return args[0], int(args[1]), int(args[3])


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    handle_stop(args[0] if args else "")  # the first act (the module's docstring)

    from laueportal.core.logging import configure_logging
    from laueportal.core.settings import ConfigError, get_settings
    from laueportal.files import probe

    parsed = _arguments(args)
    if parsed is None:
        print("usage: python -m laueportal.jobs.process run <id> [--cores <n>] | convert <id>", file=sys.stderr)
        return 2
    try:
        settings = get_settings()
    except ConfigError as error:
        print(f"laueportal.jobs.process: {error}", file=sys.stderr)
        return 2
    configure_logging(settings)
    probe.configure(bound=False)  # as the CLI and the worker: a slow but healthy mount must not fail a conversion
    kind, key, cores = parsed
    return run(kind, key, settings, cores=cores)


if __name__ == "__main__":
    sys.exit(main())
