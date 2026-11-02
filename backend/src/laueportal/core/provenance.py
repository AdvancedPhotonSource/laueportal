"""The code that computes: the installed lauelab (what a run records in ``run.json``; the entity tags) and the
portal's own source (the entity tags, ``core.etag``).

Standard library only, so the compute modules use it too (no database, settings or web layer: import-linter
rule 3). Nothing here runs at import.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path

LAUELAB = "lauelab"
PORTAL_SOURCE = Path(__file__).resolve().parents[1]
"""The ``laueportal`` package's directory."""


def lauelab_provenance() -> dict[str, str]:
    """The installed lauelab's ``lauelab_version`` and, from its ``direct_url.json`` (an install from a pinned
    commit), ``lauelab_commit`` and ``lauelab_source``; empty without lauelab."""

    provenance: dict[str, str] = {}
    try:
        distribution = importlib.metadata.distribution(LAUELAB)
    except importlib.metadata.PackageNotFoundError:
        return provenance
    provenance["lauelab_version"] = distribution.version
    direct_url = distribution.read_text("direct_url.json")
    if direct_url:
        try:
            document = json.loads(direct_url)
        except json.JSONDecodeError:
            return provenance
        commit = document.get("vcs_info", {}).get("commit_id")
        if commit:
            provenance["lauelab_commit"] = commit
        if document.get("url"):
            provenance["lauelab_source"] = document["url"]
    return provenance


def library_provenance(engine: str) -> dict[str, str]:
    """A compute's ``run.json`` provenance starts so (the batch and live computes, LaueGo and wire alike): its
    ``engine`` first, then :func:`lauelab_provenance`."""

    return {"engine": engine, **lauelab_provenance()}


def source_digest(directory: str | os.PathLike[str]) -> str:
    """A digest of every file under ``directory``: names and contents, without ``__pycache__`` and bytecode. The
    same source gives the same digest in every process."""

    root = Path(directory)
    files = sorted(
        (path.relative_to(root).as_posix(), path)
        for path in root.rglob("*")
        if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.relative_to(root).parts
    )
    digest = hashlib.blake2b(digest_size=16)
    for name, path in files:
        content = path.read_bytes()
        for part in (name.encode(), content):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def portal_identity() -> str:
    """The portal's code: a digest of the ``laueportal`` package's source (it changes with any edit, committed
    or not)."""

    return source_digest(PORTAL_SOURCE)


def lauelab_identity() -> str:
    """lauelab's code: the commit it was installed from (the pin), else its version and a digest of its installed
    files (an editable or index install)."""

    provenance = lauelab_provenance()
    if commit := provenance.get("lauelab_commit"):
        return commit
    spec = importlib.util.find_spec(LAUELAB)  # locates the package without importing it
    locations = list(spec.submodule_search_locations or ()) if spec is not None else []
    files = source_digest(locations[0]) if locations else "none"
    return f"{provenance.get('lauelab_version', 'none')}+{files}"
