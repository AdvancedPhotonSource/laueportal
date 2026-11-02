"""Planning estimates from measured profiles, without enumerating images or reading pixels.

The measured profiles are ``runtime_profiles.json``. The create forms label each execution target ("Run on") with
these estimates.

- **Typed inputs.** Each run is one :class:`RunSelection` in the request's field names, and a target's resources
  are an :class:`ExecutionLayout`, built from the settings by :func:`execution_layout`.
- **Allowed roots.** The one read, a reconstruction catalog's snapshot (the ``scan.h5`` metadata, never a point
  file), is made only for a path inside ``files.roots`` (``files.roots.resolve_inside_roots``); for a path outside
  them the estimate fails, saying why, and for one whose file system does not answer it raises
  ``files.probe.NotResponding`` (a 503 ``urn:laueportal:problem:not-responding`` for the route that serves it).
  Nothing else touches the input files.
- **Per-target estimates.** :func:`estimate_targets` estimates each choice of
  ``remote.settings.available_backends``: the runtime with that target's layout,
  plus the round-trip transfer on ``crux``, and the target's PBS walltime to warn against. Queue wait is excluded.
- lauelab is read through ``science.recon`` (import rule 2), filename templates through ``files.patterns``, integer
  ranges through ``util.srange``.

Every "cannot estimate" is a ``ValueError`` whose message is for the user.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from laueportal.core.settings import Settings
from laueportal.files.patterns import compile_filename_templates
from laueportal.files.roots import PathNotAllowed, resolve_inside_roots
from laueportal.jobs.backends import LOCAL, REMOTE_BACKENDS
from laueportal.remote.settings import BackendOption, ExecutionPolicy, RemoteSettings, available_backends
from laueportal.util.srange import srange

Kind = Literal["indexing", "reconstruction"]

MAX_RUNS = 100
"""The most runs one estimate covers."""
HDF5_SUFFIXES = frozenset({".h5", ".hdf5", ".hdf", ".he5"})
"""An indexing input with one of these suffixes may be a reconstruction catalog."""


@lru_cache(maxsize=1)
def measured_profiles() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("runtime_profiles.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class RunSelection:
    """The inputs one run selects, in the request's field names.

    ``scan_points`` and ``depth_range`` are integer ranges in srange syntax (``"1-825"``, ``"0,3,10"``); they are
    counted, never expanded. Indexing reads ``input_path`` (a reconstruction catalog, ``scan.h5``, or a folder),
    ``filename_prefixes``, ``scan_points`` (optional for a catalog: every point) and ``depth_range`` (optional).
    Reconstruction reads ``filename_prefixes``, ``scan_points``, the depth range in µm (``depth_start``,
    ``depth_end``, ``depth_resolution``) and the skips. Fields the kind does not read are ignored.
    """

    filename_prefixes: Sequence[str]
    scan_points: str | None = None
    input_path: str | None = None
    depth_range: str | None = None
    depth_start: float | None = None
    depth_end: float | None = None
    depth_resolution: float | None = None
    frame_skip: int | None = 1
    wire_skip: int | None = 1


@dataclass(frozen=True)
class ExecutionLayout:
    """A target's resources, as the estimate reads them (:func:`execution_layout`).

    ``workers`` are indexing workers per node (or per rank), ``reconstruction_workers`` and ``threads_per_worker``
    the reconstruction pool, ``max_in_flight`` the indexing window (not set: twice the workers), ``nodes`` the PBS
    nodes and ``indexing_ranks_per_node`` the indexing ranks on each.
    """

    workers: int = 1
    reconstruction_workers: int = 1
    threads_per_worker: int = 1
    max_in_flight: int | None = None
    nodes: int = 1
    indexing_ranks_per_node: int = 1

    def __post_init__(self) -> None:
        # The estimate divides by these; a remote run needs at least one of each too
        # (remote.pbs.build_script).
        for key in ("nodes", "indexing_ranks_per_node"):
            if getattr(self, key) < 1:
                raise ValueError(f"The execution layout needs at least one of {key}.")


@dataclass(frozen=True)
class Selection:
    points: int
    frames: int
    depths: int | None
    basis: str
    notes: tuple[str, ...] = ()
    staged_frames: int | None = None  # Complete point stacks are transferred for catalog inputs.


@dataclass(frozen=True)
class RuntimeEstimate:
    seconds: float
    selection: Selection
    profile: dict[str, Any]
    notes: tuple[str, ...]
    runs: int


@dataclass(frozen=True)
class TransferEstimate:
    input_bytes: float
    output_bytes: float
    bytes_per_second: float

    @property
    def input_seconds(self) -> float:
        return self.input_bytes / self.bytes_per_second

    @property
    def output_seconds(self) -> float:
        return self.output_bytes / self.bytes_per_second

    @property
    def seconds(self) -> float:
        return self.input_seconds + self.output_seconds


def estimate_transfers(kind: Kind, selection: Selection) -> TransferEstimate:
    """Project both directions from profiling sizes; never stat or read the data files."""
    reference = measured_profiles()["transfer"]
    reconstructed_frame_bytes = (
        reference["estimated_reconstruction_bytes"] / reference["reference_reconstructed_frames"]
    )
    if kind == "reconstruction":
        input_bytes = selection.points * reference["measured_input_bytes"] / reference["reference_points"]
        output_bytes = selection.frames * reconstructed_frame_bytes
    else:
        # A depth subset still stages the entire point file containing those depths.
        frames = selection.staged_frames if selection.staged_frames is not None else selection.frames
        input_bytes = frames * reconstructed_frame_bytes
        output_bytes = (
            selection.frames
            * reference["measured_indexing_result_bytes"]
            / reference["measured_indexing_result_frames"]
        )
    return TransferEstimate(
        input_bytes, output_bytes, reference["measured_input_bytes"] / reference["measured_seconds"]
    )


def integer_range(value: str | None, *, optional: bool = False) -> srange | None:
    if value is None or not str(value).strip():
        if optional:
            return None
        raise ValueError("Select scan points to calculate an estimate.")
    try:
        result = srange(str(value))
        if not len(result) or result.first() < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Enter valid, nonnegative point and depth indices.") from error
    return result


@lru_cache(maxsize=32)
def _catalog_points(path: str, refresh_window: int) -> tuple[Any, ...] | None:
    from laueportal.science.recon import read_scan_catalog

    # One small catalog snapshot, cached for ten seconds. Never open point files.
    try:
        with read_scan_catalog(path) as scan:
            return scan.points
    except (OSError, ValueError) as error:
        if Path(path).name == "scan.h5":
            raise ValueError("The reconstruction catalog is unavailable; check the input path.") from error
        return None


def _inside_roots(path: str, settings: Settings) -> str:
    """``path`` resolved inside the allowed roots (``files.roots``), before anything reads it."""
    try:
        return os.fspath(resolve_inside_roots(path, settings)[1])
    except PathNotAllowed as error:
        raise ValueError(error.problem.detail or "The input path is not allowed.") from None


def count_selection(kind: Kind, selection: RunSelection, *, settings: Settings) -> Selection:
    """Count one run's points and frames from its ranges, or from a reconstruction catalog's metadata.

    ``settings`` gives the allowed roots a catalog must lie in (``files.roots``).
    """
    templates = tuple(dict.fromkeys(part.strip() for part in selection.filename_prefixes if part.strip()))
    if not templates:
        raise ValueError("Choose a filename pattern to calculate an estimate.")
    points = integer_range(selection.scan_points, optional=kind == "indexing")
    depths = integer_range(selection.depth_range, optional=True) if kind == "indexing" else None
    path = selection.input_path or ""
    if kind == "indexing" and Path(path).suffix.lower() in HDF5_SUFFIXES:
        catalog = _catalog_points(_inside_roots(path, settings), int(time.monotonic() // 10))
        if catalog is not None:
            if any(template.count("%d") > 1 for template in templates):
                raise ValueError("A reconstruction catalog uses one point index; select depths separately.")
            matcher = compile_filename_templates(templates, append_suffix_wildcard=True)
            selected_points = selected_frames = staged_frames = 0
            for point in catalog:
                match = matcher.match(os.path.basename(point.source_path))
                if match is None or (points is not None and match.indices and not points.is_in_range(match.indices[0])):
                    continue
                if not point.complete:
                    raise ValueError("The selection includes incomplete reconstructed points.")
                n_depths = point.shape[0]
                if depths is not None and depths.last() >= n_depths:
                    raise ValueError(f"Selected depth indices exceed this reconstruction's 0–{n_depths - 1} range.")
                selected_points += 1
                selected_frames += len(depths) if depths is not None else n_depths
                staged_frames += n_depths
            if not selected_frames:
                raise ValueError("No reconstructed frames match this selection.")
            return Selection(
                selected_points, selected_frames, None, "Reconstruction catalog", staged_frames=staged_frames
            )
    if points is None:
        raise ValueError("Select scan points to calculate an estimate.")
    count = 0
    for template in templates:
        placeholders = template.count("%d")
        if placeholders > (2 if kind == "indexing" else 1):
            raise ValueError("The filename pattern has too many index placeholders.")
        if not placeholders and any(symbol in template for symbol in "*?["):
            raise ValueError(
                "Use an indexed filename pattern for a fast estimate; wildcard file counts need validation."
            )
        if placeholders == 2 and depths is None:
            raise ValueError("Select depth indices for the second filename placeholder.")
        if placeholders == 1 and depths is not None:
            raise ValueError("For directory inputs, depth indices need a second filename placeholder.")
        count += (len(points) if placeholders else 1) * (len(depths) if placeholders == 2 and depths is not None else 1)
    if kind == "indexing":
        point_count = len(points) if any("%d" in template for template in templates) else count
        return Selection(
            point_count,
            count,
            None,
            "Selected indices",
            ("Assumes one file per index/template match; validation checks actual files.",),
        )
    try:
        start, end, step = (
            _number(value) for value in (selection.depth_start, selection.depth_end, selection.depth_resolution)
        )
        if not all(math.isfinite(value) for value in (start, end, step)) or step <= 0 or end < start:
            raise ValueError
        n_depths = math.floor((end - start) / step + 1e-9) + 1
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("Enter a valid reconstruction depth range and positive resolution.") from error
    return Selection(
        count, count * n_depths, n_depths, "Selected indices", ("File availability is checked during validation.",)
    )


def _number(value: float | None) -> float:
    if value is None:  # a missing form field
        raise TypeError("not set")
    return float(value)


def choose_profile(kind: Kind, backend: str, layout: ExecutionLayout) -> tuple[dict[str, Any], list[str]]:
    machine = "crux" if backend == "crux" else "aps"
    workers = layout.reconstruction_workers if kind == "reconstruction" else layout.workers
    threads = layout.threads_per_worker if kind == "reconstruction" else 1
    window = layout.max_in_flight or 2 * workers
    nodes = layout.nodes
    ranks = layout.indexing_ranks_per_node if kind == "indexing" else 1
    candidates = [p for p in measured_profiles()["profiles"] if p["backend"] == machine and p["kind"] == kind]
    profile = min(
        candidates,
        key=lambda p: (
            abs(math.log2(p.get("nodes", 1) / nodes)),
            abs(math.log2(p.get("ranks_per_node", 1) / ranks)),
            abs(math.log2(p["workers"] / max(1, workers))),
            abs(math.log2(p["threads"] / max(1, threads))),
            abs((p.get("max_in_flight") or 2 * p["workers"]) - window),
        ),
    )
    notes = []
    if profile.get("nodes", 1) != nodes or profile.get("ranks_per_node", 1) != ranks:
        notes.append("The current node/coordinator layout differs from this measured reference configuration.")
    if profile["workers"] != workers or profile["threads"] != threads:
        notes.append("The current worker settings differ from this measured reference configuration.")
    return profile, notes


def estimate_runtime(
    kind: Kind, backend: str, runs: Sequence[RunSelection], *, layout: ExecutionLayout, settings: Settings
) -> RuntimeEstimate:
    """The processing time of ``runs`` (one selection per run) on ``backend`` with ``layout``'s resources."""
    profile, notes = choose_profile(kind, backend, layout)
    n_runs = len(runs)
    if n_runs > MAX_RUNS:
        raise ValueError("Estimate at most 100 pooled runs at a time.")
    if not n_runs:
        raise ValueError("Add a run to calculate an estimate.")
    selections, seconds = [], 0.0
    rate = profile["count"] / profile["seconds"]
    for run in runs:
        selection = count_selection(kind, run, settings=settings)
        units = selection.points if kind == "reconstruction" else selection.frames
        # A batch smaller than its worker pool still needs one wave of work.
        wave = profile["workers"] * profile.get("nodes", 1) * profile.get("ranks_per_node", 1)
        duration = max(units, wave) / rate
        if kind == "reconstruction":
            assert selection.depths is not None  # count_selection counts a reconstruction's depths
            duration *= selection.depths / measured_profiles()["reference"]["depths_per_point"]
            # An empty skip reads as 1 (``value or 1``).
            if any((value or 1) != 1 for value in (run.frame_skip, run.wire_skip)):
                notes.append("Skip settings differ from profiling; their speedup has not been measured.")
        seconds += duration
        selections.append(selection)
        notes.extend(selection.notes)
    notes.append(
        "Based on 2048 × 2048 reference images; image size, peak settings, and storage load can change runtime."
    )
    if kind == "reconstruction":
        notes.append(
            "Reference input: 510 raw frames per point. Depth scaling is approximate because input I/O also takes time."
        )
    selection = Selection(
        sum(s.points for s in selections),
        sum(s.frames for s in selections),
        None,
        ", ".join(dict.fromkeys(s.basis for s in selections)),
        staged_frames=sum(s.staged_frames if s.staged_frames is not None else s.frames for s in selections),
    )
    return RuntimeEstimate(seconds, selection, profile, tuple(dict.fromkeys(notes)), n_runs)


