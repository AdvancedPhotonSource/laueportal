"""problem+json shapes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from laueportal.app import create_app
from laueportal.core.errors import PROBLEM_JSON, problem_response
from laueportal.core.problems import (
    MAX_LOC_PART_LENGTH,
    MAX_LOC_PARTS,
    MAX_MESSAGE_LENGTH,
    MAX_PROBLEM_ITEMS,
    MAX_TEXT_LENGTH,
    Problem,
    ProblemError,
    ProblemItem,
    field_message,
    sendable,
)
from tests.api_support import PORTAL_HEADERS
from tests.settings_support import make_settings

router = APIRouter()


class Item(BaseModel):
    name: str
    count: int


@router.get("/items/{item_id}", name="getTestItem", response_model=Item)
def get_item(item_id: int, limit: int = Query(...)) -> Item:
    if item_id == 0:
        raise ProblemError(404, "Item not found", f"No item {item_id}", headers={"X-Probe": "yes"})
    if item_id == 1:
        raise ProblemError(
            409,
            "Conflict",
            errors=[
                ProblemItem(loc=["rows", 0, "name"], message="taken", code="duplicate"),
                {"loc": ["rows", 1], "message": "odd", "code": "odd", "severity": "warning"},
            ],
        )
    return Item(name="x", count=limit)


@router.post("/items", name="createTestItem", response_model=Item)
def create_item(item: Item) -> Item:
    return item


@router.get("/boom", name="getTestBoom", response_model=Item)
def boom() -> Item:
    raise RuntimeError("kaboom")


@router.get("/echo", name="getTestEcho", response_model=Item)
def echo(text: str = Query(...), count: int = Query(1)) -> Item:
    """A careless endpoint: its problem repeats the request's text ``count`` times, in every field."""
    raise ProblemError(
        400,
        f"Bad {text}",
        f"Got {text}" * count,
        errors=[{"loc": ["query", text * count, *range(count)], "message": text * count, "code": "echo"}] * count,
    )


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    app = create_app(make_settings(tmp_path))
    app.include_router(router, prefix="/api/v1/test")
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, headers=PORTAL_HEADERS)


def assert_problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["status"] == status and body["type"] == "about:blank" and body["title"]
    return body


def test_query_validation_error(client: TestClient) -> None:
    body = assert_problem(client.get("/api/v1/test/items/5?limit=abc"), 422)
    assert body["title"] == "Validation failed"
    assert body["detail"] == "1 problem"
    assert body["errors"] == [
        {
            "loc": ["query", "limit"],
            "message": "Must be a whole number",
            "code": "int_parsing",
            "severity": "error",
        }
    ]


class Fields(BaseModel):
    """One field per pydantic error type a form field can produce."""

    whole: int = 0
    number: float = 0
    flag: bool = False
    text: str = ""
    positive: int = Field(0, gt=0)
    at_least: float = Field(0, ge=1)
    below: int = Field(0, lt=10)
    at_most: float = Field(0, le=1)
    entries: list[str] = Field(default_factory=lambda: ["x"], min_length=1)
    few: list[str] = Field(default_factory=list, max_length=2)
    finite: float = Field(0, allow_inf_nan=False)
    choice: Literal["a", "b"] = "a"
    required: int


