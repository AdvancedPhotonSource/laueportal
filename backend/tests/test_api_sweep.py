"""Every route of the API against hostile requests.

The routes come from the app's own OpenAPI document, checked against ``app.routes``, so a route added later is
swept too. For each route:

- **Text**: a NUL or a lone surrogate in any string input is a 422 problem that locates it (code ``invalid_text``),
  never a 500 or a wrong answer: each path and query parameter, an unknown query parameter's name, each string of a JSON
  body and a body member's name, and the JSON of a grid's ``filter`` (a field name and a value). A URL cannot carry a
  lone surrogate (its percent-encoded bytes decode to U+FFFD), so in a parameter it comes as a JSON escape
  (``"\\ud800"``) inside JSON text; a NUL comes as ``%00``.
- **Size**: a value of 60,000 characters (about uvicorn's longest URL; letters, and digits: a number longer than Python
  converts), JSON nested deeper than Python decodes, and a sort of 8 KB of commas give no 5xx, and every problem answer
  stays small (``SMALL_ANSWER``).
- **Cross-site requests**: every route but the GET ones refuses a request without ``X-Laue-Portal: 1``, or from a page
  on another origin (``Sec-Fetch-Site``), with a 403 problem, also in the two shapes such a page can send without a CORS
  preflight (a form and a ``no-cors`` fetch). With the header, from the portal's own origin or from a script, it gets
  through. GET routes need nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from laueportal.app import create_app, openapi_document
from laueportal.core.api import BODY_TOO_LARGE_TITLE, CROSS_SITE_TITLE, JSON_BODY_LIMIT, SAFE_METHODS, WRITE_HEADER
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.paging import GRID_FIELDS_KEY
from laueportal.core.text import INVALID_TEXT
from tests.api_support import PORTAL_HEADERS
from tests.indexing.test_viewer import add_run, converted
from tests.settings_support import make_database

NUL = "\x00"
LONE = "\ud800"  # a lone surrogate: half of a character, which UTF-8 cannot encode
URL_LONE = "%ED%A0%80"  # U+D800's bytes, as a URL can carry them; the server decodes them as U+FFFD
LONG = 60_000
DIGITS = "9" * LONG
"""A number longer than Python converts (4,300 digits): an ID or a count must be bounded before it is converted."""
DEEP = 20_000
"""Deeper than ``json.loads`` decodes (about 9,000 here), and still a URL httpx and uvicorn take (40 KB)."""
SMALL_ANSWER = 8192
"""The largest problem answer, in bytes, that a hostile request may get."""

DOCUMENT = openapi_document()
_SCHEMAS: dict[str, Any] = DOCUMENT["components"]["schemas"]


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    operation_id: str
    parameters: tuple[tuple[str, str, str], ...]
    """``(in, name, a value the schema accepts)`` of each path, query and header parameter."""
    body: dict[str, Any] | None
    """The JSON body's schema."""

    @property
    def writes(self) -> bool:
        return self.method not in SAFE_METHODS

    def url(self, path_values: dict[str, str] | None = None, query: str = "") -> str:
        values = {name: valid for where, name, valid in self.parameters if where == "path"}
        values.update(path_values or {})
        url = self.path
        for name, value in values.items():
            url = url.replace(f"{{{name}}}", value)
        return f"{url}?{query}" if query else url

    def request(
        self, client: TestClient, *, path: dict[str, str] | None = None, query: str = "", **kwargs: Any
    ) -> httpx.Response:
        if self.body is not None and "content" not in kwargs:
            kwargs["content"] = json.dumps(example(self.body))
            kwargs["headers"] = {"content-type": "application/json", **kwargs.get("headers", {})}
        return client.request(self.method, self.url(path, query), **kwargs)


