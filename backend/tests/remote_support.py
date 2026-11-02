"""Remote-execution fixtures: portal runs under an APS filesystem root, the submission plan, staging, goldens.

The layout: the APS filesystem root holds the experiment directory, whose ``analysis/`` holds the run directories; a
second root plays Eagle. Runs are created by the portal's run creation from the case's request values
(:func:`lauego_request_values`, :func:`wire_request_values`), each with the case's ``backend``.

The plan is ``laueportal.remote.plan`` (:func:`save_plan` validates a settings mapping into
``remote.settings.RemoteSettings`` first); ``tests/fixtures/remote_goldens/`` holds the plans the same runs must get,
and ``tests/jobs/test_runner.py`` and ``tests/remote/test_remote_goldens.py`` check that the portal writes the same.

:func:`golden_documents` turns a run's files into the text the goldens hold: the temporary base directory,
hosts, the runner's PID, library versions and timings are replaced by placeholders, and every digit of a
time is zeroed (so the time format is still compared). It works on the files' text, so the JSON formatting
(indentation, key order, separators, the final newline) is compared byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import h5py
import numpy as np
from sqlalchemy.orm import Session

from laueportal.indexing import service as indexing_service
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import write_json_file
from laueportal.reconstruction import service as reconstruction_service
from laueportal.reconstruction.wire.params import WireParamsV1
from laueportal.remote import plan as remote_plan
from laueportal.remote.dm import DMClient, DMRecordError, Records
from laueportal.remote.settings import RemoteSettings, parse_settings
from laueportal.scans.models import Metadata
from tests.conftest import create_test_metadata
from tests.wire_support import OPTIONS, write_wire_scan

FIXTURES = Path(__file__).parent / "fixtures"
LAUELAB_FIXTURES = FIXTURES / "lauelab"
GOLDENS = FIXTURES / "remote_goldens"
GEOMETRY = LAUELAB_FIXTURES / "geoN_2022-03-29_14-15-05.xml"
CRYSTAL = LAUELAB_FIXTURES / "Ni.xml"
NI_FRAMES = ("synthetic_ni_grain_a.h5", "synthetic_ni_two_grains.h5", "synthetic_ni_empty.h5")

# A fixed submission ID, so the goldens need no ID normalization (the backend generates a uuid4).
SUBMISSION_ID = "6f1c2a4e-3b5d-4e7f-8a9b-0c1d2e3f4a5b"
SUBMITTED_AT = datetime(2026, 10, 16, 9, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Layout:
    """Where everything lives under one temporary ``base`` directory."""

    base: Path

    @property
    def aps(self) -> Path:  # the APS filesystem root (storage.aps.filesystem_root)
        return self.base / "aps"

    @property
    def experiment(self) -> Path:  # bdp.yaml's experiment_directory
        return self.aps / "LauePortal"

    @property
    def analysis(self) -> Path:  # where run directories live
        return self.experiment / "analysis"

    @property
    def data(self) -> Path:  # inputs, geometry and crystal files
        return self.experiment / "data"

    @property
    def eagle(self) -> Path:  # the Eagle filesystem root (storage.eagle.filesystem_root)
        return self.base / "eagle"

    @property
    def submissions(self) -> Path:  # bdp.yaml's submission_directory (the DM inbox)
        return self.analysis / "submissions"


def bdp_settings(layout: Layout) -> dict[str, Any]:
    """The BDP settings (laid out as ``deploy/remote/bdp.yaml.template``) for ``layout``, with small resources."""

    return {
        "enabled": True,
        "experiment_directory": str(layout.experiment),
        "poll_seconds": 10,
        "submission_directory": str(layout.submissions),
        "state_directory": str(layout.base / "launcher-state"),
        "auth_profile": "personal",
        "dm": {
            "ssh_host": None,
            "setup_file": "/dm/etc/dm.setup.sh",
            "owner": "test-owner",
            "workflow": "laue-portal-bdp",
            "cancel_workflow": "laue-portal-bdp-cancel",
            "timeout_seconds": 30,
            "launcher_ssh_host": None,
            "launcher_python": "/launcher/envs/laue-portal/bin/python",
            "launcher_code": "/launcher/src",
            "launcher_config": "/launcher/config/bdp.yaml",
        },
        "storage": {
            "aps": {
                "collection_id": "00000000-0000-0000-0000-000000000001",
                "filesystem_root": str(layout.aps),
                "collection_root": "/",
            },
            "eagle": {
                "collection_id": "00000000-0000-0000-0000-000000000002",
                "filesystem_root": str(layout.eagle),
                "collection_root": "/",
            },
        },
        "targets": {
            "crux": {
                "enabled": True,
                "compute_endpoint": "00000000-0000-0000-0000-000000000003",
                "workspace": str(layout.eagle / "workspace"),
                "analysis_python": "/compute/envs/analysis/bin/python",
                "code_directory": "/compute/src",
                "module_commands": [],
                "pbs": {"account": "TEST_ACCOUNT", "queue": "debug", "nodes": 1, "walltime": "00:10:00"},
                "execution": {
                    "workers": 1,
                    "max_in_flight": 2,
                    "reconstruction_workers": 1,
                    "threads_per_worker": 1,
                    "indexing_ranks_per_node": 1,
                    "indexing_threads_per_worker": 1,
                    "memory_limit_mb": 256,
                },
            },
            "aps": {
                "enabled": True,
                "submit_host": None,
                "analysis_python": "/local-compute/envs/analysis/bin/python",
                "code_directory": "/local-compute/src",
                "module_commands": [],
                "pbs": {"account": None, "queue": "batch", "nodes": 1, "cpus": 4, "walltime": "00:10:00"},
                "execution": {"workers": 1, "max_in_flight": 2, "reconstruction_workers": 1, "threads_per_worker": 1},
            },
        },
    }


# --- fake DM ----------------------------------------------------------------------------------------


class BatchedFake:
    """``fetch``, the remote poller's one DM call per pass (``remote.dm.DMClient.fetch``), for a fake DM client
    that answers ``get`` and ``find``, as DM's API script answers it: the jobs of each submission asked for, with the
    record of a single job found, then the records asked for; whatever ``get`` or ``find`` raises is that record's
    error. ``fetches`` holds each call's IDs and submission IDs; ``failing["fetch"]`` (when the fake has
    ``failing``) is an error the next call raises as a whole, as the real client's ``_call`` does. ``run`` (the
    launcher's command run directly, ``remote.poller.request_cancellation``) succeeds, each recorded in ``commands``
    with its timeout; ``abort`` (the worker's end) is recorded in ``aborted``."""

    def run(self, argv: list[str], *, input: str | None = None, timeout: float | None = None) -> Any:
        if not hasattr(self, "commands"):
            self.commands: list[tuple[list[str], float | None]] = []
        self.commands.append((list(argv), timeout))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def abort(self) -> None:
        self.aborted = True

    def fetch(self, ids: Any, submission_ids: Any) -> Records:
        if not hasattr(self, "fetches"):
            self.fetches: list[tuple[list[str], list[str]]] = []
        ids, submission_ids = list(dict.fromkeys(ids)), list(dict.fromkeys(submission_ids))
        self.fetches.append((list(ids), list(submission_ids)))
        error = getattr(self, "failing", {}).pop("fetch", None)
        if error is not None:
            raise error
        by_id: dict[str, Any] = {}
        by_submission: dict[str, Any] = {}
        for submission_id in submission_ids:
            try:
                found = list(self.find(submission_id))  # type: ignore[attr-defined]
            except Exception as failure:
                by_submission[submission_id] = DMRecordError(f"{type(failure).__name__}: {failure}")
                continue
            by_submission[submission_id] = found
            if len(found) == 1 and found[0].get("id") is not None and found[0]["id"] not in ids:
                ids.append(found[0]["id"])
        for job_id in ids:
            try:
                by_id[str(job_id)] = self.get(job_id)  # type: ignore[attr-defined]
            except Exception as failure:
                by_id[str(job_id)] = DMRecordError(f"{type(failure).__name__}: {failure}")
        return Records(by_id, by_submission)


_FAKE_DM_PACKAGE = '''"""The stand-in ``dm`` package (tests.remote_support): every DM call imports it once, whatever its operation, so
with ``FAKE_DM_CALLS`` set each call appends a line (its pid, the time) to that file, and a test counts the calls made
in other processes (``FakeDMInstall``)."""
import os, time

if os.environ.get("FAKE_DM_CALLS"):
    with open(os.environ["FAKE_DM_CALLS"], "a") as handle:
        handle.write("%d %.3f\\n" % (os.getpid(), time.time()))
'''

_FAKE_DM_API = '''"""A stand-in for DM's WorkflowProcApi over a JSON file of processing jobs (tests.remote_support.FakeDMService);
the remote runs' operations build one (``submit``, ``get``, ``fetch``)."""
import json, os

STATE = os.environ["FAKE_DM_STATE"]


class Job:  # DM's DmObject: the record is its ``data``
    def __init__(self, data):
        self.data = data


class WorkflowProcApi:
    def __init__(self):
        with open(STATE) as handle:
            self.state = json.load(handle)

    def getProcessingJobById(self, owner, job_id):
        if job_id in self.state["failing"]:
            raise RuntimeError("DM cannot read job " + job_id)
        return Job(self.state["jobs"][job_id])

    def getProcessingJobRecord(self, job_id):
        if job_id in self.state["failing"] or job_id not in self.state["jobs"]:
            raise LookupError("no processing job record " + job_id)
        return Job(self.state["jobs"][job_id])

    def listProcessingJobRecords(self, queryDict, limit):
        wanted = queryDict["submissionId"]
        return [Job(job) for job in self.state["jobs"].values() if job.get("submissionId") == wanted][:limit]

    def startProcessingJob(self, owner, workflow, arguments):
        job = {"id": "dm-%03d" % (len(self.state["jobs"]) + 1), "status": "pending", "workflow": workflow}
        job.update({key: value for key, value in arguments.items() if key.endswith("ubmissionId")})
        self.state["jobs"][job["id"]] = job
        with open(STATE, "w") as handle:
            json.dump(self.state, handle)
        return Job(job)
'''


_FAKE_DAQ_API = '''"""A stand-in for DM's ExperimentDaqApi over the fake DM's ``daqs`` (DAQ records by ID; a record that is no
object is given as it is, as DM could; an unknown ID is DM's ``ObjectNotFound``)."""
import json, os

STATE = os.environ["FAKE_DM_STATE"]


class ObjectNotFound(Exception):  # DM's dm.common.exceptions.objectNotFound.ObjectNotFound
    pass


class Daq:  # DM's DmObject: the record is its ``data``
    def __init__(self, data):
        self.data = data


class ExperimentDaqApi:
    def __init__(self):
        with open(STATE) as handle:
            self.state = json.load(handle)

    def listDaqs(self, status):
        return [Daq(daq) if isinstance(daq, dict) else daq for daq in self.state["daqs"].values()]

    def getDaqInfo(self, daq_id):
        if daq_id not in self.state["daqs"]:
            raise ObjectNotFound("no DAQ " + daq_id)
        daq = self.state["daqs"][daq_id]
        return Daq(daq) if isinstance(daq, dict) else daq
'''

_FAKE_CAT_API = '''"""A stand-in for DM's FileCatApi over the fake DM's ``files``: the catalog's rows in the order they were
catalogued, an experiment's matched as DM's query does for what the catalog thread asks (equality, ``$regex``,
``$gte``), then ``skip`` and ``limit``, each row with only the keys asked for; ``delay``: seconds each request waits
first (a call in flight, a slow DM); ``start_delay``: seconds the API's construction waits (DM's start: its imports,
its login). With ``FAKE_DM_REQUESTS`` set, each request appends a JSON line (its pid, query, skip and limit) to that
file, so a test counts the DM requests inside a call."""
import json, os, re, time

STATE = os.environ["FAKE_DM_STATE"]
REQUESTS = os.environ.get("FAKE_DM_REQUESTS")


class FileMetadata:  # DM's DmObject
    def __init__(self, data):
        self.data = data


def _matches(row, query):
    for key, wanted in query.items():
        value = row.get(key)
        if isinstance(wanted, dict):
            if "$regex" in wanted and not (isinstance(value, str) and re.search(wanted["$regex"], value)):
                return False
            if "$gte" in wanted and not (isinstance(value, (int, float)) and value >= wanted["$gte"]):
                return False
        elif value != wanted:
            return False
    return True


class FileCatApi:
    def __init__(self):
        with open(STATE) as handle:
            self.state = json.load(handle)
        time.sleep(self.state.get("start_delay", 0))

    def getExperimentFiles(self, experimentName, queryDict=None, skip=0, limit=None, keyList=None):
        if REQUESTS:
            with open(REQUESTS, "a") as handle:
                request = {"pid": os.getpid(), "query": queryDict, "skip": skip, "limit": limit}
                handle.write(json.dumps(request) + "\\n")
        time.sleep(self.state.get("delay", 0))
        rows = [row for row in self.state["files"] if row.get("experimentName") == experimentName]
        rows = [row for row in rows if _matches(row, queryDict or {})][skip:]
        if limit is not None:
            rows = rows[:limit]
        if keyList is not None:
            rows = [{key: row[key] for key in keyList if key in row} for row in rows]
        return [FileMetadata(row) for row in rows]
'''


_FAKE_DS_API = '''"""A stand-in for DM's FileDsApi (its storage service): ``restorePermissions`` (the launcher's
permissions stage) answers nothing and, with ``FAKE_DM_REQUESTS`` set, appends a JSON line with its arguments (and its
pid) to that file."""
import json, os

REQUESTS = os.environ.get("FAKE_DM_REQUESTS")


class FileDsApi:
    def restorePermissions(self, experimentPath, experimentName):
        if REQUESTS:
            with open(REQUESTS, "a") as handle:
                restored = {"experimentPath": experimentPath, "experimentName": experimentName}
                handle.write(json.dumps({"pid": os.getpid(), "restorePermissions": restored}) + "\\n")
        return None
'''


def write_fake_dm_api(root: Path, *, workflow: bool = True) -> None:
    """The stand-in ``dm`` package (DM's workflow, DAQ, catalog and storage APIs) under ``root``; ``workflow`` False:
    without the workflow service (``proc_web_service``), as a DM setup for the live runs' services only."""
    modules = {
        ("daq_web_service", "experimentDaqApi.py"): _FAKE_DAQ_API,
        ("cat_web_service", "fileCatApi.py"): _FAKE_CAT_API,
        ("ds_web_service", "fileDsApi.py"): _FAKE_DS_API,
    }
    if workflow:
        modules[("proc_web_service", "workflowProcApi.py")] = _FAKE_DM_API
    for (service, name), source in modules.items():
        package = root / "dm" / service / "api"
        package.mkdir(parents=True, exist_ok=True)
        for folder in (package, package.parent):
            (folder / "__init__.py").write_text("")
        (package / name).write_text(source)
    (root / "dm" / "__init__.py").write_text(_FAKE_DM_PACKAGE)


def _requests(log: Path) -> list[dict[str, Any]]:
    """The catalog's DM requests logged in ``log`` (``FAKE_DM_REQUESTS``), in order."""
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def fake_dm_state(
    jobs: Any = None,
    failing: Any = (),
    daqs: Any = None,
    files: Any = None,
    delay: float = 0,
    start_delay: float = 0,
) -> dict[str, Any]:
    return {
        "jobs": jobs or {},
        "failing": sorted(failing),
        "daqs": daqs or {},
        "files": files or [],
        "delay": delay,
        "start_delay": start_delay,
    }


class FakeDMService:
    """DM behind the real DM client (``remote.dm.DMClient``): ``DMClient.run`` (where the client starts its processes)
    and ``subprocess.run`` are patched, so every process the portal starts is counted (``runs``: its argv, its
    request and its timeout) and none reaches a shell, SSH or DM.
    A DM call (a shell that is no login shell running ``python -c`` on DM's API script, the request on its stdin) runs
    the real ``remote.dm._API_SCRIPT`` in a fresh interpreter whose ``dm`` package is a stand-in over ``jobs`` (DM's
    processing jobs by ID, each with its ``submissionId``; ``failing_records``: the IDs whose record DM cannot give);
    any other process (the launcher's cancel stage, run directly) only succeeds. ``failing`` holds errors the next calls
    raise as a whole, in order (``CalledProcessError``, ``TimeoutExpired``). Live runs' operations: ``daqs`` (DAQ
    records by ID) and ``files`` (the file catalog's rows, in catalogue order); ``requests()``, the catalog's DM
    requests inside the calls; ``delay`` and ``start_delay``, the catalog stand-in's waits (each request's; its
    construction's). ``workflow`` False: no workflow service installed (the live runs' services only). No network, DM or
    SSH."""

    def __init__(self, directory: Path, monkeypatch: Any, *, workflow: bool = True) -> None:
        self.directory = directory
        self.jobs: dict[str, dict[str, Any]] = {}
        self.failing_records: set[str] = set()
        self.failing: list[BaseException] = []
        self.runs: list[dict[str, Any]] = []
        self.daqs: dict[str, Any] = {}
        self.files: list[dict[str, Any]] = []
        self.delay = 0.0
        self.start_delay = 0.0
        write_fake_dm_api(directory / "api", workflow=workflow)
        self._requests = directory / "requests.log"
        self._real_run = subprocess.run
        monkeypatch.setattr(subprocess, "run", self._run)
        monkeypatch.setattr(DMClient, "run", lambda client, argv, **kwargs: self._run(argv, **kwargs))

    def operations(self, since: int = 0) -> list[str]:
        """The operation of each process started since run ``since`` (``launcher`` for one that is no DM call)."""
        return [run["request"]["operation"] if run["request"] else "launcher" for run in self.runs[since:]]

    def requests(self) -> list[dict[str, Any]]:
        """Each catalog request DM was sent (its ``pid``: the call's process; ``query``, ``skip``, ``limit``), and each
        ``restorePermissions`` (its arguments), in order."""
        return _requests(self._requests)

    def _run(self, argv: list[str], *, input: str | None = None, timeout: float | None = None, **kwargs: Any) -> Any:
        request = json.loads(input) if input else None
        self.runs.append({"argv": list(argv), "request": request, "timeout": timeout})
        if self.failing:
            raise self.failing.pop(0)
        if request is None:
            return subprocess.CompletedProcess(argv, 0, "", "")
        from laueportal.remote.dm import _API_SCRIPT

        # no login shell: DM's setup file, sourced first, gives the call its environment
        assert argv[:2] == ["/bin/bash", "-c"] and f"exec python -c {shlex.quote(_API_SCRIPT)}" in argv[2], argv
        state = self.directory / "state.json"
        state.write_text(
            json.dumps(
                fake_dm_state(self.jobs, self.failing_records, self.daqs, self.files, self.delay, self.start_delay)
            )
        )
        environment = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(self.directory / "api"),
            "FAKE_DM_STATE": str(state),
            "FAKE_DM_REQUESTS": str(self._requests),
        }
        result = self._real_run(
            [sys.executable, "-c", _API_SCRIPT],
            input=input,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
            env=environment,
            cwd=self.directory,
        )
        self.jobs = json.loads(state.read_text())["jobs"]
        return subprocess.CompletedProcess(argv, 0, result.stdout, result.stderr)


