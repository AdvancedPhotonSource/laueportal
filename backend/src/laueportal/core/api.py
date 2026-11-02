"""FastAPI dependencies shared by the domain ``api.py`` modules (the adapter over ``core.db``), and the two request
guards ``create_app`` puts in front of every route (``REQUEST_GUARDS``).

The app factory keeps the settings, engine and session factory on ``app.state``; endpoints receive
them through these annotated types::

    @router.get("/scans/{scan_id}", name="getScan", response_model=ScanDetail)
    def get_scan(scan_id: int, session: SessionDep) -> ScanDetail: ...

``SessionDep`` commits when the endpoint returns (before the response is sent) and rolls back if it
raises, so build the response model inside the endpoint, while the session is open. Its connection is held from
the first query until then, so a route that waits (a dataset load, a capped computation's turn:
docs/contributing/backend.md, "Long work") takes ``SessionsDep`` instead and reads what it needs in a short
``core.db.session_scope`` before it waits; ``turn`` is the wait for a capped computation.

The app's engine never creates the database file: a request that needs a missing database answers 503
(``core.db.DatabaseMissing``), and ``/health`` keeps reporting the database as down until ``laueportal db
upgrade`` (or the seed) creates it.

**The request guards** run before anything else of a route, in this order:

1. ``refuse_cross_site_writes``: a request that can change data (any method but GET, HEAD and OPTIONS) needs the
   header ``X-Laue-Portal: 1``, and a browser's ``Sec-Fetch-Site`` must say the portal's own origin. A page on
   another origin can make a browser send a form or a ``no-cors`` fetch without a CORS preflight, but never with a
   custom header (that needs a preflight, which the API never grants), so it cannot stop or reorder runs. A refusal
   is a 403 problem. ``src/api/fetcher.ts`` sends the header with every request; scripts must send it too.
2. ``check_request_text``: no NUL and no lone surrogate in any path or query parameter (name or value), anywhere
   in a JSON body, or in a form's text fields (names and values; the scan-log upload) (``core.text``); a 422 problem
   locates each one.

FastAPI reads a route's body before its dependencies run, so the guards see a parsed body. A route that takes an
upload checks the request before that (``scans.api.ScanLogUploadRoute``: the write guard, then the body's size);
every other body is bounded as it is read (``BodyLimitMiddleware``: the write guard, then ``JSON_BODY_LIMIT``, a
413).

``document_request_guards`` writes the first rule into the OpenAPI document: every operation that changes data
gets a 403 problem response and a note on the header in its description; and every operation with a JSON body its
413.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar

import anyio
from anyio import CapacityLimiter
from fastapi import Depends, Request, Response
from fastapi.params import Depends as DependsParam
from fastapi.params import Form as FormParam
from fastapi.routing import APIRoute
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from laueportal.core import db, etag
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.problems import Problem, ProblemError, ProblemItem, problems_detail
from laueportal.core.settings import Settings
from laueportal.core.text import INVALID_TEXT, escaped, invalid_strings, invalid_text, quoted

WRITE_HEADER = "X-Laue-Portal"
"""The header (with ``WRITE_HEADER_VALUE``) that every request changing data must carry."""
WRITE_HEADER_VALUE = "1"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""The methods that never change data, so need no ``WRITE_HEADER``."""
SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})
"""The ``Sec-Fetch-Site`` values of a request from the portal's own pages (``none``: the user's own action, such as
a bookmark); browsers send ``same-site`` or ``cross-site`` for a request from a page on another origin."""
CROSS_SITE_TITLE = "Cross-site request refused"
WRITE_GUARD_NOTE = (
    f"Needs the header `{WRITE_HEADER}: {WRITE_HEADER_VALUE}`, as every request that changes data (the portal's "
    "pages send it; scripts must too). Without it, or from a page on another origin (a `Sec-Fetch-Site` other than "
    "`same-origin` or `none`), the answer is a 403 problem and nothing changes."
)
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_db_session(request: Request) -> Iterator[Session]:
    yield from db.get_session(request.app.state.sessions)


def get_sessions(request: Request) -> sessionmaker[Session]:
    return request.app.state.sessions


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EngineDep = Annotated[Engine, Depends(get_engine)]
SessionDep = Annotated[Session, Depends(get_db_session, scope="function")]
SessionsDep = Annotated[sessionmaker[Session], Depends(get_sessions)]
"""The app's session factory, for a route that waits: it reads in a ``core.db.session_scope`` of its own, closed
before the wait, so a waiting request holds no database connection (the pool has 15)."""