def _resolve(schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = _SCHEMAS[schema["$ref"].rsplit("/", 1)[1]]
    return schema


def _valid_parameter(schema: dict[str, Any]) -> str:
    """A parameter's value the schema accepts, so that a request reaches the route's handler: its first enum value
    (a path's ``{kind}``), ``1`` for a number, else ``x``."""
    value = example(schema)
    return value if isinstance(value, str) else "1"


def example(schema: dict[str, Any]) -> Any:
    """A value the schema accepts (the first enum value, one-item lists, every property)."""
    schema = _resolve(schema)
    if "anyOf" in schema:
        return example(next(option for option in schema["anyOf"] if _resolve(option).get("type") != "null"))
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {name: example(member) for name, member in schema.get("properties", {}).items()}
    if kind == "array":
        return [example(schema["items"])]
    return {"integer": 1, "number": 1.5, "boolean": False}.get(kind, "x")


def string_locations(schema: dict[str, Any], loc: tuple[str | int, ...] = ()) -> Iterator[tuple[str | int, ...]]:
    """The location of every string in ``example(schema)`` (enum values and list items included)."""
    schema = _resolve(schema)
    if "anyOf" in schema:
        yield from string_locations(
            next(option for option in schema["anyOf"] if _resolve(option).get("type") != "null"), loc
        )
    elif schema.get("type") == "object":
        for name, member in schema.get("properties", {}).items():
            yield from string_locations(member, (*loc, name))
    elif schema.get("type") == "array":
        yield from string_locations(schema["items"], (*loc, 0))
    elif schema.get("type", "string") == "string":
        yield loc


def replaced(value: Any, loc: tuple[str | int, ...], new: Any) -> Any:
    if not loc:
        return new
    copy = dict(value) if isinstance(value, dict) else list(value)
    copy[loc[0]] = replaced(copy[loc[0]], loc[1:], new)  # type: ignore[index]
    return copy


def _operations() -> list[Operation]:
    found = []
    for path, item in DOCUMENT["paths"].items():
        for method, operation in item.items():
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            parameters = tuple(
                (parameter["in"], parameter["name"], _valid_parameter(parameter.get("schema", {})))
                for parameter in operation.get("parameters", [])
            )
            found.append(Operation(method.upper(), path, operation["operationId"], parameters, body))
    return found


OPERATIONS = _operations()
WRITES = [operation for operation in OPERATIONS if operation.writes]
READS = [operation for operation in OPERATIONS if not operation.writes]
PARAMETERS = [
    (operation, where, name) for operation in OPERATIONS for where, name, _ in operation.parameters if where != "header"
]
"""Each path and query parameter. A header parameter (a create's ``Idempotency-Key``) cannot carry a NUL or a lone
surrogate (its value is bytes, and the server refuses a control character in it before the app sees it), so it is
swept for its size only (``HEADERS``)."""
HEADERS = [
    (operation, name) for operation in OPERATIONS for where, name, _ in operation.parameters if where == "header"
]
BODIES = [operation for operation in OPERATIONS if operation.body is not None]
BODY_STRINGS = [(operation, loc) for operation in BODIES for loc in string_locations(operation.body or {})]
GRIDS = [operation for operation in OPERATIONS if {"filter", "sort"} <= {name for _, name, _ in operation.parameters}]
GRID_TEXT_FIELDS = {
    "listScans": "sample_name",
    "listJobs": "author",
    "listReconstructionRuns": "author",
    "listIndexingRuns": "author",
    "listIndexingPeaks": "frame_id",
    "listIndexingPatterns": "frame_id",
}
"""A text field each grid list filters on."""


def _id(value: Any) -> str:
    if isinstance(value, Operation):
        return value.operation_id
    if isinstance(value, tuple):
        return ".".join(str(part) for part in value) or "body"
    return str(value)


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FastAPI]:
    """The API on a database holding one indexing run with results (I1, so the results' grid lists answer), with
    one file root: the guards run before any lookup."""
    tmp: Path = tmp_path_factory.mktemp("sweep")
    (tmp / "data").mkdir()
    settings, engine = make_database(tmp, files={"roots": {"data": tmp / "data"}})
    directory = tmp / "data" / "index_1"
    directory.mkdir()
    converted(directory)
    add_run(engine, 1, directory)
    yield create_app(settings)
    engine.dispose()


