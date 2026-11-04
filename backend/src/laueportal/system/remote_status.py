"""On-demand remote health, one bounded launcher process shared by all browsers.

GETs never wait for the probe. While a visible browser asks, at most one probe starts
every five minutes (up to fifteen after repeated failures). No browser, no probe. Nothing
uses the worker queue or the database. A failed bridge never means 'endpoint offline'.
"""

from __future__ import annotations

import json
import shlex
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath

from pydantic import TypeAdapter

from laueportal.core.settings import RemoteSection
from laueportal.core.timeutil import utcnow
from laueportal.files.probe import bounded
from laueportal.remote.dm import DMClient
from laueportal.remote.health import CHECKS, RemoteCheck
from laueportal.remote.settings import DMSettings, load_settings
from laueportal.remote.transport import shell_command
from laueportal.system.schemas import RemoteReadiness

INTERVAL = 300.0
TIMEOUT = 45.0
MAX_BACKOFF = 900.0
STALE_AFTER = INTERVAL * 2


class RemoteStatus:
    def __init__(self, settings: RemoteSection, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._client = DMClient(DMSettings(), own_session=True)
        self._checks: list[RemoteCheck] = []
        self._checked_at: datetime | None = None
        self._finished = 0.0
        self._next = 0.0
        self._wait = INTERVAL
        self._running = False
        self._closed = False
        self._enabled = settings.enabled
        self._failed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._client.abort()

    def get(self) -> RemoteReadiness:
        with self._lock:
            now = self._clock()
            if self._settings.enabled and not self._closed and not self._running and now >= self._next:
                self._running = True
                self._next = now + self._wait
                threading.Thread(target=self._refresh, name="remote-readiness", daemon=True).start()
            stale = self._failed or (self._checked_at is not None and now - self._finished > STALE_AFTER)
            states = {check.state for check in self._checks}
            state = (
                "disabled"
                if not self._enabled
                else "unknown"
                if stale or not self._checks
                else "error"
                if "unavailable" in states
                else "warning"
                if states & {"login_required", "denied", "unknown"}
                else "ok"
                if states == {"ok"}
                else "unknown"
            )
            return RemoteReadiness(
                enabled=self._enabled,
                state=state,
                checks=self._checks,
                checked_at=self._checked_at,
                stale=stale,
                checking=self._running,
                retry_after_seconds=max(0, self._next - now),
            )

    def _probe(self) -> list[RemoteCheck]:
        assert self._settings.settings_file is not None
        file = self._settings.settings_file
        settings = bounded(file, lambda: load_settings(file))
        if settings.targets.crux is None or not settings.targets.crux.enabled:
            return []
        dm = settings.dm
        if not all((dm.launcher_code, dm.launcher_python, dm.launcher_config)):
            raise ValueError("Launcher is not configured")
        packages = str(PurePosixPath(str(dm.launcher_code)) / "backend/src")
        command = shlex.join(
            [str(dm.launcher_python), "-m", "laueportal.remote.health", "--config", str(dm.launcher_config)]
        )
        host = dm.launcher_ssh_host if "launcher_ssh_host" in dm.model_fields_set else dm.ssh_host
        # A remote helper needs its own deadline if SSH disconnects. Locally DMClient's
        # session already bounds the whole tree: another timeout would create a new
        # process group that survives the API's group kill.
        if host:
            command = f"timeout --kill-after=2 40 {command}"
        script = f"export PYTHONNOUSERSITE=1 PYTHONPATH={shlex.quote(packages)}; exec {command}"
        result = self._client.run(shell_command(script, host), timeout=TIMEOUT)
        checks = TypeAdapter(list[RemoteCheck]).validate_python(json.loads(result.stdout))
        if len(checks) != len(CHECKS) or {c.name for c in checks} != set(CHECKS):
            raise ValueError("Incomplete readiness answer")
        return checks

    def _refresh(self) -> None:
        try:
            checks = self._probe()
            failed = False
        except Exception:
            checks = [RemoteCheck(name=n, state="unknown", detail="Status check unavailable") for n in CHECKS]
            failed = True
        with self._lock:
            self._checks = checks
            self._enabled = self._settings.enabled and bool(checks)
            if not failed:
                self._checked_at = utcnow()
                self._finished = self._clock()
            self._wait = (
                min(MAX_BACKOFF, self._wait * 2) if failed or any(c.state != "ok" for c in checks) else INTERVAL
            )
            self._next = self._clock() + self._wait
            self._failed = failed
            self._running = False