class FakeDMInstall:
    """DM's setup file and API installed under ``directory`` for processes the test does not run itself (a worker's
    job processes): the real DM client sources ``setup_file`` in its shell, which puts the stand-in ``dm``
    package and this interpreter first, so DM's real API script runs on the stand-in over ``state.json`` (``daqs``,
    ``files``: ``set_state``). Each call appends a line to ``calls.log`` (``calls``: their pids and times), each
    catalog request one to ``requests.log`` (``requests``). No network, DM or SSH."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        write_fake_dm_api(directory / "api")
        self.state = directory / "state.json"
        self.calls_log = directory / "calls.log"
        self.requests_log = directory / "requests.log"
        self.set_state()
        self.setup_file = directory / "dm-setup.sh"
        exports = {
            "PYTHONPATH": str(directory / "api"),
            "FAKE_DM_STATE": str(self.state),
            "FAKE_DM_CALLS": str(self.calls_log),
            "FAKE_DM_REQUESTS": str(self.requests_log),
        }
        lines = [f"export {name}={shlex.quote(value)}" for name, value in exports.items()]
        lines.append(f'export PATH={shlex.quote(str(Path(sys.executable).parent))}:"$PATH"')
        self.setup_file.write_text("\n".join(lines) + "\n")

    def set_state(self, *, daqs: Any = None, files: Any = None, delay: float = 0) -> None:
        """Replace the stand-in's state (atomically: a call reading it meanwhile sees the old or the new one);
        ``delay``: each catalog page waits that long first."""
        partial = self.state.with_suffix(".partial")
        partial.write_text(json.dumps(fake_dm_state(daqs=daqs, files=files, delay=delay)))
        partial.replace(self.state)

    def calls(self) -> list[tuple[int, float]]:
        """Each call's pid and time, in order."""
        if not self.calls_log.exists():
            return []
        return [(int(pid), float(at)) for pid, at in (line.split() for line in self.calls_log.read_text().splitlines())]

    def requests(self) -> list[dict[str, Any]]:
        """Each catalog request, in order (``FakeDMService.requests``)."""
        return _requests(self.requests_log)


