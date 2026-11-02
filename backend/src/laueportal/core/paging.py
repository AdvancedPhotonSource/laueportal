"""The grid query protocol (with its ``changed_since``): server-side paging, sort and filter for AG Grid's Infinite Row
Model, over any SQLAlchemy select.

A list endpoint declares its grid once (``GridSpec``): the fields of its select that can be sorted and filtered,
each with its filter type (``text``, ``number`` or ``date``), the unique field that breaks ties, the default sort
and, for a live list, what "changed since" means. ``fetch_page(session, query, spec, params)`` then answers with
two queries, the total and one page, however many rows match::

    @router.get("/scans", name="listScans", summary="List scans", response_model=ScanPage)
    def list_scans(params: Annotated[GridParams, Query()], session: SessionDep) -> ScanPage:
        page = paging.fetch_page(session, scan_list_query(), SCAN_GRID, params)
        rows = [ScanSummary.model_validate(dict(row)) for row in page.rows]
        return ScanPage(rows=rows, total=page.total, as_of=page.as_of)

The route publishes the fields in its OpenAPI operation (``openapi_extra=SCAN_GRID.openapi_extra()``, the
``x-grid-fields`` extension). The request is validated once, by ``grid_request``, into the sort and the filter both
translators apply: this SQL one and the pyarrow one for in-memory tables (``core.arrow_paging``). The fields are the select's column labels; the filters and the order apply to the select wrapped as a subquery,
so computed columns (counts, the scans' technique) sort and filter like stored ones.

**Request** (``GridParams``; a live list takes ``LiveGridParams``, which adds ``changed_since``):

- ``offset`` >= 0 and ``limit`` 1 to 1000 (default 100, the DataGrid's block size);
- ``sort``: a comma list of fields, ``-`` for descending (``sort=-submit_time,id``), at most ``MAX_SORT_LENGTH``
  characters and naming at most the spec's sortable fields, each once. Text sorts ignore case;
  blanks (NULL) come first ascending and last descending (SQLite's order). A field declared with ``sort_by``
  sorts by that other column of the select instead, as it is (a run's status, filtered by its name, sorts by its
  code: the lifecycle order). The spec's key ends every order, so pages neither overlap nor skip rows. Without
  ``sort`` (or with a blank one) the spec's default applies;
- ``filter``: JSON, a subset of AG Grid's filter model with one entry per field: a condition
  (``{"filterType": "text", "type": "contains", "filter": "smith"}``) or two conditions joined with
  ``{"filterType": ..., "operator": "AND" | "OR", "conditions": [{...}, {...}]}``. ``filterType`` may be left
  out; when given, it must be the field's. A blank ``filter`` means none.

**Conditions** (``type``):

- text (value in ``filter``): contains, notContains, equals, notEqual, startsWith, endsWith. They ignore case
  (AG Grid's default) and take ``%`` and ``_`` literally;
- number (``filter``; inRange also ``filterTo``): equals, notEqual, lessThan, lessThanOrEqual, greaterThan,
  greaterThanOrEqual, inRange. inRange includes both ends (AG Grid's client-side default excludes them);
- date (``dateFrom``; inRange also ``dateTo``), by day: a value is the start of a day, in ISO 8601. A value with
  an offset is that instant, and the browser should send its local midnight so: ``2026-01-01T00:00:00-06:00``
  (or ``2026-01-01T06:00:00Z``). AG Grid's own ``2026-01-01 00:00:00`` (or ``2026-01-01``) has no offset and is
  read as a UTC day, which is not the local day the grid shows (the frontend's DataGrid adds the offset). A day is
  the 24 hours from its start, so on a day the clocks change (23 or 25 local hours) its window is an hour
  off at the end. A date before year 1 or after 9999-12-30 in UTC is refused. equals matches that day,
  lessThan the days before it, greaterThan the days after it, inRange the first day through the last;
- every type: blank (NULL; for text also an empty value, or one of only spaces, tabs and line breaks) and
  notBlank.

notEqual and notContains are the complements of equals and contains, so they also match blank values; every
other condition but blank excludes them. inRange takes its two values in either order.

**Response:** ``GridPage``, ``{"rows": [...], "total": n, "as_of": t}``: ``total`` counts every row that matches
the filter (and ``changed_since``); the frontend passes it to AG Grid as ``lastRow``. ``as_of`` is the server's
time (UTC) just before the page was read: a live list's client sends it back as the next ``changed_since``.

**Errors:** a sort or filter field the spec does not declare, or declares unsortable or unfilterable, and any
malformed sort or filter, are a 422 problem (``ProblemError``) whose ``errors`` locate each problem:
``["query", "sort"]``, ``["query", "filter"]`` (not a JSON object) or ``["query", "filter", <field>, ...]``.
Every problem is reported at once, and none grows with the request: a sort's empty items are one problem, a sort
naming more fields than the spec can sort is one problem, and a message shows a value from the request shortened
(``core.text.quoted``). A NUL or a lone surrogate anywhere in the filter (a field name or a value: SQLite's
``LIKE`` stops at a NUL, and UTF-8 cannot encode a lone surrogate) is a problem located there (``invalid_text``,
the name escaped), and so is JSON nested too deeply to decode. An ``offset`` or ``limit`` out of range, a ``sort``
longer than ``MAX_SORT_LENGTH``, and an unknown parameter, are FastAPI's validation 422s (``["query", "limit"]``).

**changed_since**: only the rows changed at or after ``changed_since`` minus
``CHANGED_SINCE_OVERLAP`` (2 s, so a row written while the previous poll ran is not missed), as the spec's
``changed_since`` condition defines them (for runs, ``jobs.progress.active_or_changed_since``). The value is
compared with the server's clock: a client sends a server time (the previous response's ``as_of``), not its
own. It needs a UTC offset (or ``Z``) and must be ``EARLIEST_CHANGED_SINCE`` (``0001-01-01T00:00:02Z``, so that
the overlap before it is still a time) or later; anything else, also a time whose offset moves it out of the years
1 to 9999 in UTC, is FastAPI's validation 422 at ``["query", "changed_since"]``, never a 500.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, Literal, TypeVar, Unpack

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import ColumnElement, Select, Subquery, and_, func, literal, not_, or_, select
from sqlalchemy.orm import Session

from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.problems import ProblemError, ProblemItem, problems_detail
from laueportal.core.text import INVALID_TEXT, invalid_strings, quoted
from laueportal.core.timeutil import UTCDateTime, UTCTimestamp, isoformat_z, utcnow

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_SORT_LENGTH = 1000
"""The longest ``sort``, in characters: every sortable field of any grid, descending, fits several times over."""
CHANGED_SINCE_OVERLAP = timedelta(seconds=2)
"""How far before ``changed_since`` a list looks, so a row written while the previous poll ran is not missed."""
EARLIEST_CHANGED_SINCE = datetime.min.replace(tzinfo=UTC) + CHANGED_SINCE_OVERLAP
"""The earliest ``changed_since``: the overlap before it must still be a ``datetime``."""

FilterType = Literal["text", "number", "date"]

TEXT_OPERATORS = ("contains", "notContains", "equals", "notEqual", "startsWith", "endsWith", "blank", "notBlank")
NUMBER_OPERATORS = (
    "equals",
    "notEqual",
    "lessThan",
    "lessThanOrEqual",
    "greaterThan",
    "greaterThanOrEqual",
    "inRange",
    "blank",
    "notBlank",
)
DATE_OPERATORS = ("equals", "lessThan", "greaterThan", "inRange", "blank", "notBlank")
OPERATORS: Mapping[str, tuple[str, ...]] = {"text": TEXT_OPERATORS, "number": NUMBER_OPERATORS, "date": DATE_OPERATORS}
JOIN_OPERATORS = ("AND", "OR")

GRID_FIELDS_KEY = "x-grid-fields"
"""The OpenAPI extension on a grid list's operation that lists its fields (``GridSpec.openapi_extra``)."""
SORT_LOC: tuple[str, ...] = ("query", "sort")
FILTER_LOC: tuple[str, ...] = ("query", "filter")

