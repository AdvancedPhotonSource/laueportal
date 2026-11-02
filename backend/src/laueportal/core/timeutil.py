"""UTC everywhere: aware datetimes in code, ISO 8601 UTC in the DB, ``Z`` in JSON.

- ``utcnow()``: the current time, aware, in UTC. Never use naive ``datetime.now()``.
- ``to_utc(value)``, ``isoformat_z(value)``: conversion and the ``Z`` text. These and ``utcnow`` live in
  ``core.timestamps`` (standard library only, with ``utc_timestamp`` for the portal's own times written into files
  and ``local_timestamp`` for a local run's ``run.json``) and are re-exported here.
- ``local_to_utc(value, zone)``: a naive local wall-clock time (a scan log's ``time``) in UTC, with the
  daylight-saving rule that revision 0005 applied to v1's naive timestamps.
- ``UTCDateTime``: SQLAlchemy column type. On SQLite it stores fixed-width ISO 8601 text with an
  explicit marker, ``2026-10-15T12:34:56.000000Z`` (strftime ``%Y-%m-%dT%H:%M:%S.%fZ``), so text
  order is time order and SQLite's date functions understand it. Naive datetimes are rejected on write; values
  without the marker are rejected on read.
- ``UTCTimestamp``: pydantic type for API schemas. Requires an aware datetime, converts it to UTC and
  serializes it as ``2026-10-15T12:34:56Z`` (``isoformat_z``). A time whose offset moves it out of the years
  1 to 9999 in UTC is a validation error (a 422 as a request value), never an ``OverflowError``.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Annotated, Any

from pydantic import AfterValidator, PlainSerializer, WithJsonSchema
from sqlalchemy import DateTime, Dialect
from sqlalchemy.dialects import sqlite
from sqlalchemy.types import TypeDecorator, TypeEngine

from laueportal.core.timestamps import isoformat_z as isoformat_z
from laueportal.core.timestamps import to_utc as to_utc
from laueportal.core.timestamps import utcnow as utcnow

_SQLITE_STORAGE_FORMAT = "%(year)04d-%(month)02d-%(day)02dT%(hour)02d:%(minute)02d:%(second)02d.%(microsecond)06dZ"
_SQLITE_REGEXP = r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z"


def local_to_utc(value: datetime, zone: tzinfo) -> datetime:
    """A naive local time in ``zone`` as an aware UTC datetime (an aware ``value`` is only converted).

    Daylight saving (PEP 495 ``fold=0``, as in migration 0005): an ambiguous time, in the hour repeated when
    clocks fall back, is its first occurrence (daylight time); a time in the hour skipped when clocks spring
    forward is read with the offset in force before the gap (standard time).
    """
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=zone, fold=0).astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp column that holds aware UTC datetimes (ISO 8601 text with ``Z`` on SQLite)."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(
                sqlite.DATETIME(storage_format=_SQLITE_STORAGE_FORMAT, regexp=_SQLITE_REGEXP)
            )
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime needs a datetime, not {type(value).__name__}")
        value = to_utc(value)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validated_utc(value: datetime) -> datetime:
    """``to_utc`` for validation. pydantic reports only a ``ValueError`` as invalid input: the ``OverflowError`` of
    a time whose offset moves it out of the years 1 to 9999 in UTC (``0001-01-01T00:30:00+01:00``) would
    escape it (as a 500 for a request value), so it becomes one."""
    try:
        return to_utc(value)
    except OverflowError:
        raise ValueError(f"{value.isoformat()} is out of range: in UTC it falls outside the years 1 to 9999") from None


UTCTimestamp = Annotated[
    datetime,
    AfterValidator(_validated_utc),
    PlainSerializer(isoformat_z, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
"""An aware datetime in API schemas, always serialized as UTC with ``Z``."""