# --- portal runs ---------------------------------------------------------------------------------


def _metadata(engine, scan_number: int = 1) -> None:
    with Session(engine) as session:
        if session.get(Metadata, scan_number) is None:
            session.add(create_test_metadata(scan_number))
            session.commit()


def _copy_config(layout: Layout) -> tuple[Path, Path]:
    config = layout.data / "config"
    config.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy(GEOMETRY, config)), Path(shutil.copy(CRYSTAL, config))


def remote_settings(values: dict[str, Any]) -> RemoteSettings:
    """The settings mapping (:func:`bdp_settings`) as the typed ``bdp.yaml`` model."""

    return parse_settings(values)


def lauego_request_values(layout: Layout, *, count: int = 3, frame_names: tuple[str, ...] = NI_FRAMES, backend="local"):
    """Stage ``count`` synthetic Ni frames and the configuration files under ``layout``; the LaueGo request's values."""

    geometry, crystal = _copy_config(layout)
    frames = layout.data / "frames"
    frames.mkdir(parents=True)
    for index in range(count):
        shutil.copy(LAUELAB_FIXTURES / frame_names[index % len(frame_names)], frames / f"frame_{index + 1}.h5")
    return dict(
        backend=backend,
        scan_number=1,
        reconstruction_id=None,
        input_path=os.fspath(frames),
        output_path_template=os.fspath(layout.analysis / "index_%d"),
        filename_prefixes=["frame_%d.h5"],
        scan_points=f"1-{count}" if count > 1 else "1",
        depth_range=None,
        detector_crop_x1=0,
        detector_crop_x2=2047,
        detector_crop_y1=0,
        detector_crop_y2=2047,
        output_xml="output.xml",
        energy_unit="keV",
        exposure_unit="sec",
        reciprocal_lattice_unit="1/nm",
        lattice_parameters_unit="nm",
        beamline="34ID-E",
        computer_name="test-host",
        submitted_at=SUBMITTED_AT,
        geometry_file=os.fspath(geometry),
        crystal_file=os.fspath(crystal),
        mask_file=None,
        box_size=18,
        max_rfactor=0.5,
        min_size=3.0,
        min_separation=20,
        threshold=None,
        threshold_ratio=None,
        peak_shape="Lorentzian",
        max_peaks=200,
        smooth=False,
        index_kev_max_calc=17.2,
        index_kev_max_test=35.0,
        index_angle_tolerance=0.1,
        index_cone=72.0,
        index_h=0,
        index_k=0,
        index_l=1,
        max_number=300,
        cosmic_filter=False,
        depth=None,
    )


