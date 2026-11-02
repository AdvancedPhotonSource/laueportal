from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import laueportal
from laueportal import cli

SECRET = "a-value-that-must-not-leak"
SOURCE_DIR = Path(laueportal.__file__).resolve().parent.parent


def write_config(tmp_path: Path, extra: str = "") -> Path:
    config = tmp_path / "dev.toml"
    config.write_text(f'[database]\npath = "{tmp_path}/dev.db"\n[files]\nruns_root = "{tmp_path}/runs"\n{extra}')
    return config


def run_openapi(out: Path, hash_seed: str) -> bytes:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(SOURCE_DIR)}
    env.pop("LAUEPORTAL_CONFIG", None)  # the contract must not depend on local settings
    subprocess.run([sys.executable, "-m", "laueportal", "openapi", "--out", str(out)], check=True, env=env)
    return out.read_bytes()


def test_openapi_output_is_deterministic(tmp_path: Path) -> None:
    first = run_openapi(tmp_path / "a" / "openapi.json", "1")
    second = run_openapi(tmp_path / "b" / "openapi.json", "4242")
    assert first == second

    text = first.decode()
    document = json.loads(text)
    assert text == json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert document["info"] == {"title": "3DMN Portal API", "version": laueportal.__version__}
    assert document["paths"]["/api/v1/health"]["get"]["operationId"] == "getHealth"
    assert {"Health", "WorkerStatus"} <= set(document["components"]["schemas"])


def test_openapi_to_stdout_matches_the_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["openapi"]) == 0
    stdout = capsys.readouterr().out
    assert cli.main(["openapi", "--out", str(tmp_path / "openapi.json")]) == 0
    assert (tmp_path / "openapi.json").read_text() == stdout


def test_config_show_prints_the_settings_and_where_they_came_from(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path)))
    clean_env.setenv("LAUEPORTAL_APP__PORT", "2105")

    assert cli.main(["config", "show"]) == 0
    captured = capsys.readouterr()
    shown = json.loads(captured.out)
    assert (shown["app"]["port"], shown["database"]["path"]) == (2105, str(tmp_path / "dev.db"))
    assert "LAUEPORTAL_APP__PORT" in captured.err and str(tmp_path / "dev.toml") in captured.err


def test_a_config_with_a_redis_section_starts_with_one_warning_line(tmp_path: Path) -> None:
    """A settings file with a retired ``[redis]`` section and the dev env's Redis password variable still start a
    command, with one warning line that names them, never a value."""
    config = write_config(tmp_path, f'[redis]\nhost = "127.0.0.1"\nport = 2102\npassword = "{SECRET}"\n')
    env = {name: value for name, value in os.environ.items() if not name.startswith("LAUEPORTAL_")}
    env |= {"LAUEPORTAL_CONFIG": str(config), "LAUEPORTAL_REDIS__PASSWORD": SECRET, "PYTHONPATH": str(SOURCE_DIR)}
    shown = subprocess.run(
        [sys.executable, "-m", "laueportal", "config", "show"], capture_output=True, text=True, env=env, check=True
    )

    warnings = [line for line in shown.stderr.splitlines() if not line.startswith("# ")]
    assert warnings == [
        f"Ignored [redis] in {config}, LAUEPORTAL_REDIS__PASSWORD: Redis and RQ are no longer used; remove them"
    ]
    assert "redis" not in json.loads(shown.stdout) and SECRET not in shown.stdout + shown.stderr
    assert "# environment overrides: none" in shown.stderr


def test_config_errors_exit_2_without_values(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path, f'[app]\nprot = "{SECRET}"\n')))
    assert cli.main(["config", "show"]) == 2
    captured = capsys.readouterr()
    assert "app.prot" in captured.err
    assert SECRET not in captured.out + captured.err

    clean_env.delenv("LAUEPORTAL_CONFIG")
    assert cli.main(["config", "show"]) == 2
    assert "LAUEPORTAL_CONFIG is not set" in capsys.readouterr().err


def test_serve_runs_uvicorn_with_the_configured_address(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, restore_logging: None
) -> None:
    import uvicorn

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    clean_env.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path, '[app]\nhost = "127.0.0.1"\nport = 2108\n')))

    assert cli.main(["serve", "--reload"]) == 0
    [(args, kwargs)] = calls
    assert args == ("laueportal.app:serve_app",)
    assert kwargs["factory"] is True and kwargs["log_config"] is None
    assert (kwargs["host"], kwargs["port"], kwargs["reload"]) == ("127.0.0.1", 2108, True)
    assert kwargs["reload_dirs"] == [str(SOURCE_DIR)]
    assert logging.getLogger().level == logging.INFO  # configure_logging ran


def test_serve_in_production_needs_the_built_spa_and_runs_one_process(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], restore_logging: None
) -> None:
    """Without ``--reload`` (production, the e2e stack), serve refuses to start (exit 2) unless ``app.static_dir`` holds
    the build's ``index.html``, and runs one uvicorn process (``WEB_CONCURRENCY`` cannot make it several: the caches,
    turns and file probe are the process's)."""
    import uvicorn

    from laueportal.core.settings import get_settings

    calls: list[dict[str, object]] = []
    clean_env.setattr(uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))
    clean_env.setenv("WEB_CONCURRENCY", "4")
    dist = tmp_path / "dist"
    for static_dir, said in (("", "is not set"), (str(dist), f"{dist} has no index.html")):
        get_settings.cache_clear()
        clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path, f'[app]\nstatic_dir = "{static_dir}"\n')))
        assert cli.main(["serve"]) == 2
        assert f"laueportal serve: app.static_dir {said}: build the SPA" in capsys.readouterr().err
    assert calls == []

    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    assert cli.main(["serve"]) == 0
    [kwargs] = calls
    assert (kwargs["reload"], kwargs["workers"], kwargs["reload_dirs"]) == (False, 1, None)