# ---------------------------------------------------------------------------
# Turns of a capped computation (docs/contributing/backend.md, "Long work")

T = TypeVar("T")

DISCONNECT_POLL_SECONDS = 0.5
"""How often a request waiting for its turn (``turn``) checks that its client is still there."""
RETRY_AFTER_SECONDS = 10
"""The ``Retry-After`` of the 503 that ``turn`` answers while too many requests wait."""
CLIENT_CLOSED_REQUEST = 499
"""What a request dropped because its client had gone answers (nginx's code; nobody reads it)."""
FOLDER_READS_AT_ONCE = 4
"""Folder reads made at once (the app's ``folder_reads`` limiter, ``read_folders``): the file
browser's listing (``GET /files/browse``), Find Matching Files and Find Indices
(``POST /files/patterns``, ``/files/indices``: up to ``files.finder.MAX_FILES`` names), and the run forms'
validation and creation (``POST /{indexing,reconstruction}-runs/validate`` and ``/{indexing,reconstruction}-runs``),
which list the input folder (up to ``execution.max_run_inputs`` inputs). A listing holds a thread of the pool's 40
for up to a quarter of a second (a 200,000-file folder) plus its checks' wait on a mount that does not answer, so a
storm of them never takes the pool. A user asks one at a time: the path picker one listing, a form's live
validation one check (a newer change aborts the one in flight, which leaves the queue)."""
FOLDER_READS_WAITING = 16
"""Folder reads that may wait for a turn (``turn``); the next is a 503 with ``Retry-After``."""