def create_lauego_run(
    engine, layout: Layout, *, count: int = 3, frame_names: tuple[str, ...] = NI_FRAMES, backend="local"
):
    """A published LaueGo indexing run of ``count`` synthetic Ni frames (the portal's run creation)."""

    _metadata(engine)
    values = lauego_request_values(layout, count=count, frame_names=frame_names, backend=backend)
    return indexing_service.create_indexing(LaueGoParamsV1(**values), engine=engine)


def wire_request_values(layout: Layout, *, count: int = 2, backend="local", name: str = "wire"):
    """Write ``count`` synthetic wire-scan points and the geometry under ``layout``; the wire request's values. Another
    ``name`` gives another run beside it."""

    geometry, _ = _copy_config(layout)
    inputs = layout.data / name
    inputs.mkdir(parents=True)
    for index in range(1, count + 1):
        write_wire_scan(inputs / f"wire_{index}.h5", seed=index)
    return dict(
        backend=backend,
        scan_number=1,
        input_path=os.fspath(inputs),
        output_path_template=os.fspath(layout.analysis / f"{name}_%d"),
        filename_prefixes=["wire_%d.h5"],
        geometry_file=os.fspath(geometry),
        percent_brightest=OPTIONS["percent_brightest"],
        wire_edges="leading",
        depth_start=OPTIONS["depth_start"],
        depth_end=OPTIONS["depth_end"],
        depth_resolution=OPTIONS["depth_resolution"],
        num_threads=OPTIONS["num_threads"],
        memory_limit_mb=OPTIONS["memory_limit_mb"],
        scan_points=f"1-{count}" if count > 1 else "1",
        verbose=0,
        computer_name="test-host",
        submitted_at=SUBMITTED_AT,
    )


