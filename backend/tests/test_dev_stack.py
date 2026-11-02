"""The development stack's scripts: the parts of ``scripts/stack.sh`` that start nothing, and when ``make dev-up``
rebuilds the SPA (a dry run). ``make dev-up``/``dev-down`` and ``make e2e`` exercise the stack itself.

Every test runs the scripts with a stand-in workspace (``$LAUE_DEV_WORKSPACE`` in ``tmp_path``), so the real one's
sockets and pidfiles are never touched.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from laueportal.core.settings import ConfigError, ExecutionSettings, load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
STACK = REPO_ROOT / "scripts" / "stack.sh"
DEV_CONF = REPO_ROOT / "deploy" / "supervisor" / "dev.conf"
E2E_CONF = REPO_ROOT / "deploy" / "supervisor" / "e2e.conf"
RENDER_PROD = REPO_ROOT / "deploy" / "render-prod.py"


def script_env(workspace: Path, **extra: str) -> dict[str, str]:
    """This environment (PATH has the env's bin) with a stand-in workspace and no ``LAUEPORTAL_*`` variable."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("LAUEPORTAL_")}
    return {**env, "LAUE_DEV_WORKSPACE": str(workspace), **extra}


def run(*command: str | Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(part) for part in command], env=env, capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# deploy/supervisor/*.conf (make dev-up checks the running stack: docs/runbooks/dev-setup.md, "Troubleshooting")


def supervisor_conf(path: Path) -> configparser.RawConfigParser:
    conf = configparser.RawConfigParser()  # raw: the %(ENV_...)s expansions are supervisor's
    assert conf.read(path) == [str(path)]
    return conf


@pytest.mark.parametrize("path", [DEV_CONF, E2E_CONF], ids=["dev", "e2e"])
def test_a_stack_creates_private_files(path: Path) -> None:
    """supervisord applies its umask before it starts the programs: their logs (and what they create) are 0600."""
    assert supervisor_conf(path).get("supervisord", "umask") == "077"


# ---------------------------------------------------------------------------
# make dev-up's SPA

SPA_SOURCES = (
    "src/features/scans/ScanNotFound.tsx",
    "public/theme-init.js",
    "index.html",
    "package.json",
    "pnpm-lock.yaml",
    "vite.config.ts",
    "tsconfig.json",
)


def test_dev_up_rebuilds_the_spa_only_when_a_source_is_newer_than_the_build(tmp_path: Path) -> None:
    """``make -n dev-up`` on a stand-in tree (the Makefile and the SPA's sources, nothing run): no build while
    ``frontend/dist/index.html`` is newer than every source; a build when it is missing, a source is newer, or a
    file under ``src`` is deleted or moved in with an old mtime (its folder's mtime). A path with a space is left
    out of the prerequisites instead of stopping make ("No rule to make target")."""
    (tmp_path / "Makefile").write_text(MAKEFILE.read_text())
    frontend = tmp_path / "frontend"
    built = frontend / "dist" / "index.html"
    spaced = frontend / "src" / "routes copy" / "a copy.tsx"
    for path in [*(frontend / source for source in SPA_SOURCES), spaced, built]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    env = {key: value for key, value in os.environ.items() if not key.startswith("MAKE")}  # not the outer make's flags
    then = time.time() - 100

    def age() -> None:
        """Every source (files and folders) at ``then``, the build 10 s later."""
        for path in [frontend, *frontend.rglob("*")]:
            os.utime(path, (then, then))
        os.utime(built, (then + 10, then + 10))

    def builds() -> bool:
        result = run("make", "-n", "-C", tmp_path, "dev-up", env=env)
        assert result.returncode == 0 and "scripts/stack.sh up" in result.stdout, result.stderr
        return "pnpm -C frontend build" in result.stdout

    age()
    assert not builds()
    for source in SPA_SOURCES:
        os.utime(frontend / source, (then + 20, then + 20))
        assert builds(), source
        age()
    (frontend / SPA_SOURCES[0]).unlink()
    assert builds()
    age()
    moved_in = frontend / "src" / "features" / "scans" / "old.ts"
    moved_in.write_text("")
    os.utime(moved_in, (then, then))
    assert builds()
    age()
    built.unlink()
    assert builds()


# ---------------------------------------------------------------------------
# scripts/stack.sh (what runs without starting a stack)


@pytest.mark.parametrize(
    "argv",
    [[], ["start", str(DEV_CONF)], ["up"], ["up", "--seed=sometimes", str(DEV_CONF)], ["down", "a.conf", "b.conf"]],
)
def test_stack_usage_errors_exit_2(tmp_path: Path, argv: list[str]) -> None:
    result = run(STACK, *argv, env=script_env(tmp_path))
    assert result.returncode == 2 and "usage:" in result.stderr


def test_stack_refuses_a_conf_without_its_pidfile_and_socket(tmp_path: Path) -> None:
    conf = tmp_path / "odd.conf"
    conf.write_text(DEV_CONF.read_text())  # names supervisord-dev.pid, not supervisord-odd.pid
    result = run(STACK, "status", conf, env=script_env(tmp_path))
    assert result.returncode == 2 and "must use the pidfile" in result.stderr


def test_the_e2e_stack_needs_its_settings_overrides(tmp_path: Path) -> None:
    """Without e2e.sh beside it, the e2e conf would seed and serve the dev database."""
    conf = tmp_path / "e2e.conf"
    conf.write_text(E2E_CONF.read_text())
    result = run(STACK, "status", conf, env=script_env(tmp_path))
    assert result.returncode == 2 and f"{tmp_path / 'e2e.sh'} is missing" in result.stderr


def test_the_e2e_stack_pins_every_execution_setting() -> None:
    """e2e.sh pins each ``execution`` setting, so a dev.toml edit (or a new setting) cannot change e2e runs."""
    pinned = re.findall(r"^export LAUEPORTAL_EXECUTION__([A-Z_]+)=", (E2E_CONF.with_suffix(".sh")).read_text(), re.M)
    assert sorted(name.lower() for name in pinned) == sorted(ExecutionSettings.model_fields)


def test_the_e2e_stack_never_enables_remote_execution_and_seeds_the_crux_run(tmp_path: Path) -> None:
    """With dev.toml enabling remote execution, the e2e stack's settings still disable it (its demo's R4 has no DM job
    behind it), and its seed adds R4 (``seed demo --crux-run``)."""
    workspace = tmp_path / "ws"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "dev.toml").write_text(
        f'[database]\npath = "{tmp_path}/dev.db"\n[files]\nruns_root = "{tmp_path}/runs"\n'
        f'[remote]\nenabled = true\nsettings_file = "{tmp_path}/bdp.yaml"\n'
    )
    script = (
        '. "$1/scripts/env.sh" && . "$1/deploy/supervisor/e2e.sh" && echo "${SEED_DEMO_OPTIONS[@]}" '
        "&& laueportal config show"
    )
    result = run("bash", "-c", script, "e2e", REPO_ROOT, env=script_env(workspace))
    assert result.returncode == 0, result.stderr
    options, settings = result.stdout.split("\n", 1)
    assert options == "--crux-run"
    assert json.loads(settings)["remote"]["enabled"] is False


