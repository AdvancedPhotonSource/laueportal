"""The Python lock sets in ``requirements/``: ``make lock`` compiles them consistently.

Only the committed files are read (no network, nothing installed): the four locks share one cutoff; ``base`` and
``launcher`` have ``dev``'s versions and hashes, so a host can install both; so does ``compute``, except for its own
pins; the launcher and compute pins are the deployed environments' exact pins; conda-forge's packages are in no lock.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPO_ROOT / "requirements"
LOCKS = ("dev", "base", "launcher", "compute")

DEPLOYED_LAUNCHER_PINS = {
    "gladier": "0.11.0",
    "gladier-tools": "0.5.4",
    "globus-compute-sdk": "4.19.0",
    "globus-sdk": "4.9.1",
    "pyyaml": "6.0.2",
}
"""The deployed launcher environment's exact pins."""
DEPLOYED_COMPUTE_PINS = {"pyyaml": "6.0.3", "psutil": "7.2.2", "mpi4py": "4.1.1"}
"""The deployed compute environment's exact pins (lauelab aside: it is installed from GitHub)."""
CONDA_PACKAGES = {"numpy", "h5py", "cffi", "pycparser"}
"""conda-forge's Python packages (environment.yml, with pycparser, which conda's cffi brings)."""

PIN = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]*\])?==(\S+)")


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock(name: str) -> dict[str, tuple[str, frozenset[str]]]:
    """A lock's packages: version and hashes."""
    packages: dict[str, tuple[str, set[str]]] = {}
    current = None
    for line in (REQUIREMENTS / f"{name}.lock").read_text().splitlines():
        if match := PIN.match(line):
            current = normalized(match.group(1))
            packages[current] = (match.group(2), set())
        elif current and (digest := re.search(r"--hash=(sha256:[0-9a-f]{64})", line)):
            packages[current][1].add(digest.group(1))
    return {package: (version, frozenset(hashes)) for package, (version, hashes) in packages.items()}


def pins(name: str) -> dict[str, str]:
    """The exact pins of a ``.in`` file."""
    lines = (REQUIREMENTS / f"{name}.in").read_text().splitlines()
    return {normalized(m.group(1)): m.group(2) for line in lines if (m := PIN.match(line))}


def test_every_lock_has_the_same_cutoff_and_is_compiled_from_its_own_input() -> None:
    headers = {name: (REQUIREMENTS / f"{name}.lock").read_text().splitlines()[:3] for name in LOCKS}
    cutoffs = {lines[0] for lines in headers.values()}
    assert len(cutoffs) == 1, cutoffs
    (cutoff,) = cutoffs
    assert re.fullmatch(r"# exclude-newer: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", cutoff)
    for name, lines in headers.items():
        assert f"--exclude-newer {cutoff.removeprefix('# exclude-newer: ')} " in lines[2]
        assert lines[2].endswith(f" requirements/{name}.in -o requirements/{name}.lock.tmp")
        # Every hash is there: `make setup` installs with --require-hashes.
        assert all(hashes for _, hashes in lock(name).values()), name


@pytest.mark.parametrize("name", ["base", "launcher"])
def test_base_and_launcher_have_devs_versions(name: str) -> None:
    dev = lock("dev")
    packages = lock(name)
    assert packages
    assert {package: pinned for package, pinned in packages.items() if dev.get(package) != pinned} == {}


def test_compute_has_devs_versions_except_its_own_pins() -> None:
    dev = lock("dev")
    differ = {package for package, pinned in lock("compute").items() if dev.get(package) != pinned}
    assert differ <= set(pins("compute")), differ


def test_the_launcher_and_compute_sets_keep_the_deployed_pins() -> None:
    assert pins("launcher") == DEPLOYED_LAUNCHER_PINS
    assert pins("compute") == DEPLOYED_COMPUTE_PINS
    launcher, compute = lock("launcher"), lock("compute")
    assert {package: launcher[package][0] for package in DEPLOYED_LAUNCHER_PINS} == DEPLOYED_LAUNCHER_PINS
    assert {package: compute[package][0] for package in DEPLOYED_COMPUTE_PINS} == DEPLOYED_COMPUTE_PINS


def test_the_dev_env_has_the_launcher_set_but_not_the_compute_set() -> None:
    dev_in = (REQUIREMENTS / "dev.in").read_text().splitlines()
    assert {"-r base.in", "-r launcher.in"} <= set(dev_in)
    assert not any("compute" in line for line in dev_in if not line.startswith("#"))
    dev = lock("dev")
    assert set(lock("launcher")) <= set(dev)
    assert "mpi4py" not in dev


def test_conda_forges_packages_are_in_no_lock() -> None:
    conda_pins = (REQUIREMENTS / "conda-pins.txt").read_text().splitlines()
    assert {normalized(m.group(1)) for line in conda_pins if (m := PIN.match(line))} == CONDA_PACKAGES
    for name in LOCKS:
        assert not CONDA_PACKAGES & set(lock(name)), name
    # The Makefile constrains and leaves out the same packages that scripts/setup.sh checks against conda.
    conda_py = re.search(r"^CONDA_PY := (.*)$", (REPO_ROOT / "Makefile").read_text(), re.MULTILINE)
    assert conda_py and conda_py[1].split() == ["numpy", "h5py", "cffi", "pycparser"]
    setup = (REPO_ROOT / "scripts" / "setup.sh").read_text()
    assert 'for name in ("numpy", "h5py", "cffi", "pycparser"):' in setup