def create_wire_run(engine, layout: Layout, *, count: int = 2, backend="local", name: str = "wire"):
    """A published wire reconstruction of ``count`` synthetic wire-scan points (the portal's run creation)."""

    _metadata(engine)
    values = wire_request_values(layout, count=count, backend=backend, name=name)
    return reconstruction_service.create_reconstruction(WireParamsV1(**values), engine=engine)


def job_view(engine, job_id: int) -> SimpleNamespace:
    """The job attributes the plan reads, detached from the database."""

    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return SimpleNamespace(
            job_id=job.job_id,
            backend=job.backend,
            run_directory=job.run_directory,
            manifest_path=job.manifest_path,
            manifest_digest=job.manifest_digest,
            n_inputs=job.n_inputs,
            request_path=job.request_path,
        )


# --- the plan ---------------------------------------------------------------------------------------

PLAN_NAME = remote_plan.PLAN_NAME


def save_plan(job, settings: dict[str, Any] | RemoteSettings, submission_id: str) -> dict[str, Any]:
    """``remote.plan.save_plan``, also taking the settings as a mapping (:func:`remote_settings`)."""

    if not isinstance(settings, RemoteSettings):
        settings = remote_settings(settings)
    return remote_plan.save_plan(job, settings, submission_id)


# --- the launcher's part, emulated -----------------------------------------------------------------


def stage(plan: dict[str, Any], settings: dict[str, Any]) -> None:
    """What happens between submission and the runner's start, without DM, Globus or PBS.

    ``crux``: the flow's input transfer, emulated (each item copied between the two filesystem roots). ``aps``: the
    launcher's ``stage.submit`` creates the output directory before ``qsub``.
    """

    if plan["backend"] == "aps":
        Path(plan["output_directory"]).mkdir(parents=True, exist_ok=False)
        return
    source, destination = settings["storage"]["aps"], settings["storage"]["eagle"]
    for item in plan["input_transfer"]["transfer_items"]:
        src = Path(source["filesystem_root"]) / item["source_path"].lstrip("/")
        dst = Path(destination["filesystem_root"]) / item["destination_path"].lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


# --- cases ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One golden: a program on a target, run by the runner or (``nodes * ranks_per_node > 1``) the MPI runner."""

    name: str
    kind: str  # lauego_indexing or wire_reconstruction
    backend: str  # aps or crux
    count: int
    nodes: int = 1  # pbs.nodes
    ranks_per_node: int = 1  # execution.indexing_ranks_per_node
    corrupt: int | None = None  # this input (1-based) is replaced by bytes that are no HDF5 file
    cancel_before_start: bool = False

    @property
    def mpi(self) -> bool:
        return self.nodes * self.ranks_per_node > 1

    @property
    def size(self) -> int:
        return self.nodes * (self.ranks_per_node if self.kind == "lauego_indexing" else 1)


CASES = (
    Case("aps-lauego", "lauego_indexing", "aps", 3),
    Case("aps-lauego-failure", "lauego_indexing", "aps", 3, corrupt=2),
    Case("crux-lauego", "lauego_indexing", "crux", 3),
    Case("aps-wire", "wire_reconstruction", "aps", 2),
    Case("crux-wire", "wire_reconstruction", "crux", 2),
    Case("crux-wire-cancelled", "wire_reconstruction", "crux", 2, cancel_before_start=True),
    Case("crux-lauego-mpi", "lauego_indexing", "crux", 3, nodes=2, ranks_per_node=2),
    Case("crux-wire-mpi", "wire_reconstruction", "crux", 3, nodes=2),
)
CASES_BY_NAME = {case.name: case for case in CASES}


def case_settings(case: Case, layout: Layout) -> dict[str, Any]:
    settings = bdp_settings(layout)
    target = settings["targets"][case.backend]
    target["pbs"]["nodes"] = case.nodes
    if case.backend == "crux":
        target["execution"]["indexing_ranks_per_node"] = case.ranks_per_node
    return settings


def create_run(case: Case, layout: Layout, engine) -> SimpleNamespace:
    """The case's portal run, as the job attributes the plan reads (:func:`job_view`): created by the portal's run
    creation, with the input ``case.corrupt`` replaced by bytes that are no HDF5 file afterwards (run creation
    only lists the inputs)."""

    if case.kind == "lauego_indexing":
        values = lauego_request_values(layout, count=case.count, backend=case.backend)
        name = "frames/frame_{}.h5"
    else:
        values = wire_request_values(layout, count=case.count, backend=case.backend)
        name = "wire/wire_{}.h5"
    _metadata(engine)
    if case.kind == "lauego_indexing":
        run = indexing_service.create_indexing(LaueGoParamsV1(**values), engine=engine)
    else:
        run = reconstruction_service.create_reconstruction(WireParamsV1(**values), engine=engine)
    job = job_view(engine, run.job_id)
    if case.corrupt is not None:
        (layout.data / name.format(case.corrupt)).write_bytes(b"not an hdf5 file")
    return job


