"""Alembic environment of the 3DMN Portal database.

Migrations run only through ``laueportal db ...`` (``laueportal.core.migrations``), which passes in a
connection made by ``core.db``'s engine (the same pragmas as the app) inside one transaction with
foreign-key enforcement off, so a table rebuild never cascades and a failed upgrade leaves the database
untouched. There is no ``alembic.ini`` and no offline (SQL script) mode.
"""

from __future__ import annotations

from alembic import context

import laueportal.models  # noqa: F401  (registers every model on Base.metadata)
from laueportal.core.db import Base

config = context.config
connection = config.attributes.get("connection")
if context.is_offline_mode() or connection is None:
    raise RuntimeError(
        "run migrations through `laueportal db upgrade|stamp|current|check` (laueportal.core.migrations)"
    )

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,  # SQLite cannot ALTER most things: autogenerate writes batch (copy-and-move) operations
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