@pytest.mark.parametrize(
    ("field", "value", "code", "message"),
    [
        ("required", None, "missing", "Required"),
        ("whole", "abc", "int_parsing", "Must be a whole number"),
        ("whole", [1], "int_type", "Must be a whole number"),
        ("whole", 2.5, "int_from_float", "Must not have a fractional part"),
        ("number", "abc", "float_parsing", "Must be a number"),
        ("number", [1], "float_type", "Must be a number"),
        ("finite", "inf", "finite_number", "Must be a finite number"),
        ("flag", "maybe", "bool_parsing", "Must be true or false"),
        ("flag", [1], "bool_type", "Must be true or false"),
        ("text", 5, "string_type", "Must be text"),
        ("entries", "x", "list_type", "Must be a list"),
        ("positive", 0, "greater_than", "Must be greater than 0"),
        ("at_least", 0.5, "greater_than_equal", "Must be at least 1"),
        ("below", 10, "less_than", "Must be less than 10"),
        ("at_most", 2, "less_than_equal", "Must be at most 1"),
        ("entries", [], "too_short", "Must have at least one entry"),
        ("few", ["a", "b", "c"], "too_long", "Must have at most 2 entries"),
        ("choice", "c", "literal_error", "Must be 'a' or 'b'"),
    ],
)
def test_a_field_message_is_plain_and_does_not_name_the_field(field: str, value: Any, code: str, message: str) -> None:
    """``core.problems.field_message``: pydantic's types in plain words, without the field's label (a form's summary
    adds it once); every 422 problem's messages and the create forms' issues use it."""

    values = {"required": 1, field: value}
    if value is None:
        del values[field]
    with pytest.raises(ValidationError) as raised:
        Fields.model_validate(values)
    [error] = raised.value.errors()
    assert (error["type"], field_message(error)) == (code, message)


def test_a_field_message_keeps_a_models_own_wording_and_otherwise_pydantics_in_plain_words() -> None:
    assert field_message(
        {"type": "value_error", "msg": "Value error, Bad x", "ctx": {"error": ValueError("Bad x")}}
    ) == ("Bad x")
    assert field_message({"type": "dict_type", "msg": "Input should be a valid dictionary"}) == (
        "Must be a valid dictionary"
    )


def test_body_validation_errors_are_located_relative_to_the_body(client: TestClient) -> None:
    body = assert_problem(client.post("/api/v1/test/items", json={"count": "many"}), 422)
    assert body["detail"] == "2 problems"
    assert {(tuple(error["loc"]), error["code"]) for error in body["errors"]} == {
        (("name",), "missing"),
        (("count",), "int_parsing"),
    }


def test_missing_and_malformed_bodies(client: TestClient) -> None:
    body = assert_problem(client.post("/api/v1/test/items"), 422)
    assert body["errors"][0]["loc"] == [] and body["errors"][0]["code"] == "missing"
    body = assert_problem(
        client.post("/api/v1/test/items", content=b"{not json", headers={"content-type": "application/json"}), 422
    )
    # FastAPI locates a JSON syntax error at ("body", <character position>); that is not a field (no [1]).
    assert body["errors"] == [
        {
            "loc": [],
            "message": "The request body is not valid JSON: Expecting property name enclosed in double quotes "
            "(at character 1)",
            "code": "json_invalid",
            "severity": "error",
        }
    ]
    body = assert_problem(
        client.post("/api/v1/test/items", content=b'{"name": "x",', headers={"content-type": "application/json"}), 422
    )
    [error] = body["errors"]
    assert error["loc"] == [] and error["message"].startswith("The request body is not valid JSON: ")
    assert "(at character 13)" in error["message"]


def test_problem_error(client: TestClient) -> None:
    response = client.get("/api/v1/test/items/0?limit=1")
    assert assert_problem(response, 404) == {
        "type": "about:blank",
        "title": "Item not found",
        "status": 404,
        "detail": "No item 0",
    }
    assert response.headers["x-probe"] == "yes"


def test_problem_error_with_items(client: TestClient) -> None:
    body = assert_problem(client.get("/api/v1/test/items/1?limit=1"), 409)
    assert body["errors"] == [
        {"loc": ["rows", 0, "name"], "message": "taken", "code": "duplicate", "severity": "error"},
        {"loc": ["rows", 1], "message": "odd", "code": "odd", "severity": "warning"},
    ]


def test_unknown_api_path_and_wrong_method(client: TestClient) -> None:
    body = assert_problem(client.get("/api/v1/nope"), 404)
    assert body == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "No API endpoint at /api/v1/nope",
    }
    assert_problem(client.delete("/api/v2/anything"), 404)
    response = client.post("/api/v1/health")
    assert assert_problem(response, 405)["title"] == "Method Not Allowed"
    assert response.headers["allow"] == "GET"