ONE_DAY = timedelta(days=1)
_LAST_DAY_START = datetime.max.replace(tzinfo=UTC) - ONE_DAY
"""The latest start of a day whose end a ``datetime`` can still hold."""
_UTC_DATETIME = UTCDateTime()
WHITESPACE = " \t\n\r\f\v"
"""What a blank text value may consist of (SQLite's ``trim`` strips only spaces unless told)."""


# ---------------------------------------------------------------------------
# Request, declaration and response


class GridParams(BaseModel):
    """The grid query parameters of a list endpoint: ``params: Annotated[GridParams, Query()]``."""

    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0, le=SQLITE_INTEGER_MAX, description="Index of the first row to return.")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description=f"Rows to return (at most {MAX_LIMIT}).")
    sort: str | None = Field(
        default=None,
        max_length=MAX_SORT_LENGTH,
        description=(
            "Comma list of fields, `-` for descending, e.g. `-time,scan_number`, each sortable field at most once. "
            "Text sorts ignore case."
        ),
    )
    filter: str | None = Field(
        default=None,
        description=(
            'JSON filter model (a subset of AG Grid\'s): `{"<field>": {"filterType": "text", "type": "contains", '
            '"filter": "smith"}}`, or two conditions joined by `{"operator": "AND", "conditions": [...]}`. '
            "Operators: text contains, notContains, equals, notEqual, startsWith, endsWith; number equals, "
            "notEqual, lessThan, lessThanOrEqual, greaterThan, greaterThanOrEqual, inRange; date (by day) "
            "equals, lessThan, greaterThan, inRange; all blank and notBlank. A date (`dateFrom`, `dateTo`) names "
            "the day starting then: send the local midnight with its UTC offset, e.g. "
            "`2026-01-01T00:00:00-06:00`; a date without an offset (AG Grid's `2026-01-01 00:00:00`) is read as a "
            "UTC day. A day is 24 hours from its start, so on a daylight-saving change day its end is an hour off."
        ),
    )