@pytest.fixture(scope="module")
def client(app: FastAPI) -> Iterator[TestClient]:
    """The portal's own pages: they send ``X-Laue-Portal: 1``. A 500 is answered, not raised."""
    with TestClient(app, raise_server_exceptions=False, headers=PORTAL_HEADERS) as client:
        yield client


@pytest.fixture(scope="module")
def foreign(app: FastAPI) -> Iterator[TestClient]:
    """A client without the portal's header: a script, or a browser on another origin."""
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text[:500]
    assert response.headers["content-type"] == PROBLEM_JSON
    assert len(response.content) <= SMALL_ANSWER, len(response.content)
    body = response.json()
    assert body["status"] == status
    return body


def invalid_text_locs(response: httpx.Response) -> list[list[str | int]]:
    body = problem(response, 422)
    assert {item["code"] for item in body["errors"]} == {INVALID_TEXT}, body
    return [item["loc"] for item in body["errors"]]


def no_server_error(response: httpx.Response) -> None:
    assert response.status_code < 500, response.text[:500]
    if response.status_code >= 400:
        assert response.headers["content-type"] == PROBLEM_JSON, response.text[:200]
        assert len(response.content) <= SMALL_ANSWER, (response.status_code, len(response.content))


# ---------------------------------------------------------------------------
# Every route is swept


def test_the_sweep_covers_every_route_of_the_app(app: FastAPI) -> None:
    routes = set()
    for context in iter_route_contexts(app.routes):
        if isinstance(context.original_route, APIRoute):
            assert context.include_in_schema, f"{context.path} is not in the OpenAPI document, so not swept"
            routes |= {(method, context.path) for method in context.methods or ()}
    assert routes == {(operation.method, operation.path) for operation in OPERATIONS}
    # What the sweep reached when it was written (a new route joins it by itself) ...
    assert len(OPERATIONS) >= 16 and len(WRITES) >= 4 and len(BODY_STRINGS) >= 6
    # ... and a new grid list names a text field it filters on, for the filter's own test.
    assert {operation.operation_id for operation in GRIDS} == set(GRID_TEXT_FIELDS)


# ---------------------------------------------------------------------------
# Text: NUL and lone surrogates


@pytest.mark.parametrize(("operation", "where", "name"), PARAMETERS, ids=_id)
def test_a_nul_in_any_parameter_is_a_422_located_there(
    client: TestClient, operation: Operation, where: str, name: str
) -> None:
    if where == "path":
        response = operation.request(client, path={name: "1%00"})
    else:
        response = operation.request(client, query=f"{name}=x%00y")
    assert invalid_text_locs(response) == [[where, name]]


@pytest.mark.parametrize("operation", OPERATIONS, ids=_id)
def test_a_nul_in_an_unknown_parameters_name_is_a_422_with_the_name_escaped(
    client: TestClient, operation: Operation
) -> None:
    assert invalid_text_locs(operation.request(client, query="a%00b=1")) == [["query", "a\\u0000b"]]


@pytest.mark.parametrize(("operation", "where", "name"), PARAMETERS, ids=_id)
def test_a_lone_surrogate_in_any_parameter_is_never_a_server_error(
    client: TestClient, operation: Operation, where: str, name: str
) -> None:
    """Percent-encoded, its bytes arrive as U+FFFD; as JSON escapes (a grid's filter), it is refused."""
    escaped = quote(json.dumps({LONE: [LONE]}), safe="")  # json.dumps writes the escape: {"\ud800": ["\ud800"]}
    for value in (URL_LONE, f"x{URL_LONE}y", escaped):
        if where == "path":
            no_server_error(operation.request(client, path={name: value}))
        else:
            no_server_error(operation.request(client, query=f"{name}={value}"))


