"""The grid query protocol's pyarrow translator (``laueportal.core.arrow_paging``): the shared operator table
(``tests.grid_cases``) and the SQL translator's orders, on an Arrow table of the same rows."""

from __future__ import annotations

import json
import math
from typing import Any

import pyarrow as pa
import pytest

from laueportal.core.arrow_paging import fetch_table_page
from laueportal.core.paging import MAX_LIMIT, GridField, GridParams, GridSpec
from laueportal.core.problems import ProblemError
from tests.grid_cases import ALL_IDS, FILTER_CASES, ROWS

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
)

TABLE = pa.table(
    {
        "id": pa.array([row["id"] for row in ROWS], pa.int64()),
        "name": pa.array([row["name"] for row in ROWS], pa.string()),
        "count": pa.array([row["count"] for row in ROWS], pa.float64()),
        "at": pa.array([row["at"] for row in ROWS], pa.timestamp("us", tz="UTC")),
        "status": pa.array([row["status"] for row in ROWS], pa.int64()),
        "note": pa.nulls(len(ROWS), pa.string()),
    }
)


def page(table: pa.Table = TABLE, rows: pa.Array | None = None, **values: Any):
    if "filter" in values and not isinstance(values["filter"], str):
        values["filter"] = json.dumps(values["filter"])
    return fetch_table_page(table, SPEC, GridParams(**values), rows=rows)


def ids(**values: Any) -> list[int]:
    return [row["id"] for row in page(**values).rows]


@pytest.mark.parametrize(("model", "expected"), [case[1:] for case in FILTER_CASES], ids=[c[0] for c in FILTER_CASES])
def test_filter_operators(model: dict[str, Any], expected: frozenset[int]) -> None:
    result = page(filter=model, sort="id", limit=MAX_LIMIT)
    assert {row["id"] for row in result.rows} == expected
    assert result.total == len(expected)


def test_a_date_column_of_any_unit_filters_alike() -> None:
    """A second's and a nanosecond's (which ends in 2262) timestamps answer the shared date cases as microseconds do."""
    for unit in ("s", "ns"):
        table = TABLE.set_column(3, "at", TABLE["at"].cast(pa.timestamp(unit, tz="UTC")))
        for name, model, expected in FILTER_CASES:
            if "at" in model:
                assert {row["id"] for row in page(table, filter=model, limit=MAX_LIMIT).rows} == expected, (unit, name)


def test_the_orders_are_the_sql_translators() -> None:
    """``tests/core/test_paging.py``'s orders: text ignores case, blanks first ascending and last descending, the
    key breaks ties, later keys apply in order."""
    assert ids() == [8, 7, 6, 5, 4, 3, 2, 1]
    assert ids(sort="name") == [6, 5, 7, 1, 2, 3, 8, 4]
    assert ids(sort="-name") == [4, 8, 3, 2, 1, 7, 5, 6]
    assert ids(sort="-count") == [8, 3, 2, 1, 7, 5, 4, 6]
    assert ids(sort="status,-id") == [4, 3, 8, 7, 2, 1, 5, 6]
    assert ids(sort=" -status , count ") == [6, 5, 7, 1, 2, 8, 3, 4]


def test_pages_carry_the_total_of_the_rows_that_match_within_the_rows_given() -> None:
    first = page(sort="status", limit=3)
    assert ([row["id"] for row in first.rows], first.total) == ([4, 3, 1], 8)
    assert [row["id"] for row in page(sort="status", offset=6, limit=3).rows] == [5, 6]
    beyond = page(sort="status", offset=100, limit=3)
    assert (beyond.rows, beyond.total) == ([], 8)
    filtered = page(filter={"count": {"filterType": "number", "type": "greaterThan", "filter": 0}}, limit=2)
    assert ([row["id"] for row in filtered.rows], filtered.total) == ([8, 7], 5)
    assert page(sort="id", limit=1).rows[0] == {**ROWS[0], "note": None}
    some = pa.array([7, 0, 2, 4])  # rows (positions) a scope keeps: ids 8, 1, 3 and 5
    scoped = page(rows=some, filter={"count": {"type": "notBlank"}}, sort="-count", limit=2)
    assert ([row["id"] for row in scoped.rows], scoped.total) == ([8, 3], 4)


def test_a_refused_filter_is_the_shared_validators_problem() -> None:
    """``paging.grid_request`` refuses a request for both translators (its cases are ``tests/core/test_paging.py``'s)."""
    with pytest.raises(ProblemError) as caught:
        page(filter={"name": {"type": "contains", "filter": "a\x00b"}})
    assert [(item.loc, item.code) for item in caught.value.problem.errors or []] == [
        (["query", "filter", "name", "filter"], "invalid_text")
    ]


def test_a_numbers_nan_is_blank_like_null_and_dictionary_text_filters_and_sorts() -> None:
    counts = {1: None, 4: math.nan, 6: math.nan}  # NaN and null: one blank group, in the key's order (SQLite's)
    table = TABLE.set_column(
        TABLE.schema.get_field_index("count"),
        "count",
        pa.array([counts.get(row["id"], row["count"]) for row in ROWS], pa.float64()),
    ).set_column(TABLE.schema.get_field_index("name"), "name", TABLE["name"].dictionary_encode())
    assert {row["id"] for row in page(table, filter={"count": {"type": "blank"}}).rows} == {1, 4, 6}
    assert [row["id"] for row in page(table, sort="count").rows] == [1, 4, 6, 5, 7, 2, 3, 8]
    assert [row["id"] for row in page(table, sort="-count").rows] == [8, 3, 2, 7, 5, 1, 4, 6]
    assert {row["id"] for row in page(table, filter={"name": {"type": "contains", "filter": "ALPHA"}}).rows} == {1, 2}
    assert [row["id"] for row in page(table, sort="name").rows] == [6, 5, 7, 1, 2, 3, 8, 4]
    assert page(table, filter={"name": {"type": "notBlank"}}).total == len(ALL_IDS) - 3


def test_a_spec_must_fit_the_table() -> None:
    with pytest.raises(ValueError, match="'note' is not a column"):
        page(TABLE.drop_columns(["note"]))
    with pytest.raises(ValueError, match="date field 'at' cannot be"):
        page(TABLE.set_column(3, "at", pa.array([1] * len(ROWS), pa.int64())))