def test_the_e2e_stack_s_dm_is_its_stand_in_which_fails_every_call_until_a_test_writes_its_state(
    tmp_path: Path,
) -> None:
    """With dev.toml naming a real DM, the e2e stack's live settings name its stand-in (``FakeDMInstall`` under the
    workspace's ``e2e/fake-dm``, installed by ``prepare_stack`` at ``up`` only), on this host, and the stand-in fails
    every DM call until a test writes its state, so the notify specs see the DAQ list unavailable
    (``e2e/live.spec.ts``)."""
    workspace = tmp_path / "ws"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "dev.toml").write_text(
        f'[database]\npath = "{tmp_path}/dev.db"\n[files]\nruns_root = "{tmp_path}/runs"\n'
        '[live]\ndm_setup_file = "/real/dm-setup.sh"\ndm_ssh_host = "dm-host"\n'
    )
    fake = workspace / "e2e" / "fake-dm"
    daq_list = "from laueportal.core.settings import get_settings; from laueportal.jobs.daq import DaqList; " + (
        "print(DaqList(get_settings().live).get().error)"
    )
    script = '. "$1/scripts/env.sh" && . "$1/deploy/supervisor/e2e.sh" && test ! -e "$2" && prepare_stack && '
    script += 'laueportal config show && python -c "$3"'
    result = run("bash", "-c", script, "e2e", REPO_ROOT, fake, daq_list, env=script_env(workspace))
    assert result.returncode == 0, result.stderr
    settings, error = result.stdout.rsplit("}\n", 1)
    live = json.loads(settings + "}")["live"]
    assert (live["dm_setup_file"], live["dm_ssh_host"]) == (str(fake / "dm-setup.sh"), None)
    assert (fake / "state.json").read_text() == "not JSON\n"
    assert error.startswith("DM call failed: exit status 1: json.decoder.JSONDecodeError"), error