def format_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if seconds < 60:
        return "under 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    return f"{hours} h" + (f" {remainder} min" if remainder else "")


def _count(value: int | float | str | None) -> int:
    """A count as ``int(value or 1)`` reads it (the remote settings keep text and floats as written)."""
    return int(value or 1)


def execution_layout(backend: str, settings: Settings, remote: RemoteSettings | None = None) -> ExecutionLayout:
    """A target's resources as the estimate reads them.

    ``local`` uses the portal's ``[execution]`` (with ``num_threads`` as the reconstruction
    threads per worker). ``aps`` and ``crux`` use the target's ``execution`` and its PBS ``nodes`` in the remote
    settings (``bdp.yaml``); a target the settings do not configure is a ``ValueError``
    (``remote.settings.RemoteSettingsError``).
    """
    if backend == LOCAL:
        execution = settings.execution
        return ExecutionLayout(
            workers=execution.workers,
            reconstruction_workers=execution.reconstruction_workers,
            threads_per_worker=execution.num_threads,
            max_in_flight=execution.max_in_flight,
        )
    if remote is None or backend not in REMOTE_BACKENDS:
        raise ValueError(f"Execution target {backend!r} is not enabled")
    target = remote.target(backend)
    policy = target.execution if target.execution is not None else ExecutionPolicy()
    return ExecutionLayout(
        workers=_count(policy.workers),
        reconstruction_workers=_count(policy.reconstruction_workers),
        threads_per_worker=_count(policy.threads_per_worker),
        max_in_flight=policy.max_in_flight,
        nodes=int(target.pbs.nodes) if target.pbs is not None else 1,
        indexing_ranks_per_node=int(policy.indexing_ranks_per_node),
    )