@pytest.mark.parametrize("operation", GRIDS, ids=_id)
@pytest.mark.parametrize("bad", [NUL, LONE], ids=["NUL", "lone surrogate"])
def test_a_nul_or_lone_surrogate_in_a_grid_filter_is_a_422_located_there(
    client: TestClient, operation: Operation, bad: str
) -> None:
    field = GRID_TEXT_FIELDS[operation.operation_id]
    shown = "\\u0000" if bad == NUL else "\\ud800"
    for model, loc in (
        ({field: {"filterType": "text", "type": "contains", "filter": f"a{bad}b"}}, [field, "filter"]),
        ({field: {"type": "equals", "filter": bad}}, [field, "filter"]),
        (
            {field: {"operator": "OR", "conditions": [{"type": "blank"}, {"type": "startsWith", "filter": f"I{bad}"}]}},
            [
                field,
                "conditions",
                1,
                "filter",
            ],
        ),
        ({f"{field}{bad}": {"type": "blank"}}, [f"{field}{shown}"]),
        ({bad: {"type": "blank"}}, [shown]),
    ):
        query = "filter=" + quote(json.dumps(model), safe="")
        assert invalid_text_locs(operation.request(client, query=query)) == [["query", "filter", *loc]]


@pytest.mark.parametrize(("operation", "loc"), BODY_STRINGS, ids=_id)
@pytest.mark.parametrize("bad", [NUL, LONE], ids=["NUL", "lone surrogate"])
def test_a_nul_or_lone_surrogate_in_any_body_string_is_a_422_located_there(
    client: TestClient, operation: Operation, loc: tuple[str | int, ...], bad: str
) -> None:
    body = replaced(example(operation.body or {}), loc, f"x{bad}y")
    response = operation.request(client, content=json.dumps(body), headers={"content-type": "application/json"})
    assert invalid_text_locs(response) == [list(loc)]


@pytest.mark.parametrize("operation", BODIES, ids=_id)
def test_a_lone_surrogate_in_a_body_members_name_is_a_422_with_the_name_escaped(
    client: TestClient, operation: Operation
) -> None:
    body = {**example(operation.body or {}), LONE: "x"}
    response = operation.request(client, content=json.dumps(body), headers={"content-type": "application/json"})
    assert invalid_text_locs(response) == [["\\ud800"]]


# ---------------------------------------------------------------------------
# Size: nothing a request sends makes the answer grow


@pytest.mark.parametrize(("operation", "where", "name"), PARAMETERS, ids=_id)
def test_long_and_deep_parameters_give_small_answers_and_no_server_error(
    client: TestClient, operation: Operation, where: str, name: str
) -> None:
    for value in (
        "a" * LONG,
        DIGITS,
        "," * LONG,
        "[" * DEEP + "]" * DEEP,
        quote(json.dumps({"a" * LONG: 1}), safe=""),
    ):
        if where == "path":
            no_server_error(operation.request(client, path={name: value}))
        else:
            no_server_error(operation.request(client, query=f"{name}={value}"))
    no_server_error(operation.request(client, query=f"{'b' * LONG}=1"))


@pytest.mark.parametrize(("operation", "name"), HEADERS, ids=_id)
def test_long_header_parameters_give_small_answers_and_no_server_error(
    client: TestClient, operation: Operation, name: str
) -> None:
    for value in ("a" * LONG, DIGITS, "," * LONG, "[" * DEEP + "]" * DEEP):
        no_server_error(operation.request(client, headers={name: value}))


@pytest.mark.parametrize(("operation", "loc"), BODY_STRINGS, ids=_id)
def test_long_and_deep_body_values_give_small_answers_and_no_server_error(
    client: TestClient, operation: Operation, loc: tuple[str | int, ...]
) -> None:
    for value in ("a" * LONG, DIGITS, [[[]] * 3] * 1000, json.loads("[" * 5000 + "]" * 5000)):
        body = replaced(example(operation.body or {}), loc, value)
        no_server_error(
            operation.request(client, content=json.dumps(body), headers={"content-type": "application/json"})
        )