class LiveGridParams(GridParams):
    """``GridParams`` plus ``changed_since``, for lists that poll for changes."""

    changed_since: UTCTimestamp | None = Field(
        default=None,
        description=(
            "Only rows changed since this server time (minus a 2 s overlap), for refreshing loaded blocks: send the "
            "previous response's `as_of`. It needs a UTC offset or `Z`."
        ),
    )

    @field_validator("changed_since")
    @classmethod
    def _overlap_is_a_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value < EARLIEST_CHANGED_SINCE:
            raise ValueError(
                f"{isoformat_z(value)} is out of range: changed_since must be {isoformat_z(EARLIEST_CHANGED_SINCE)} "
                f"or later (the list returns the rows changed up to {CHANGED_SINCE_OVERLAP.seconds} s before it)"
            )
        return value


@dataclass(frozen=True)
class GridField:
    """How a grid sorts and filters one field: a column of the endpoint's select, named by its label."""

    filter_type: FilterType
    """Which filter models the field takes; ``text`` also makes its sort ignore case. A ``date`` field must be a
    ``UTCDateTime`` column."""
    sortable: bool = True
    filterable: bool = True
    sort_by: str | None = None
    """Another column of the select (its label) that the field sorts by, as it is (no case folding): e.g. a status
    that filters by its name but sorts by its code. None: the field sorts by its own column."""


@dataclass(frozen=True, eq=False)
class GridSpec:
    """An endpoint's grid: its fields, its unique key, its default sort and, for a live list, ``changed_since``."""

    fields: Mapping[str, GridField]
    key: str
    """A unique column of the select (e.g. the primary key): the last sort key of every order, for stable pages."""
    default_sort: str
    """The order without a ``sort`` parameter, in the same syntax (``-scan_number``)."""
    changed_since: Callable[[datetime], ColumnElement[bool]] | None = None
    """The rows changed since a time, as a condition on the select's own tables (the overlap is already applied)."""

    def __post_init__(self) -> None:
        problems = _Problems()
        _sort_keys(self, self.default_sort, problems)
        if problems.items:
            raise ValueError(f"default sort {self.default_sort!r}: {problems.items[0].message}")

    def openapi_extra(self) -> dict[str, Any]:
        """The endpoint operation's ``openapi_extra``: the fields it sorts and filters, published in
        ``openapi.json`` as ``x-grid-fields`` (``{field: {filterType, sortable, filterable}}``) so that a client
        (the frontend's grid contract test) can check what its grids offer against what the list accepts."""
        return {
            GRID_FIELDS_KEY: {
                name: {"filterType": field.filter_type, "sortable": field.sortable, "filterable": field.filterable}
                for name, field in self.fields.items()
            }
        }


RowT = TypeVar("RowT", bound=BaseModel)


class GridPage(BaseModel, Generic[RowT]):
    """One page of a grid list. Subclass it per row type (``class ScanPage(GridPage[ScanSummary])``)."""

    rows: list[RowT]
    total: int = Field(description="Rows matching the filter (and changed_since): AG Grid's `lastRow`.")
    as_of: UTCTimestamp = Field(
        description="The server's time (UTC) just before the page was read: a live list sends it back as the next "
        "`changed_since`."
    )


