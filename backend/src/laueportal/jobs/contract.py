"""Requests, results, and executor hooks for compute functions, and the compute registry.

A compute function receives a plain :class:`RunRequest`, the run's manifest
entries, and :class:`RunHooks`; it writes the run's scientific output and
returns a :class:`RunOutcome`. The queue executor supplies hooks and persists
state. Nothing here opens a database connection, and this module never
imports the database or the settings (import-linter rule 3).

Each program registers its compute function by run kind when its module is
imported (``indexing.lauego.compute``, ``reconstruction.wire.compute``);
:func:`get_compute_function` imports them on first use.

A live run has a live compute function of its own, registered by the same run kind
(``register_live_compute_function``): it takes no manifest entries, but asks the hooks for each batch of inputs
as they arrive (``RunHooks.arrivals``) until ``RunHooks.should_stop`` says the run is sealed, then drains, unless
``RunHooks.shutting_down`` says the service stops (then it starts no further input). The live wire compute is
``reconstruction.wire.live_compute``, the live LaueGo compute on raw frames ``indexing.lauego.live_compute``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from laueportal.jobs.rundir import FailureRecord, ManifestEntry


@dataclass(frozen=True)
class RunRequest:
    """Compute settings loaded from ``request.json`` and the execution policy."""

    job_id: int
    kind: str
    display_id: str
    run_directory: str
    manifest_path: str
    document: Mapping[str, Any]  # the full request.json document
    n_inputs: int
    workers: int
    max_in_flight: int | None
    reconstruction_workers: int = 1  # points a wire reconstruction computes at once
    live: Mapping[str, Any] | None = None  # a live run's live.json; None for a batch run
    cores: int | None = None  # a live run's cores (the dispatcher's share); None for a batch run
    publish_seconds: float | None = None  # a live run's live.publish_seconds; None for a batch run

    @property
    def parameters(self) -> Mapping[str, Any]:
        """Saved form parameters."""

        return self.document.get("request", {})

    @property
    def output(self) -> Mapping[str, Any]:
        return self.document.get("output", {})


class RunHooks(Protocol):
    """Executor callbacks for progress, cancellation, and output reporting."""

    def should_stop(self) -> bool:
        """True once cancellation or shutdown was requested; stop admitting new inputs.

        For a live run: True once it is sealed (its Stop: every input has been handed out by ``arrivals``, so finish
        them) or the service stops (stop admitting, as a batch run; ``shutting_down`` tells the two apart)."""
        ...

    def shutting_down(self) -> bool:
        """True once the service stops (``should_stop`` is then True too): start no further input, a sealed live
        run's drain included; inputs already running finish."""
        ...

    def arrivals(self) -> Sequence[ManifestEntry]:
        """A live run's inputs that arrived since the last call, in arrival order, each counted in the run's
        ``n_inputs`` before it is returned (a batch run's: none). Their ``index`` runs on from the last call's; a
        followed point gives one entry per selected depth (a depth the point lacks is the compute's failed input)."""
        ...

    def report_progress(self, *, succeeded: int, failed: int) -> None:
        """Absolute processed counts so far; the executor rate-limits persistence."""
        ...

    def record_failure(self, record: FailureRecord) -> None:
        """Append one failed or unattempted input to the run's failure report."""
        ...

    def report_outcome(self, outcome: RunOutcome) -> None:
        """Keep output details for the run summary if computation raises."""
        ...

    def log(self, message: str) -> None:
        """Operational message for the run log."""
        ...


@dataclass
class RunOutcome:
    """Compute results used by the executor to determine the final run status."""

    n_succeeded: int
    n_failed: int
    stopped: bool = False  # admission ended because should_stop() returned True
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> path of published outputs
    warnings: list[str] = field(default_factory=list)  # auxiliary problems (for example XML export)
    validation_error: str | None = None  # authoritative output failed structural validation
    provenance: dict[str, Any] = field(default_factory=dict)  # versions and effective configuration
    summary: str | None = None
    n_indexed: int | None = None  # frames with at least one pattern, when known

    @property
    def n_processed(self) -> int:
        return self.n_succeeded + self.n_failed


ComputeFunction = Callable[[RunRequest, Iterable[ManifestEntry], RunHooks], RunOutcome]
LiveComputeFunction = Callable[[RunRequest, RunHooks], RunOutcome]


class UnknownRunKind(LookupError):
    """No compute function is registered for a request kind."""


_REGISTRY: dict[str, ComputeFunction] = {}
_LIVE_REGISTRY: dict[str, LiveComputeFunction] = {}


def register_compute_function(kind: str, function: ComputeFunction) -> None:
    _REGISTRY[kind] = function


def register_live_compute_function(kind: str, function: LiveComputeFunction) -> None:
    """Register the compute function of a live run of ``kind`` (the module's docstring)."""
    _LIVE_REGISTRY[kind] = function


def get_compute_function(kind: str) -> ComputeFunction:
    try:
        return _REGISTRY[kind]
    except KeyError:
        # Importing a program module registers its compute function (lazily, so lauelab loads only here).
        from laueportal.indexing.lauego import compute as _lauego  # noqa: F401
        from laueportal.reconstruction.wire import compute as _wire  # noqa: F401

        try:
            return _REGISTRY[kind]
        except KeyError:
            raise UnknownRunKind(f"no compute function is registered for run kind {kind!r}") from None


def get_live_compute_function(kind: str) -> LiveComputeFunction:
    """The live compute function of ``kind`` (``UnknownRunKind`` when none is registered); importing the programs'
    compute modules and their live compute modules first, as ``get_compute_function`` does."""
    try:
        return _LIVE_REGISTRY[kind]
    except KeyError:
        from laueportal.indexing.lauego import compute as _lauego  # noqa: F401
        from laueportal.indexing.lauego import live_compute as _live_lauego  # noqa: F401
        from laueportal.reconstruction.wire import compute as _wire  # noqa: F401
        from laueportal.reconstruction.wire import live_compute as _live_wire  # noqa: F401

        try:
            return _LIVE_REGISTRY[kind]
        except KeyError:
            raise UnknownRunKind(f"no live compute function is registered for run kind {kind!r}") from None