@pytest.mark.parametrize("operation", BODIES, ids=_id)
def test_a_body_that_cannot_be_decoded_is_a_422_as_malformed_json_is(client: TestClient, operation: Operation) -> None:
    """FastAPI answers these with a 400 of its own; the portal answers malformed input with a 422."""
    json_type = {"content-type": "application/json"}
    not_utf8 = operation.request(client, content=b'{"name": "caf\xe9"}', headers=json_type)  # Latin-1
    too_deep = operation.request(client, content="[" * 5 * DEEP + "]" * 5 * DEEP, headers=json_type)

    for response, reason in ((not_utf8, "it is not UTF-8 text"), (too_deep, "it is nested too deeply")):
        body = problem(response, 422)
        assert [(item["loc"], item["code"]) for item in body["errors"]] == [([], "json_invalid")]
        assert body["errors"][0]["message"].startswith(f"The request body is not valid JSON: {reason}")


@pytest.mark.parametrize("operation", BODIES, ids=_id)
def test_a_body_over_the_limit_is_a_413_unread_by_its_length_or_once_that_much_arrived(
    client: TestClient, foreign: TestClient, operation: Operation
) -> None:
    """At most ``JSON_BODY_LIMIT`` (1 MiB): a longer ``Content-Length`` is refused before a byte is received, a body
    without one (chunked) once more has arrived; without the portal's header, the 403 comes first (as the upload's)."""
    big = json.dumps({"notes": "a" * JSON_BODY_LIMIT}).encode()
    json_type = {"content-type": "application/json"}
    pulled: list[bytes] = []

    def body() -> Iterator[bytes]:
        pulled.append(big)
        yield big

    declared = operation.request(client, content=body(), headers={**json_type, "content-length": str(len(big))})
    chunked = operation.request(client, content=iter([big]), headers=json_type)
    cross_site = operation.request(foreign, content=body(), headers={**json_type, "content-length": str(len(big))})

    assert [problem(response, 413)["title"] for response in (declared, chunked)] == [BODY_TOO_LARGE_TITLE] * 2
    assert problem(cross_site, 403)["title"] == CROSS_SITE_TITLE
    assert pulled == []
    assert "413" in DOCUMENT["paths"][operation.path][operation.method.lower()]["responses"]


@pytest.mark.parametrize("operation", BODIES, ids=_id)
def test_a_body_with_many_long_names_is_no_server_error(client: TestClient, operation: Operation) -> None:
    many_names = json.dumps({f"name{index}" + "x" * 100: index for index in range(2000)})
    no_server_error(operation.request(client, content=many_names, headers={"content-type": "application/json"}))


@pytest.mark.parametrize("operation", GRIDS, ids=_id)
def test_a_grid_list_takes_every_sort_and_filter_it_publishes(client: TestClient, operation: Operation) -> None:
    """The frontend checks its grids against ``x-grid-fields`` (``src/features/grid-contract.test.ts``): every
    field published sortable sorts (all at once), every field published filterable filters with its type, and an
    unpublished field is refused."""
    fields: dict[str, dict[str, Any]] = DOCUMENT["paths"][operation.path]["get"][GRID_FIELDS_KEY]
    sortable = [name for name, field in fields.items() if field["sortable"]]
    filterable = {name: field["filterType"] for name, field in fields.items() if field["filterable"]}
    assert sortable and filterable
    response = operation.request(client, query="sort=" + quote(",".join(f"-{name}" for name in sortable)))
    assert response.status_code == 200, response.text[:500]
    model = {name: {"filterType": kind, "type": "notBlank"} for name, kind in filterable.items()}
    response = operation.request(client, query="filter=" + quote(json.dumps(model), safe=""))
    assert response.status_code == 200, response.text[:500]
    for name, field in fields.items():
        if not field["sortable"]:
            problem(operation.request(client, query=f"sort={name}"), 422)
    problem(operation.request(client, query="sort=no_such_field"), 422)


@pytest.mark.parametrize("operation", GRIDS, ids=_id)
def test_a_sort_of_8_kb_of_commas_is_a_small_422(client: TestClient, operation: Operation) -> None:
    """One small error, not one per empty item repeating the whole sort (65 MB for 8 KB of commas)."""
    for commas in (8 * 1024, 999, 3):
        body = problem(operation.request(client, query=f"sort={',' * commas}"), 422)
        assert len(body["errors"]) == 1 and body["errors"][0]["loc"] == ["query", "sort"], body
        assert len(json.dumps(body)) < 1024


