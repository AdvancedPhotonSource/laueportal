"""Per-test settings: every test builds its own ``Settings`` (docs/contributing/backend.md)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

import laueportal
from laueportal.core.settings import AppSettings, Settings

SOURCE_DIR = Path(laueportal.__file__).resolve().parent.parent
"""``backend/src`` of this checkout."""
TEST_HOST = "testserver"
"""The ``Host`` Starlette's ``TestClient`` sends: an allowed host of ``make_settings``' settings."""


def make_settings(tmp_path: Path, **sections: dict[str, Any]) -> Settings:
    """Settings rooted in ``tmp_path``: database ``tmp_path/laueportal.db``, runs ``tmp_path/runs``, and the host
    names of the default settings plus the ``TestClient``'s (``testserver``). ``sections`` replace keys per
    section."""
    values: dict[str, dict[str, Any]] = {
        "app": {"allowed_hosts": [*AppSettings().allowed_hosts, TEST_HOST]},
        "database": {"path": tmp_path / "laueportal.db"},
        "files": {"runs_root": tmp_path / "runs"},
    }
    for section, overrides in sections.items():
        values[section] = {**values.get(section, {}), **overrides}
    return Settings(**values)


def create_database(settings: Settings) -> Path:
    """An existing (empty-schema) SQLite file at ``settings.database.path``."""
    path = settings.database.path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY)")
    connection.close()
    return path


def make_database(tmp_path: Path, **sections: dict[str, Any]) -> tuple[Settings, Engine]:
    """``make_settings(tmp_path, ...)`` plus an engine on a fresh SQLite file holding every table.

    The tables come from ``Base.metadata.create_all`` (the models of ``laueportal.models``); dispose the
    engine when done.
    """
    import laueportal.models  # noqa: F401  (registers every table on Base.metadata)
    from laueportal.core.db import Base, make_engine

    settings = make_settings(tmp_path, **sections)
    settings.database.path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return settings, engine


def fresh_interpreter_env(config: Path | None = None) -> dict[str, str]:
    """The environment of a fresh ``python`` subprocess, as the worker, its job processes and the CLI run.

    It finds ``laueportal`` in this checkout's ``src/`` and nothing from the tests (no conftest, so no
    ``laueportal.models`` either: run it with a ``cwd`` outside ``backend/``). No LAUEPORTAL_* variable
    is set except ``LAUEPORTAL_CONFIG=config`` when given.
    """
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("LAUEPORTAL_")}
    env["PYTHONPATH"] = str(SOURCE_DIR)
    if config is not None:
        env["LAUEPORTAL_CONFIG"] = str(config)
    return env


def mount_table(tmp_path: Path, *mounts: str) -> str:
    """A mount table (``/proc/self/mountinfo``'s format): the root file system on ext4, then ``mounts``, each
    ``"<mount point> <type> <root>"`` (``root``: the folder of the source mounted there, other than ``/`` for a bind
    mount)."""
    table = tmp_path / "mountinfo"
    lines = ["22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw"]
    for number, mount in enumerate(mounts, start=40):
        point, fstype, root = mount.split()
        lines.append(f"{number} 22 0:{number} {root} {point} rw,relatime - {fstype} server:/export rw")
    table.write_text("\n".join(lines) + "\n")
    return str(table)
