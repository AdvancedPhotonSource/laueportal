"""The execution targets a job names (``job.backend``, written to ``request.json``), defined once.

``local`` is this portal's worker (``jobs.worker``). Every other target is remote (``laueportal.remote``):
the worker's dispatcher never takes it, ``jobs.queue.enqueue_run`` hands it to DM submission, and the remote poller owns its reconciliation. A job created
before the column existed has ``None``, which is local. Standard library only (import-linter rule 3): the
launcher-side ``remote.settings`` imports it on the DM host.
"""

from __future__ import annotations

LOCAL = "local"
REMOTE_BACKENDS: tuple[str, ...] = ("aps", "crux", "pioneer")
"""The remote targets: ``aps`` (PBS on the APS cluster), ``crux`` (the Globus flow to ALCF's Crux) and ``pioneer``
(the GPU queue of the APS cluster, for LaueMatching)."""
BACKENDS: tuple[str, ...] = (LOCAL, *REMOTE_BACKENDS)
"""Every target a request and a job may name (``jobs.records.new_job`` refuses others)."""
LOCAL_BACKENDS = frozenset({None, LOCAL})
"""The ``job.backend`` values that run on the worker; any other value is remote."""
SHARED = "shared"
"""A remote target whose PBS jobs read and write the run directory itself (the experiment folder is mounted on the
portal, the DM host and the compute nodes): the launcher submits with ``qsub`` over SSH, nothing is transferred."""
GLOBUS = "globus"
"""A remote target reached through the Globus flow: inputs and results are transferred, Globus Compute submits PBS."""
TRANSPORTS: dict[str, str] = {"aps": SHARED, "crux": GLOBUS, "pioneer": SHARED}
"""How each remote target is reached. The launcher, the plan and the PBS script branch on this, never on a target's
name, so a new target is added here and in its settings."""


def is_remote(backend: str | None) -> bool:
    """Whether a job with this ``backend`` runs remotely (never on the worker; the remote poller reconciles it)."""
    return backend not in LOCAL_BACKENDS


def transport(backend: str | None) -> str:
    """How the remote target ``backend`` is reached (``TRANSPORTS``); ``ValueError`` for any other value, so that an
    unknown target is never handled as another one."""
    if backend is None or backend not in TRANSPORTS:
        raise ValueError(f"Unknown remote execution target {backend!r}")
    return TRANSPORTS[backend]