@dataclass(frozen=True)
class GridResult:
    """What ``fetch_page`` (or ``core.arrow_paging.fetch_table_page``) found: the page's rows (by column label), the
    total, and when it was read."""

    rows: Sequence[Mapping[Any, Any]]
    total: int
    as_of: datetime
    """The server's time (aware, UTC) just before the queries ran: the response's ``as_of``."""


@dataclass(frozen=True)
class Condition:
    """One filter condition of a request, validated (``grid_request``)."""

    filter_type: FilterType
    operator: str
    value: str | int | float | datetime | None = None
    """text: the value; number: the number (inRange: the lower end); date: the start of the day, aware UTC
    (inRange: the first day's); None for blank and notBlank."""
    upper: int | float | datetime | None = None
    """inRange only: the upper end, or the last day's start."""


@dataclass(frozen=True)
class Combined:
    """Two conditions of one field joined with AND or OR."""

    operator: Literal["AND", "OR"]
    conditions: tuple[Condition, Condition]


FieldFilter = Condition | Combined


@dataclass(frozen=True)
class GridRequest:
    """A request's sort and filter, validated against a grid (``grid_request``): what a translator applies (this
    module's SQL one, or ``core.arrow_paging``'s pyarrow one), so both accept and refuse the same requests."""

    sort: tuple[tuple[str, bool], ...]
    """``(field, descending)`` in order (the spec's default without a ``sort``); the spec's key is not added."""
    filters: tuple[tuple[str, FieldFilter], ...]
    """Each filtered field and its filter; the fields are ANDed."""


def grid_request(spec: GridSpec, params: GridParams) -> GridRequest:
    """The sort and filter of ``params`` for ``spec``, or a 422 ``ProblemError`` that locates every problem."""

    problems = _Problems()
    sort = params.sort if params.sort is not None and params.sort.strip() else spec.default_sort
    keys = _sort_keys(spec, sort, problems)
    filters = _filters(spec, params.filter, problems)
    problems.raise_any()
    return GridRequest(sort=tuple(keys), filters=tuple(filters))


def fetch_page(
    session: Session,
    query: Select[Unpack[tuple[Any, ...]]],
    spec: GridSpec,
    params: GridParams,
    *,
    as_of: datetime | None = None,
) -> GridResult:
    """One page of ``query`` for ``params``, and the total: two queries.

    ``as_of`` is the time the result reports (default: now); a query that itself uses the current time (e.g. a
    running run's duration) passes the time it used, so that the two agree.

    Raises a 422 ``ProblemError`` for a sort or filter the spec does not allow, before touching the database.
    """

    moment = utcnow() if as_of is None else as_of
    since = params.changed_since if isinstance(params, LiveGridParams) else None
    if since is not None:
        if spec.changed_since is None:
            raise ValueError("this grid takes no changed_since (its GridSpec defines none)")
        query = query.where(spec.changed_since(since - CHANGED_SINCE_OVERLAP))
    base = query.subquery("grid")
    columns = _columns(base, spec)
    request = grid_request(spec, params)
    conditions = [_sql_filter(columns[name], item) for name, item in request.filters]

    total = session.scalar(select(func.count()).select_from(base).where(*conditions)) or 0
    page = (
        select(base)
        .where(*conditions)
        .order_by(*_order_by(spec, columns, request.sort))
        .offset(params.offset)
        .limit(params.limit)
    )
    return GridResult(rows=session.execute(page).mappings().all(), total=total, as_of=moment)


# ---------------------------------------------------------------------------
# Implementation


class _Problems:
    """The problems found in one request's sort and filter, raised together as one 422."""

    def __init__(self) -> None:
        self.items: list[ProblemItem] = []

    def add(self, loc: Sequence[str | int], message: str, code: str) -> None:
        self.items.append(ProblemItem(loc=list(loc), message=message, code=code))

    def raise_any(self) -> None:
        if self.items:
            raise ProblemError(422, "Validation failed", problems_detail(len(self.items)), errors=self.items)


