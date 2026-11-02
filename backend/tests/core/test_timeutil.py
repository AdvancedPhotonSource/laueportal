from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import Column, Engine, Integer, MetaData, Table, insert, select
from sqlalchemy.exc import StatementError

from laueportal.core import db, timestamps
from laueportal.core.timeutil import UTCDateTime, UTCTimestamp, isoformat_z, to_utc, utcnow
from tests.settings_support import make_settings

CHICAGO_SUMMER = timezone(timedelta(hours=-5))


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5


def test_to_utc_and_isoformat_z() -> None:
    local = datetime(2026, 7, 1, 7, 30, tzinfo=CHICAGO_SUMMER)
    assert to_utc(local) == datetime(2026, 7, 1, 12, 30, tzinfo=UTC)
    assert isoformat_z(local) == "2026-07-01T12:30:00Z"
    assert isoformat_z(datetime(2026, 7, 1, 12, 30, 0, 250000, tzinfo=UTC)) == "2026-07-01T12:30:00.250000Z"
    with pytest.raises(ValueError, match="naive"):
        to_utc(datetime(2026, 7, 1, 12, 30))


def test_one_implementation_for_the_api_and_the_files() -> None:
    """``core.timestamps`` (standard library only) is what ``core.timeutil`` re-exports."""
    assert (utcnow, to_utc, isoformat_z) == (timestamps.utcnow, timestamps.to_utc, timestamps.isoformat_z)


def test_utc_timestamp_is_the_text_the_portal_writes_into_files() -> None:
    local = datetime(2026, 7, 1, 7, 30, 0, 123456, tzinfo=CHICAGO_SUMMER)
    assert timestamps.utc_timestamp(local) == "2026-07-01T12:30:00.123456Z"
    assert timestamps.utc_timestamp(datetime(2026, 7, 1, 12, 30, tzinfo=UTC)) == "2026-07-01T12:30:00Z"
    now = timestamps.utc_timestamp()
    assert now.endswith("Z") and abs((datetime.now(UTC) - datetime.fromisoformat(now)).total_seconds()) < 5
    with pytest.raises(ValueError, match="naive"):
        timestamps.utc_timestamp(datetime(2026, 7, 1, 12, 30))


def test_local_timestamp_is_a_local_runs_time_naive_local_to_the_second() -> None:
    """A local run's ``run.json`` times, as the local executor writes them."""

    saved = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        moment = datetime(2026, 7, 1, 17, 30, 5, 999999, tzinfo=UTC)
        assert timestamps.local_timestamp(moment) == "2026-07-01T12:30:05"  # CDT, truncated like isoformat
        assert timestamps.local_timestamp(datetime(2026, 1, 12, 15, 0, tzinfo=CHICAGO_SUMMER)) == "2026-01-12T14:00:00"
        with pytest.raises(ValueError, match="naive"):
            timestamps.local_timestamp(datetime(2026, 7, 1, 12, 30))
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


@pytest.fixture
def stamps(tmp_path: Path) -> Iterator[tuple[Engine, Table]]:
    engine = db.make_engine(make_settings(tmp_path))
    table = Table("stamp", MetaData(), Column("id", Integer, primary_key=True), Column("at", UTCDateTime))
    table.metadata.create_all(engine)
    yield engine, table
    engine.dispose()


def test_column_stores_iso_utc_text_and_reads_aware_utc(stamps: tuple[Engine, Table]) -> None:
    engine, table = stamps
    local = datetime(2026, 1, 12, 9, 15, 30, 123456, tzinfo=timezone(timedelta(hours=-6)))
    with engine.begin() as connection:
        connection.execute(insert(table).values(id=1, at=local))
        raw = connection.exec_driver_sql("SELECT at FROM stamp").scalar_one()
        assert raw == "2026-01-12T15:15:30.123456Z"
        value = connection.execute(select(table.c.at)).scalar_one()
    assert value == local and value.tzinfo is UTC


def test_column_text_sorts_chronologically(stamps: tuple[Engine, Table]) -> None:
    engine, table = stamps
    times = [datetime(2026, 1, 1, 9, 0, 0, 5, tzinfo=UTC), datetime(2026, 1, 1, 10, 0, tzinfo=UTC)]
    with engine.begin() as connection:
        connection.execute(insert(table), [{"id": 1, "at": times[1]}, {"id": 2, "at": times[0]}])
        ordered = connection.exec_driver_sql("SELECT id FROM stamp ORDER BY at").scalars().all()
    assert ordered == [2, 1]


def test_column_rejects_naive_datetimes(stamps: tuple[Engine, Table]) -> None:
    engine, table = stamps
    with pytest.raises(StatementError, match="naive"), engine.begin() as connection:
        connection.execute(insert(table).values(id=1, at=datetime(2026, 1, 1, 12, 0)))


def test_column_rejects_stored_values_without_the_utc_marker(stamps: tuple[Engine, Table]) -> None:
    engine, table = stamps
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO stamp VALUES (1, '2026-01-01 12:00:00.000000')")  # naive: no UTC marker
        with pytest.raises(ValueError):
            connection.execute(select(table.c.at)).scalar_one()


class Event(BaseModel):
    at: UTCTimestamp


def test_api_timestamps_are_utc_with_z() -> None:
    event = Event(at=datetime(2026, 7, 1, 7, 30, tzinfo=CHICAGO_SUMMER))
    assert event.at.tzinfo is UTC
    assert event.model_dump_json() == '{"at":"2026-07-01T12:30:00Z"}'
    assert Event.model_validate_json('{"at":"2026-07-01T12:30:00+02:00"}').model_dump(mode="json") == {
        "at": "2026-07-01T10:30:00Z"
    }
    for mode in ("validation", "serialization"):
        schema = Event.model_json_schema(mode=mode)["properties"]["at"]
        assert (schema["type"], schema["format"]) == ("string", "date-time")
    with pytest.raises(ValidationError):
        Event.model_validate({"at": "2026-07-01T12:30:00"})


@pytest.mark.parametrize("text", ["0001-01-01T00:30:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_an_api_timestamp_outside_the_years_1_to_9999_in_utc_is_invalid_not_an_overflow(text: str) -> None:
    """The offset moves the time out of what a datetime holds in UTC: a validation error (a 422 for a request
    value), never the OverflowError that pydantic would let through (a 500)."""
    with pytest.raises(ValidationError) as caught:
        Event.model_validate({"at": text})
    [error] = caught.value.errors()
    assert error["type"] == "value_error"
    assert "out of range: in UTC it falls outside the years 1 to 9999" in error["msg"]
    assert Event.model_validate({"at": "0001-01-01T00:30:00-01:00"}).at == datetime(1, 1, 1, 1, 30, tzinfo=UTC)
