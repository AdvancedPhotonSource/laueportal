"""The FastAPI adapter over core.db and core.settings (core/api.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import anyio
import anyio.lowlevel
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import text

from laueportal.app import create_app
from laueportal.core.api import SessionDep, SettingsDep, turn
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.problems import ProblemError
from laueportal.core.settings import Settings
from tests.api_support import PORTAL_HEADERS
from tests.settings_support import create_database, make_settings

router = APIRouter()


class Row(BaseModel):
    name: str
    title: str


@router.post("/rows/{name}", name="createTestRow", response_model=Row)
def create_row(name: str, session: SessionDep, settings: SettingsDep, fail: bool = False) -> Row:
    session.execute(text("INSERT INTO row_item (name) VALUES (:name)"), {"name": name})
    if fail:
        raise ProblemError(409, "Conflict", "rolled back")
    return Row(name=name, title=settings.app.title)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = make_settings(tmp_path, app={"title": "Test Portal"})
    with sqlite3.connect(create_database(settings)) as connection:
        connection.execute("CREATE TABLE row_item (name TEXT)")
    connection.close()
    return settings


def stored_names(settings: Settings) -> list[str]:
    with sqlite3.connect(settings.database.path) as connection:
        names = [name for (name,) in connection.execute("SELECT name FROM row_item ORDER BY name")]
    connection.close()
    return names


def test_session_dependency_commits_on_success_and_rolls_back_on_error(settings: Settings) -> None:
    app = create_app(settings)
    app.include_router(router, prefix="/api/v1/test")
    client = TestClient(app, headers=PORTAL_HEADERS)

    response = client.post("/api/v1/test/rows/kept")
    assert response.json() == {"name": "kept", "title": "Test Portal"}
    assert stored_names(settings) == ["kept"]

    response = client.post("/api/v1/test/rows/dropped?fail=true")
    assert response.status_code == 409
    assert stored_names(settings) == ["kept"]


def test_the_api_never_creates_a_missing_database(tmp_path: Path) -> None:
    """A request that needs the database answers 503 instead of creating an empty one; /health stays down."""
    root = tmp_path / "data"
    settings = make_settings(tmp_path, files={"roots": {"data": root}})
    client = TestClient(create_app(settings), headers=PORTAL_HEADERS)
    catalog = {"filefolder": str(root), "filename_prefix": [], "aperture": "none", "sample_name": None, "notes": None}
    catalog["version"] = None
    responses = [
        client.get("/api/v1/scans"),
        client.get("/api/v1/scans/1"),
        client.put("/api/v1/scans/1/catalog", json=catalog),
        client.post("/api/v1/scans/1/notes", json={"text": "x"}),
    ]
    for response in responses:
        assert response.status_code == 503, response.text
        assert response.headers["content-type"] == PROBLEM_JSON
        assert response.json() == {
            "type": "about:blank",
            "title": "Database unavailable",
            "status": 503,
            "detail": f"The database file {settings.database.path} does not exist; create it with `laueportal db upgrade`.",
        }
    assert client.get("/api/v1/health").json()["db"] == "error"
    assert client.get("/api/v1/system/status").json()["database"]["status"] == "error"
    assert not settings.database.path.exists()


def test_a_turn_cancelled_from_outside_right_after_its_acquire_gives_its_token_back() -> None:
    """``core.api.turn`` under a caller's cancel scope (a timeout, say): a cancellation in the first event-loop steps
    after the acquire, while the watcher is being stopped, must not leave the limiter's token borrowed."""

    class Connected:
        async def is_disconnected(self) -> bool:
            return False

    async def tokens_left_after(steps: int) -> int:
        limiter, outer = anyio.CapacityLimiter(1), anyio.CancelScope()

        async def waiter() -> None:
            with outer:
                async with turn(Connected(), limiter, waiting=4, what="tests"):  # type: ignore[arg-type]
                    await anyio.sleep(1)

        async def canceller() -> None:
            for _ in range(steps):
                await anyio.lowlevel.checkpoint()
            outer.cancel()

        async with anyio.create_task_group() as group:
            group.start_soon(waiter)
            group.start_soon(canceller)
        return limiter.statistics().borrowed_tokens

    async def every_window() -> list[int]:
        return [await tokens_left_after(steps) for steps in range(5)]

    assert anyio.run(every_window) == [0] * 5
