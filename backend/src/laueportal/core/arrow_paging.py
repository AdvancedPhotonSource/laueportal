"""The grid query protocol's pyarrow translator: server-side paging, sort
and filter over an in-memory Arrow table (the results viewer's peaks and patterns), with the same request, the same
validation and the same meaning as the SQL translator, ``core.paging``, which describes the protocol.

``fetch_table_page(table, spec, params)`` validates the request with ``paging.grid_request`` (so a request is
refused with the same 422 problems), filters the table, sorts the rows that match and answers one page and the
total, as ``paging.fetch_page`` does::

    page = arrow_paging.fetch_table_page(table, PEAK_GRID, params)
    return PeakPage(rows=[PeakRow.model_validate(row) for row in page.rows], total=page.total, as_of=page.as_of)

The spec's fields (and its key, and any ``sort_by`` column) are columns of the table: a text field a string column
(also large or dictionary-encoded), a number field an integer or float one, a date field a ``timestamp`` of any unit
with ``tz="UTC"``. What SQLite does, this does: a text filter or sort ignores case, blanks (null, and a number
column's NaN) sort first ascending and last descending, as one group in the key's order, the key ends every order, a
condition on a blank value is unknown (SQL's three values: ``notEqual`` and ``notContains`` match blanks, the others
do not), and numbers compare by their values (an integer beyond ±2**53 with a float column too, a date beyond what a
nanosecond column holds too). Case is folded by lower-casing the whole text where SQLite folds only ASCII. A table has
no ``changed_since``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

import pyarrow as pa
import pyarrow.compute as _compute

from laueportal.core.paging import (
    ONE_DAY,
    WHITESPACE,
    Combined,
    Condition,
    FieldFilter,
    GridParams,
    GridResult,
    GridSpec,
    LiveGridParams,
    grid_request,
)
from laueportal.core.timeutil import utcnow

pc: Any = _compute
"""pyarrow's compute functions, which it generates at import (so a type checker cannot see them)."""

type Column = pa.Array | pa.ChunkedArray


def fetch_table_page(
    table: pa.Table,
    spec: GridSpec,
    params: GridParams,
    *,
    rows: pa.Array | None = None,
    as_of: datetime | None = None,
) -> GridResult:
    """One page of ``table`` for ``params``, and the total of the rows that match. ``rows`` (positions in the
    table, in order) limits the grid to those rows, e.g. a scope's, without copying the table; None: every row.

    Raises a 422 ``ProblemError`` for a sort or filter the spec does not allow (``paging.grid_request``), and a
    ``ValueError`` for a spec that does not fit the table (a programming error)."""

    moment = utcnow() if as_of is None else as_of
    if isinstance(params, LiveGridParams) and params.changed_since is not None:
        raise ValueError("an in-memory table takes no changed_since")
    check_table(table, spec)
    request = grid_request(spec, params)

    matching = _matching(table, request.filters, rows)
    total = table.num_rows if matching is None else len(matching)
    if params.offset >= total:
        return GridResult(rows=[], total=total, as_of=moment)
    order = _order(table, spec, request.sort, matching)
    positions = order[params.offset : params.offset + params.limit]
    rows = positions if matching is None else matching.take(positions)
    return GridResult(rows=table.take(rows).to_pylist(), total=total, as_of=moment)


def check_table(table: pa.Table, spec: GridSpec) -> None:
    """A ``ValueError`` unless every field of ``spec`` is a column of ``table`` of its filter type."""

    names = set(table.column_names)
    for name in (*spec.fields, spec.key):
        if name not in names:
            raise ValueError(f"grid field {name!r} is not a column of the table")
    for name, grid_field in spec.fields.items():
        if grid_field.sort_by is not None and grid_field.sort_by not in names:
            raise ValueError(f"grid field {name!r} sorts by {grid_field.sort_by!r}, which is not a column of the table")
        kind = table.schema.field(name).type
        if pa.types.is_dictionary(kind):
            kind = kind.value_type
        fits = {
            "text": pa.types.is_string(kind) or pa.types.is_large_string(kind),
            "number": pa.types.is_integer(kind) or pa.types.is_floating(kind),
            "date": pa.types.is_timestamp(kind) and getattr(kind, "tz", None) == "UTC",
        }[grid_field.filter_type]
        if not fits:
            raise ValueError(f"{grid_field.filter_type} field {name!r} cannot be a {kind} column")