def prepare(case: Case, layout: Layout, engine, *, plan_writer=None) -> dict[str, Any]:
    """Create the portal run (:func:`create_run`), write its plan (``plan_writer``, by default :func:`save_plan`)
    and stage it.

    Returns the plan.
    """

    job = create_run(case, layout, engine)
    settings = case_settings(case, layout)
    plan = (plan_writer or save_plan)(job, settings, SUBMISSION_ID)
    stage(plan, settings)
    if case.cancel_before_start:
        # The portal's stop request (written in the run directory; for crux the launcher's
        # cancel transfer puts it into the control directory). Only its existence matters.
        write_json_file(Path(plan["control_directory"]) / "cancel.request", {"requested_at": "<time>"})
    return plan


# --- running the remote side ------------------------------------------------------------------------

V2_MODULES = {"runner": "laueportal.jobs.runner", "mpi_runner": "laueportal.jobs.mpi_runner"}
# What a compute node never loads: the database, the settings, Redis and RQ.
V2_BLOCKED = ("laueportal.core.db", "laueportal.core.settings", "sqlalchemy", "redis", "rq")
# The packages outside the standard library a compute node has (requirements/compute.in, plus lauelab and
# the conda-forge numpy, h5py and cffi it needs); the runners may load nothing else.
COMPUTE_PACKAGES = frozenset(
    {"laueportal", "lauelab", "numpy", "h5py", "cffi", "pycparser", "psutil", "mpi4py", "yaml", "cython_runtime"}
)

# The remote side in a fresh interpreter: ``python -m <module> <control directory>`` (as the PBS script
# runs it), with an import hook that fails on any blocked module. For the MPI runner, a file-backed fake
# communicator stands in for mpi4py: the ranks run one after another, highest first, each in its own
# interpreter; gather() leaves each rank's object in a file and returns them all on rank 0, and bcast()
# returns None off the root (nothing waits for the root, which runs last). Each rank reports its host as
# ``node-<rank // ranks_per_node>``. The last line printed is the exit status, the modules loaded and every
# blocked import attempted (also one the code caught and survived).
DRIVER = """
import json
import os
import pickle
import platform
import runpy
import sys
import types
from pathlib import Path

OPTIONS = json.loads(sys.argv[1])


class Blocker:
    attempts = []

    def find_spec(self, fullname, path=None, target=None):
        for blocked in OPTIONS["blocked"]:
            if fullname == blocked or fullname.startswith(blocked + "."):
                self.attempts.append(fullname)
                raise ImportError("the remote side imported " + fullname)
        return None


class FakeComm:
    def __init__(self, directory, rank, size):
        self.directory, self.rank, self.size = Path(directory), rank, size

    def Get_rank(self):
        return self.rank

    def Get_size(self):
        return self.size

    def gather(self, value, root=0):
        (self.directory / f"gather-{self.rank:04d}.pickle").write_bytes(pickle.dumps(value))
        if self.rank != root:
            return None
        return [pickle.loads((self.directory / f"gather-{r:04d}.pickle").read_bytes()) for r in range(self.size)]

    def bcast(self, value, root=0):
        return value if self.rank == root else None

    def Abort(self, code=1):
        sys.stdout.flush()
        os._exit(code)


def main():
    sys.meta_path.insert(0, Blocker())
    mpi = OPTIONS.get("mpi")
    if mpi:
        comm = FakeComm(mpi["directory"], mpi["rank"], mpi["size"])
        sys.modules["mpi4py"] = types.SimpleNamespace(MPI=types.SimpleNamespace(COMM_WORLD=comm))
        host = mpi["host"]
        platform.node = lambda: host
    sys.argv = [OPTIONS["module"], OPTIONS["control"]]
    try:
        runpy.run_module(OPTIONS["module"], run_name="__main__", alter_sys=True)
        status = 0
    except SystemExit as stop:
        status = stop.code
    print(json.dumps({"exit": status, "modules": sorted(sys.modules), "blocked": Blocker.attempts}))


if __name__ == "__main__":
    main()
"""


def run_remote(
    case: Case,
    plan: dict[str, Any],
    *,
    modules: dict[str, str],
    blocked: tuple[str, ...],
    env: dict[str, str],
    scratch: Path,
) -> list[dict[str, Any]]:
    """Run the case's runner (or each MPI rank) on ``plan``'s control directory; one report per process.

    ``env`` is the processes' environment (:func:`tests.settings_support.fresh_interpreter_env`). Each report is
    the driver's last line plus ``stdout`` and ``stderr``.
    """

    scratch.mkdir(parents=True, exist_ok=True)
    driver = scratch / "remote_driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    module = modules["mpi_runner" if case.mpi else "runner"]
    ranks: list[dict[str, Any] | None] = [None]
    if case.mpi:
        gather = scratch / "gather"
        gather.mkdir(exist_ok=True)
        env = {**env, "PBS_JOBID": "1234.fake-pbs"}
        per_node = case.ranks_per_node if case.kind == "lauego_indexing" else 1
        ranks = [
            {"directory": os.fspath(gather), "rank": rank, "size": case.size, "host": f"node-{rank // per_node}"}
            for rank in reversed(range(case.size))
        ]
    reports = []
    for mpi in ranks:
        options = {"module": module, "control": plan["control_directory"], "blocked": list(blocked), "mpi": mpi}
        result = subprocess.run(
            [sys.executable, os.fspath(driver), json.dumps(options)],
            env=env,
            cwd=scratch,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        lines = result.stdout.strip().splitlines()
        try:
            report = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            report = {"exit": result.returncode, "modules": [], "blocked": []}
        report.update(stdout=result.stdout, stderr=result.stderr, returncode=result.returncode)
        reports.append(report)
    return reports


# --- normalization -----------------------------------------------------------------------------------

_TIME_KEYS = ("heartbeat_at", "started_at", "finished_at", "recorded_at")
_LIBRARY_KEYS = ("lauelab_version", "lauelab_commit", "lauelab_source")


def normalize_text(text: str, base: Path) -> str:
    """Placeholders for what differs between two runs of the same case; the format is kept."""

    for prefix in sorted({os.fspath(base), os.path.realpath(base)}, key=len, reverse=True):
        text = text.replace(prefix, "<BASE>")
    for key in _TIME_KEYS:  # every digit zeroed, so the format (precision, offset) is still compared
        text = re.sub(rf'("{key}":\s?)"([^"]*)"', lambda match: f'{match[1]}"{re.sub(r"[0-9]", "0", match[2])}"', text)
    for key in _LIBRARY_KEYS:
        text = re.sub(rf'("{key}":\s?)"[^"]*"', rf'\1"<{key}>"', text)
    text = re.sub(r'("[a-z0-9_]*seconds":\s?)-?[0-9][0-9.eE+-]*', r'\1"<seconds>"', text)
    text = re.sub(r'(executionTime=)"[^"]*"', r'\1"<seconds>"', text)  # the Output XML's timings
    return text.replace(json.dumps(platform.node()), '"<host>"')


def output_listing(directory: Path) -> list[str]:
    """Every entry under ``directory``, relative, directories with a trailing ``/``."""

    return sorted(
        path.relative_to(directory).as_posix() + ("/" if path.is_dir() else "") for path in directory.rglob("*")
    )


def _hdf5_value(value: Any, base: Path) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "surrogateescape")
    if isinstance(value, str):
        return normalize_text(value, base)
    if isinstance(value, np.ndarray):
        if value.dtype.kind in "OSU":
            return [_hdf5_value(item, base) for item in value.reshape(-1).tolist()]
        return {
            "dtype": value.dtype.str,
            "shape": list(value.shape),
            "sha256": hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest(),
        }
    if isinstance(value, np.generic):
        return value.item()
    return value


