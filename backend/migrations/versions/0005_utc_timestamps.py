"""UTC timestamps: convert v1's naive local times to UTC, stored as ISO 8601 with a ``Z``.

v1 wrote ``datetime.now()`` (the server's local time, no zone) into every DATETIME column; the scan-log
``metadata.time`` is the beamline's local time too (it matches the log's EPICS epoch in that zone). The
zone is ``settings.database.legacy_timezone`` (``America/Chicago``), handed to this revision by
``laueportal db`` in the Alembic config.

Converted (every DATETIME column at 0004; ``COLUMNS``): ``job.submit_time``, ``start_time``,
``finish_time``, ``updated_at``, ``heartbeat_at``, ``cancel_requested_at``; ``indexing_run.created_at``;
``reconstruction_run.created_at``; ``metadata.time``. Left alone: text columns that hold times as text
(``metadata.scan_end_time``, as written in the scan log) and the integer epochs (``time_epoch``,
``scan_end_time_epoch``). The column types (DDL) do not change.

Stored form: ``2026-01-23T17:32:08.491324Z`` (strftime ``%Y-%m-%dT%H:%M:%S.%fZ``), the storage format of
``laueportal.core.timeutil.UTCDateTime``: fixed width, so text order is time order.

Daylight-saving rule (deterministic; PEP 495 ``fold=0``):
- an **ambiguous** local time (the repeated hour when clocks fall back, e.g. 2025-11-02 01:30) is the
  **first** occurrence, daylight time (01:30 CDT = 06:30Z);
- a **nonexistent** local time (the skipped hour when clocks spring forward, e.g. 2026-03-08 02:30) is
  read with the offset in force **before** the gap, standard time (02:30 CST = 08:30Z, which is 03:30 CDT).
A value that already carries an offset (``Z``, ``+00:00``) is converted from that offset, not the zone.
Anything else that is not an ISO 8601 date-time stops the migration with the offending rows, and
nothing is changed.

The downgrade writes local naive times in SQLAlchemy's SQLite format (``2026-01-23 11:32:08.491324``),
which restores v1's values exactly, except a time from a skipped hour (it comes back an hour later) and
the second occurrence of a repeated hour recorded after the upgrade (it comes back as the first).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import context, op
from alembic.util import CommandError

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

COLUMNS: dict[str, tuple[str, tuple[str, ...]]] = {
    # table: (primary key, its timestamp columns)
    "job": (
        "job_id",
        ("submit_time", "start_time", "finish_time", "updated_at", "heartbeat_at", "cancel_requested_at"),
    ),
    "indexing_run": ("id", ("created_at",)),
    "reconstruction_run": ("id", ("created_at",)),
    "metadata": ("scan_number", ("time",)),
}

UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
LEGACY_FORMAT = "%Y-%m-%d %H:%M:%S.%f"  # SQLAlchemy's DateTime on SQLite (v1)


def legacy_zone() -> tzinfo:
    name = context.config.attributes.get("legacy_timezone")
    if not name:
        raise CommandError("revision 0005 needs database.legacy_timezone: run it through `laueportal db upgrade`")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CommandError(f"revision 0005: unknown time zone {name!r} (database.legacy_timezone)") from None


def local_to_utc(value: object, zone: tzinfo) -> str:
    """A stored v1 value (naive local time, or ISO 8601 with an offset) as UTC text with ``Z``."""
    if not isinstance(value, str):
        raise ValueError(f"not a date-time text: {value!r}")
    moment = datetime.fromisoformat(value.strip())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone, fold=0)  # the DST rule above
    return moment.astimezone(UTC).strftime(UTC_FORMAT)


def utc_to_local(value: object, zone: tzinfo) -> str:
    """A stored UTC value as v1's naive local text (the downgrade)."""
    if not isinstance(value, str):
        raise ValueError(f"not a date-time text: {value!r}")
    moment = datetime.fromisoformat(value.strip())
    if moment.tzinfo is None:
        raise ValueError(f"no UTC marker: {value!r}")
    return moment.astimezone(zone).replace(tzinfo=None).strftime(LEGACY_FORMAT)


def _convert_all(convert: Callable[[object, tzinfo], str]) -> None:
    zone = legacy_zone()
    bind = op.get_bind()
    errors: list[str] = []
    for table, (key, columns) in COLUMNS.items():
        for column in columns:
            rows = bind.execute(
                sa.text(f'SELECT "{key}", "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL ORDER BY "{key}"')
            ).fetchall()
            updates = []
            for row_key, value in rows:
                try:
                    updates.append({"key": row_key, "value": convert(value, zone)})
                except ValueError as error:
                    errors.append(f"{table}.{column} where {key} = {row_key}: {error}")
            if updates and not errors:
                bind.execute(sa.text(f'UPDATE "{table}" SET "{column}" = :value WHERE "{key}" = :key'), updates)
    if errors:
        raise CommandError(
            f"revision 0005 cannot convert {len(errors)} timestamps; nothing was changed:\n  "
            + "\n  ".join(errors[:20])
        )


def upgrade() -> None:
    _convert_all(local_to_utc)


def downgrade() -> None:
    _convert_all(utc_to_local)