def _values(table: pa.Table, name: str, rows: pa.Array | None = None) -> Column:
    """The column ``name`` (its values, for a dictionary-encoded one), at ``rows`` (all without)."""

    column = table[name]
    if rows is not None:
        column = column.take(rows)
    if pa.types.is_dictionary(column.type):
        column = column.cast(column.type.value_type)
    return column


# ---------------------------------------------------------------------------
# Filter


def _matching(table: pa.Table, filters: Sequence[tuple[str, FieldFilter]], rows: pa.Array | None) -> pa.Array | None:
    """The positions of the rows (of ``rows``) every filter accepts (an unknown result rejects), in order; None for
    every row of the table."""

    mask: Any = None
    for name, item in filters:
        part = _filter(_values(table, name, rows), item)
        mask = part if mask is None else pc.and_kleene(mask, part)
    if mask is None:
        return rows
    mask = pc.fill_null(mask, False)
    if isinstance(mask, pa.ChunkedArray):
        mask = mask.combine_chunks()
    positions = pc.indices_nonzero(mask)
    return positions if rows is None else rows.take(positions)


def _filter(column: Column, item: FieldFilter) -> Any:
    if isinstance(item, Combined):
        first, second = (_condition(column, condition) for condition in item.conditions)
        return pc.and_kleene(first, second) if item.operator == "AND" else pc.or_kleene(first, second)
    return _condition(column, item)


def _blank(column: Column, filter_type: str) -> Any:
    missing = pc.is_null(column, nan_is_null=True)
    if filter_type == "text":
        return pc.or_kleene(missing, pc.equal(pc.utf8_trim(column, characters=WHITESPACE), ""))
    return missing


def _condition(column: Column, condition: Condition) -> Any:
    operator, value, upper = condition.operator, condition.value, condition.upper
    if operator in ("blank", "notBlank"):
        blank = _blank(column, condition.filter_type)
        return blank if operator == "blank" else pc.invert(blank)
    if condition.filter_type == "text":
        assert isinstance(value, str)
        return _text(column, operator, value)
    if condition.filter_type == "number":
        return _number(column, operator, value, upper)
    assert isinstance(value, datetime)
    return _date(column, operator, value, upper)


def _text(column: Column, operator: str, value: str) -> Any:
    if operator == "contains":
        return pc.match_substring(column, value, ignore_case=True)
    if operator == "notContains":
        return pc.or_kleene(pc.is_null(column), pc.invert(pc.match_substring(column, value, ignore_case=True)))
    if operator == "equals":
        return pc.equal(pc.utf8_lower(column), value.lower())
    if operator == "notEqual":
        return pc.or_kleene(pc.is_null(column), pc.not_equal(pc.utf8_lower(column), value.lower()))
    if operator == "startsWith":
        return pc.starts_with(column, value, ignore_case=True)
    return pc.ends_with(column, value, ignore_case=True)  # endsWith


def _number(column: Column, operator: str, value: Any, upper: Any) -> Any:
    if operator == "notEqual":
        return pc.or_kleene(_blank(column, "number"), pc.invert(_compare(column, "equals", value)))
    if operator == "inRange":  # the ends in order (grid_request)
        return pc.and_kleene(_compare(column, "greaterThanOrEqual", value), _compare(column, "lessThanOrEqual", upper))
    return _compare(column, operator, value)


_COMPARE = {
    "equals": pc.equal,
    "lessThan": pc.less,
    "lessThanOrEqual": pc.less_equal,
    "greaterThan": pc.greater,
    "greaterThanOrEqual": pc.greater_equal,
}


