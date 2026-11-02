"""The grid query protocol's SQL translator (``laueportal.core.paging``).

Every filter operator runs from the shared table in ``tests.grid_cases``, on a real SQLite file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Column, Engine, Float, Integer, MetaData, String, Table, case, event, insert, or_, select, update
from sqlalchemy.orm import Session

from laueportal.core.db import make_engine
from laueportal.core.paging import (
    CHANGED_SINCE_OVERLAP,
    EARLIEST_CHANGED_SINCE,
    MAX_LIMIT,
    MAX_SORT_LENGTH,
    OPERATORS,
    GridField,
    GridParams,
    GridSpec,
    LiveGridParams,
    fetch_page,
)
from laueportal.core.problems import ProblemError
from laueportal.core.timeutil import UTCDateTime, utcnow
from tests.grid_cases import ALL_IDS, FILTER_CASES, REFUSED_TEXT_CASES, ROWS, utc
from tests.settings_support import make_settings

TABLE_METADATA = MetaData()
ITEMS = Table(
    "grid_item",
    TABLE_METADATA,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=True),
    Column("count", Float, nullable=True),
    Column("at", UTCDateTime, nullable=True),
    Column("status", Integer, nullable=False),
    Column("updated_at", UTCDateTime, nullable=True),
    Column("note", String, nullable=True),
)

SPEC = GridSpec(
    fields={
        "id": GridField("number"),
        "name": GridField("text"),
        "count": GridField("number"),
        "at": GridField("date"),
        "status": GridField("number"),
        "note": GridField("text", sortable=False, filterable=False),
    },
    key="id",
    default_sort="-id",
    changed_since=lambda since: or_(ITEMS.c.status.in_((0, 1)), ITEMS.c.updated_at >= since),
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(make_settings(tmp_path))
    TABLE_METADATA.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(ITEMS), [{"updated_at": None, "note": None, **row} for row in ROWS])
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def page(session: Session, spec: GridSpec = SPEC, params: GridParams | None = None, **values: Any):
    if "filter" in values and not isinstance(values["filter"], str):
        values["filter"] = json.dumps(values["filter"])
    return fetch_page(session, select(ITEMS), spec, params or GridParams(**values))


def ids(session: Session, **values: Any) -> list[int]:
    return [row["id"] for row in page(session, **values).rows]


def problems(session: Session, **values: Any) -> list[tuple[list[str | int], str]]:
    with pytest.raises(ProblemError) as caught:
        page(session, **values)
    problem = caught.value.problem
    assert (problem.status, problem.title) == (422, "Validation failed")
    assert problem.errors
    assert problem.detail == f"{len(problem.errors)} problem{'' if len(problem.errors) == 1 else 's'}"
    return [(item.loc, item.code) for item in problem.errors]


# ---------------------------------------------------------------------------
# Filters: every operator of every type (the shared table)


@pytest.mark.parametrize(("model", "expected"), [case[1:] for case in FILTER_CASES], ids=[c[0] for c in FILTER_CASES])
def test_filter_operators(session: Session, model: dict[str, Any], expected: frozenset[int]) -> None:
    result = page(session, filter=model, sort="id", limit=MAX_LIMIT)
    assert {row["id"] for row in result.rows} == expected
    assert result.total == len(expected)


def test_the_shared_table_covers_every_operator_and_both_joins() -> None:
    covered: dict[str, set[str]] = {kind: set() for kind in OPERATORS}
    joins: set[str] = set()
    kinds = {"name": "text", "count": "number", "id": "number", "at": "date"}

    def visit(field: str, model: dict[str, Any]) -> None:
        if "conditions" in model:
            joins.add(model["operator"])
            for condition in model["conditions"]:
                visit(field, condition)
        else:
            covered[kinds[field]].add(model["type"])

    for _, filter_model, _ in FILTER_CASES:
        for field, model in filter_model.items():
            visit(field, model)
    assert covered == {kind: set(operators) for kind, operators in OPERATORS.items()}
    assert joins == {"AND", "OR"}


# ---------------------------------------------------------------------------
# Sort and paging


def test_default_sort_is_the_specs(session: Session) -> None:
    assert ids(session) == [8, 7, 6, 5, 4, 3, 2, 1]
    assert ids(session, sort="  ") == [8, 7, 6, 5, 4, 3, 2, 1]  # a blank sort is no sort


def test_text_sorts_ignore_case_and_put_blanks_first(session: Session) -> None:
    assert ids(session, sort="name") == [6, 5, 7, 1, 2, 3, 8, 4]
    assert ids(session, sort="-name") == [4, 8, 3, 2, 1, 7, 5, 6]


def test_number_sort_descending_puts_blanks_last(session: Session) -> None:
    assert ids(session, sort="-count") == [8, 3, 2, 1, 7, 5, 4, 6]


def test_the_key_breaks_ties_and_later_keys_apply_in_order(session: Session) -> None:
    # status: 2 for 1, 2, 7, 8; 1 for 3; 0 for 4; 3 for 5; 4 for 6
    assert ids(session, sort="status") == [4, 3, 1, 2, 7, 8, 5, 6]
    assert ids(session, sort="-status") == [6, 5, 1, 2, 7, 8, 3, 4]
    assert ids(session, sort="status,-id") == [4, 3, 8, 7, 2, 1, 5, 6]
    assert ids(session, sort=" -status , count ") == [6, 5, 7, 1, 2, 8, 3, 4]


def test_a_field_can_sort_by_another_column(session: Session) -> None:
    """``sort_by``: a status that filters by its name sorts by its code (the lifecycle order, not alphabetically);
    the sort column need not be a grid field, and it is sorted as it is (a text one keeps its case)."""
    state = case({0: "queued", 1: "running", 2: "finished", 3: "failed", 4: "cancelled"}, value=ITEMS.c.status)
    spec = GridSpec(
        fields={
            "id": GridField("number"),
            "state": GridField("text", sort_by="status"),
            "cased": GridField("text", sort_by="name"),
        },
        key="id",
        default_sort="id",
    )
    query = select(ITEMS, state.label("state"), ITEMS.c.name.label("cased"))

    def by(**values: Any) -> list[int]:
        if "filter" in values:
            values["filter"] = json.dumps(values["filter"])
        return [row["id"] for row in fetch_page(session, query, spec, GridParams(**values)).rows]

    # status: 0 for 4, 1 for 3, 2 for 1, 2, 7, 8, 3 for 5, 4 for 6 (by name: cancelled, failed, finished, ...)
    assert by(sort="state") == [4, 3, 1, 2, 7, 8, 5, 6]
    assert by(sort="-state") == [6, 5, 1, 2, 7, 8, 3, 4]
    assert by(sort="-state,-id") == [6, 5, 8, 7, 2, 1, 3, 4]
    assert by(filter={"state": {"type": "equals", "filter": "Finished"}}) == [1, 2, 7, 8]  # its own text
    assert by(filter={"state": {"type": "startsWith", "filter": "f"}}, sort="-state") == [5, 1, 2, 7, 8]
    assert by(sort="cased") == [6, 5, 7, 1, 3, 2, 8, 4]  # "Beta 50%" before "alphabet": no case folding
    with pytest.raises(ProblemError) as caught:
        fetch_page(session, query, spec, GridParams(sort="status"))  # the sort column is no grid field
    assert [(item.loc, item.code) for item in caught.value.problem.errors or []] == [
        (["query", "sort"], "unknown_field")
    ]


def test_paging_returns_the_total_with_each_page(session: Session) -> None:
    first = page(session, sort="status", limit=3)
    assert ([row["id"] for row in first.rows], first.total) == ([4, 3, 1], 8)
    last = page(session, sort="status", offset=6, limit=3)
    assert ([row["id"] for row in last.rows], last.total) == ([5, 6], 8)
    beyond = page(session, sort="status", offset=100, limit=3)
    assert (list(beyond.rows), beyond.total) == ([], 8)


def test_pages_neither_overlap_nor_skip_rows_with_ties(session: Session) -> None:
    pages = [ids(session, sort="status", offset=offset, limit=3) for offset in (0, 3, 6)]
    assert [row for chunk in pages for row in chunk] == ids(session, sort="status", limit=MAX_LIMIT)
    assert sorted(row for chunk in pages for row in chunk) == sorted(ALL_IDS)


def test_the_total_counts_the_filtered_rows(session: Session) -> None:
    result = page(session, filter={"count": {"filterType": "number", "type": "greaterThan", "filter": 0}}, limit=2)
    assert ([row["id"] for row in result.rows], result.total) == ([8, 7], 5)


def test_rows_carry_every_column_by_label(session: Session) -> None:
    row = page(session, sort="id", limit=1).rows[0]
    assert dict(row) == {**ROWS[0], "updated_at": None, "note": None}
    assert row["at"] == utc("2026-01-01T00:00:00")  # UTCDateTime: aware UTC


def test_a_page_takes_two_queries(engine: Engine, session: Session) -> None:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        page(session, sort="name", filter={"count": {"filterType": "number", "type": "notBlank"}}, limit=2)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 2


def test_a_rejected_request_never_queries(engine: Engine, session: Session) -> None:
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    problems(session, sort="bogus")
    assert statements == []


# ---------------------------------------------------------------------------
# changed_since


def test_changed_since_returns_active_rows_and_rows_changed_within_the_overlap(
    engine: Engine, session: Session
) -> None:
    since = utc("2026-10-18T12:00:00")
    with engine.begin() as connection:
        connection.execute(update(ITEMS).where(ITEMS.c.id == 1).values(updated_at=since + timedelta(seconds=5)))
        connection.execute(update(ITEMS).where(ITEMS.c.id == 2).values(updated_at=since - timedelta(seconds=1)))
        connection.execute(update(ITEMS).where(ITEMS.c.id == 5).values(updated_at=since - CHANGED_SINCE_OVERLAP))
        connection.execute(update(ITEMS).where(ITEMS.c.id == 6).values(updated_at=since - timedelta(seconds=3)))
    result = page(session, params=LiveGridParams(changed_since=since, sort="id"))
    # 3 (running) and 4 (queued) are active; 1, 2 and 5 changed at or after since - 2 s; 6 changed before
    assert ([row["id"] for row in result.rows], result.total) == ([1, 2, 3, 4, 5], 5)


def test_changed_since_combines_with_the_filter(session: Session) -> None:
    params = LiveGridParams(
        changed_since=utc("2026-10-18T12:00:00"),
        filter=json.dumps({"name": {"filterType": "text", "type": "startsWith", "filter": "gamma"}}),
    )
    assert [row["id"] for row in page(session, params=params).rows] == [4]


def test_without_changed_since_a_live_list_returns_every_row(session: Session) -> None:
    assert page(session, params=LiveGridParams()).total == len(ROWS)


def test_changed_since_needs_an_aware_time() -> None:
    with pytest.raises(ValidationError):
        LiveGridParams.model_validate({"changed_since": "2026-10-18T12:00:00"})
    assert LiveGridParams.model_validate({"changed_since": "2026-10-18T07:00:00-05:00"}).changed_since == utc(
        "2026-10-18T12:00:00"
    )


def test_changed_since_must_leave_room_for_the_overlap(engine: Engine, session: Session) -> None:
    """The overlap before ``changed_since`` must still be a time: ``datetime.min`` overflowed into a 500."""
    for text in ("0001-01-01T00:00:00Z", "0001-01-01T00:00:01.999999Z", "0001-01-01T01:00:01+01:00"):
        with pytest.raises(ValidationError) as caught:
            LiveGridParams.model_validate({"changed_since": text})
        [error] = caught.value.errors()
        assert (error["loc"], error["type"]) == (("changed_since",), "value_error")
        assert "changed_since must be 0001-01-01T00:00:02Z or later" in error["msg"]
    with engine.begin() as connection:  # changed at the earliest time the overlap reaches back to
        connection.execute(update(ITEMS).where(ITEMS.c.id == 1).values(updated_at=datetime.min.replace(tzinfo=UTC)))
    earliest = LiveGridParams.model_validate({"changed_since": "0001-01-01T00:00:02Z", "sort": "id"})
    assert earliest.changed_since == EARLIEST_CHANGED_SINCE
    assert [row["id"] for row in page(session, params=earliest).rows] == [1, 3, 4]  # 3 and 4 are active
    latest = LiveGridParams.model_validate({"changed_since": "9999-12-31T23:59:59.999999Z", "sort": "id"})
    assert [row["id"] for row in page(session, params=latest).rows] == [3, 4]


def test_changed_since_outside_the_years_1_to_9999_in_utc_is_invalid() -> None:
    for text in ("0001-01-01T00:30:00+01:00", "9999-12-31T23:59:59-01:00"):
        with pytest.raises(ValidationError) as caught:
            LiveGridParams.model_validate({"changed_since": text})
        assert [error["type"] for error in caught.value.errors()] == ["value_error"]


def test_the_result_says_when_it_was_read(session: Session) -> None:
    before = utcnow()
    result = page(session)
    assert before <= result.as_of <= utcnow()
    assert result.as_of.tzinfo is UTC
    moment = utc("2026-10-18T12:00:00")
    assert fetch_page(session, select(ITEMS), SPEC, GridParams(), as_of=moment).as_of == moment


def test_a_grid_without_changed_since_refuses_it(session: Session) -> None:
    spec = GridSpec(fields=SPEC.fields, key="id", default_sort="id")
    with pytest.raises(ValueError, match="changed_since"):
        page(session, spec=spec, params=LiveGridParams(changed_since=utc("2026-10-18T12:00:00")))


# ---------------------------------------------------------------------------
# Errors: 422 problems located in the query


def test_unknown_unsortable_repeated_and_empty_sort_fields_are_problems(session: Session) -> None:
    sort = ["query", "sort"]
    assert problems(session, sort="bogus") == [(sort, "unknown_field")]
    assert problems(session, sort="note") == [(sort, "not_sortable")]
    assert problems(session, sort="name,-name") == [(sort, "duplicate_field")]
    assert problems(session, sort="name,,id") == [(sort, "invalid_sort")]
    assert problems(session, sort="-") == [(sort, "invalid_sort")]


def test_empty_sort_items_are_one_problem_that_does_not_repeat_the_sort(session: Session) -> None:
    """Each empty item repeated the whole sort, so 8 KB of commas made a 65 MB answer."""
    for sort in (",", "name,,id", ",,,,", " , -,"):
        with pytest.raises(ProblemError) as caught:
            page(session, sort=sort)
        assert [(item.loc, item.code, item.message) for item in caught.value.problem.errors or []] == [
            (["query", "sort"], "invalid_sort", "Empty sort field; use a comma list such as -a,b")
        ]


def test_a_sort_naming_more_fields_than_the_grid_sorts_is_one_problem(session: Session) -> None:
    assert ids(session, sort="-status,name,count,at,id") == [6, 5, 7, 1, 2, 8, 3, 4]  # all five sortable fields
    for sort in ("id,id,id,id,id,id", "a,b,c,d,e,f", "," * (MAX_SORT_LENGTH - 1)):
        with pytest.raises(ProblemError) as caught:
            page(session, sort=sort)
        [item] = caught.value.problem.errors or []
        assert (item.loc, item.code) == (["query", "sort"], "too_many_fields")
        assert item.message == (
            f"The sort names {sort.count(',') + 1} fields; it can name each of the 5 sortable fields once: at, count, "
            "id, name, status"
        )


def test_a_sort_is_at_most_max_sort_length_characters() -> None:
    assert GridParams(sort="x" * MAX_SORT_LENGTH).sort
    with pytest.raises(ValidationError, match="at most 1000 characters"):
        GridParams(sort="," * (MAX_SORT_LENGTH + 1))


def test_problems_show_values_from_the_request_shortened(session: Session) -> None:
    """A message never repeats an unbounded part of the request."""
    huge = "x" * 100_000
    for values in (
        {"sort": "y" * (MAX_SORT_LENGTH - 1)},
        {"filter": {huge: {"type": "blank"}}},
        {"filter": {"name": {"type": huge}}},
        {"filter": {"name": {"type": "contains", "filter": [huge] * 1000}}},
        {"filter": {"name": {"filterType": huge, "type": "blank"}}},
        {"filter": {"count": {"type": "equals", "filter": huge}}},
        {"filter": {"count": {"operator": huge, "conditions": [{"type": "blank"}, {"type": "blank"}]}}},
        {"filter": {"at": {"type": "equals", "dateFrom": huge}}},
        {"filter": {"at": {"type": "equals", "dateFrom": [huge]}}},
    ):
        with pytest.raises(ProblemError) as caught:
            page(session, **values)
        [item] = caught.value.problem.errors or []
        assert len(item.message) < 200, (values.keys(), item.message[:300])


def test_the_problem_names_the_sortable_fields(session: Session) -> None:
    with pytest.raises(ProblemError) as caught:
        page(session, sort="bogus")
    assert caught.value.problem.errors is not None
    assert caught.value.problem.errors[0].message == "Unknown sort field 'bogus'; sortable: at, count, id, name, status"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("{not json", "json_invalid"),
        ('{"count": {"type": "equals", "filter": NaN}}', "json_invalid"),
        ("[1, 2]", "invalid_filter"),
        ('"text"', "invalid_filter"),
    ],
)
def test_a_filter_that_is_not_a_json_object_is_a_problem(session: Session, raw: str, code: str) -> None:
    assert problems(session, filter=raw) == [(["query", "filter"], code)]


def test_the_largest_64_bit_offset_still_queries(session: Session) -> None:
    """The extreme 64-bit integers and valid dates in a filter are shared cases (``tests.grid_cases``)."""
    assert ids(session, offset=2**63 - 1) == []


def test_a_filter_nested_too_deeply_to_decode_is_a_problem(session: Session) -> None:
    deep = "[" * 20_000 + "]" * 20_000  # deeper than json.loads goes: a RecursionError, which was a 500
    assert problems(session, filter=deep) == [(["query", "filter"], "json_invalid")]
    assert problems(session, filter='{"name": ' + "[" * 5000 + "]" * 5000 + "}") == [
        (["query", "filter", "name"], "invalid_filter")
    ]


@pytest.mark.parametrize(
    ("model", "loc"), [case[1:] for case in REFUSED_TEXT_CASES], ids=[c[0] for c in REFUSED_TEXT_CASES]
)
def test_a_nul_or_lone_surrogate_anywhere_in_the_filter_is_a_problem_located_there(
    engine: Engine, session: Session, model: dict[str, Any], loc: list[str | int]
) -> None:
    """A lone surrogate was a 500 (SQLite cannot encode it), a NUL gave wrong rows (LIKE stops at it)."""
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    assert problems(session, filter=model) == [(["query", "filter", *loc], "invalid_text")]
    assert statements == []  # refused before any query


def test_only_the_text_problems_are_reported_for_a_filter_holding_any(session: Session) -> None:
    """The rest of such a filter is not looked at: its field names may be the bad text."""
    model = {"bogus": {"type": "blank"}, "name": {"type": "contains", "filter": "\x00"}, "x\ud800": 1}
    assert problems(session, filter=model) == [
        (["query", "filter", "name", "filter"], "invalid_text"),
        (["query", "filter", "x\\ud800"], "invalid_text"),
    ]


def test_a_blank_filter_is_no_filter(session: Session) -> None:
    assert page(session, filter=" ").total == len(ROWS)


def test_unknown_and_unfilterable_fields_are_problems(session: Session) -> None:
    assert problems(session, filter={"bogus": {"type": "blank"}}) == [(["query", "filter", "bogus"], "unknown_field")]
    assert problems(session, filter={"note": {"type": "blank"}}) == [(["query", "filter", "note"], "not_filterable")]


@pytest.mark.parametrize(
    ("model", "loc", "code"),
    [
        ("contains", [], "invalid_filter"),
        ({"filterType": "number", "type": "equals", "filter": 1}, ["filterType"], "filter_type"),
        ({"filterType": "text", "type": "lessThan", "filter": "a"}, ["type"], "unsupported_operator"),
        ({"filterType": "text"}, ["type"], "unsupported_operator"),
        ({"filterType": "text", "type": "contains"}, ["filter"], "missing"),
        ({"filterType": "text", "type": "contains", "filter": 5}, ["filter"], "invalid_value"),
    ],
)
def test_bad_text_filter_models(session: Session, model: Any, loc: list[str], code: str) -> None:
    assert problems(session, filter={"name": model}) == [(["query", "filter", "name", *loc], code)]


@pytest.mark.parametrize(
    ("model", "loc", "code"),
    [
        ({"filterType": "number", "type": "contains", "filter": 1}, ["type"], "unsupported_operator"),
        ({"filterType": "number", "type": "equals"}, ["filter"], "missing"),
        ({"filterType": "number", "type": "equals", "filter": "12"}, ["filter"], "invalid_value"),
        ({"filterType": "number", "type": "equals", "filter": True}, ["filter"], "invalid_value"),
        ({"filterType": "number", "type": "equals", "filter": 2**63}, ["filter"], "invalid_value"),
        ({"filterType": "number", "type": "lessThan", "filter": -(2**63) - 1}, ["filter"], "invalid_value"),
        ({"filterType": "number", "type": "inRange", "filter": 1}, ["filterTo"], "missing"),
    ],
)
def test_bad_number_filter_models(session: Session, model: Any, loc: list[str], code: str) -> None:
    assert problems(session, filter={"count": model}) == [(["query", "filter", "count", *loc], code)]


@pytest.mark.parametrize(
    ("model", "loc", "code"),
    [
        ({"filterType": "date", "type": "notEqual", "dateFrom": "2026-01-01"}, ["type"], "unsupported_operator"),
        ({"filterType": "date", "type": "equals"}, ["dateFrom"], "missing"),
        ({"filterType": "date", "type": "equals", "dateFrom": "yesterday"}, ["dateFrom"], "invalid_value"),
        ({"filterType": "date", "type": "equals", "dateFrom": 20260101}, ["dateFrom"], "invalid_value"),
        ({"filterType": "date", "type": "inRange", "dateFrom": "2026-01-01"}, ["dateTo"], "missing"),
        ({"filterType": "date", "type": "equals", "dateFrom": "9999-12-31"}, ["dateFrom"], "invalid_value"),
        (
            {"filterType": "date", "type": "lessThan", "dateFrom": "0001-01-01T00:00:00+01:00"},
            ["dateFrom"],
            "invalid_value",
        ),
        (
            {"filterType": "date", "type": "inRange", "dateFrom": "2026-01-01", "dateTo": "9999-12-31"},
            ["dateTo"],
            "invalid_value",
        ),
    ],
)
def test_bad_date_filter_models(session: Session, model: Any, loc: list[str], code: str) -> None:
    assert problems(session, filter={"at": model}) == [(["query", "filter", "at", *loc], code)]


@pytest.mark.parametrize(
    ("model", "loc"),
    [
        ({"operator": "XOR", "conditions": [{"type": "blank"}, {"type": "notBlank"}]}, ["operator"]),
        ({"operator": "AND", "conditions": [{"type": "blank"}]}, ["conditions"]),
        ({"operator": "AND", "conditions": [{"type": "blank"}] * 3}, ["conditions"]),
        ({"operator": "AND"}, ["conditions"]),
        ({"operator": "AND", "conditions": [{"type": "blank"}, "blank"]}, ["conditions", 1]),
        (
            {"operator": "OR", "conditions": [{"type": "blank"}, {"operator": "AND", "conditions": []}]},
            ["conditions", 1],
        ),
    ],
)
def test_bad_combined_filter_models(session: Session, model: Any, loc: list[str | int]) -> None:
    assert problems(session, filter={"count": model}) == [(["query", "filter", "count", *loc], "invalid_filter")]


def test_a_problem_in_one_condition_is_located_there(session: Session) -> None:
    model = {"operator": "AND", "conditions": [{"type": "blank"}, {"filterType": "number", "type": "equals"}]}
    assert problems(session, filter={"count": model}) == [
        (["query", "filter", "count", "conditions", 1, "filter"], "missing")
    ]


def test_every_problem_is_reported_at_once(session: Session) -> None:
    filters = {"bogus": {"type": "blank"}, "count": {"type": "equals", "filter": "x"}, "at": {"type": "equals"}}
    assert problems(session, sort="-nope,name", filter=filters) == [
        (["query", "sort"], "unknown_field"),
        (["query", "filter", "bogus"], "unknown_field"),
        (["query", "filter", "count", "filter"], "invalid_value"),
        (["query", "filter", "at", "dateFrom"], "missing"),
    ]


def test_offset_limit_and_unknown_parameters_are_validated() -> None:
    assert GridParams().model_dump() == {"offset": 0, "limit": 100, "sort": None, "filter": None}
    for values in (
        {"offset": -1},
        {"offset": 2**63},
        {"limit": 0},
        {"limit": MAX_LIMIT + 1},
        {"changed_since": None},
        {"page": 2},
    ):
        with pytest.raises(ValidationError):
            GridParams.model_validate(values)
    assert GridParams(limit=MAX_LIMIT).limit == MAX_LIMIT


# ---------------------------------------------------------------------------
# Declaration errors (programming errors, found on first use)


def test_a_default_sort_must_be_valid() -> None:
    with pytest.raises(ValueError, match="default sort 'bogus'"):
        GridSpec(fields=SPEC.fields, key="id", default_sort="bogus")


def test_every_declared_field_must_be_a_column_of_the_query(session: Session) -> None:
    spec = GridSpec(fields={**SPEC.fields, "missing": GridField("text")}, key="id", default_sort="id")
    with pytest.raises(ValueError, match="'missing' is not a column"):
        page(session, spec=spec)


def test_a_sort_by_column_must_be_a_column_of_the_query(session: Session) -> None:
    spec = GridSpec(
        fields={"id": GridField("number"), "name": GridField("text", sort_by="missing")}, key="id", default_sort="id"
    )
    with pytest.raises(ValueError, match="'name' sorts by 'missing', which is not a column of the query"):
        page(session, spec=spec)


def test_a_date_field_must_be_a_utc_datetime_column(session: Session) -> None:
    spec = GridSpec(fields={**SPEC.fields, "name": GridField("date")}, key="id", default_sort="id")
    with pytest.raises(ValueError, match="UTCDateTime"):
        page(session, spec=spec)