def test_unexpected_error_is_a_500_problem_with_a_logged_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="laueportal.core.errors"):
        body = assert_problem(client.get("/api/v1/test/boom"), 500)
    assert body["title"] == "Internal Server Error"
    assert body["instance"].startswith("urn:uuid:")
    assert body["instance"] in body["detail"]
    assert "kaboom" not in str(body)  # no internals in the response
    [record] = [r for r in caplog.records if body["instance"] in r.getMessage()]
    assert record.exc_info is not None and "kaboom" in str(record.exc_info[1])


def test_openapi_documents_validation_failures_as_problems(app: FastAPI) -> None:
    document = app.openapi()
    response_422 = document["paths"]["/api/v1/test/items/{item_id}"]["get"]["responses"]["422"]
    assert response_422["content"] == {PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/Problem"}}}
    schemas = document["components"]["schemas"]
    assert {"Problem", "ProblemItem"} <= set(schemas)
    assert "HTTPValidationError" not in schemas and "ValidationError" not in schemas
    assert schemas["Problem"]["properties"]["errors"]["anyOf"][0]["items"] == {
        "$ref": "#/components/schemas/ProblemItem"
    }


# ---------------------------------------------------------------------------
# Every problem answer is bounded and can be sent (core.problems.sendable)


def test_a_problem_that_echoes_a_request_stays_small(client: TestClient) -> None:
    response = client.get("/api/v1/test/echo", params={"text": "x" * 50_000, "count": 40})
    body = assert_problem(response, 400)
    assert len(response.content) < 40_000  # it echoed 80 MB before the bounds
    assert len(body["title"]) == len(body["detail"].split(" (")[0]) == MAX_TEXT_LENGTH
    assert body["detail"].endswith(f"(the first {MAX_PROBLEM_ITEMS} of 40 are listed)")
    assert len(body["errors"]) == MAX_PROBLEM_ITEMS
    for error in body["errors"]:
        assert len(error["message"]) == MAX_MESSAGE_LENGTH and error["message"].endswith("…")
        assert len(error["loc"]) == MAX_LOC_PARTS and error["loc"][-1] == "…"
        assert len(error["loc"][1]) == MAX_LOC_PART_LENGTH


def test_a_problem_holding_a_nul_or_a_lone_surrogate_is_still_sent() -> None:
    """A message may repeat what UTF-8 cannot encode; the answer shows it as a JSON escape instead of failing."""
    with pytest.raises(UnicodeEncodeError):
        "\ud800".encode("utf-8")
    problem = ProblemError(
        422, "Bad \ud800", "detail \udfff", errors=[{"loc": ["x\ud800", 0], "message": "m\x00", "code": "c"}]
    )
    body = json.loads(bytes(problem_response(problem.problem).body))
    assert body == {
        "type": "about:blank",
        "title": "Bad \\ud800",
        "status": 422,
        "detail": "detail \\udfff",
        "errors": [{"loc": ["x\\ud800", 0], "message": "m\\u0000", "code": "c", "severity": "error"}],
    }


def test_sendable_keeps_a_small_problem_as_it_is() -> None:
    problem = Problem(
        title="Validation failed",
        status=422,
        detail="2 problems",
        errors=[
            ProblemItem(loc=["query", "filter", "author", "conditions", 1, "filter"], message="m", code="c"),
            ProblemItem(loc=[], message="whole body", code="json_invalid", severity="warning"),
        ],
    )
    assert sendable(problem) == problem
    many = problem.model_copy(update={"errors": [problem.errors[0]] * MAX_PROBLEM_ITEMS})  # type: ignore[index]
    assert sendable(many) == many
    listed = sendable(problem.model_copy(update={"detail": None, "errors": [problem.errors[1]] * 30}))  # type: ignore[index]
    assert listed.detail == f"The first {MAX_PROBLEM_ITEMS} of 30 are listed."
