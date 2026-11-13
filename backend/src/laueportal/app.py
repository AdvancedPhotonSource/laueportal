"""The FastAPI application: ``create_app(settings)``.

- Routers are mounted under ``/api/v1``. Every route's ``operationId`` is its ``name``, which must be
  camelCase (``name="getHealth"``) and unique: Orval turns it into the frontend hook name.
- Every route first runs the request guards (``core.api.REQUEST_GUARDS``, the app's dependencies): a request that
  changes data needs ``X-Laue-Portal: 1`` from the portal's own origin (else a 403), and no text input may hold a
  NUL or a lone surrogate (else a 422); a body is at most 1 MiB (``core.api.BodyLimitMiddleware``, a 413), but an
  upload's, which its route bounds.
- Errors are problem+json (``core.errors``); responses of 4 KB and more are gzipped.
- The production guards: a request whose ``Host`` is not one of ``app.allowed_hosts`` is a
  400 problem, logged (``HostCheckMiddleware``, Starlette's check: a DNS-rebinding page cannot read the answers), and every answer
  carries ``SECURITY_HEADERS`` (no framing by another site, no type sniffing). The write guard refuses a
  ``Sec-Fetch-Site`` other than ``same-origin``, so the SPA and the API stay on one origin: this app serves both.
- The built SPA (``settings.app.static_dir``) is served for every other path, and client routes fall
  back to ``index.html``. Unknown ``/api/...`` paths stay problem+json 404s, and so is a missing file
  of the SPA (``/assets/x.js``). Without a built SPA the app serves the API alone.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from anyio import CapacityLimiter
from fastapi import APIRouter, FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from laueportal import __version__
from laueportal.calibration import api as calibration_api
from laueportal.calibration.service import CALIBRATIONS_AT_ONCE
from laueportal.core import etag
from laueportal.core.api import FOLDER_READS_AT_ONCE, REQUEST_GUARDS, BodyLimitMiddleware, document_request_guards
from laueportal.core.db import make_engine, make_sessionmaker
from laueportal.core.errors import install_error_handlers, is_api_path, problem_response, use_problem_responses
from laueportal.core.logging import configure_logging
from laueportal.core.memory import return_freed_buffers
from laueportal.core.problems import Problem, ProblemError
from laueportal.core.settings import DatabaseSettings, FilesSettings, Settings, get_settings
from laueportal.core.text import quoted
from laueportal.files import api as files_api
from laueportal.files import probe
from laueportal.indexing import api as indexing_api
from laueportal.indexing.detector import IMAGES_AT_ONCE, SIMULATIONS_AT_ONCE
from laueportal.jobs import api as jobs_api
from laueportal.jobs.daq import DaqList
from laueportal.reconstruction import api as reconstruction_api
from laueportal.reconstruction.viewer import COMPUTATIONS_AT_ONCE, FRAMES_AT_ONCE
from laueportal.scans import api as scans_api
from laueportal.science.cache import DatasetCache
from laueportal.science.recon import MIB, ProductCache
from laueportal.system import api as system_api
from laueportal.system.remote_status import RemoteStatus

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
API_TITLE = "3DMN Portal API"
GZIP_MINIMUM_SIZE = 4096
GZIP_LEVEL = 1
"""The GZip middleware's level: an 8 MiB uint16 frame of the measured reconstruction compresses in about 116 ms to
2.59 MiB at level 1, against 396 ms to 2.38 MiB at level 5, and the compression is most of a depth change."""

SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}
"""Set on every answer (``SecurityHeadersMiddleware``): no page of another site may frame the portal's (CSP's
``frame-ancestors``, and ``X-Frame-Options`` for older browsers), and a browser takes every answer as the type it
is sent with."""

INVALID_HOST_TITLE = "Invalid host header"
HOST_WARNING_SECONDS = 300
"""A refused host name is logged at most once in this many seconds (``HostCheckMiddleware``)."""
HOST_WARNINGS_KEPT = 64
"""The refused names remembered for that: while this many were logged within ``HOST_WARNING_SECONDS``, a new name
is not logged (a flood of names writes at most this many lines in that time)."""
_ADMITTED = "laueportal.host_admitted"

_CAMEL_CASE = re.compile(r"[a-z][a-zA-Z0-9]*")


def _routers() -> list[APIRouter]:
    return [
        system_api.router,
        scans_api.router,
        reconstruction_api.router,
        indexing_api.router,
        jobs_api.router,
        files_api.router,
        calibration_api.router,
    ]


def route_operation_id(route: APIRoute) -> str:
    """``generate_unique_id_function`` for the app: the operationId is the route's camelCase name.

    FastAPI calls it lazily (when it first resolves the routes); ``create_app`` forces that through
    ``check_operation_ids`` so a bad name fails at startup.
    """
    if not _CAMEL_CASE.fullmatch(route.name):
        raise ValueError(
            f"{getattr(route, 'path', '?')}: the route name {route.name!r} becomes the operationId and must be "
            "camelCase, e.g. @router.get(..., name='getHealth')"
        )
    return route.name


def check_operation_ids(app: FastAPI) -> None:
    """Every documented route has one method and a unique camelCase operationId."""
    seen: dict[str, str] = {}
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute) or not context.include_in_schema:
            continue
        operation_id = context.operation_id or context.unique_id
        for method in sorted(context.methods or ()):
            where = f"{method} {context.path}"
            if not _CAMEL_CASE.fullmatch(operation_id):
                raise ValueError(f"{where}: operationId {operation_id!r} must be camelCase")
            if operation_id in seen:
                raise ValueError(f"{where}: operationId {operation_id!r} is already used by {seen[operation_id]}")
            seen[operation_id] = where


class PortalAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        if not self.openapi_schema:
            document = super().openapi()
            use_problem_responses(document)
            document_request_guards(document)
        assert self.openapi_schema is not None
        return self.openapi_schema


class SPAStaticFiles(StaticFiles):
    """The built SPA. Paths without a file extension are client routes and get ``index.html``.

    A missing asset (``/assets/x.js``) stays a 404 rather than turning into HTML: a problem+json 404, as
    every error (and so is a client route when the build has no ``index.html``). ``index.html`` is
    revalidated on every load; the hashed files under ``assets/`` are cached for good.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if PurePosixPath(path).suffix:
                raise ProblemError(404, "Not Found", f"No file at {scope['path']}") from exc
            try:
                response = await super().get_response("index.html", scope)
            except StarletteHTTPException as missing:
                if missing.status_code != 404:
                    raise
                raise ProblemError(404, "Not Found", "The SPA build has no index.html (make build)") from missing
        if str(getattr(response, "path", "")).endswith("index.html"):
            response.headers["Cache-Control"] = "no-cache"
        elif path.startswith("assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class SecurityHeadersMiddleware:
    """Adds ``SECURITY_HEADERS`` to every HTTP answer that does not set them itself."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HostCheckMiddleware:
    """Refuses a request whose ``Host`` header is not one of ``allowed_hosts`` (so a page whose DNS
    name was pointed at the portal cannot read its answers) with a 400 problem, as every other error is answered,
    and logs a warning naming the refused host (that header only, shortened; once per name
    per ``HOST_WARNING_SECONDS``), so a wrong ``[app] allowed_hosts`` or proxy shows in the API's log. Starlette's
    ``TrustedHostMiddleware`` decides (its matching: the port aside, ``*.domain``, ``*``); its text/plain answer is
    dropped. Anything but HTTP (the lifespan; the app has no websocket) goes through Starlette's own."""

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str]) -> None:
        self.app = app
        self.allowed_hosts = list(allowed_hosts)
        self._check = TrustedHostMiddleware(self._admit, allowed_hosts=self.allowed_hosts, www_redirect=False)
        self._other = TrustedHostMiddleware(app, allowed_hosts=self.allowed_hosts, www_redirect=False)
        self._warned: dict[str, float] = {}  # a refused name: when it was last logged (time.monotonic)

    @staticmethod
    async def _admit(scope: Scope, receive: Receive, send: Send) -> None:
        scope[_ADMITTED] = True

    @staticmethod
    async def _drop(message: Message) -> None:
        pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._other(scope, receive, send)
            return
        await self._check(scope, receive, self._drop)
        if scope.pop(_ADMITTED, False):
            await self.app(scope, receive, send)
            return
        header = Headers(scope=scope).get("host")
        name = "no Host header" if header is None else quoted(header)
        self._warn(name)
        detail = (
            "The request has no Host header."
            if header is None
            else f"The Host header names {name}, which is not a name this portal is served by."
        )
        await problem_response(Problem(title=INVALID_HOST_TITLE, status=400, detail=detail))(scope, receive, send)

    def _warn(self, name: str) -> None:
        now = time.monotonic()
        last = self._warned.get(name)
        if last is not None and now - last < HOST_WARNING_SECONDS:
            return
        if last is None and len(self._warned) >= HOST_WARNINGS_KEPT:
            self._warned = {kept: at for kept, at in self._warned.items() if now - at < HOST_WARNING_SECONDS}
            if len(self._warned) >= HOST_WARNINGS_KEPT:
                return
        self._warned[name] = now
        logger.warning(
            "Refused a request for host %s: not in [app] allowed_hosts (%s); a name users reach the portal by "
            "belongs there (each name is logged once in %d s)",
            name,
            ", ".join(self.allowed_hosts),
            HOST_WARNING_SECONDS,
        )


