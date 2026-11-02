#!/usr/bin/env python3
"""Verify the VM installation without logging in, registering flows, or submitting jobs.

The launcher installation is <root>/src (a checkout; the package is under backend/src), <root>/envs/laue-portal and
<root>/config/bdp.yaml. The installed packages are checked against the hash-locked sets (requirements/launcher.lock,
plus --lock for more, such as requirements/base.lock on a host that also runs the portal); the settings are read with
laueportal.remote.settings, which refuses unknown keys. Usage:

    python deploy/remote/verify-dm-vm.py --root "$LAUE_PORTAL_ROOT" --config "$LAUE_BDP_CONFIG" [--network]

The installed source is checked against <root>/state/setup/source-manifest.json, the private record of the
reviewed revision: {"files": {"<path relative to <root>/src>": "<sha256 of its bytes>", ...}} (other keys are
kept for the record and not checked). Write it once after installing a reviewed revision, with
--write-source-manifest: it hashes the checkout's tracked files (git ls-files; every regular file outside .git and
__pycache__ when <root>/src is not a git checkout), then verifies as usual.

The native lauelab checks run only when lauelab is installed (a launcher-only environment has none; the runbook's
Validation section); URL requirements are not checked (the locks hold none); only requirements/launcher.lock is
checked unless --lock names more.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import re
from datetime import datetime, timezone
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

LOCK_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^]]*\])?)==([^ \\;]+)")


def requirements(path):
    """The exact pins of a hash-locked requirements file (``uv pip compile --generate-hashes`` output)."""
    for line in path.read_text().splitlines():
        match = LOCK_PIN.match(line.strip())
        if match:
            yield Requirement(f"{match[1]}=={match[2]}")


SOURCE_MANIFEST = Path("state/setup/source-manifest.json")
"""The source manifest, relative to the installation root."""


def source_files(source):
    """The deployed checkout's files, relative to ``source``: git's tracked files in a git checkout, else every
    regular file outside ``.git`` and ``__pycache__``."""
    if (source / ".git").exists():
        listed = subprocess.run(["git", "-C", str(source), "ls-files", "-z"], check=True, capture_output=True).stdout
        return sorted(name for name in listed.decode().split("\0") if name)
    names = []
    for directory, subdirectories, files in os.walk(source):
        subdirectories[:] = [name for name in subdirectories if name not in {".git", "__pycache__"}]
        names += [os.path.relpath(os.path.join(directory, name), source) for name in files]
    return sorted(name for name in names if (source / name).is_file())


def write_source_manifest(source, path):
    """Record the sha256 of each of ``source``'s files in ``path`` (the format :func:`check_source` reads)."""
    files = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in source_files(source)}
    document = {"files": files, "written_at": datetime.now(timezone.utc).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    partial.replace(path)
    return document


def check_source(source, path):
    """Every file the source manifest ``path`` records still has its recorded bytes; the manifest."""
    if not path.is_file():
        raise SystemExit(
            f"No source manifest at {path}: write it once after installing the reviewed revision "
            "(verify-dm-vm.py --write-source-manifest; docs/runbooks/remote-execution.md, Validation)"
        )
    source_manifest = json.loads(path.read_text())
    for name, expected in source_manifest["files"].items():
        actual = hashlib.sha256((source / name).read_bytes()).hexdigest()
        assert actual == expected, f"Source checksum mismatch: {name}"
    return source_manifest


def verify_flow(config, temporary):
    """Check the generated batched flow with disposable, unauthenticated state."""
    from laueportal.remote.flow import client_class

    flow = client_class(config.model_copy(update={"state_directory": str(temporary)}))(auto_registration=False)
    try:
        if config.auth_profile == "personal":
            from globus_sdk.scopes import TransferScopes

            flow.login_manager.add_requirements([TransferScopes.all])
            scopes = flow.login_manager.scopes_by_resource_server(flow.login_manager.required_scopes)
            flow.login_manager.globus_app.add_scope_requirements(scopes)
            assert {"flows.globus.org", "funcx_service", "transfer.api.globus.org"} <= set(scopes)
        definition = flow.get_flow_definition()
        states = definition["States"]
        assert definition["StartAt"] == "InitInputTransfer"
        assert states["InitInputTransfer"]["Next"] == "InputTransfer"
        assert states["InputTransfer"]["Next"] == "AdvanceInputTransfer"
        assert states["AdvanceInputTransfer"]["Next"] == "MoreInputTransfers"
        assert states["MoreInputTransfers"]["Choices"][0]["Next"] == "InputTransfer"
        assert states["MoreInputTransfers"]["Default"] == "InputsReady"
        assert states["InputsReady"]["Next"] == "RunPbsJob"
        assert states["RunPbsJob"]["Next"] == "ResultTransfer"
        assert states["ResultTransfer"]["End"] is True
        return list(states)
    finally:
        # Gladier owns the token store, so GlobusApp.close() does not close it.
        app = flow.login_manager.globus_app
        if app is not None:
            app.close()
        token_storage = getattr(flow.login_manager, "token_storage", None)
        if token_storage is not None:
            token_storage.close()


def verify(root, network, config_file=None, locks=(), write_manifest=False):
    root = root.expanduser().resolve()
    source = root / "src"
    prefix = root / "envs/laue-portal"
    setup = root / "state/setup"
    assert Path(sys.prefix).resolve() == prefix.resolve(), "Use the installed environment's Python"
    os.chdir(source)
    sys.path.insert(0, str(source / "backend" / "src"))
    from laueportal.remote.settings import load_settings

    if write_manifest:
        written = write_source_manifest(source, root / SOURCE_MANIFEST)
        print(f"Wrote {root / SOURCE_MANIFEST} ({len(written['files'])} files)", file=sys.stderr)
    source_manifest = check_source(source, root / SOURCE_MANIFEST)

    checked = {}
    for lock in [source / "requirements" / "launcher.lock", *(source / name for name in locks)]:
        for requirement in requirements(lock):
            distribution = metadata.distribution(requirement.name)
            assert requirement.specifier.contains(distribution.version), f"{lock.name}: {requirement}"
            checked[canonicalize_name(requirement.name)] = distribution.version

    lauelab_file = None
    try:  # a launcher-only environment has no lauelab; one that also runs the portal does
        lauelab_distribution = metadata.distribution("lauelab")
    except metadata.PackageNotFoundError:
        lauelab_distribution = None
    if lauelab_distribution is not None:
        import lauelab

        lauelab_file = lauelab.__file__
        assert Path(lauelab.__file__).resolve().is_relative_to(prefix.resolve())
        direct = json.loads(lauelab_distribution.read_text("direct_url.json") or "{}")
        assert not direct.get("dir_info", {}).get("editable"), "Native lauelab must not depend on a dev checkout"
    names = sorted({d.metadata["Name"] for d in metadata.distributions()}, key=str.lower)
    assert not any(name.lower() in {"funcx", "funcx-endpoint", "globus-compute-endpoint"} for name in names)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)

    config_file = Path(config_file or root / "config/bdp.yaml").expanduser().resolve()
    config = load_settings(config_file)
    assert config.state_directory and Path(config.state_directory).is_dir(), "Configured state directory must exist"
    subprocess.run(
        [str(source / "deploy/remote/dm-stage.sh"), "--help"],
        env={
            **os.environ,
            "LAUE_PORTAL_ROOT": str(root),
            "LAUE_PORTAL_SOURCE": str(source),
            "LAUE_PORTAL_PYTHON": str(prefix / "bin/python"),
            "LAUE_BDP_CONFIG": str(config_file),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    temporary_root = root / "tmp"
    temporary_root.mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="verify-flow-", dir=temporary_root) as temporary:
        states = verify_flow(config, temporary)

    connectivity = {}
    if network:
        for name, url in {
            "auth": "https://auth.globus.org/",
            "flows": "https://flows.globus.org/",
            "compute": "https://compute.api.globus.org/",
            "transfer": "https://transfer.api.globus.org/",
        }.items():
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    connectivity[name] = response.status
            except urllib.error.HTTPError as error:
                # An HTTP authorization/not-found response still verifies HTTPS connectivity.
                connectivity[name] = error.code

    manifest = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "host": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "prefix": str(prefix),
        "source": source_manifest,
        "checked_requirements": checked,
        "packages": {name: metadata.version(name) for name in names},
        "lauelab_file": lauelab_file,
        "https_connectivity": connectivity,
        "flow_states": list(states),
        "globus_login_performed": False,
        "dm_workflow_registration_performed": False,
        "services_started": False,
        "experiment_database_modified": False,
        "pbs_jobs_submitted": False,
    }
    (setup / "install-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in [
                    "host",
                    "python",
                    "prefix",
                    "flow_states",
                    "https_connectivity",
                    "experiment_database_modified",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--config", type=Path, help="Private BDP configuration; defaults to ROOT/config/bdp.yaml")
    parser.add_argument(
        "--network", action="store_true", help="Check public Globus HTTPS responses without credentials"
    )
    parser.add_argument(
        "--lock",
        action="append",
        default=[],
        help="Another lock to check, relative to the checkout (e.g. requirements/base.lock for a portal host)",
    )
    parser.add_argument(
        "--write-source-manifest",
        action="store_true",
        help="First record the checkout's files in ROOT/state/setup/source-manifest.json (after installing a "
        "reviewed revision), then verify",
    )
    arguments = parser.parse_args()
    verify(arguments.root, arguments.network, arguments.config, arguments.lock, arguments.write_source_manifest)