def _compare(column: Column, operator: str, value: int | float | Fraction) -> Any:
    """``column operator value`` by the numbers' values, as SQLite compares an integer with a float.

    pyarrow would compare in a common type, which need not hold both (a float column and an integer beyond ±2**53,
    or an integer column beyond ±2**53 and a float: an ``ArrowInvalid``, a 500). So the column keeps its type (a
    float one as float64, which holds every float) and ``value`` becomes the largest value of that type at or below
    it. Below a value the type cannot hold, ``< value`` means ``<= below``, ``> value`` means ``> below``, and no
    value equals it."""

    if pa.types.is_floating(column.type):
        column = column.cast(pa.float64())
    kind = column.type
    below = _at_or_below(kind, value)
    if below is None:  # below every value of the (integer) type
        least = pa.scalar(_integer_range(kind)[0], kind)
        return (pc.greater_equal if operator.startswith("greaterThan") else pc.less)(column, least)
    bound = pa.scalar(below, kind)
    if below == value:
        return _COMPARE[operator](column, bound)
    if operator == "equals":
        return pc.and_kleene(pc.greater(column, bound), pc.less_equal(column, bound))  # false (unknown for a blank)
    return (pc.greater if operator.startswith("greaterThan") else pc.less_equal)(column, bound)


def _at_or_below(kind: pa.DataType, value: int | float | Fraction) -> int | float | None:
    """The largest value of ``kind`` (float64 or an integer type) at or below ``value``; None when there is none."""

    if pa.types.is_floating(kind):
        nearest = float(value)
        return math.nextafter(nearest, -math.inf) if nearest > value else nearest
    least, most = _integer_range(kind)
    return None if value < least else min(math.floor(value), most)


def _integer_range(kind: pa.DataType) -> tuple[int, int]:
    bits = kind.bit_width
    if pa.types.is_signed_integer(kind):
        return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    return 0, 2**bits - 1


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PER_SECOND = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}


def _date(column: Column, operator: str, start: datetime, last: Any) -> Any:
    """By the column's count of its unit since the epoch (its int64 values), so that a moment the unit cannot hold
    (a nanosecond column ends in 2262) compares as beyond every value."""

    kind = column.type
    assert isinstance(kind, pa.TimestampType)  # check_table
    per_second = _PER_SECOND[kind.unit]
    counts = column.cast(pa.int64())

    def at(moment: datetime) -> Fraction:
        return Fraction((moment - _EPOCH) // timedelta(microseconds=1) * per_second, 10**6)

    if operator == "equals":
        return pc.and_kleene(
            _compare(counts, "greaterThanOrEqual", at(start)), _compare(counts, "lessThan", at(start + ONE_DAY))
        )
    if operator == "lessThan":
        return _compare(counts, "lessThan", at(start))
    if operator == "greaterThan":
        return _compare(counts, "greaterThanOrEqual", at(start + ONE_DAY))
    assert isinstance(last, datetime)  # inRange: the first and the last day's starts (grid_request)
    return pc.and_kleene(
        _compare(counts, "greaterThanOrEqual", at(start)), _compare(counts, "lessThan", at(last + ONE_DAY))
    )


# ---------------------------------------------------------------------------
# Sort


def _order(table: pa.Table, spec: GridSpec, keys: Sequence[tuple[str, bool]], rows: pa.Array | None) -> pa.Array:
    """Positions in ``rows`` (in the table without) in the order of ``keys`` then the spec's key: each key sorts by
    whether it has a value (blanks first ascending, last descending), then by the value, a text field's lower-cased
    and a ``sort_by`` field's other column as it is."""

    columns: dict[str, Column] = {}
    sort_keys: list[tuple[str, str]] = []
    order = list(keys)
    if spec.key not in {name for name, _ in keys}:
        order.append((spec.key, False))
    for index, (name, descending) in enumerate(order):
        grid_field = spec.fields.get(name) if index < len(keys) else None
        if grid_field is not None and grid_field.sort_by is not None:
            values = _values(table, grid_field.sort_by, rows)
        elif grid_field is not None and grid_field.filter_type == "text":
            values = pc.utf8_lower(_values(table, name, rows))
        else:
            values = _values(table, name, rows)
        direction = "descending" if descending else "ascending"
        present = pc.invert(pc.is_null(values, nan_is_null=True))
        if pa.types.is_floating(values.type):  # NaN is null, so that the blanks sort as one group (SQLite's)
            values = pc.if_else(present, values, None)
        columns[f"present_{index}"] = present
        columns[f"value_{index}"] = values
        sort_keys += [(f"present_{index}", direction), (f"value_{index}", direction)]
    return pc.sort_indices(pa.table(columns), sort_keys=sort_keys)