def test_a_stack_that_is_not_running_is_reported_and_its_stale_files_removed(tmp_path: Path) -> None:
    """``running`` says no (exit 1), ``status`` says "not running" with the LSB status 3, ``down`` says "not
    running" (exit 0). ``down`` removes a stale pidfile and socket, and never signals the process a stale pidfile
    names (here: this test's own process)."""
    workspace = tmp_path / "ws"
    (workspace / "tmp").mkdir(parents=True)
    pidfile, socket = workspace / "tmp" / "supervisord-dev.pid", workspace / "tmp" / "supervisor-dev.sock"
    pidfile.write_text(f"{os.getpid()}\n")
    socket.write_text("")
    env = script_env(workspace)

    assert run(STACK, "running", DEV_CONF, env=env).returncode == 1
    status = run(STACK, "status", DEV_CONF, env=env)
    assert (status.returncode, status.stdout) == (3, "dev stack: not running\n")
    down = run(STACK, "down", DEV_CONF, env=env)
    assert (down.returncode, down.stdout) == (0, "dev stack: not running\n")
    assert not pidfile.exists() and not socket.exists()


def test_no_stack_runs_redis_or_a_remote_poller_program_the_worker_polls(tmp_path: Path) -> None:
    """The remote poller is a thread of the worker, so no stack has a remote_poller program (dev, e2e, the production
    template), and stack.sh starts and counts the same programs whether the settings enable remote execution or not. No
    stack runs Redis (the job table is the queue)."""
    for conf in (DEV_CONF, E2E_CONF, REPO_ROOT / "deploy/supervisor/prod.conf.template"):
        parsed = supervisor_conf(conf)
        programs = [section for section in parsed.sections() if section.startswith("program:")]
        assert "program:worker" in programs and "program:remote_poller" not in programs, conf
        assert not any(" remote poll" in parsed.get(section, "command") for section in programs), conf
        assert "program:redis" not in programs and "redis" not in conf.read_text().lower(), conf

    workspace = tmp_path / "ws"
    (workspace / "config").mkdir(parents=True)
    settings = workspace / "config" / "dev.toml"
    base = f'[database]\npath = "{tmp_path}/dev.db"\n[files]\nruns_root = "{tmp_path}/runs"\n'
    env = script_env(workspace)
    for remote in ("", f'[remote]\nenabled = true\nsettings_file = "{tmp_path}/bdp.yaml"\n'):
        settings.write_text(base + remote)
        result = run(STACK, "programs", DEV_CONF, env=env)
        assert (result.returncode, result.stdout.split()) == (0, ["api", "worker", "vite"]), result.stderr
        result = run(STACK, "programs", E2E_CONF, env=env)
        assert (result.returncode, result.stdout.split()) == (0, ["api", "worker"]), result.stderr


# ---------------------------------------------------------------------------
# The production templates (deploy/render-prod.py)

DATA_ROOTS = {"data": "/net/fileserver/export/data1", "archive": "/net/storage/archive"}
"""The data roots a deployer writes into the rendered prod.toml in place of the template's placeholder."""


def prod_values(root: Path) -> dict[str, str]:
    return {
        "ROOT": str(root),
        "RUNS_ROOT": "/net/fileserver/export/data1/laueportal/runs",
        "API_PORT": "2110",
        "HOST_NAME": "portal.example.org",
        "REMOTE_ENABLED": "false",
    }


