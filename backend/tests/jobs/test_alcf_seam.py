"""The remote-execution seam: compute code, the run contract, the remote runners, the remote path helpers and the
launcher side (``remote.settings``, ``plan``, ``transport``, ``stage``, ``flow``, ``pbs``, ``auth``) load without the
database, the settings, Redis or RQ; the launcher side needs nothing outside its lock set
(``requirements/launcher.lock``)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from packaging.utils import canonicalize_name

from tests.remote_support import V2_BLOCKED
from tests.settings_support import fresh_interpreter_env

LAUNCHER_MODULES = tuple(
    f"laueportal.remote.{name}"
    for name in ("settings", "plan", "transport", "stage", "dm", "flow", "pbs", "auth", "health")
)
"""What the launcher runs on the DM host (``python -m laueportal.remote.stage``), from the YAML settings only."""
REPO_ROOT = Path(__file__).resolve().parents[3]

PRINT_LOADED = (
    'print("\\n".join(sorted(name for name in sys.modules if name.startswith(("laueportal", "sqlalchemy", "redis", '
    '"rq")))))'
)
"""The last line of a script: the ``laueportal``, ``sqlalchemy``, ``redis`` and ``rq`` modules the interpreter
holds."""


def _loaded_by(script: str, cwd: Path) -> set[str]:
    """The ``laueportal``, ``sqlalchemy``, ``redis`` and ``rq`` modules a fresh interpreter holds after ``script``.

    The interpreter has no LAUEPORTAL_* variable (no settings file, no password) and runs outside ``backend/``
    (``tests.settings_support.fresh_interpreter_env``)."""

    result = subprocess.run(
        [sys.executable, "-c", f"import sys\n{script}\n{PRINT_LOADED}"],
        env=fresh_interpreter_env(),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split())


def _assert_no_database_or_settings(loaded: set[str]) -> None:
    """No database, settings, Redis or RQ module (nor a submodule of one) was loaded."""
    blocked = sorted(name for name in loaded for root in V2_BLOCKED if name == root or name.startswith(root + "."))
    assert not blocked, blocked


@pytest.mark.parametrize(
    "modules",
    [
        (
            "laueportal.indexing.lauego.compute",
            "laueportal.indexing.lauego.live_compute",
            "laueportal.reconstruction.wire.compute",
            "laueportal.reconstruction.wire.live_compute",
        ),
        ("laueportal.jobs.contract", "laueportal.jobs.rundir"),
        ("laueportal.jobs.runner", "laueportal.jobs.processes", "laueportal.remote.paths"),
        ("laueportal.jobs.mpi_runner", "laueportal.science.merge"),
        LAUNCHER_MODULES,
    ],
)
def test_compute_and_run_contract_load_without_the_database_or_the_settings(modules, tmp_path):
    loaded = _loaded_by("\n".join(f"import {name}" for name in modules), tmp_path)

    assert set(modules) <= loaded
    _assert_no_database_or_settings(loaded)


def test_the_registry_imports_the_programs_without_the_database_or_the_settings(tmp_path):
    script = textwrap.dedent(
        """
        from laueportal.jobs.contract import get_compute_function, get_live_compute_function
        get_compute_function("lauego_indexing")
        get_compute_function("wire_reconstruction")
        get_live_compute_function("wire_reconstruction")
        get_live_compute_function("lauego_indexing")
        assert "laueportal.core.db" not in sys.modules and "laueportal.core.settings" not in sys.modules
        """
    )

    loaded = _loaded_by(script, tmp_path)

    assert {
        "laueportal.indexing.lauego.compute",
        "laueportal.indexing.lauego.live_compute",
        "laueportal.reconstruction.wire.compute",
        "laueportal.reconstruction.wire.live_compute",
    } <= loaded
    _assert_no_database_or_settings(loaded)


def test_the_launcher_side_needs_only_the_launchers_packages(tmp_path):
    """Importing the launcher side, gladier's client class included, loads no package outside the launcher's lock
    set (requirements/launcher.lock), the conda-forge packages it is installed with (environment.yml) and the
    standard library."""
    script = textwrap.dedent(
        f"""
        import json
        import importlib.metadata as metadata
        {"; ".join(f"import {name}" for name in LAUNCHER_MODULES)}
        from laueportal.remote.flow import client_class
        from laueportal.remote.pbs import build_script
        from laueportal.remote.plan import build_plan
        packages = metadata.packages_distributions()
        # cython_runtime: the module Cython's extensions (PyYAML's) create, not a package.
        roots = {{name.split(".")[0] for name in sys.modules}} - set(sys.stdlib_module_names)
        roots -= {{"laueportal", "cython_runtime"}}
        print(json.dumps({{root: packages.get(root, []) for root in sorted(roots) if not root.startswith("_")}}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", f"import sys\n{script}"],
        env=fresh_interpreter_env(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    distributions = json.loads(result.stdout.strip().splitlines()[-1])
    lock = (REPO_ROOT / "requirements" / "launcher.lock").read_text(encoding="utf-8")
    allowed = {canonicalize_name(match) for match in re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", lock, re.M)}
    allowed |= {"numpy", "h5py", "cffi", "pycparser"}  # conda-forge (environment.yml), as the env has them
    foreign = {
        root: names
        for root, names in distributions.items()
        if not {canonicalize_name(name) for name in names} & allowed
    }
    assert not foreign, f"the launcher side loads packages outside its lock set: {foreign}"
