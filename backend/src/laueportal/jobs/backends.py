"""The execution targets a job names (``job.backend``, written to ``request.json``), defined once.

``local`` is this portal's worker (``jobs.worker``). Every other target is remote (``laueportal.remote``):
the worker's dispatcher never takes it, ``jobs.queue.enqueue_run`` hands it to DM submission, and the remote poller owns its reconciliation. A job created
before the column existed has ``None``, which is local. Standard library only (import-linter rule 3): the
launcher-side ``remote.settings`` imports it on the DM host.
"""

from __future__ import annotations

LOCAL = "local"
REMOTE_BACKENDS: tuple[str, ...] = ("aps", "crux")
"""The remote targets: ``aps`` (PBS on the APS cluster) and ``crux`` (the Globus flow to ALCF's Crux)."""
BACKENDS: tuple[str, ...] = (LOCAL, *REMOTE_BACKENDS)
"""Every target a request and a job may name (``jobs.records.new_job`` refuses others)."""
LOCAL_BACKENDS = frozenset({None, LOCAL})
"""The ``job.backend`` values that run on the worker; any other value is remote."""


def is_remote(backend: str | None) -> bool:
    """Whether a job with this ``backend`` runs remotely (never on the worker; the remote poller reconciles it)."""
    return backend not in LOCAL_BACKENDS