@asynccontextmanager
async def turn(request: Request, limiter: CapacityLimiter, *, waiting: int, what: str) -> AsyncIterator[bool]:
    """A turn of ``limiter`` (a capped computation's, held by the app), waited for in an ``async`` route outside the
    thread pool; it yields whether the request's client is still there, and ends the turn on leaving::

        async with turn(request, limiter, waiting=4, what="detector simulations") as ready:
            if not ready:
                return Response(status_code=CLIENT_CLOSED_REQUEST)  # the client has gone: dropped unrun
            return await to_thread.run_sync(work)

    A waiting request leaves the queue as soon as its client goes (checked every ``DISCONNECT_POLL_SECONDS``), so
    only live requests wait, and at most ``waiting`` of them: the next one is a 503 problem with ``Retry-After``.
    Read the database before the turn (``SessionsDep``): a waiting request holds no connection."""

    statistics = limiter.statistics()
    if statistics.borrowed_tokens >= statistics.total_tokens and statistics.tasks_waiting >= waiting:
        raise ProblemError(
            503,
            f"Too many {what} waiting",
            f"{statistics.tasks_waiting} {what} are already waiting for their turn ({statistics.total_tokens:g} at a "
            "time): try again in a moment.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    acquired = gone = False
    try:  # from the acquire to the release: a cancellation from outside may land anywhere after the acquire
        async with anyio.create_task_group() as group:

            async def watch() -> None:
                nonlocal gone
                # Starlette has no event for a client that goes, only this check, so it is polled.
                while not await request.is_disconnected():  # noqa: ASYNC110
                    await anyio.sleep(DISCONNECT_POLL_SECONDS)
                gone = True
                group.cancel_scope.cancel()  # leave the queue (an acquire cancelled while waiting takes no turn)

            group.start_soon(watch)
            await limiter.acquire()
            acquired = True  # no checkpoint between: anyio gives back a token whose acquire was cancelled
            group.cancel_scope.cancel()  # stop watching
        if not acquired:
            yield False
            return
        yield not gone and not await request.is_disconnected()
    finally:
        if acquired:
            limiter.release()


async def read_folders(request: Request, work: Callable[[], T]) -> T | Response:
    """``work`` (blocking: it lists or reads folders a request names) in the thread pool, in a turn of the app's
    folder reads (``app.state.folder_reads``, ``FOLDER_READS_AT_ONCE``) waited for outside the pool (``turn``): while
    ``FOLDER_READS_WAITING`` wait, a 503 problem with ``Retry-After``; one whose client has gone is dropped unrun (a
    499 ``Response``). ``work`` opens any database session itself, so a waiting request holds no connection."""

    limiter: CapacityLimiter = request.app.state.folder_reads
    async with turn(request, limiter, waiting=FOLDER_READS_WAITING, what="folder reads") as ready:
        if not ready:
            return Response(status_code=CLIENT_CLOSED_REQUEST)
        return await anyio.to_thread.run_sync(work)


# ---------------------------------------------------------------------------
# Revalidated responses (``core.etag``)


def revalidated(request: Request, response: Response, tag: str) -> Response | None:
    """Tag the endpoint's ``response`` (``ETag`` ``tag``, ``Cache-Control: private, no-cache``); a bodiless 304 to
    return instead when the request's ``If-None-Match`` holds ``tag`` (the browser's copy is current), else None::

        if (unchanged := revalidated(request, response, tag)) is not None:
            return unchanged

    An endpoint that builds its own ``Response`` (Arrow, PNG) passes ``etag.headers(tag)`` to it."""

    response.headers.update(etag.headers(tag))
    if etag.matches(request.headers.get("if-none-match"), tag):
        return Response(status_code=304, headers=etag.headers(tag))
    return None


# ---------------------------------------------------------------------------
# Request guards


async def refuse_cross_site_writes(request: Request) -> None:
    """403 for a request that can change data (not GET, HEAD or OPTIONS) unless it carries ``X-Laue-Portal: 1``
    and, when a browser sent it, ``Sec-Fetch-Site`` is ``same-origin`` (or ``none``)."""

    if request.method in SAFE_METHODS:
        return
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() not in SAME_ORIGIN_FETCH_SITES:
        raise ProblemError(
            403,
            CROSS_SITE_TITLE,
            f"This request came from a page on another origin (Sec-Fetch-Site: {quoted(site, 20)}): only the "
            "portal's own pages can change data.",
        )
    if request.headers.get(WRITE_HEADER) != WRITE_HEADER_VALUE:
        raise ProblemError(
            403,
            CROSS_SITE_TITLE,
            f"A request that changes data needs the header {WRITE_HEADER}: {WRITE_HEADER_VALUE}. The portal's pages "
            "send it, and a page on another origin cannot; scripts must send it too.",
        )


def _is_json(content_type: str | None) -> bool:
    """FastAPI's rule for a body it parses as JSON: ``application/json`` or ``application/*+json``."""

    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


_NO_BODY = object()


async def _json_body(request: Request) -> Any:
    """The JSON body FastAPI parsed for the route (Starlette caches it), or ``_NO_BODY``."""

    route = request.scope.get("route")
    if not isinstance(route, APIRoute) or route.body_field is None or not _is_json(request.headers.get("content-type")):
        return _NO_BODY
    if not await request.body():
        return _NO_BODY
    try:
        return await request.json()
    except ValueError:  # FastAPI answers a malformed body itself, before the guards run
        return _NO_BODY


async def _form_fields(request: Request) -> list[tuple[str, str]]:
    """The text fields of the form FastAPI parsed for a route whose body is a form (Starlette caches it), names and
    values; an uploaded file is not text."""

    route = request.scope.get("route")
    if not isinstance(route, APIRoute) or route.body_field is None:
        return []
    if not isinstance(route.body_field.field_info, FormParam):
        return []
    form = await request.form()
    return [(name, value) for name, value in form.multi_items() if isinstance(value, str)]


async def check_request_text(request: Request) -> None:
    """422 for a NUL or a lone surrogate anywhere in the request's text: path and query parameters (names and
    values), a JSON body (its names and strings, at any depth) and a form's text fields. ``errors[].loc`` locates
    each one as FastAPI's validation does (``["query", "path"]``; a body or form field without ``"body"``:
    ``["notes"]``), a name escaped."""

    found: list[tuple[list[str | int], str]] = []
    for name, value in request.path_params.items():
        if isinstance(value, str):
            found.extend(invalid_strings(value, ["path", name]))
    for name, value in request.query_params.multi_items():
        reason = invalid_text(name, "The name")
        if reason is not None:
            found.append((["query", escaped(name)], reason))
        else:
            found.extend(invalid_strings(value, ["query", name]))
    body = await _json_body(request)
    if body is not _NO_BODY:
        found.extend(invalid_strings(body))
    for name, value in await _form_fields(request):
        reason = invalid_text(name, "The name")
        if reason is not None:
            found.append(([escaped(name)], reason))
        else:
            found.extend(invalid_strings(value, [name]))
    if found:
        errors = [ProblemItem(loc=loc, message=message, code=INVALID_TEXT) for loc, message in found]
        raise ProblemError(422, "Validation failed", problems_detail(len(errors)), errors=errors)


REQUEST_GUARDS: list[DependsParam] = [Depends(refuse_cross_site_writes), Depends(check_request_text)]
"""The app's dependencies (``FastAPI(dependencies=...)``), so every route, also one added later, runs them first."""


# ---------------------------------------------------------------------------
# The body limit

JSON_BODY_LIMIT = 1024 * 1024
"""The longest body a route that is not an upload reads, in bytes (1 MiB; a create form's values take a few KB)."""
BODY_TOO_LARGE_TITLE = "Request body too large"


def _takes_form(route: object) -> bool:
    """Whether ``route`` (the matched route, ``scope["route"]``) reads its body as a form: an upload, which bounds its
    own body (``scans.api.ScanLogUploadRoute``)."""

    return (
        isinstance(route, APIRoute)
        and route.body_field is not None
        and isinstance(route.body_field.field_info, FormParam)
    )


def _too_long(length: str | None) -> bool:
    """Whether a ``Content-Length`` declares more than ``JSON_BODY_LIMIT`` bytes (one that is not a number is the
    server's to refuse; counting still bounds the body)."""

    text = (length or "").strip()
    if not (text.isascii() and text.isdigit()):
        return False
    return len(text) > len(str(JSON_BODY_LIMIT)) or int(text) > JSON_BODY_LIMIT


def body_too_large() -> ProblemError:
    return ProblemError(
        413, BODY_TOO_LARGE_TITLE, f"A request body may hold at most {JSON_BODY_LIMIT // 1024 // 1024} MiB."
    )


class BodyLimitMiddleware:
    """Bounds every body a route reads whole (JSON: every route but an upload) to ``JSON_BODY_LIMIT``.

    FastAPI reads a route's body before any dependency runs, the request guards included, so the check is in the
    body's ``receive``, at the route's first read (the route is known then, ``scope["route"]``): the write guard
    first (a cross-site post is a 403, unread, as the upload's), then a ``Content-Length`` over the limit is a 413
    before a byte is received, and a body without one (chunked) is counted as it arrives, a 413 once past the limit.
    FastAPI answers an error raised while it reads with a 400 caused by it, which ``core.errors`` answers as the
    problem raised. A route that takes a form (the scan-log upload) bounds its own body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = 0
        bounded: bool | None = None  # decided at the first read

        async def bounded_receive() -> Message:
            nonlocal received, bounded
            if bounded is None:
                bounded = not _takes_form(scope.get("route"))
                if bounded:
                    request = Request(scope)
                    await refuse_cross_site_writes(request)
                    if _too_long(request.headers.get("content-length")):
                        raise body_too_large()
            message = await receive()
            if bounded and message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > JSON_BODY_LIMIT:
                    raise body_too_large()
            return message

        await self.app(scope, bounded_receive, send)


def document_request_guards(openapi: dict[str, Any]) -> None:
    """Document ``refuse_cross_site_writes`` on every operation that changes data: a 403 problem response, and the
    header in the operation's description. Not as a header parameter: the generated client would then ask every
    caller for it, while ``src/api/fetcher.ts`` sends it with every request."""

    schemas: dict[str, Any] = openapi.setdefault("components", {}).setdefault("schemas", {})
    if "Problem" not in schemas:
        problem_schema = Problem.model_json_schema(ref_template="#/components/schemas/{model}", mode="serialization")
        schemas.update(problem_schema.pop("$defs", {}))
        schemas["Problem"] = problem_schema
    for path_item in openapi.get("paths", {}).values():
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            if "application/json" in operation.get("requestBody", {}).get("content", {}):
                operation.setdefault("responses", {})["413"] = {
                    "description": f"{BODY_TOO_LARGE_TITLE}: more than {JSON_BODY_LIMIT // 1024 // 1024} MiB, refused "
                    "by its `Content-Length` before it is read, or (chunked) once that much has arrived.",
                    "content": {PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/Problem"}}},
                }
            if method.upper() in SAFE_METHODS:
                continue
            operation.setdefault("responses", {})["403"] = {
                "description": f"{CROSS_SITE_TITLE}: the request lacks `{WRITE_HEADER}: {WRITE_HEADER_VALUE}`, or "
                "comes from a page on another origin.",
                "content": {PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/Problem"}}},
            }
            description = operation.get("description")
            operation["description"] = f"{description}\n\n{WRITE_GUARD_NOTE}" if description else WRITE_GUARD_NOTE
