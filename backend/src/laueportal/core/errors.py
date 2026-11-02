"""Problem details (RFC 9457) for every API error.

Services and endpoints raise ``laueportal.core.problems.ProblemError`` (that module has no FastAPI
dependency, so services can raise it). ``install_error_handlers(app)`` maps:

- ``ProblemError`` -> its status and body;
- ``RequestValidationError`` -> 422 with one ``errors[]`` item per problem. ``loc`` is relative to the
  JSON body for body fields (``["rows", 0, "scan_points"]``) and starts with the parameter's location
  otherwise (``["query", "limit"]``). A missing or malformed (not JSON) body has ``loc`` ``[]``;
- FastAPI's 400 for a JSON body it cannot decode (bytes that are not UTF-8, UTF-16 or UTF-32 text, nesting deeper
  than the decoder's recursion limit, an integer too long to convert) -> the same 422 as a malformed body
  (``json_invalid``), for every route with a JSON body;
- Starlette's 400 for a form it cannot parse (malformed multipart, a field over 1 MB, too many fields or files) ->
  422 ``form_invalid`` with ``loc`` ``[]`` (the scan-log upload);
- ``HTTPException`` under ``/api`` (unknown path 404, wrong method 405, ...) -> problem+json; other
  paths keep FastAPI's default handling (the SPA raises ``ProblemError`` for a missing file, so that
  404 is problem+json too);
- any other exception -> 500 whose ``instance`` (``urn:uuid:...``) is logged with the traceback.

Every problem response has the media type ``application/problem+json`` and goes through
``core.problems.sendable``: its texts are bounded and escaped, and it lists at most ``MAX_PROBLEM_ITEMS`` errors, so
an answer never grows with the request and can always be sent.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from laueportal.core.problems import Problem, ProblemError, ProblemItem, field_message, problems_detail, sendable

logger = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def problem_response(problem: Problem, headers: Mapping[str, str] | None = None) -> JSONResponse:
    members = sendable(problem).model_dump(mode="json")
    return JSONResponse(
        {name: value for name, value in members.items() if value is not None},  # an extension member's own nulls stay
        status_code=problem.status,
        headers=dict(headers) if headers else None,
        media_type=PROBLEM_JSON,
    )


def _json_invalid_message(error: Mapping[str, Any]) -> str:
    reason = (error.get("ctx") or {}).get("error")
    position = next((part for part in error.get("loc", ())[1:] if isinstance(part, int)), None)
    message = "The request body is not valid JSON"
    if reason:
        message += f": {reason}"
    if position is not None:
        message += f" (at character {position})"
    return message


def validation_problem(errors: Sequence[Any]) -> Problem:
    """422 problem from pydantic/FastAPI error dicts (``loc``, ``msg``, ``type``), each message in plain words
    (``core.problems.field_message``: ``"Required"``, ``"Must be a whole number"``; a form shows it under its field).

    A malformed JSON body is about the whole body: FastAPI reports it at ``("body", <character
    position>)``, which would read as list index 1 once ``"body"`` is dropped, so its ``loc`` is ``[]``
    (as for a missing body) and the message names the position.
    """
    items = []
    for error in errors:
        loc = list(error.get("loc", ()))
        message = field_message(error)
        code = str(error.get("type", "invalid"))
        if code == "json_invalid" and loc[:1] == ["body"]:
            loc, message = [], _json_invalid_message(error)
        elif loc[:1] == ["body"]:
            loc = loc[1:]
        items.append(
            ProblemItem(loc=[part if isinstance(part, int) else str(part) for part in loc], message=message, code=code)
        )
    return Problem(title="Validation failed", status=422, detail=problems_detail(len(items)), errors=items)


def is_api_path(scope: Scope) -> bool:
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :]
    return path == "/api" or path.startswith("/api/")


async def _problem_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ProblemError)
    return problem_response(exc.problem, exc.headers)


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    return problem_response(validation_problem(exc.errors()))


def undecodable_body_problem(exc: StarletteHTTPException) -> Problem | None:
    """The 422 for a JSON body FastAPI could not decode, else None. FastAPI answers a ``JSONDecodeError`` with its
    own 422 (``json_invalid``) but any other error of the decoder with a 400 "There was an error parsing the body",
    raised from that error: ``UnicodeDecodeError`` (not UTF-8, UTF-16 or UTF-32 text), ``RecursionError`` (nested
    too deeply) or another ``ValueError`` (an integer of more than 4,300 digits). It is malformed input all the
    same."""

    cause = exc.__cause__
    if exc.status_code != 400 or not isinstance(cause, ValueError | RecursionError):
        return None
    if isinstance(cause, UnicodeDecodeError):
        reason = f"it is not UTF-8 text ({cause.reason} at byte {cause.start})"
    elif isinstance(cause, RecursionError):
        reason = "it is nested too deeply"
    else:
        reason = str(cause).split(";", 1)[0]
    return validation_problem(
        [{"type": "json_invalid", "loc": ("body",), "msg": "JSON decode error", "ctx": {"error": reason}}]
    )


def unparsable_form_problem(exc: StarletteHTTPException) -> Problem | None:
    """The 422 for a form Starlette could not parse, else None: it answers a ``MultiPartException`` (malformed
    multipart, a field over 1 MB, too many fields or files) with a 400 holding the parser's message. It is malformed
    input, as an undecodable JSON body is."""

    if exc.status_code != 400 or not isinstance(exc.__context__, MultiPartException):
        return None
    item = ProblemItem(loc=[], message=f"The request body is not a valid form: {exc.detail}", code="form_invalid")
    return Problem(title="Validation failed", status=422, detail=problems_detail(1), errors=[item])


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 400 and isinstance(exc.__cause__, ProblemError):  # raised while FastAPI read the body
        return problem_response(exc.__cause__.problem, exc.__cause__.headers)  # (core.api.BodyLimitMiddleware)
    if (undecodable := undecodable_body_problem(exc) or unparsable_form_problem(exc)) is not None:
        return problem_response(undecodable)
    if not is_api_path(request.scope):
        return await http_exception_handler(request, exc)
    if exc.status_code < 200 or exc.status_code in (204, 205, 304):  # no body allowed
        return Response(status_code=exc.status_code, headers=exc.headers)
    title = _phrase(exc.status_code)
    detail = None if exc.detail in (None, "", title) else str(exc.detail)
    return problem_response(Problem(title=title, status=exc.status_code, detail=detail), exc.headers)


class UnhandledErrorMiddleware:
    """Turns an unexpected exception into a 500 problem with an error id, logged once with its traceback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            instance = f"urn:uuid:{uuid.uuid4()}"
            logger.error("unhandled error %s on %s %s", instance, scope.get("method"), scope.get("path"), exc_info=exc)
            if response_started:  # too late for a problem response; the server drops the connection
                raise
            problem = Problem(
                title="Internal Server Error",
                status=500,
                detail=f"Unexpected error. The server log has the details under {instance}.",
                instance=instance,
            )
            await problem_response(problem)(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    """Register the problem+json handlers. Call it after adding the other middleware (the 500 layer goes outside
    them), but for one that only adds headers (``app.SecurityHeadersMiddleware``)."""
    app.add_exception_handler(ProblemError, _problem_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_middleware(UnhandledErrorMiddleware)


# ---------------------------------------------------------------------------
# OpenAPI: document validation failures as what the handler returns.

_REF = "#/components/schemas/"


def use_problem_responses(openapi: dict[str, Any]) -> None:
    """Replace FastAPI's automatic ``422 HTTPValidationError`` responses with problem+json ``Problem``, and type
    every error response a route documents with a model (``responses={409: {"model": ...}}``, which FastAPI puts
    under ``application/json``) as the problem+json it is.

    ``Problem`` and ``ProblemItem`` are added to the components only when an operation refers to them.
    """
    replaced = False
    for path_item in openapi.get("paths", {}).values():
        for operation in path_item.values():
            responses = operation.get("responses", {}) if isinstance(operation, dict) else {}
            for status, response in responses.items():
                content = response.get("content", {})
                if str(status)[:1] in "45" and "application/json" in content:
                    content[PROBLEM_JSON] = content.pop("application/json")
            if f"{_REF}HTTPValidationError" in json.dumps(responses.get("422", {})):
                responses["422"] = {
                    "description": "Validation failed",
                    "content": {PROBLEM_JSON: {"schema": {"$ref": f"{_REF}Problem"}}},
                }
                replaced = True
    if not replaced:
        return
    schemas: dict[str, Any] = openapi.setdefault("components", {}).setdefault("schemas", {})
    problem_schema = Problem.model_json_schema(ref_template=_REF + "{model}", mode="serialization")
    schemas.update(problem_schema.pop("$defs", {}))
    schemas["Problem"] = problem_schema
    for name in ("HTTPValidationError", "ValidationError"):  # FastAPI's; drop them once nothing refers to them
        if name in schemas and f'"{_REF}{name}"' not in json.dumps(openapi):
            del schemas[name]
