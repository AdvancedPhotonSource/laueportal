"""System endpoints: health (the navbar polls it) and the system status (the status page)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from laueportal.core.api import EngineDep, SessionDep, SettingsDep
from laueportal.system import status
from laueportal.system.schemas import Health, SystemStatus

router = APIRouter(tags=["system"])


@router.get("/health", name="getHealth", summary="Health", response_model=Health)
def get_health(settings: SettingsDep, engine: EngineDep, response: Response) -> Health:
    """The database and the worker (as its row records it: running, late or not running). Never fails: an
    unreadable database reports "error" and the worker null."""
    response.headers["Cache-Control"] = "no-store"
    return status.collect_health(settings, engine)


@router.get("/system/status", name="getSystemStatus", summary="System status", response_model=SystemStatus)
def get_system_status(
    settings: SettingsDep, engine: EngineDep, session: SessionDep, response: Response
) -> SystemStatus:
    """Database (and its path), API version, the worker and its threads, the queue's counts (the job and conversion
    tables) and its next items in dispatch order, and the queued and running runs. Never fails because the database
    is down: it reports `error`, and what needs it is null."""
    response.headers["Cache-Control"] = "no-store"
    return status.collect_status(settings, engine, session)
