"""Timestamps as the portal writes them: aware UTC, ISO 8601 with ``Z``, and the run files' own times.

Standard library only, so any module may use it without the database layer. ``core.timeutil`` re-exports
``utcnow``, ``to_utc`` and ``isoformat_z`` for the database columns and the API schemas. Run-directory files
keep their own formats: the remote runners write ``jobs.runner.timestamp`` (``+00:00`` to the second) and the local
executor ``local_timestamp``.

- ``utc_timestamp(value=None)``: the text the portal writes for its own times outside the run files' formats (the seed
  and conversion reports), ``2026-10-15T12:34:56.123456Z`` (``value``, or now).
- ``isoformat_z(value)``: that text for an aware datetime (``isoformat`` in UTC with ``Z``; the fraction
  has six digits and is left out when it is zero).
- ``local_timestamp(value)``: a local run's ``run.json`` ``started_at``/``finished_at``,
  naive local time to the second (the form of ``datetime.now().isoformat(timespec="seconds")``).
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """``value`` converted to UTC. Naive datetimes are ambiguous, so they are an error."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"naive datetime {value.isoformat()} (use laueportal.core.timestamps.utcnow())")
    return value.astimezone(UTC)


def isoformat_z(value: datetime) -> str:
    """ISO 8601 in UTC with a ``Z`` suffix: ``2026-10-15T12:34:56Z`` (microseconds only when non-zero)."""
    return to_utc(value).isoformat().replace("+00:00", "Z")


def utc_timestamp(value: datetime | None = None) -> str:
    """``value`` (an aware datetime; by default now) as the portal writes a time into a file: ``isoformat_z``."""
    return isoformat_z(utcnow() if value is None else value)


def local_timestamp(value: datetime) -> str:
    """``value`` (an aware datetime) as the local executor writes a time into ``run.json``.

    The host's local time, naive, truncated to the second: ``2026-10-15T07:34:56``. The database keeps the
    aware UTC time; only this file text is local (the form of ``datetime.now().isoformat(timespec="seconds")``).
    """
    return to_utc(value).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