# HDF5 attributes and datasets that hold a time, a timing or a library version, which differ between runs
# (or environments): by name, and every timing named ``*_seconds`` except the frames' exposure.
VOLATILE_HDF5 = ("created", "seconds", "source_mtime_ns", "lauelab_version", "native_version")


def _volatile(name: str) -> bool:
    return name in VOLATILE_HDF5 or (name.endswith("_seconds") and name != "exposure_seconds")


def hdf5_summary(path: Path, base: Path) -> dict[str, Any]:
    """Every group, dataset and attribute of an HDF5 file: dtype, shape and a digest of the values (strings
    normalized), with times and timings replaced by placeholders."""

    nodes: dict[str, Any] = {}

    def attributes(node) -> dict[str, Any]:
        return {
            name: "<volatile>" if _volatile(name) else _hdf5_value(node.attrs[name], base)
            for name in sorted(node.attrs)
        }

    def visit(name: str, node) -> None:
        entry: dict[str, Any] = {"attrs": attributes(node)}
        if isinstance(node, h5py.Dataset):
            leaf = name.rsplit("/", 1)[-1]
            value = "<volatile>" if _volatile(leaf) else _hdf5_value(node[()], base)
            dtype = str(node.dtype)
            if node.dtype.kind == "S" and "<BASE>" in json.dumps(value):
                dtype = "|S<length of a path>"  # a fixed-length string as long as its temporary path
            entry.update(dtype=dtype, shape=list(node.shape), value=value)
        nodes[name] = entry

    with h5py.File(path, "r") as handle:
        nodes["/"] = {"attrs": attributes(handle)}
        handle.visititems(visit)
    return nodes


def output_summary(output: Path, base: Path) -> dict[str, Any]:
    """The science outputs of an output directory: each HDF5 file summarized, each XML file normalized."""

    summary: dict[str, Any] = {}
    for path in sorted(output.rglob("*")):
        relative = path.relative_to(output).as_posix()
        if path.suffix == ".h5" and path.is_file():
            summary[relative] = hdf5_summary(path, base)
        elif path.suffix == ".xml" and path.is_file():
            summary[relative] = normalize_text(path.read_text(encoding="utf-8"), base)
    return summary


GOLDEN_FILES = ("run.json", "progress.json", "failures.jsonl", "ranks.json")


_SCHEMA_ID_LINE = re.compile(r'\n    "schema_id": "(?:lauego|wire)/v1",(?=\n)')


def without_schema_id(text: str) -> str:
    """``request.json``'s text without the ``schema_id`` line the parameter models add to ``request`` (the goldens'
    ``request`` has none); anything else is kept."""
    return _SCHEMA_ID_LINE.sub("", text, count=1)


def golden_documents(plan: dict[str, Any], layout: Layout) -> dict[str, str]:
    """The normalized text of each golden file of a finished case, by golden file name."""

    base = layout.base
    output = Path(plan["output_directory"])
    run = Path(plan["run_directory"])
    inbox = layout.submissions / f"{SUBMISSION_ID}.json"
    # The plan's digests name the control files, whose bytes hold the temporary paths.
    digests = {
        hashlib.sha256((run / "request.json").read_bytes()).hexdigest(): "<request.json sha256>",
        hashlib.sha256((run / "inputs.jsonl").read_bytes()).hexdigest(): "<inputs.jsonl sha256>",
    }

    def plan_text(path: Path) -> str:
        text = normalize_text(path.read_text(encoding="utf-8"), base)
        for digest, placeholder in digests.items():
            text = text.replace(f'"{digest}"', f'"{placeholder}"')
        return text

    documents = {
        "request.json": without_schema_id(plan_text(run / "request.json")),
        "inputs.jsonl": plan_text(run / "inputs.jsonl"),
        "bdp.json": plan_text(run / PLAN_NAME),
        "submission.json": plan_text(inbox),
        "listing.json": json.dumps(
            {
                "control": output_listing(Path(plan["control_directory"])),
                "output": output_listing(output),
                "shards": output_listing(output.parent / "shards"),  # the MPI runner's, beside the output
            },
            indent=2,
        )
        + "\n",
        "outputs.json": json.dumps(output_summary(output, base), indent=1, sort_keys=True) + "\n",
    }
    for name in GOLDEN_FILES:
        if (output / name).exists():
            documents[name] = normalize_text((output / name).read_text(encoding="utf-8"), base)
    lock = output / ".runner.lock"
    if lock.exists():
        documents["runner.lock"] = "<pid>\n" if lock.read_text().isdigit() else lock.read_text()
    return documents


