"""Storage paths across the portal/remote boundary: containment checks and prefix mapping.

The runners (``jobs.runner``, ``jobs.mpi_runner``) remap the portal's paths
with ``map_path`` when they read a run's request and manifest; the portal side checks returned and
planned paths with ``child_path``. Standard library only: no database, no settings (import-linter
rule 3), so the compute nodes import it.

The way back: :func:`inverse_path_map` is the inverse the publication applies to the input-image paths of ``output.h5``
and the Output XML (``remote.poller``). Every other path a remote run wrote stays as the remote host saw it (a
reconstruction catalog's ``/points/source_paths`` and ``/geometry@path``, a point file's ``source_path`` and
``geometry_path``, ``output.h5``'s ``/geometry@path`` and ``/crystal@source``, the Output XML's ``geoFile`` and
``xtlFile``, ``failures.jsonl``'s ``source``): portal readers map them when they read them, with :func:`portal_path` and
the ``path_map`` of the run's own plan, ``bdp.json``, which the portal wrote at submission (:func:`run_path_map`). The
returned ``run.json`` records the same map, but it comes from the remote side, so it is never used for this. A mapped
path is confined to the portal directory its prefix maps to (the run directory, or the APS root): one that leaves it (a
``..`` segment, a symlink) is unavailable (``None``), and so is a remote run's value no prefix covers, and every value
of a run whose plan exists but cannot be used (the rule fails closed). ``jobs.summary.failure_records`` maps
``failures.jsonl``'s sources this way. On request the portal resolves both sides through its file probe
(``resolve=files.roots.recorded_real_path``: a hung ``/net`` mount answers within the probe's bound); this module only
takes the function, so it imports nothing of the portal's.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

PLAN_NAME = "bdp.json"
MAX_PLAN_BYTES = 16 * 1024 * 1024
"""The poller's limit for a control document (``remote.poller``), applied to the plan read here."""


def child_path(root, path):
    """Reject prefix lookalikes, parent traversal, and symlinks leaving the root."""
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"{path} must be inside {root}")
    return path


def map_path(path, mappings):
    """Translate an absolute path with the longest matching directory prefix."""
    p = PurePosixPath(path)
    for source, destination in sorted(mappings.items(), key=lambda pair: len(pair[0]), reverse=True):
        try:
            relative = p.relative_to(source)
        except ValueError:
            continue
        return str(PurePosixPath(destination) / relative)
    raise ValueError(f"No storage mapping for {path}")


def inverse_path_map(mappings: Mapping[str, str]) -> dict[str, str]:
    """Remote prefix → portal prefix, and every portal prefix to itself (the publication's inverse)."""
    return {
        **{source: source for source in mappings},
        **{destination: source for source, destination in mappings.items()},
    }


def _matching_prefix(path: str, mappings: Mapping[str, str]) -> tuple[str, str] | None:
    """The ``(source, destination)`` pair :func:`map_path` would use for ``path`` (the longest whole-component
    prefix), or ``None``."""
    p = PurePosixPath(path)
    for source, destination in sorted(mappings.items(), key=lambda pair: len(pair[0]), reverse=True):
        try:
            p.relative_to(source)
        except ValueError:
            continue
        return source, destination
    return None


def portal_path(
    path: str,
    mappings: Mapping[str, str] | None,
    *,
    resolve: Callable[[str], str | os.PathLike[str]] | None = None,
) -> str | None:
    """A path a remote run wrote, as the portal sees it: the inverse of the run's ``path_map`` (:func:`run_path_map`).

    - ``None`` (a run without a plan: a local run, whose paths are portal paths already): returned as written.
    - A map: the longest remote prefix maps to its portal prefix (a portal prefix maps to itself), and the result
      must lie inside that portal directory (:func:`child_path`'s rule: no ``..`` or symlink leaving it);
      otherwise, and for a value no prefix covers (a relative name, another host's path), the path is
      unavailable: ``None``. The empty map (a plan :func:`run_path_map` cannot use) covers nothing: every value is
      unavailable.

    ``resolve`` resolves the portal directory and the mapped path for that check (``Path.resolve`` by default); an
    ``OSError``, ``RuntimeError`` or ``ValueError`` from it makes the path unavailable, and any other exception
    passes through (the portal's ``files.roots.recorded_real_path``: ``NotResponding``).

    Readers still confine every path they open to the portal's file roots, as for any path
    (``files.roots.resolve_inside_roots``), and report a missing file as such.
    """
    if mappings is None:
        return path
    inverse = inverse_path_map(mappings)
    match = _matching_prefix(path, inverse)
    if match is None:
        return None
    mapped = map_path(path, {match[0]: match[1]})
    resolved = resolve or (lambda value: Path(value).resolve())
    try:
        root, child = Path(resolved(match[1])), Path(resolved(mapped))
    except (OSError, RuntimeError, ValueError):  # a symlink loop, a NUL
        return None
    if not child.is_relative_to(root) or child == root:  # outside its portal directory (child_path's rule)
        return None
    return mapped


def run_path_map(run_directory: str | os.PathLike[str]) -> dict[str, str] | None:
    """The ``path_map`` of the remote run in ``run_directory``: its plan's (``bdp.json``, written by the portal at
    submission), never the returned ``run.json``'s.

    ``None`` when the run has no plan (``bdp.json`` does not exist): a local run, or a remote run never submitted,
    whose paths are portal paths. A plan that exists but cannot be used gives ``{}``, which maps nothing:
    :func:`portal_path` then reports every value unavailable rather than trusting the remote side's. That is a plan
    that is a symbolic link or not a regular file (it is opened without following a link and without blocking, so a
    FIFO cannot hang the reader), unreadable, larger than ``MAX_PLAN_BYTES``, not UTF-8 JSON (or nested too deeply
    to parse), or without a ``path_map`` of text to text."""
    path = Path(run_directory) / PLAN_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):  # a symbolic link (ELOOP), a component that is not a directory, a NUL in the path
        return {}
    try:
        with open(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PLAN_BYTES:
                return {}
            data = handle.read(MAX_PLAN_BYTES + 1)
        if len(data) > MAX_PLAN_BYTES:
            return {}
        plan = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    mappings = plan.get("path_map") if isinstance(plan, dict) else None
    if not isinstance(mappings, dict) or not all(
        isinstance(source, str) and isinstance(destination, str) for source, destination in mappings.items()
    ):
        return {}
    return dict(mappings)