def test_serve_fails_fast_on_a_bad_config(clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve"]) == 2
    assert "LAUEPORTAL_CONFIG" in capsys.readouterr().err


def test_worker_configures_logging_then_runs_the_worker_with_the_settings(
    clean_env: pytest.MonkeyPatch, restore_logging: None, tmp_path: Path
) -> None:
    """The live worker itself is tested in ``tests/jobs/test_worker.py``."""
    from laueportal.jobs import worker

    calls: list[tuple[Any, bool]] = []

    def run_worker(settings: Any, *, burst: bool = False) -> int:
        calls.append((settings, burst))
        return 7

    clean_env.setattr(worker, "run_worker", run_worker)
    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path, '[app]\nlog_level = "warning"\n')))
    clean_env.setenv("LAUEPORTAL_EXECUTION__DISPATCH_SECONDS", "0.5")

    assert cli.main(["worker", "--burst"]) == 7
    assert cli.main(["worker"]) == 7
    [(settings, burst), (_, plain_burst)] = calls
    assert settings.execution.dispatch_seconds == 0.5
    assert (burst, plain_burst) == (True, False)
    assert logging.getLogger().level == logging.WARNING  # configure_logging ran


def test_seed_demo_builds_the_configured_database_or_the_one_named_by_db(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command passes the settings, --force, --db and --crux-run to ``seed.build.build_demo`` and prints its
    summary."""
    from laueportal.seed import build, demo

    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path)))
    calls: list[tuple[Any, bool, Path | None, bool]] = []

    def build_demo(settings: Any, *, force: bool, database: Path | None, crux_run: bool) -> build.DemoResult:
        calls.append((settings, force, database, crux_run))
        return build.DemoResult(
            database=database or settings.database.path,
            runs_root=settings.files.runs_root,
            revision="0005",
            rebuilt=force,
            runs=demo.DemoRuns(1, 1, 1, 2),
            registered=demo.RegisteredRuns(2, 2, 3),
            ran_by="worker laue-run-test",
        )

    clean_env.setattr(build, "build_demo", build_demo)
    assert cli.main(["seed", "demo"]) == 0
    assert cli.main(["seed", "demo", "--force", "--db", str(tmp_path / "other.db"), "--crux-run"]) == 0
    [(settings, force, database, crux_run), (_, force_again, other, crux_run_again)] = calls
    assert (settings.database.path, force, database, crux_run) == (tmp_path / "dev.db", False, None, False)
    assert (force_again, other, crux_run_again) == (True, tmp_path / "other.db", True)
    out = capsys.readouterr().out
    assert f"Demo database ready: {tmp_path / 'dev.db'} (revision 0005" in out
    assert "R1 and I1 ran on worker laue-run-test" in out and "R2, I2 and R3 are registered results" in out


def test_seed_demo_reports_why_it_could_not_build_the_demo(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from laueportal.seed import build

    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path)))

    def build_demo(settings: Any, **_options: Any) -> build.DemoResult:
        raise build.SeedError("the database is locked")

    clean_env.setattr(build, "build_demo", build_demo)
    assert cli.main(["seed", "demo"]) == 1
    assert capsys.readouterr().err == "laueportal seed demo: the database is locked\n"


def test_seed_slow_runs_prints_the_queued_runs_as_json(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command passes --count and --inputs to ``seed.slow.queue_slow_runs`` and prints the runs as JSON."""
    from laueportal.seed import slow

    clean_env.setenv("LAUEPORTAL_CONFIG", str(write_config(tmp_path)))
    calls: list[tuple[int, int]] = []

    def queue_slow_runs(settings: Any, *, count: int, n_inputs: int) -> slow.SlowRuns:
        calls.append((count, n_inputs))
        return slow.SlowRuns(settings.database.path, (slow.SlowRun(7, 72), slow.SlowRun(8, 73)))

    clean_env.setattr(slow, "queue_slow_runs", queue_slow_runs)
    assert cli.main(["seed", "slow-runs"]) == 0
    assert cli.main(["seed", "slow-runs", "--count", "3", "--inputs", "600"]) == 0
    assert calls == [(2, 1000), (3, 600)]
    first = capsys.readouterr().out.splitlines()[0]
    assert json.loads(first) == {
        "database": str(tmp_path / "dev.db"),
        "runs": [{"job_id": 7, "indexing_id": 72}, {"job_id": 8, "indexing_id": 73}],
    }

    def refuse(settings: Any, **_options: Any) -> slow.SlowRuns:
        raise slow.SlowRunsError("the database is locked; try again")

    clean_env.setattr(slow, "queue_slow_runs", refuse)
    assert cli.main(["seed", "slow-runs"]) == 1
    assert capsys.readouterr().err == "laueportal seed slow-runs: the database is locked; try again\n"


def test_results_convert_needs_exactly_one_target(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["results", "convert"]) == 2
    assert cli.main(["results", "convert", "3", "--all"]) == 2


def test_usage_errors_exit_2() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["seed", "nonsense"])
    assert error.value.code == 2