_WALLTIME = re.compile(r"(?:(?:(\d+):)?(\d+):)?(\d+)")


def walltime_seconds(value: int | float | str | None) -> int | None:
    """A PBS walltime (``[[hours:]minutes:]seconds``) in seconds; ``None`` if not set or not
    in that form."""
    match = _WALLTIME.fullmatch(str(value)) if value is not None and not isinstance(value, bool) else None
    if match is None:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True)
class TargetEstimate:
    """One "Run on" choice with its estimate.

    ``estimate`` is the processing time with the target's ``layout``; ``transfer`` the round trip to ALCF (``crux``
    only), which :attr:`seconds` includes. ``walltime_seconds`` is the target's PBS walltime; :attr:`exceeds_walltime`
    says when the processing time alone exceeds it. ``error`` says why there is no estimate.
    """

    option: BackendOption
    layout: ExecutionLayout | None = None
    estimate: RuntimeEstimate | None = None
    transfer: TransferEstimate | None = None
    walltime_seconds: int | None = None
    error: str | None = None

    @property
    def seconds(self) -> float | None:
        """Processing plus, on ``crux``, the round-trip transfer; queue wait is excluded."""
        if self.estimate is None:
            return None
        return self.estimate.seconds + (self.transfer.seconds if self.transfer is not None else 0.0)

    @property
    def exceeds_walltime(self) -> bool:
        return (
            self.estimate is not None
            and self.walltime_seconds is not None
            and self.estimate.seconds > self.walltime_seconds
        )


def estimate_targets(
    kind: Kind, runs: Sequence[RunSelection], *, settings: Settings, remote: RemoteSettings | None
) -> list[TargetEstimate]:
    """Every execution target the form offers (``available_backends(remote)``), each with its estimate for ``runs``,
    with the target's layout, the round-trip transfer on ``crux``, the PBS walltime of a
    remote target. A target that cannot be estimated carries the reason."""
    results = []
    for option in available_backends(remote):
        backend = option.value
        target = remote.target(backend) if remote is not None and backend in REMOTE_BACKENDS else None
        walltime = walltime_seconds(target.pbs.walltime) if target is not None and target.pbs is not None else None
        try:
            layout = execution_layout(backend, settings, remote)
            estimate = estimate_runtime(kind, backend, runs, layout=layout, settings=settings)
        except (ValueError, OSError) as error:
            results.append(TargetEstimate(option, walltime_seconds=walltime, error=str(error)))
            continue
        transfer = estimate_transfers(kind, estimate.selection) if backend == "crux" else None
        results.append(TargetEstimate(option, layout, estimate, transfer, walltime))
    return results