def _columns(base: Subquery, spec: GridSpec) -> dict[str, ColumnElement[Any]]:
    columns: dict[str, ColumnElement[Any]] = {}
    for name in (*spec.fields, spec.key):
        try:
            column = base.c[name]
        except KeyError:
            raise ValueError(f"grid field {name!r} is not a column of the query") from None
        grid_field = spec.fields.get(name)
        if grid_field is not None and grid_field.filter_type == "date" and not isinstance(column.type, UTCDateTime):
            raise ValueError(f"date field {name!r} must be a UTCDateTime column, not {column.type!r}")
        columns[name] = column
    for name, grid_field in spec.fields.items():
        if grid_field.sort_by is not None and grid_field.sort_by not in columns:
            try:
                columns[grid_field.sort_by] = base.c[grid_field.sort_by]
            except KeyError:
                raise ValueError(
                    f"grid field {name!r} sorts by {grid_field.sort_by!r}, which is not a column of the query"
                ) from None
    return columns


def _listing(names: Sequence[str]) -> str:
    return ", ".join(names) if names else "none"


def _sort_keys(spec: GridSpec, text: str, problems: _Problems) -> list[tuple[str, bool]]:
    """``[(field, descending), ...]`` from ``-a,b``; problems for unknown, unsortable or repeated fields, one for
    any number of empty items, and one for more items than the spec has sortable fields (none of which repeats the
    whole text, so the answer stays small)."""

    sortable = sorted(name for name, grid_field in spec.fields.items() if grid_field.sortable)
    items = text.split(",")
    if len(items) > max(len(sortable), 1):
        problems.add(
            SORT_LOC,
            f"The sort names {len(items)} fields; it can name each of the {len(sortable)} sortable fields once: "
            f"{_listing(sortable)}",
            "too_many_fields",
        )
        return []
    keys: list[tuple[str, bool]] = []
    seen: set[str] = set()
    empty = False
    for item in items:
        item = item.strip()
        descending = item.startswith("-")
        name = item[1:].strip() if descending else item
        if not name:
            if not empty:
                problems.add(SORT_LOC, "Empty sort field; use a comma list such as -a,b", "invalid_sort")
                empty = True
            continue
        grid_field = spec.fields.get(name)
        if grid_field is None:
            problems.add(
                SORT_LOC, f"Unknown sort field {quoted(name)}; sortable: {_listing(sortable)}", "unknown_field"
            )
            continue
        if not grid_field.sortable:
            problems.add(SORT_LOC, f"{name} cannot be sorted; sortable: {_listing(sortable)}", "not_sortable")
            continue
        if name in seen:
            problems.add(SORT_LOC, f"{name} appears more than once in the sort", "duplicate_field")
            continue
        seen.add(name)
        keys.append((name, descending))
    return keys


def _order_by(spec: GridSpec, columns: Mapping[str, ColumnElement[Any]], keys: Sequence[tuple[str, bool]]) -> list[Any]:
    clauses: list[Any] = []
    for name, descending in keys:
        grid_field = spec.fields[name]
        if grid_field.sort_by is not None:
            expression = columns[grid_field.sort_by]
        elif grid_field.filter_type == "text":
            expression = columns[name].collate("NOCASE")
        else:
            expression = columns[name]
        clauses.append(expression.desc() if descending else expression.asc())
    if spec.key not in {name for name, _ in keys}:
        clauses.append(columns[spec.key].asc())
    return clauses


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _filters(spec: GridSpec, text: str | None, problems: _Problems) -> list[tuple[str, FieldFilter]]:
    if text is None or not text.strip():
        return []
    try:
        model = json.loads(text, parse_constant=_reject_constant)
    except ValueError as error:  # json.JSONDecodeError is a ValueError
        problems.add(FILTER_LOC, f"The filter is not valid JSON: {error}", "json_invalid")
        return []
    except RecursionError:
        problems.add(FILTER_LOC, "The filter is not valid JSON: it is nested too deeply", "json_invalid")
        return []
    invalid = list(invalid_strings(model, FILTER_LOC))
    for loc, message in invalid:
        problems.add(loc, message, INVALID_TEXT)
    if invalid:
        return []
    if not isinstance(model, dict):
        problems.add(FILTER_LOC, "The filter must be a JSON object: {field: filter model}", "invalid_filter")
        return []
    filterable = sorted(name for name, grid_field in spec.fields.items() if grid_field.filterable)
    filters: list[tuple[str, FieldFilter]] = []
    for name, field_model in model.items():
        loc = (*FILTER_LOC, name)
        grid_field = spec.fields.get(name)
        if grid_field is None:
            problems.add(
                loc, f"Unknown filter field {quoted(name)}; filterable: {_listing(filterable)}", "unknown_field"
            )
            continue
        if not grid_field.filterable:
            problems.add(loc, f"{name} cannot be filtered; filterable: {_listing(filterable)}", "not_filterable")
            continue
        item = _field_filter(grid_field.filter_type, field_model, loc, problems)
        if item is not None:
            filters.append((name, item))
    return filters