def _spa(static_dir: Path | None) -> SPAStaticFiles | None:
    if static_dir is None:
        logger.info("app.static_dir is not set: serving the API only")
        return None
    if not static_dir.is_dir():
        logger.warning(
            "app.static_dir %s does not exist (build the SPA with `make build`): serving the API only", static_dir
        )
        return None
    if not (static_dir / "index.html").is_file():
        logger.warning("app.static_dir %s has no index.html: client routes will 404", static_dir)
    return SPAStaticFiles(directory=static_dir, html=True)


def _fallback(spa: SPAStaticFiles | None, not_found: ASGIApp) -> ASGIApp:
    """What the router runs when no route matches (after its 405 and trailing-slash handling)."""

    async def fallback(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_api_path(scope):
            raise StarletteHTTPException(404, detail=f"No API endpoint at {scope['path']}")
        if scope["type"] == "http" and spa is not None:
            await spa(scope, receive, send)
            return
        await not_found(scope, receive, send)

    return fallback


def create_app(settings: Settings) -> FastAPI:
    """Build the app. Does no I/O beyond checking ``static_dir``; the database is opened on first use and never
    created (a request that needs a missing database file is a 503 problem, ``core.db.DatabaseMissing``). Bounds
    the process's file checks (``files.probe.configure``: ``[files]`` ``probe_timeout_seconds``,
    ``hung_mount_seconds`` and ``probe_threads``)."""
    probe.configure(settings.files)
    engine = make_engine(settings, create=False)
    remote_status = RemoteStatus(settings.remote)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            etag.versions()  # the code's identity in every entity tag, made once, from the source this process loaded
            yield
        finally:
            remote_status.close()
            engine.dispose()

    app = PortalAPI(
        title=API_TITLE,
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,  # Swagger UI and ReDoc load from a CDN; the contract is openapi.json
        redoc_url=None,
        generate_unique_id_function=route_operation_id,
        dependencies=REQUEST_GUARDS,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = make_sessionmaker(engine)
    app.state.datasets = DatasetCache(settings.indexing.dataset_cache_size)  # the results viewer's (science.cache)
    app.state.simulations = CapacityLimiter(SIMULATIONS_AT_ONCE)  # the detector overlays' (indexing.detector)
    app.state.detector_images = CapacityLimiter(IMAGES_AT_ONCE)  # the turns for a step's detector image
    # the wire viewer's (science.recon), and the turns for a point's computed images (reconstruction.viewer)
    app.state.reconstruction_products = ProductCache(settings.reconstruction.product_cache_mib * MIB)
    app.state.point_computations = CapacityLimiter(COMPUTATIONS_AT_ONCE)
    app.state.frame_reads = CapacityLimiter(FRAMES_AT_ONCE)  # the turns for a point's depth frames
    # the folder reads' turns: the file browser, the forms' file helpers, a run's validation and creation (core.api)
    app.state.folder_reads = CapacityLimiter(FOLDER_READS_AT_ONCE)
    app.state.calibrations = CapacityLimiter(CALIBRATIONS_AT_ONCE)  # the Calibration page's computations
    # the Live page's (jobs.live_session): the DAQ list's one shared answer (no DM call until one is asked for), and
    # Start's one lock across the one-session check, the creations and the queueing (the api is one process)
    app.state.daq_list = DaqList(settings.live)
    app.state.remote_status = remote_status
    app.state.live_start = threading.Lock()

    for router in _routers():
        app.include_router(router, prefix=API_PREFIX)
    check_operation_ids(app)

    app.add_middleware(BodyLimitMiddleware)  # a body over 1 MiB is a 413 (an upload bounds its own)
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE, compresslevel=GZIP_LEVEL)
    # An unknown Host is refused before anything else runs (no body is read, no route is matched).
    app.add_middleware(HostCheckMiddleware, allowed_hosts=settings.app.allowed_hosts)
    install_error_handlers(app)  # its 500 layer wraps everything above
    app.add_middleware(SecurityHeadersMiddleware)  # outermost (it only adds headers): every answer gets them
    app.router.default = _fallback(_spa(settings.app.static_dir), app.router.not_found)
    return app


def serve_app() -> FastAPI:
    """uvicorn factory for ``laueportal serve`` (``--reload`` runs it in uvicorn's child process), in the process
    that serves: it also makes the allocator give freed frame buffers back (``core.memory``)."""
    settings = get_settings()
    configure_logging(settings)
    return_freed_buffers()
    return create_app(settings)


def openapi_document() -> dict[str, Any]:
    """The OpenAPI contract. It never depends on local configuration, so placeholder settings will do."""
    placeholder = Path("/nonexistent/laueportal-openapi")
    settings = Settings(
        database=DatabaseSettings(path=placeholder / "unused.db", journal_mode="delete"),  # never refused (core.db)
        files=FilesSettings(runs_root=placeholder / "runs"),
    )
    return create_app(settings).openapi()