# --- the return and its publication -------------------------------------------------------------------

PUBLISHED = "published"
"""The goldens of a case's publication: ``tests/fixtures/remote_goldens/<case>/published/``."""


def return_results(plan: dict[str, Any]) -> Path:
    """What the launcher's return does, without Globus: ``crux``'s result transfer copies the output directory
    into the run's inbox (``incoming/<submission>``); on ``aps`` the runner wrote into the inbox itself."""

    if plan["backend"] == "crux":
        shutil.copytree(plan["output_directory"], plan["incoming_directory"])
        return Path(plan["incoming_directory"])
    return Path(plan["output_directory"])


def publication_documents(plan: dict[str, Any], layout: Layout) -> dict[str, str]:
    """The normalized text of each publication golden: the run directory after publication (``listing.json``),
    its published ``run.json``, the marker (``publication.json``) and the science outputs (``outputs.json``,
    with the rewritten input paths and the remote originals)."""

    base = layout.base
    run = Path(plan["run_directory"])
    return {
        "listing.json": json.dumps({"run": output_listing(run)}, indent=2) + "\n",
        "run.json": normalize_text((run / "run.json").read_text(encoding="utf-8"), base),
        "publication.json": normalize_text((run / ".bdp-publication.json").read_text(encoding="utf-8"), base),
        "outputs.json": json.dumps(output_summary(run, base), indent=1, sort_keys=True) + "\n",
    }


# --- checking the portal against the goldens ---------------------------------------------------------


def committed_goldens(case: Case, part: str | None = None) -> dict[str, str]:
    """The committed goldens of ``case`` (``part``: a subdirectory, such as ``PUBLISHED``), by file name."""

    directory = GOLDENS / case.name if part is None else GOLDENS / case.name / part
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.iterdir()) if path.is_file()}


def text_differences(expected: dict[str, str], actual: dict[str, str]) -> dict[str, str]:
    """A unified diff (golden → portal) for each document that differs (or exists on one side only)."""

    import difflib

    differences = {}
    for name in sorted(set(expected) | set(actual)):
        if expected.get(name) != actual.get(name):
            diff = difflib.unified_diff(
                expected.get(name, "").splitlines(True), actual.get(name, "").splitlines(True), "golden", "portal", n=1
            )
            differences[name] = "".join(list(diff)[:80])
    return differences


def golden_differences(case: Case, plan: dict[str, Any], layout: Layout) -> dict[str, str]:
    """A unified diff (golden → portal) for each golden file the portal's run does not reproduce exactly."""

    return text_differences(committed_goldens(case), golden_documents(plan, layout))


def run_v2_case(case: Case, base: Path) -> tuple[dict[str, Any], Layout, list[dict[str, Any]], dict[str, bytes]]:
    """Prepare ``case`` under ``base`` and run the portal's remote side on it in fresh interpreters.

    Returns the plan, the layout, one report per process (``run_remote``) and the control files' bytes
    as the portal published them, before anything ran.
    """

    from tests.run_support import make_engine
    from tests.settings_support import fresh_interpreter_env

    layout = Layout(base)
    engine = make_engine(base / "portal")
    try:
        plan = prepare(case, layout, engine)
    finally:
        engine.dispose()
    run = Path(plan["run_directory"])
    published = {name: (run / name).read_bytes() for name in ("request.json", "inputs.jsonl", PLAN_NAME)}
    reports = run_remote(
        case, plan, modules=V2_MODULES, blocked=V2_BLOCKED, env=fresh_interpreter_env(), scratch=base / "remote"
    )
    return plan, layout, reports, published


def assert_matches_goldens(case: Case, base: Path) -> dict[str, Any]:
    """Run ``case`` with the portal's remote side and assert the goldens, the seam and the untouched control files."""

    plan, layout, reports, published = run_v2_case(case, base)
    status = json.loads(committed_goldens(case)["run.json"])["status"]
    expected_exit = [None] * (len(reports) - 1) + [0 if status == "Finished" else 1]
    assert [report["exit"] for report in reports] == expected_exit, [report["stderr"][-3000:] for report in reports]
    # The module run as __main__ is not in the list under its own name; what it imported is.
    imported = {"laueportal.jobs.contract", "laueportal.jobs.rundir", "laueportal.jobs.processes"}
    imported |= {"laueportal.remote.paths"} | (
        {"laueportal.jobs.runner", "laueportal.science.merge"} if case.mpi else set()
    )
    for index, report in enumerate(reports):
        loaded = set(report["modules"])
        assert imported <= loaded, f"process {index} did not import {sorted(imported - loaded)}"
        packages = {name.split(".")[0] for name in loaded if not name.startswith("_")}
        foreign = packages - set(sys.stdlib_module_names) - COMPUTE_PACKAGES
        assert not foreign, f"process {index} loaded packages a compute node lacks: {sorted(foreign)}"
        blocked = [name for name in loaded for root in V2_BLOCKED if name == root or name.startswith(root + ".")]
        assert not blocked, f"process {index} loaded {blocked}"
        assert not report["blocked"], f"process {index} tried to import {report['blocked']}"
    run, control = Path(plan["run_directory"]), Path(plan["control_directory"])
    # Never rewritten: the run's files, and the staged copies the runner read.
    for directory in (run, control):
        changed = [name for name in published if (directory / name).read_bytes() != published[name]]
        assert not changed, f"{directory} has rewritten {changed}"
    differences = golden_differences(case, plan, layout)
    assert not differences, "the remote side differs from the goldens:\n" + "\n".join(
        f"{name}:\n{diff}" for name, diff in differences.items()
    )
    return plan


def tree_state(root: Path) -> dict[str, tuple[Any, ...]]:
    """Everything under ``root``: each entry's kind and permission bits, a file's bytes, a link's target."""

    import stat

    state: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            state[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            state[relative] = ("directory", oct(mode))
        else:
            state[relative] = ("file", oct(mode), path.read_bytes())
    return state