def _model_type_ok(
    model: Mapping[str, Any], filter_type: FilterType, loc: Sequence[str | int], problems: _Problems
) -> bool:
    declared = model.get("filterType")
    if declared is not None and declared != filter_type:
        problems.add(
            (*loc, "filterType"), f"This field takes a {filter_type} filter, not {quoted(declared)}", "filter_type"
        )
        return False
    return True


def _field_filter(
    filter_type: FilterType, model: Any, loc: Sequence[str | int], problems: _Problems
) -> FieldFilter | None:
    """One field's filter model: one condition, or two joined with AND or OR."""

    if not isinstance(model, dict):
        problems.add(loc, "A filter model must be a JSON object", "invalid_filter")
        return None
    if not _model_type_ok(model, filter_type, loc, problems):
        return None
    if "operator" not in model and "conditions" not in model:
        return _condition(filter_type, model, loc, problems)

    operator = model.get("operator")
    conditions = model.get("conditions")
    valid = True
    if operator not in JOIN_OPERATORS:
        problems.add((*loc, "operator"), f"operator must be AND or OR, not {quoted(operator)}", "invalid_filter")
        valid = False
    if not isinstance(conditions, list) or len(conditions) != 2:
        problems.add((*loc, "conditions"), "conditions must be a list of two conditions", "invalid_filter")
        return None
    parts: list[Condition] = []
    for index, condition in enumerate(conditions):
        part_loc = (*loc, "conditions", index)
        if not isinstance(condition, dict):
            problems.add(part_loc, "A condition must be a JSON object", "invalid_filter")
            continue
        if "operator" in condition or "conditions" in condition:
            problems.add(part_loc, "Conditions cannot be nested", "invalid_filter")
            continue
        if not _model_type_ok(condition, filter_type, part_loc, problems):
            continue
        part = _condition(filter_type, condition, part_loc, problems)
        if part is not None:
            parts.append(part)
    if not valid or len(parts) != 2:
        return None
    return Combined("AND" if operator == "AND" else "OR", (parts[0], parts[1]))


def _condition(
    filter_type: FilterType, model: Mapping[str, Any], loc: Sequence[str | int], problems: _Problems
) -> Condition | None:
    operator = model.get("type")
    supported = OPERATORS[filter_type]
    if not isinstance(operator, str) or operator not in supported:
        problems.add(
            (*loc, "type"),
            f"{filter_type} filters support {', '.join(supported)}; got {quoted(operator)}",
            "unsupported_operator",
        )
        return None
    if operator in ("blank", "notBlank"):
        return Condition(filter_type, operator)
    if filter_type == "text":
        text = _text_value(model, operator, loc, problems)
        return None if text is None else Condition(filter_type, operator, text)
    if filter_type == "number":
        number = _number_value(model, "filter", operator, loc, problems)
        upper = _number_value(model, "filterTo", operator, loc, problems) if operator == "inRange" else None
        if number is None:
            return None
        if operator != "inRange":
            return Condition(filter_type, operator, number)
        if upper is None:
            return None
        low, high = sorted((number, upper))
        return Condition(filter_type, operator, low, high)
    start = _date_value(model, "dateFrom", operator, loc, problems)
    end = _date_value(model, "dateTo", operator, loc, problems) if operator == "inRange" else None
    if start is None:
        return None
    if operator != "inRange":
        return Condition(filter_type, operator, start)
    if end is None:
        return None
    first, last = sorted((start, end))
    return Condition(filter_type, operator, first, last)


def _text_value(model: Mapping[str, Any], operator: str, loc: Sequence[str | int], problems: _Problems) -> str | None:
    value = model.get("filter")
    if value is None:
        problems.add((*loc, "filter"), f"{operator} needs a value in filter", "missing")
        return None
    if not isinstance(value, str):
        problems.add((*loc, "filter"), f"A text filter value must be a string, not {quoted(value)}", "invalid_value")
        return None
    return value


