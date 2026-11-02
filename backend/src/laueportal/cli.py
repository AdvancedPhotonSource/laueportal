"""The ``laueportal`` command (also ``python -m laueportal``).

Settings come from ``$LAUEPORTAL_CONFIG`` plus ``LAUEPORTAL_<SECTION>__<KEY>`` overrides
(``core.settings``). A configuration error (``ConfigError``: invalid settings, or WAL asked for on a network file
system, ``core.db.UnsafeJournalMode``) exits with status 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from laueportal import __version__
from laueportal.core.db import DATABASE_SUFFIXES, database_files
from laueportal.core.settings import (
    ConfigError,
    DatabaseSettings,
    config_file_from_env,
    environment_overrides,
    get_settings,
    load_settings,
)

SOURCE_DIR = Path(__file__).resolve().parent.parent
"""``backend/src`` in a checkout: what ``serve --reload`` watches."""

EXIT_CONFIG_ERROR = 2
EXIT_FAILURE = 1

PROTECTED_SUFFIXES = (*DATABASE_SUFFIXES, ".sha256", ".migration.sha256")
"""What is protected of each path ``database.protected`` lists: its files (``core.db.DATABASE_SUFFIXES``: the
database, its journal, WAL and shared memory), its checksum, and a migration manifest's checksum; and, when it is a
folder, everything under it."""


# ---------------------------------------------------------------------------
# Commands


def cmd_serve(args: argparse.Namespace) -> int:
    """The API and the built SPA on ``app.host``/``app.port`` in one uvicorn process (its caches, turns and file
    probe are the process's own; ``WEB_CONCURRENCY`` cannot raise it). ``--reload`` (development) restarts it when
    ``backend/src`` changes and serves the API alone without a built SPA; without it (production, the e2e stack)
    it refuses to start (exit 2) unless ``app.static_dir`` holds the build's ``index.html``."""
    import uvicorn

    from laueportal.core.db import guard_journal_mode
    from laueportal.core.logging import configure_logging

    settings = get_settings()  # fail here, before uvicorn starts, if the configuration is invalid
    guard_journal_mode(settings.database)  # and if WAL is asked for on a network file system (the app checks too)
    static_dir = settings.app.static_dir
    if not args.reload and (static_dir is None or not (static_dir / "index.html").is_file()):
        where = "is not set" if static_dir is None else f"{static_dir} has no index.html"
        print(
            f"laueportal serve: app.static_dir {where}: build the SPA (make build) and set app.static_dir to its "
            "folder (frontend/dist), or serve with --reload (development) for the API alone",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    configure_logging(settings)
    uvicorn.run(
        "laueportal.app:serve_app",
        factory=True,
        host=settings.app.host,
        port=settings.app.port,
        workers=1,
        reload=args.reload,
        reload_dirs=[str(SOURCE_DIR)] if args.reload else None,
        log_config=None,  # keep configure_logging's handlers
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Run the worker (``jobs.worker.run_worker``: the dispatcher and reconciler threads; one per database).

    Exit status 0 after a stop signal or (``--burst``) once nothing is queued or running; 1 for a missing database
    or when another worker holds the database's worker lock."""
    from laueportal.core.logging import configure_logging
    from laueportal.jobs.worker import run_worker

    settings = get_settings()
    configure_logging(settings)
    return run_worker(settings, burst=args.burst)


def cmd_live_notify(args: argparse.Namespace) -> int:
    """Admit one file into a live run whose source is ``notify``: ``jobs.arrivals.notify``, from an acquisition hook
    over SSH (its key's forced command, ``jobs.notify_hook``) or by hand, on the portal host. Prints the arrival as one
    JSON line (``admitted`` false for a file admitted before with the same identity: a no-op) and exits 0; a refusal (no
    ``--complete``, not an open live run, not its source, not one of its files, a changed file) is one line on stderr
    and exit status 1, and so is an unexpected error (the file system: a permission, the disk, the lock; a config file
    or a database folder the hook's user may not read; the database; the admission index beside it,
    ``jobs.arrival_index``), "failed: <type>: <reason>", without a traceback; a missing database exits 2. Its cost per
    file does not grow with the run's arrivals (the index); its process start is most of it
    (``docs/runbooks/live-deployment-test.md``, "Which source to keep")."""
    import sqlite3

    from sqlalchemy.exc import SQLAlchemyError

    from laueportal.core.db import make_engine
    from laueportal.jobs.arrivals import ArrivalRefused, ArrivalsUnreadable, notify

    def failed(error: BaseException) -> int:
        reason = getattr(error, "orig", None) or error  # a database error: the driver's, without the SQL
        message = (str(reason).splitlines() or [""])[0]
        print(f"laueportal live notify: failed: {type(reason).__name__}: {message}", file=sys.stderr)
        return EXIT_FAILURE

    try:  # a config file the hook's user may not read
        settings = get_settings()  # no logging configured: a refusal is its one line (the hook's log)
    except OSError as error:
        return failed(error)
    if not args.complete:
        print(
            "laueportal live notify: refused: --complete is required (the file's acquisition and transfer are done)",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    try:  # Path.is_file raises for a folder the hook's user may not enter (Python 3.12)
        missing = not settings.database.path.is_file()  # SQLite would create an empty database on connect
    except OSError as error:
        return failed(error)
    if missing:
        print(f"laueportal live notify: database {settings.database.path} does not exist", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    engine = None
    try:
        engine = make_engine(settings)
        admission = notify(engine, args.job_id, args.path)
    except (ArrivalRefused, ArrivalsUnreadable) as refused:
        print(f"laueportal live notify: refused: {refused}", file=sys.stderr)
        return EXIT_FAILURE
    except (OSError, SQLAlchemyError, sqlite3.Error) as error:  # sqlite3: the admission index's folder
        return failed(error)
    finally:
        if engine is not None:
            engine.dispose()
    arrival = admission.arrival
    print(json.dumps({"job_id": args.job_id, "n": arrival.n, "path": arrival.path, "admitted": admission.admitted}))
    return 0


def cmd_remote_poll(args: argparse.Namespace) -> int:
    """One pass of the remote poller (``remote.poller.run_poller``): reconcile remote runs
    with DM, publish their results and list what Finished runs left behind, under the poller lock. The loop is the
    worker's poller thread, so the command needs ``--once``. Another poller holding the lock (the worker's thread,
    or another portal's) exits 1. With remote execution disabled (``remote.enabled``) it says so and
    exits 0; remote settings that cannot be used, or a missing database, exit 2."""
    from laueportal.core.db import make_engine
    from laueportal.core.logging import configure_logging
    from laueportal.remote.poller import PollerBusyError, run_poller
    from laueportal.remote.settings import RemoteSettingsError, portal_settings

    settings = get_settings()
    configure_logging(settings)
    try:
        remote = portal_settings(settings.remote, runs_root=settings.files.runs_root)
    except RemoteSettingsError as error:
        print(f"laueportal remote poll: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if remote is None:
        print("laueportal remote poll: remote execution is disabled (remote.enabled); nothing to poll", file=sys.stderr)
        return 0
    if not settings.database.path.is_file():  # SQLite would create an empty database on connect
        print(f"laueportal remote poll: database {settings.database.path} does not exist", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    engine = make_engine(settings)
    try:
        run_poller(
            remote,
            engine=engine,
            on_locked=lambda path: print(f"laueportal remote poll: polling (lock {path})", file=sys.stderr, flush=True),
        )
    except PollerBusyError as error:
        print(f"laueportal remote poll: {error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        engine.dispose()
    return 0


def cmd_jobs_reconcile(args: argparse.Namespace) -> int:
    """Reconcile the database's unfinished runs and conversions (``jobs.reconciler``, which the worker runs every
    ``execution.reconcile_seconds``): one cycle with ``--once``, else a cycle every ``reconcile_seconds`` until
    SIGTERM or SIGINT. Prints what each cycle changed. A missing database exits 2."""
    import signal
    import threading

    from laueportal.core.db import make_engine
    from laueportal.core.logging import configure_logging
    from laueportal.jobs.reconciler import ReconcilePolicy, reconcile_once

    settings = get_settings()
    configure_logging(settings)
    if not settings.database.path.is_file():  # SQLite would create an empty database on connect
        print(f"laueportal jobs reconcile: database {settings.database.path} does not exist", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    policy = ReconcilePolicy.from_settings(settings.execution)
    stop = threading.Event()
    if not args.once:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _number, _frame: stop.set())
    engine = make_engine(settings)
    try:
        while True:
            result = reconcile_once(engine, policy)
            print(
                f"interrupted {result.interrupted}; stuck in creation {result.abandoned}; "
                f"conversions failed {result.conversions_failed}",
                flush=True,
            )
            if args.once or stop.wait(policy.reconcile_seconds):
                return 0
    finally:
        engine.dispose()


def openapi_text() -> str:
    """The OpenAPI document as deterministic JSON: sorted keys, 2-space indent, trailing newline."""
    from laueportal.app import openapi_document

    return json.dumps(openapi_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def cmd_openapi(args: argparse.Namespace) -> int:
    text = openapi_text()
    if args.out is None:
        sys.stdout.write(text)
    else:
        _write_atomically(args.out, text)
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


def cmd_config_show(_args: argparse.Namespace) -> int:
    """Print the effective settings as JSON; where they came from goes to stderr."""
    config_file = config_file_from_env()
    settings = load_settings(config_file)
    overrides = environment_overrides()
    print(f"# settings file: {config_file}", file=sys.stderr)
    print(f"# environment overrides: {', '.join(overrides) if overrides else 'none'}", file=sys.stderr)
    print(json.dumps(settings.redacted(), indent=2))
    return 0


def cmd_results_convert(args: argparse.Namespace) -> int:
    """Convert historical indexing XML results to lauelab results files.

    Sources are never modified; ``--destination-root`` places ``index_<id>/output.h5`` under a writable
    root when the historical output directories are read-only. Exit status 1 when a conversion failed.

    Without ``--geometry``, each run converts with the geometry its XML names, mapped and confined to the file roots
    as the viewer's conversion does (``indexing.viewer.conversion_geometry``): a run whose XML names a geometry file
    lauelab would open as written although it is unavailable for the run fails, and nothing is written for it. With
    ``--db`` no configuration is read, so there are no roots: such a run fails; give ``--geometry``, or convert the
    configured database.
    """
    if bool(args.ids) == args.all:
        print("laueportal results convert: give indexing run ids (I12, 12, 3-7) or --all, not both", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    from laueportal.core.db import database_engine
    from laueportal.core.settings import DatabaseSettings
    from laueportal.indexing import results
    from laueportal.indexing.models import IndexingRun
    from laueportal.indexing.viewer import conversion_geometry

    try:
        ids = None if args.all else results.iter_ids(args.ids)
    except ValueError:
        print(f"laueportal results convert: not an indexing run id or range: {' '.join(args.ids)}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.db is not None:  # no configuration is read, so there are no file roots
        settings, database = None, DatabaseSettings(path=args.db)
    else:
        settings = get_settings()
        database = settings.database
    if not database.path.is_file():  # SQLite would create an empty database on connect
        print(f"laueportal results convert: database {database.path} does not exist", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    def geometry_for(run: IndexingRun, xml_path: str) -> tuple[str | None, str | None]:
        return conversion_geometry(xml_path, run_directory=run.output_path, settings=settings)

    engine = database_engine(database)
    try:
        report = results.convert_indexing_results(
            ids,
            engine=engine,
            dry_run=args.dry_run,
            destination_root=str(args.destination_root) if args.destination_root else None,
            geometry=str(args.geometry) if args.geometry else None,
            geometry_for=geometry_for,
            replace_invalid=args.replace_invalid,
            progress=lambda outcome: print(f"I{outcome.indexing_id}: {outcome.outcome}", file=sys.stderr, flush=True),
        )
    finally:
        engine.dispose()
    print(report.format_text())
    if args.report is not None:
        results.write_report(report, args.report)
        print(f"Report written to {args.report}")
    return 1 if report.counts().get(results.OUTCOME_FAILED) else 0


def _database(args: argparse.Namespace, *, guard: bool = True) -> DatabaseSettings:
    """``settings.database``, or the database named by ``--db`` (same journal mode and legacy time zone).

    Applies the journal-mode guard first (``core.db.guard_journal_mode``: WAL on a network file system is an
    ``UnsafeJournalMode``, a ``ConfigError``: exit 2), so every ``db`` command refuses it, the read-only ones too;
    ``db check`` passes ``guard=False`` and reports it as one of its checks."""
    from laueportal.core.db import guard_journal_mode

    configured = get_settings().database
    database = (
        configured
        if getattr(args, "db", None) is None
        else configured.model_copy(update={"path": args.db.expanduser().resolve()})
    )
    if guard:
        guard_journal_mode(database)
    return database


def cmd_db_upgrade(args: argparse.Namespace) -> int:
    """Upgrade to a revision (default head); exit 1 when refused (e.g. a revision below the database's)."""
    from sqlalchemy.exc import SQLAlchemyError

    from laueportal.core import migrations

    database = _database(args)
    try:
        before, after = migrations.upgrade(database, args.revision)
    except (migrations.MigrationError, SQLAlchemyError) as error:
        print(f"laueportal db upgrade: {_database_error(database, error)}", file=sys.stderr)
        return EXIT_FAILURE
    if before == after:
        print(f"{database.path}: already at {after}")
    else:
        print(f"{database.path}: upgraded {before or '(empty)'} -> {after}")
    return 0


def cmd_db_stamp(args: argparse.Namespace) -> int:
    """Stamp a revision after checking the schema; ``--detect`` stamps the v1 revision the schema matches."""
    from sqlalchemy.exc import SQLAlchemyError

    from laueportal.core import migrations

    database = _database(args)
    detected = ""
    try:
        if args.detect:
            # a v1 cb1159c database: its live tables, if it has them, dropped first
            found = migrations.stamp_detected(database)
            revision = found.revision
            if found.drops:
                shape = f"; its live tables {', '.join(found.drops)} dropped"
            else:  # v1 cb1159c's migration script's output: the column, no live table
                shape = " without its live tables" if found.name == migrations.CB1159C else ""
            detected = f" (detected: {found.name}'s schema{shape})"
        else:
            revision = args.revision
            migrations.stamp(database, revision)
    except (migrations.MigrationError, SQLAlchemyError) as error:
        print(f"laueportal db stamp: {_database_error(database, error)}", file=sys.stderr)
        return EXIT_FAILURE
    print(f"{database.path}: stamped {revision}{detected}")
    return 0


def cmd_db_current(args: argparse.Namespace) -> int:
    """Print the revision and what to do next (read-only: the database is never written)."""
    from sqlalchemy.exc import SQLAlchemyError

    from laueportal.core import migrations

    database = _database(args)
    try:
        description = migrations.describe_current(database)
    except (migrations.MigrationError, SQLAlchemyError) as error:
        print(f"laueportal db current: {_database_error(database, error)}", file=sys.stderr)
        return EXIT_FAILURE
    print(description)
    return 0


def cmd_db_check(args: argparse.Namespace) -> int:
    """Exit 1 unless the database is fit to run (read-only): at head and matching the models and head's
    DDL, ``quick_check`` and ``foreign_key_check`` clean, every timestamp UTC (``migrations.examine``), its journal
    mode safe on its file system (``core.db.journal_mode_problem``), and no run stuck in created or published (the
    reconciler's rule, ``jobs.reconciler.runs_stuck_in_creation``, counted once the models can read the rows).
    Prints what it measured, with the counts, then OK; the problems go to stderr."""
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    from laueportal.core import migrations
    from laueportal.core.db import journal_mode_problem, journal_mode_report
    from laueportal.jobs.reconciler import runs_stuck_in_creation

    timeout = get_settings().execution.creation_timeout_seconds
    database = _database(args, guard=False)
    report = [journal_mode_report(database)]
    problems = [problem] if (problem := journal_mode_problem(database)) is not None else []
    try:
        found = migrations.examine(database)
        report += found.report
        problems += found.problems
        if found.readable:
            with migrations.read_connection(database) as connection, Session(connection) as session:
                stuck = runs_stuck_in_creation(session, creation_timeout_seconds=timeout)
            report.append(f"runs stuck in created or published: {len(stuck)}")
            if stuck:
                shown = ", ".join(str(job_id) for job_id in stuck[:10]) + (", ..." if len(stuck) > 10 else "")
                problems.append(
                    f"{len(stuck)} runs stuck in created or published for more than {timeout:g} s (job {shown}): the "
                    "worker's reconciler fails them (`laueportal jobs reconcile --once`)"
                )
    except (migrations.MigrationError, SQLAlchemyError) as error:
        problems.append(_database_error(database, error))
    for line in report:
        print(line)
    if problems:
        for problem in problems:
            print(f"laueportal db check: {problem}", file=sys.stderr)
        return EXIT_FAILURE
    print(f"{database.path}: OK")
    return 0


def cmd_db_backup(args: argparse.Namespace) -> int:
    """Copy the database (``database.path``, or ``--db``) to ``dest``, consistent while the app runs
    (``core.db.backup``: SQLite's online backup API, then ``quick_check`` of the copy, which takes ``dest``'s
    name only once it passed). Refused (exit 1, nothing written): a destination that is the configured database or
    the source, or one of their files (``_companions``: journal, WAL, shared memory; by any path, a hard link too), a
    protected original (``_refuse_destination``), a folder, and an existing file unless ``--force``."""
    from laueportal.core.db import BackupError, backup

    database = _database(args)
    dest = args.dest.expanduser().resolve()
    reason = _refuse_backup(dest, [get_settings().database.path, database.path], force=args.force)
    if reason is not None:
        print(f"laueportal db backup: refusing {dest}: {reason}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        for stale in _companions(dest)[1:]:
            stale.unlink(missing_ok=True)  # another database's journal or WAL would be read with the copy
        pages = backup(database, dest)
    except (BackupError, OSError) as error:
        print(f"laueportal db backup: {error}", file=sys.stderr)
        return EXIT_FAILURE
    print(f"{dest}: backed up {database.path} ({pages} pages, {dest.stat().st_size} bytes; quick_check ok)")
    return 0


def _refuse_backup(dest: Path, databases: list[Path], *, force: bool) -> str | None:
    """Why ``db backup`` must not write ``dest``; None when it may."""
    if dest.is_dir():
        return "it is a folder: name the backup file"
    if not dest.parent.is_dir():
        return f"folder {dest.parent} does not exist"
    if (reason := _refuse_destination(dest)) is not None:
        return reason
    for database in databases:
        theirs = _companions(database.expanduser().resolve())
        identities = {identity for path in theirs if (identity := _identity(path)) is not None}
        for path in _companions(dest):
            if path.resolve() in theirs or _identity(path) in identities:
                return f"{path} is the database {database} (or one of its files); it is never written by a backup"
    existing = [path for path in _companions(dest) if path.exists()]
    if existing and not force:
        return f"{existing[0]} exists; pass --force to replace it"
    return None


def _companions(path: Path) -> list[Path]:
    """A database's files (``core.db.database_files``): copied, replaced and protected together."""
    return database_files(path)


def _identity(path: Path) -> tuple[int, int] | None:
    """``(device, inode)`` of the file ``path`` names (symlinks followed); None when there is none."""
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_dev, status.st_ino


def _protected_originals() -> tuple[set[Path], list[Path], set[tuple[int, int]]]:
    """The protected originals (``database.protected``; none by default): their paths and resolved paths, the
    folders everything under which is protected (each listed path, as given and resolved), and the (device, inode)
    of every file among them that exists (to recognize hard links)."""
    paths: set[Path] = set()
    directories: list[Path] = []
    identities: set[tuple[int, int]] = set()
    for original in get_settings().database.protected:
        files = [Path(f"{original}{suffix}") for suffix in PROTECTED_SUFFIXES]
        directories += [original, original.resolve()]
        for walk_root, _dirs, names in os.walk(original):
            files += [Path(walk_root) / name for name in names]
        for path in files:
            paths |= {path, path.resolve()}
            if (identity := _identity(path)) is not None:
                identities.add(identity)
    return paths, directories, identities


def _refuse_destination(dest: Path) -> str | None:
    """Why a command (``seed demo``, ``seed slow-runs``, ``db backup``) must not write ``dest`` or one of its files
    (``_companions``), even with ``--force``.

    Refused: a protected original, reached by any path (relative, through symlinks, or a hard link: the
    same device and inode), anything under a protected folder (``database.protected``), and an existing file
    that is not writable (``--force`` would unlink it: the directory, not the file, decides that).
    """
    paths, directories, identities = _protected_originals()
    for path in _companions(dest):
        resolved = path.resolve()
        if resolved in paths or any(resolved == root or resolved.is_relative_to(root) for root in directories):
            return f"{path} is a protected original ({resolved}); it is never modified"
        if _identity(path) in identities:
            return f"{path} is the same file as a protected original (a link to it); it is never modified"
        if path.exists() and not os.access(path, os.W_OK):
            return f"{path} exists and is not writable; it is not replaced"
    return None


def _database_error(database: DatabaseSettings, error: Exception) -> str:
    """One line for a failed database command: a ``MigrationError`` says it all; SQLite's own errors
    (wrapped by SQLAlchemy: a locked, read-only or corrupt file) are given with the database's path."""
    from laueportal.core.migrations import MigrationError

    if isinstance(error, MigrationError):
        return str(error)
    return f"database {database.path}: {getattr(error, 'orig', None) or error}"


def cmd_seed_demo(args: argparse.Namespace) -> int:
    """Build the demo database and its runs root (``seed.build.build_demo``); exit 1 when it could not.

    R1 and I1 run on the worker serving the database (the stack's), or on a burst worker when none takes them. The destination may never be a protected original (``_refuse_destination``).
    ``--crux-run`` adds R4, a run recorded as running on CRUX, and is refused while remote execution is enabled.
    """
    from laueportal.seed.build import SeedError, build_demo

    settings = get_settings()
    database = (args.db or settings.database.path).expanduser().resolve()
    if (reason := _refuse_destination(database)) is not None:
        print(f"laueportal seed demo: refusing {database}: {reason}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        result = build_demo(
            settings,
            force=args.force,
            database=database if args.db is not None else None,
            crux_run=args.crux_run,
        )
    except SeedError as error:
        print(f"laueportal seed demo: {error}", file=sys.stderr)
        return EXIT_FAILURE
    runs, registered = result.runs, result.registered
    print(f"Demo database ready: {result.database} (revision {result.revision}; runs in {result.runs_root})")
    print(f"  R{runs.reconstruction_id} and I{runs.indexing_id} ran on {result.ran_by}")
    print(
        f"  R{registered.fine_reconstruction_id}, I{registered.map_indexing_id} and "
        f"R{registered.multipoint_reconstruction_id} are registered results"
    )
    return 0


def cmd_seed_slow_runs(args: argparse.Namespace) -> int:
    """Queue long demo indexing runs (``seed.slow.queue_slow_runs``) and print them as JSON; exit 1 when it could not.

    For the run monitor's live test: the stack's worker runs them, and Stop and Move to Front act on
    them through the API. The database is the configured one, never a protected original.
    """
    from laueportal.seed.slow import SlowRunsError, queue_slow_runs

    settings = get_settings()
    database = settings.database.path.expanduser().resolve()
    if (reason := _refuse_destination(database)) is not None:
        print(f"laueportal seed slow-runs: refusing {database}: {reason}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        result = queue_slow_runs(settings, count=args.count, n_inputs=args.inputs)
    except SlowRunsError as error:
        print(f"laueportal seed slow-runs: {error}", file=sys.stderr)
        return EXIT_FAILURE
    runs = [{"job_id": run.job_id, "indexing_id": run.indexing_id} for run in result.runs]
    print(json.dumps({"database": str(result.database), "runs": runs}))
    return 0


# ---------------------------------------------------------------------------
# Parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="laueportal", description="3DMN Portal (Laue Portal) backend")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    serve = commands.add_parser(
        "serve", help="run the API and the built SPA (one uvicorn process, host/port from settings)"
    )
    serve.add_argument(
        "--reload",
        action="store_true",
        help="restart when backend/src changes, and serve the API alone without a built SPA (development)",
    )
    serve.set_defaults(func=cmd_serve)

    worker = commands.add_parser("worker", help="run the worker: the dispatcher and the reconciler (one per database)")
    worker.add_argument("--burst", action="store_true", help="exit once nothing is queued or running")
    worker.set_defaults(func=cmd_worker)

    db = commands.add_parser("db", help="database migrations and maintenance (Alembic)")
    db_commands = db.add_subparsers(dest="db_command", required=True, metavar="DB_COMMAND")
    db_option = argparse.ArgumentParser(add_help=False)
    db_option.add_argument("--db", type=Path, default=None, help="SQLite database (default: database.path)")
    upgrade = db_commands.add_parser(
        "upgrade", parents=[db_option], help="upgrade to a revision (default head; never down); creates a new database"
    )
    upgrade.add_argument("revision", nargs="?", default="head")
    upgrade.set_defaults(func=cmd_db_upgrade)
    stamp = db_commands.add_parser(
        "stamp",
        parents=[db_option],
        help="record a revision without running migrations, after checking the tables match it "
        "(a v1 database: 0001 for v1 dde1bde's schema, 0002 for v1 3ad1557's, or --detect)",
    )
    stamped = stamp.add_mutually_exclusive_group(required=True)
    stamped.add_argument(
        "revision", nargs="?", default=None, help="the revision to record (a v1 database: 0001 or 0002)"
    )
    stamped.add_argument(
        "--detect",
        action="store_true",
        help="stamp the v1 revision whose schema the database has (0001 or 0002; v1 cb1159c's: refused while a v1 "
        "live session has not ended, else its live tables dropped and 0002 recorded)",
    )
    stamp.set_defaults(func=cmd_db_stamp)
    db_commands.add_parser("current", parents=[db_option], help="show the current revision").set_defaults(
        func=cmd_db_current
    )
    db_commands.add_parser(
        "check",
        parents=[db_option],
        help="exit 1 unless the database is fit to run: at head and matching the models, quick_check and "
        "foreign_key_check clean, timestamps UTC, its journal mode safe on its file system, no run stuck in creation",
    ).set_defaults(func=cmd_db_check)
    backup = db_commands.add_parser(
        "backup",
        parents=[db_option],
        help="copy the database consistently while the app runs (SQLite's backup API), then quick_check the copy",
    )
    backup.add_argument("dest", type=Path, help="the copy: a new file, never the live database or a protected original")
    backup.add_argument("--force", action="store_true", help="replace an existing file")
    backup.set_defaults(func=cmd_db_backup)

    openapi = commands.add_parser("openapi", help="print or write the OpenAPI document")
    openapi.add_argument("--out", type=Path, default=None, help="write to this file instead of stdout")
    openapi.set_defaults(func=cmd_openapi)

    seed = commands.add_parser("seed", help="create development databases")
    seed_commands = seed.add_subparsers(dest="dataset", required=True, metavar="DATASET")
    demo = seed_commands.add_parser(
        "demo",
        help="build the demo database (database.path) and its runs root from scratch; runs R1 and I1 on a worker",
    )
    demo.add_argument(
        "--force", action="store_true", help="build an existing database again (its runs root is emptied too)"
    )
    demo.add_argument("--db", type=Path, default=None, help="SQLite database (default: database.path)")
    demo.add_argument(
        "--crux-run",
        action="store_true",
        help="also add R4, R1's request recorded as running on CRUX with a launcher's recorded observation "
        "(no DM job behind it: refused while remote execution is enabled; never enable it on that database)",
    )
    demo.set_defaults(func=cmd_seed_demo)
    slow = seed_commands.add_parser(
        "slow-runs",
        help="queue long LaueGo indexing runs of the demo scan on the demo database (database.path) for the run "
        "monitor's live test; prints their job and indexing IDs as JSON",
    )
    slow.add_argument("--count", type=int, default=2, help="runs to queue, in order (default 2)")
    slow.add_argument(
        "--inputs", type=int, default=1000, help="frames each run indexes, about 0.25 s each (default 1000)"
    )
    slow.set_defaults(func=cmd_seed_slow_runs)

    results = commands.add_parser("results", help="indexing result files")
    results_commands = results.add_subparsers(dest="results_command", required=True, metavar="RESULTS_COMMAND")
    convert = results_commands.add_parser("convert", help="convert indexing XML results to HDF5")
    convert.add_argument("ids", nargs="*", help="indexing run ids: I12, 12, or a range 3-7")
    convert.add_argument("--all", action="store_true", help="every indexing run in the database")
    convert.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    convert.add_argument(
        "--destination-root", type=Path, default=None, help="write index_<id>/output.h5 under this directory"
    )
    convert.add_argument(
        "--geometry",
        type=Path,
        default=None,
        help="geometry XML recorded instead of the one named in the XML (that one is mapped and confined to the "
        "file roots; with --db there are none)",
    )
    convert.add_argument(
        "--replace-invalid", action="store_true", help="replace an existing destination that is not a results file"
    )
    convert.add_argument("--report", type=Path, default=None, help="write a machine-readable JSON report here")
    convert.add_argument("--db", type=Path, default=None, help="SQLite database (default: database.path)")
    convert.set_defaults(func=cmd_results_convert)

    jobs = commands.add_parser("jobs", help="local runs and the run queue")
    jobs_commands = jobs.add_subparsers(dest="jobs_command", required=True, metavar="JOBS_COMMAND")
    reconcile = jobs_commands.add_parser(
        "reconcile",
        help="reconcile unfinished local runs and conversions (dead job processes, runs stuck in creation; the "
        "worker does it every reconcile_seconds)",
    )
    reconcile.add_argument("--once", action="store_true", help="one cycle, then exit")
    reconcile.set_defaults(func=cmd_jobs_reconcile)

    live = commands.add_parser("live", help="live runs: their arrivals")
    live_commands = live.add_subparsers(dest="live_command", required=True, metavar="LIVE_COMMAND")
    notify = live_commands.add_parser(
        "notify", help="admit a completed file into a live run whose source is notify (an acquisition hook)"
    )
    notify.add_argument("job_id", type=int, help="the live run's job ID")
    notify.add_argument("path", help="the file, in the run's folder and matching its file name")
    notify.add_argument(
        "--complete", action="store_true", help="the file is complete: acquired and transferred (required)"
    )
    notify.set_defaults(func=cmd_live_notify)

    remote = commands.add_parser("remote", help="remote execution through DM")
    remote_commands = remote.add_subparsers(dest="remote_command", required=True, metavar="REMOTE_COMMAND")
    poll = remote_commands.add_parser(
        "poll", help="one pass of the remote poller (the worker's thread polls): reconcile remote runs with DM"
    )
    poll.add_argument(
        "--once", action="store_true", required=True, help="one reconciliation pass, then exit (required)"
    )
    poll.set_defaults(func=cmd_remote_poll)

    config = commands.add_parser("config", help="inspect the settings")
    config_commands = config.add_subparsers(dest="config_command", required=True, metavar="CONFIG_COMMAND")
    config_commands.add_parser("show", help="print the effective settings and where they came from").set_defaults(
        func=cmd_config_show
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from laueportal.files import probe

    probe.configure(bound=False)  # the CLI and the worker check files in their own thread (serve's app bounds them)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"laueportal: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