def render_prod(output: Path | None, values: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the renderer; ``output`` None renders into its default folder, ``<ROOT>/config``."""
    arguments = [f"{name}={value}" for name, value in values.items()]
    folder = () if output is None else ("--output-dir", output)
    return run(sys.executable, RENDER_PROD, *folder, *extra, *arguments, env=dict(os.environ))


def toml_keys(table: dict[str, Any], prefix: str = "") -> set[str]:
    """Every key of a TOML document, dotted; the named roots and motor groups as their table."""
    keys = set()
    for key, value in table.items():
        if isinstance(value, dict) and key not in ("roots", "motor_groups"):
            keys |= toml_keys(value, f"{prefix}{key}.")
        else:
            keys.add(f"{prefix}{key}")
    return keys


def test_the_production_templates_render_and_parse(outside_checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendered, the settings refuse to load until the deployer replaces the data roots' placeholder; then they load
    with the real model (the data roots, the runs root on NFS, the database on the root's local disk in WAL mode, the
    template's execution values, every key the dev template documents); supervisor's own parser reads the stack (the API
    without reload and the worker, which runs the remote poller as a thread; TERM; the worker's stopwaitsecs above the
    drain; rotated logs). The files are private, with no render placeholder left."""
    tmp_path = outside_checkout
    from supervisor.options import ServerOptions

    for name in [name for name in os.environ if name.upper().startswith("LAUEPORTAL_")]:
        monkeypatch.delenv(name)  # the settings file alone (scripts/env.sh sets some)
    root = tmp_path / "root"
    for folder in ("src", "logs", "state"):  # the runbook makes them; supervisor's parser checks they exist
        (root / folder).mkdir(parents=True)
    values = prod_values(root)
    result = render_prod(None, values)  # into <ROOT>/config, which supervisord.conf names
    assert result.returncode == 0, result.stderr
    files = {path.name: path for path in map(Path, result.stdout.split())}
    assert sorted(files) == ["prod.toml", "supervisord.conf"]
    assert {path.parent for path in files.values()} == {root / "config"} and not result.stderr
    assert all(path.stat().st_mode & 0o777 == 0o600 and "{{" not in path.read_text() for path in files.values())

    with pytest.raises(ConfigError, match=r"files\.roots: Value error, root 'data' must be an absolute path"):
        load_settings(files["prod.toml"])
    text = files["prod.toml"].read_text()
    placeholder = 'data = "EDIT: a data root\'s absolute path"\n'
    assert text.count(placeholder) == 1
    files["prod.toml"].write_text(text.replace(placeholder, "".join(f'{k} = "{v}"\n' for k, v in DATA_ROOTS.items())))
    settings = load_settings(files["prod.toml"])
    assert {name: str(path) for name, path in settings.files.roots.items()} == {
        **DATA_ROOTS,
        "runs": values["RUNS_ROOT"],
    }
    assert str(settings.files.runs_root) == values["RUNS_ROOT"]
    assert (settings.database.path, settings.database.journal_mode) == (root / "db" / "laueportal.db", "wal")
    execution = settings.execution
    assert (execution.num_threads, execution.memory_limit_mb, execution.workers) == (35, 50000, 4)
    assert (settings.app.host, settings.app.port) == ("127.0.0.1", 2110)
    assert settings.app.allowed_hosts == ["localhost", "127.0.0.1", "portal.example.org"]
    assert settings.app.static_dir == root / "src" / "frontend" / "dist"
    assert not settings.remote.enabled
    documented = toml_keys(tomllib.loads((REPO_ROOT / "deploy/config/dev.toml.template").read_text()))
    assert toml_keys(tomllib.loads(files["prod.toml"].read_text())) == documented
    assert {"files.probe_timeout_seconds", "files.hung_mount_seconds", "files.probe_threads"} <= documented
    assert "reconstruction.product_cache_mib" in documented

    options = ServerOptions()
    options.configfile = str(files["supervisord.conf"])
    options.process_config(do_usage=False)
    programs = {config.name: config for group in options.process_group_configs for config in group.process_configs}
    assert list(programs) == ["api", "worker"]
    bin_dir = root / "envs" / "laue-portal" / "bin"
    assert programs["api"].command == f"{bin_dir}/laueportal serve"
    assert programs["worker"].command == f"{bin_dir}/laueportal worker"
    assert [config.autostart for config in programs.values()] == [True, True]
    assert all(config.stopsignal == 15 for config in programs.values())  # SIGTERM
    assert programs["worker"].stopwaitsecs > execution.shutdown_grace_seconds
    assert all(config.stdout_logfile_maxbytes and config.stdout_logfile_backups for config in programs.values())
    assert (options.logfile_maxbytes, options.logfile_backups) == (10 * 1024 * 1024, 5)


def test_the_production_renderer_refuses_what_does_not_fit_and_writes_nothing(outside_checkout: Path) -> None:
    """A placeholder without a value, a value no template uses or one unfit for its place (a path ending in '/' among
    them: ``<ROOT>/config`` would read ``//config``) is refused before anything is written, and so is an existing file
    without --force (the deployer edits prod.toml). A render elsewhere than ``<ROOT>/config`` says it is not the
    installed files."""
    tmp_path = outside_checkout
    output = tmp_path / "config"
    values = prod_values(tmp_path / "root")
    del values["REMOTE_ENABLED"]
    values |= {
        "ROOT": f"{tmp_path}/root/",
        "RUNS_ROOT": "/net/my runs",
        "HOST_NAME": "http://portal",
        "PORT": "1",
    }

    refused = render_prod(output, values)

    assert refused.returncode == 1 and not output.exists()
    for problem in (
        "REMOTE_ENABLED: no value given",
        "PORT: no template uses it",
        "ROOT: must be an absolute path",
        "RUNS_ROOT: must be an absolute path",
        "HOST_NAME: must be a host name",
    ):
        assert problem in refused.stderr, problem
    elsewhere = render_prod(output, prod_values(tmp_path / "root"))
    assert elsewhere.returncode == 0 and "not the installed files" in elsewhere.stderr
    again = render_prod(output, prod_values(tmp_path / "root"))
    assert again.returncode == 1 and "exists (--force replaces it)" in again.stderr
    assert render_prod(output, prod_values(tmp_path / "root"), "--force").returncode == 0