def _number_value(
    model: Mapping[str, Any], key: str, operator: str, loc: Sequence[str | int], problems: _Problems
) -> int | float | None:
    value = model.get(key)
    if value is None:
        problems.add((*loc, key), f"{operator} needs a number in {key}", "missing")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        problems.add((*loc, key), f"{key} must be a number, not {quoted(value)}", "invalid_value")
        return None
    if isinstance(value, int) and not -SQLITE_INTEGER_MAX - 1 <= value <= SQLITE_INTEGER_MAX:
        problems.add((*loc, key), f"{key} {quoted(value)} is out of range (a 64-bit integer)", "invalid_value")
        return None
    return value


def _date_value(
    model: Mapping[str, Any], key: str, operator: str, loc: Sequence[str | int], problems: _Problems
) -> datetime | None:
    raw = model.get(key)
    if raw is None:
        problems.add((*loc, key), f"{operator} needs a date in {key}", "missing")
        return None
    if not isinstance(raw, str):
        problems.add((*loc, key), f"{key} must be an ISO 8601 date, not {quoted(raw)}", "invalid_value")
        return None
    try:
        value = datetime.fromisoformat(raw.strip())
    except ValueError:
        problems.add((*loc, key), f"{key} {quoted(raw)} is not an ISO 8601 date", "invalid_value")
        return None
    try:
        start = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except OverflowError:  # e.g. 0001-01-01T00:00:00+01:00 falls before year 1 in UTC
        start = None
    if start is None or start > _LAST_DAY_START:  # the day must end before year 10000 too
        problems.add(
            (*loc, key), f"{key} {quoted(raw)} is out of range (year 1 to 9999-12-30, in UTC)", "invalid_value"
        )
        return None
    return start


# ---------------------------------------------------------------------------
# The SQL translator


def _sql_filter(column: ColumnElement[Any], item: FieldFilter) -> ColumnElement[bool]:
    if isinstance(item, Combined):
        first, second = (_sql_condition(column, condition) for condition in item.conditions)
        return and_(first, second) if item.operator == "AND" else or_(first, second)
    return _sql_condition(column, item)


def _sql_condition(column: ColumnElement[Any], condition: Condition) -> ColumnElement[bool]:
    operator, value, upper = condition.operator, condition.value, condition.upper
    if operator in ("blank", "notBlank"):
        if condition.filter_type == "text":
            blank = or_(column.is_(None), func.trim(column, WHITESPACE) == "")
        else:
            blank = column.is_(None)
        return blank if operator == "blank" else not_(blank)
    if condition.filter_type == "text":
        assert isinstance(value, str)
        return _text_condition(column, operator, value)
    if condition.filter_type == "number":
        return _number_condition(column, operator, value, upper)
    assert isinstance(value, datetime)
    return _date_condition(column, operator, value, upper)


def _text_condition(column: ColumnElement[Any], operator: str, value: str) -> ColumnElement[bool]:
    if operator == "contains":
        return column.icontains(value, autoescape=True)
    if operator == "notContains":
        return or_(column.is_(None), not_(column.icontains(value, autoescape=True)))
    if operator == "equals":
        return func.lower(column) == func.lower(value)
    if operator == "notEqual":
        return or_(column.is_(None), func.lower(column) != func.lower(value))
    if operator == "startsWith":
        return column.istartswith(value, autoescape=True)
    return column.iendswith(value, autoescape=True)  # endsWith


def _number_condition(column: ColumnElement[Any], operator: str, value: Any, upper: Any) -> ColumnElement[bool]:
    if operator == "equals":
        return column == value
    if operator == "notEqual":
        return or_(column.is_(None), column != value)
    if operator == "lessThan":
        return column < value
    if operator == "lessThanOrEqual":
        return column <= value
    if operator == "greaterThan":
        return column > value
    if operator == "greaterThanOrEqual":
        return column >= value
    return column.between(value, upper)  # inRange: the ends in order (grid_request)


def _date_condition(column: ColumnElement[Any], operator: str, start: datetime, last: Any) -> ColumnElement[bool]:
    def at(moment: datetime) -> ColumnElement[Any]:
        return literal(moment, _UTC_DATETIME)

    if operator == "equals":
        return and_(column >= at(start), column < at(start + ONE_DAY))
    if operator == "lessThan":
        return column < at(start)
    if operator == "greaterThan":
        return column >= at(start + ONE_DAY)
    assert isinstance(last, datetime)  # inRange: the first and the last day's starts (grid_request)
    return and_(column >= at(start), column < at(last + ONE_DAY))