# ---------------------------------------------------------------------------
# Cross-site requests

FOREIGN_ORIGIN = "http://127.0.0.1:2104"
FORM_POST = {  # what a browser sends for an auto-submitted form on a page of another origin
    "content-type": "application/x-www-form-urlencoded",
    "origin": FOREIGN_ORIGIN,
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
}
NO_CORS_FETCH = {  # fetch(url, {method: "POST", mode: "no-cors"}) from that page (custom headers are dropped)
    "content-type": "text/plain;charset=UTF-8",
    "origin": FOREIGN_ORIGIN,
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-dest": "empty",
}


def refused(response: httpx.Response) -> dict[str, Any]:
    body = problem(response, 403)
    assert body["title"] == CROSS_SITE_TITLE
    return body


@pytest.mark.parametrize("operation", WRITES, ids=_id)
def test_a_write_without_the_portal_header_is_refused(foreign: TestClient, operation: Operation) -> None:
    detail = refused(operation.request(foreign))["detail"]
    assert WRITE_HEADER in detail
    refused(operation.request(foreign, content=b"a=1", headers=FORM_POST))
    refused(operation.request(foreign, content=b"", headers=NO_CORS_FETCH))
    refused(operation.request(foreign, headers={WRITE_HEADER: "0"}))


@pytest.mark.parametrize("operation", WRITES, ids=_id)
@pytest.mark.parametrize("site", ["cross-site", "same-site", "Same-Site", "bogus"])
def test_a_write_from_another_origin_is_refused_even_with_the_header(
    client: TestClient, operation: Operation, site: str
) -> None:
    detail = refused(operation.request(client, headers={"sec-fetch-site": site, "origin": FOREIGN_ORIGIN}))["detail"]
    assert "another origin" in detail


@pytest.mark.parametrize("operation", WRITES, ids=_id)
@pytest.mark.parametrize("site", [None, "same-origin", "none"])
def test_a_write_with_the_header_from_the_portals_origin_or_a_script_gets_through(
    client: TestClient, operation: Operation, site: str | None
) -> None:
    headers = {} if site is None else {"sec-fetch-site": site}
    response = operation.request(client, headers=headers)
    no_server_error(response)
    assert response.status_code != 403, response.text


@pytest.mark.parametrize("operation", WRITES, ids=_id)
def test_the_api_grants_no_cors_preflight(foreign: TestClient, operation: Operation) -> None:
    """A page on another origin needs a preflight to send X-Laue-Portal: 1; the API refuses it (no CORS)."""
    response = foreign.options(
        operation.url(),
        headers={
            "origin": FOREIGN_ORIGIN,
            "access-control-request-method": operation.method,
            "access-control-request-headers": WRITE_HEADER.lower(),
        },
    )
    assert response.status_code == 405
    assert not [name for name in response.headers if name.lower().startswith("access-control-")]


@pytest.mark.parametrize("operation", OPERATIONS, ids=_id)
def test_the_contract_documents_the_header_and_the_403_on_every_write(operation: Operation) -> None:
    """A script author learns the rule from openapi.json: a 403 problem and the header, per write."""
    documented = DOCUMENT["paths"][operation.path][operation.method.lower()]
    if operation.writes:
        content = documented["responses"]["403"]["content"]
        assert content == {PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/Problem"}}}
        assert f"`{WRITE_HEADER}: 1`" in documented["description"]
    else:
        assert "403" not in documented["responses"]
        assert WRITE_HEADER not in documented.get("description", "")


@pytest.mark.parametrize("operation", READS, ids=_id)
def test_a_read_needs_no_header_even_from_another_origin(foreign: TestClient, operation: Operation) -> None:
    for headers in ({}, {"sec-fetch-site": "cross-site", "origin": FOREIGN_ORIGIN}):
        response = operation.request(foreign, headers=headers)
        no_server_error(response)
        assert response.status_code != 403, response.text
