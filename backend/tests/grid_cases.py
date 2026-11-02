"""The grid query protocol's shared test table: rows, and the IDs each filter model must match.

``tests/core/test_paging.py`` runs every case through the SQL translator (``laueportal.core.paging``),
``tests/core/test_arrow_paging.py`` the filter cases through the pyarrow translator (``laueportal.core.arrow_paging``)
for in-memory tables (peaks, patterns, points).

Fields: ``id`` (number, the key), ``name`` (text), ``count`` (number), ``at`` (date, UTC), ``status`` and
``updated_at`` (for ``changed_since``), and ``note`` (neither sortable nor filterable).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


ROWS: list[dict[str, Any]] = [
    {"id": 1, "name": "Alpha", "count": 10, "at": utc("2026-01-01T00:00:00"), "status": 2},
    {"id": 2, "name": "alphabet", "count": 20, "at": utc("2026-01-01T23:59:59"), "status": 2},
    {"id": 3, "name": "Beta 50%", "count": 30, "at": utc("2026-01-02T12:00:00"), "status": 1},
    {"id": 4, "name": "gamma_ray", "count": -5, "at": utc("2026-01-03T00:00:00"), "status": 0},
    {"id": 5, "name": "", "count": 0, "at": None, "status": 3},
    {"id": 6, "name": None, "count": None, "at": utc("2025-12-31T23:59:59"), "status": 4},
    {"id": 7, "name": " \t\n", "count": 2.5, "at": utc("2026-01-02T05:59:59"), "status": 2},
    {"id": 8, "name": "delta/50", "count": 100, "at": utc("2026-01-10T08:00:00"), "status": 2},
]
ALL_IDS = frozenset(row["id"] for row in ROWS)


def text(operator: str, value: str | None = None) -> dict[str, Any]:
    model: dict[str, Any] = {"filterType": "text", "type": operator}
    if value is not None:
        model["filter"] = value
    return model


def number(operator: str, value: float | None = None, to: float | None = None) -> dict[str, Any]:
    model: dict[str, Any] = {"filterType": "number", "type": operator}
    if value is not None:
        model["filter"] = value
    if to is not None:
        model["filterTo"] = to
    return model


def date(operator: str, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    model: dict[str, Any] = {"filterType": "date", "type": operator, "dateFrom": start, "dateTo": end}
    return model


def combined(filter_type: str, operator: str, *conditions: dict[str, Any]) -> dict[str, Any]:
    return {"filterType": filter_type, "operator": operator, "conditions": list(conditions)}


FILTER_CASES: list[tuple[str, dict[str, Any], frozenset[int]]] = [
    # text
    ("text contains ignores case", {"name": text("contains", "ALPHA")}, frozenset({1, 2})),
    ("text contains takes % literally", {"name": text("contains", "50%")}, frozenset({3})),
    ("text contains takes _ literally", {"name": text("contains", "_")}, frozenset({4})),
    ("text contains takes the escape character literally", {"name": text("contains", "/")}, frozenset({8})),
    ("text notContains matches blanks", {"name": text("notContains", "alpha")}, ALL_IDS - {1, 2}),
    ("text equals ignores case", {"name": text("equals", "ALPHA")}, frozenset({1})),
    ("text equals the empty string", {"name": text("equals", "")}, frozenset({5})),
    ("text notEqual matches blanks", {"name": text("notEqual", "alpha")}, ALL_IDS - {1}),
    ("text startsWith ignores case", {"name": text("startsWith", "AL")}, frozenset({1, 2})),
    ("text startsWith takes _ literally", {"name": text("startsWith", "gamma_")}, frozenset({4})),
    ("text endsWith ignores case", {"name": text("endsWith", "BET")}, frozenset({2})),
    ("text endsWith takes % literally", {"name": text("endsWith", "%")}, frozenset({3})),
    ("text blank is null, empty, or only spaces, tabs and line breaks", {"name": text("blank")}, frozenset({5, 6, 7})),
    ("text notBlank", {"name": text("notBlank")}, frozenset({1, 2, 3, 4, 8})),
    # number
    ("number equals", {"count": number("equals", 20)}, frozenset({2})),
    ("number equals zero", {"count": number("equals", 0)}, frozenset({5})),
    ("number equals a fraction", {"count": number("equals", 2.5)}, frozenset({7})),
    ("number notEqual matches blanks", {"count": number("notEqual", 20)}, ALL_IDS - {2}),
    ("number lessThan", {"count": number("lessThan", 10)}, frozenset({4, 5, 7})),
    ("number lessThanOrEqual", {"count": number("lessThanOrEqual", 10)}, frozenset({1, 4, 5, 7})),
    ("number greaterThan", {"count": number("greaterThan", 20)}, frozenset({3, 8})),
    ("number greaterThanOrEqual", {"count": number("greaterThanOrEqual", 20)}, frozenset({2, 3, 8})),
    ("number inRange includes both ends", {"count": number("inRange", 10, 30)}, frozenset({1, 2, 3})),
    ("number inRange takes its ends in either order", {"count": number("inRange", 30, 10)}, frozenset({1, 2, 3})),
    ("number inRange of one value", {"count": number("inRange", 2.5, 2.5)}, frozenset({7})),
    ("number blank", {"count": number("blank")}, frozenset({6})),
    ("number notBlank", {"count": number("notBlank")}, ALL_IDS - {6}),
    ("number filter on the key", {"id": number("greaterThan", 6)}, frozenset({7, 8})),
    # the extreme 64-bit integers, which a float column cannot hold, and a float no 64-bit integer reaches
    ("number lessThan the largest 64-bit integer, on a float", {"count": number("lessThan", 2**63 - 1)}, ALL_IDS - {6}),
    ("number notEqual an integer a float cannot hold", {"count": number("notEqual", 2**53 + 1)}, ALL_IDS),
    ("number lessThan the smallest 64-bit integer", {"id": number("lessThan", -(2**63))}, frozenset()),
    ("number greaterThan a float below every integer", {"id": number("greaterThan", -1e300)}, ALL_IDS),
    # date (by day; a value without an offset is UTC)
    ("date equals a UTC day", {"at": date("equals", "2026-01-01")}, frozenset({1, 2})),
    ("date equals in AG Grid's format", {"at": date("equals", "2026-01-01 00:00:00")}, frozenset({1, 2})),
    ("date equals a local day sent in UTC", {"at": date("equals", "2026-01-01T06:00:00.000Z")}, frozenset({2, 7})),
    ("date equals a local day with its offset", {"at": date("equals", "2026-01-01T00:00:00-06:00")}, frozenset({2, 7})),
    ("date lessThan is before the day", {"at": date("lessThan", "2026-01-01")}, frozenset({6})),
    ("date greaterThan is after the day", {"at": date("greaterThan", "2026-01-02")}, frozenset({4, 8})),
    ("date inRange includes both days", {"at": date("inRange", "2026-01-01", "2026-01-02")}, frozenset({1, 2, 3, 7})),
    ("date inRange in either order", {"at": date("inRange", "2026-01-02", "2026-01-01")}, frozenset({1, 2, 3, 7})),
    ("date equals the last valid day", {"at": date("equals", "9999-12-30")}, frozenset()),
    ("date lessThan the first valid moment", {"at": date("lessThan", "0001-01-01T00:00:00Z")}, frozenset()),
    ("date greaterThan the first valid day", {"at": date("greaterThan", "0001-01-01")}, ALL_IDS - {5}),
    ("date blank", {"at": date("blank")}, frozenset({5})),
    ("date notBlank", {"at": date("notBlank")}, ALL_IDS - {5}),
    # two conditions, and several fields
    (
        "number AND",
        {"count": combined("number", "AND", number("greaterThan", 0), number("lessThan", 30))},
        frozenset({1, 2, 7}),
    ),
    (
        "number OR",
        {"count": combined("number", "OR", number("lessThan", 0), number("greaterThan", 50))},
        frozenset({4, 8}),
    ),
    (
        "text OR",
        {"name": combined("text", "OR", text("startsWith", "alpha"), text("endsWith", "%"))},
        frozenset({1, 2, 3}),
    ),
    (
        "date AND",
        {"at": combined("date", "AND", date("greaterThan", "2025-12-31"), date("lessThan", "2026-01-03"))},
        frozenset({1, 2, 3, 7}),
    ),
    (
        "conditions without filterType",
        {
            "count": {
                "filterType": "number",
                "operator": "AND",
                "conditions": [{"type": "greaterThan", "filter": 0}, {"type": "lessThan", "filter": 30}],
            }
        },
        frozenset({1, 2, 7}),
    ),
    ("a model without filterType", {"name": {"type": "contains", "filter": "alpha"}}, frozenset({1, 2})),
    (
        "several fields are ANDed",
        {"name": text("contains", "a"), "count": number("greaterThan", 15)},
        frozenset({2, 3, 8}),
    ),
    ("an empty filter model", {}, ALL_IDS),
]


NUL = "\x00"
LONE = "\ud800"  # a lone surrogate (a JSON "\ud800"): UTF-8 cannot encode it

REFUSED_TEXT_CASES: list[tuple[str, dict[str, Any], list[str | int]]] = [
    # A NUL or a lone surrogate anywhere in a filter is a 422 (invalid_text) at its place, under ["query", "filter"]:
    # SQLite's LIKE stops at a NUL (contains "a\x00b" matched what contains "a" does), and a lone surrogate cannot be
    # encoded for SQLite at all. A field name is shown escaped.
    *[
        (f"text {operator} with a {what}", {"name": text(operator, f"a{bad}b")}, ["name", "filter"])
        for operator in ("contains", "notContains", "equals", "notEqual", "startsWith", "endsWith")
        for what, bad in (("NUL", NUL), ("lone surrogate", LONE))
    ],
    ("a lone first half of a pair", {"name": text("startsWith", "I\ud83d")}, ["name", "filter"]),
    ("a lone second half", {"name": text("equals", "\udfff")}, ["name", "filter"]),
    (
        "a NUL in the second of two conditions",
        {"name": combined("text", "OR", text("blank"), text("endsWith", NUL))},
        ["name", "conditions", 1, "filter"],
    ),
    ("a NUL in a field name", {f"name{NUL}": text("blank")}, ["name\\u0000"]),
    ("a lone surrogate as a field name", {LONE: text("blank")}, ["\\ud800"]),
    ("a NUL in a date", {"at": date("equals", f"2026-01-01{NUL}")}, ["at", "dateFrom"]),
    ("a lone surrogate in an operator", {"count": {"type": f"equals{LONE}", "filter": 1}}, ["count", "type"]),
    ("a NUL in a filterType", {"count": {"filterType": NUL, "type": "blank"}}, ["count", "filterType"]),
]
