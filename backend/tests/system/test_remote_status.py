"""A shared on-demand check cannot block page loads or launch one probe per browser."""

import json
import subprocess
import sys
import threading
import time

import psutil
import pytest
from fastapi.testclient import TestClient

from laueportal.app import create_app
from laueportal.core.settings import RemoteSection
from laueportal.remote.health import CHECKS, RemoteCheck
from laueportal.system.remote_status import INTERVAL, RemoteStatus
from tests.settings_support import make_settings


def good():
    return [RemoteCheck(name=name, state="ok", detail="Available") for name in CHECKS]


def settled(cache):
    deadline = time.monotonic() + 3
    while cache.get().checking:
        assert time.monotonic() < deadline, "Probe did not settle"
        time.sleep(0.005)
    return cache.get()


def test_requests_share_one_inflight_probe_and_five_minutes_of_fresh_cached_results(tmp_path, monkeypatch):
    now = [1.0]
    cache = RemoteStatus(RemoteSection(enabled=True, settings_file=tmp_path / "private.yaml"), clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return good()

    monkeypatch.setattr(cache, "_probe", probe)
    try:
        first = cache.get()
        assert first.checking and first.checked_at is None
        assert entered.wait(1)
        for _ in range(25):
            assert cache.get().checking
        assert len(calls) == 1
        release.set()
        answer = settled(cache)
        assert answer.state == "ok" and answer.checked_at is not None
        now[0] += 121
        assert cache.get().state == "ok" and not cache.get().stale
        assert len(calls) == 1
        now[0] = 300  # 299 s since the answer: still inside the five-minute cache
        assert cache.get().state == "ok" and len(calls) == 1
        now[0] += 2
        cache.get()
        settled(cache)
        assert len(calls) == 2
    finally:
        release.set()
        cache.close()


def test_lost_bridge_never_reports_offline_or_keeps_an_old_green_result(tmp_path, monkeypatch):
    now = [1.0]
    cache = RemoteStatus(RemoteSection(enabled=True, settings_file=tmp_path / "private.yaml"), clock=lambda: now[0])
    monkeypatch.setattr(cache, "_probe", good)
    cache.get()
    previous = settled(cache)

    def unavailable():
        raise RuntimeError("secret credential in SSH error")

    monkeypatch.setattr(cache, "_probe", unavailable)
    try:
        now[0] += INTERVAL + 1
        cache.get()
        answer = settled(cache)
        assert answer.state == "unknown" and answer.stale
        assert answer.checked_at == previous.checked_at
        assert {c.state for c in answer.checks} == {"unknown"}
        assert "secret" not in answer.model_dump_json()
        assert answer.retry_after_seconds > INTERVAL
    finally:
        cache.close()


def test_disabled_remote_status_works_without_a_database_and_never_starts_a_probe(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    monkeypatch.setattr(app.state.remote_status, "_probe", lambda: (_ for _ in ()).throw(AssertionError("called")))
    with TestClient(app) as client:
        result = client.get("/api/v1/system/remote-status")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["state"] == "disabled"
    assert not settings.database.path.exists()


def test_probe_uses_launcher_bridge_and_bounds_the_command_without_starting_workflows(tmp_path, monkeypatch):
    config = tmp_path / "private.yaml"
    config.write_text("""targets:
  crux: {enabled: true}
dm:
  launcher_ssh_host: user@launcher.example.invalid
  launcher_python: /installed/env/bin/python
  launcher_code: /installed/source with spaces
  launcher_config: /private/bdp.yaml
""")
    cache = RemoteStatus(RemoteSection(enabled=True, settings_file=config))
    calls = []

    def run(argv, *, timeout):
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, json.dumps([c.model_dump() for c in good()]), "")

    monkeypatch.setattr(cache._client, "run", run)
    try:
        assert len(cache._probe()) == 6
        argv, timeout = calls[0]
        assert argv[:2] == ["ssh", "-T"] and "user@launcher.example.invalid" in argv
        assert 0 < timeout <= 45
    finally:
        cache.close()


@pytest.mark.slow
def test_closing_the_api_aborts_the_probe_and_its_child_processes(tmp_path):
    """The configured launcher is a stand-in Python process, never a real DM/Globus job."""
    source = tmp_path / "source"
    package = source / "backend/src/laueportal/remote"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package.parent / "__init__.py").touch()
    marker = tmp_path / "child.pid"
    (package / "health.py").write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"Path({str(marker)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    config = tmp_path / "private.yaml"
    config.write_text(f"""targets:
  crux: {{enabled: true}}
dm:
  launcher_ssh_host: null
  launcher_python: {sys.executable}
  launcher_code: {source}
  launcher_config: /private/unused.yaml
""")
    cache = RemoteStatus(RemoteSection(enabled=True, settings_file=config))
    try:
        assert cache.get().checking
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline, "Launcher did not start"
            time.sleep(0.01)
        child = psutil.Process(int(marker.read_text()))
        cache.close()
        deadline = time.monotonic() + 3
        while child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "Probe child survived API shutdown"
            time.sleep(0.01)
        assert settled(cache).stale
    finally:
        cache.close()
