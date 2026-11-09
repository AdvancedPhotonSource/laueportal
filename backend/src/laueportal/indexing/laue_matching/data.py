"""The data folder as the API sees it: the values a run freezes, its checks at validation and the Forward Cache list.

Every path in the folder is the administrator's (``[laue_matching] data_root``); the geometry and crystal files are the
form's, inside the roots. A check of either goes through ``files.probe``, and the requests that make them hold a folder
read's turn (``core.api.read_folders``), so a hung mount costs a request a timeout and holds no database connection.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from laueportal.core.settings import Settings
from laueportal.files.inputs import WorkflowValidationError
from laueportal.files.probe import NotResponding, bounded, open_check
from laueportal.files.roots import PathNotAllowed, resolve_inside_roots
from laueportal.indexing.laue_matching.cache import (
    DATABASE_NAME,
    FORWARD_NAME,
    ORIENTATION_BYTES,
    CacheKey,
    cache_folder,
    read_record,
    record_mismatch,
)
from laueportal.indexing.laue_matching.params import LaueMatchingParamsV1
from laueportal.indexing.schemas import ForwardCacheList
from laueportal.jobs.validation import Checked, Issues

NOT_SET = "The LaueMatching data folder ([laue_matching] data_root) is not set on this portal"
READ_ERRORS = (OSError, ValueError, KeyError, SyntaxError)
"""What reading a geometry or crystal file raises: ``xml.etree``'s ``ParseError`` is a ``SyntaxError``."""
DEEP_DEPTH_UM = 100.0
"""Depth beyond which the geometry's missing depth term moves a spot by about half a pixel."""
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _database_orientations(root: Path) -> int:
    """The orientations in the database (``OSError`` when it cannot be read, ``ValueError`` for a truncated file)."""
    database = root / DATABASE_NAME
    size = bounded(database, database.stat).st_size
    if size == 0 or size % ORIENTATION_BYTES:
        raise ValueError(f"{DATABASE_NAME} is {size} bytes, not a whole number of orientations")
    return size // ORIENTATION_BYTES


def _instrument_values(geometry_file: str, crystal_file: str) -> dict[str, Any]:
    from dataclasses import asdict

    from laueportal.science.laue_matching import read_instrument  # lauelab, loaded only when a form needs it

    for path in (geometry_file, crystal_file):
        bounded(path, lambda checked=path: open_check(checked), requested=True)
    return asdict(read_instrument(geometry_file, crystal_file))


def with_instrument(request: LaueMatchingParamsV1, settings: Settings) -> LaueMatchingParamsV1:
    """``request`` with the crystal, detector and database values it freezes. ``WorkflowValidationError`` (run creation's
    own refusal, nothing created) when they cannot be read."""
    root = settings.laue_matching.data_root
    if root is None:
        raise WorkflowValidationError(NOT_SET)
    try:
        values = _instrument_values(request.geometry_file, request.crystal_file)
        return request.with_values(**values, n_orientations=_database_orientations(root))
    except READ_ERRORS as error:
        raise WorkflowValidationError(f"The geometry, crystal or orientation files cannot be read: {error}") from None


def _key(values: dict[str, Any], elo: float, ehi: float, max_spots: int, n_orientations: int) -> CacheKey:
    return CacheKey(
        space_group=values["space_group"],
        lattice_parameters=tuple(values["lattice_parameters"]),
        p_array=tuple(values["p_array"]),
        r_array=tuple(values["r_array"]),
        px_x=values["px_x"],
        px_y=values["px_y"],
        nr_px_x=values["nr_px_x"],
        nr_px_y=values["nr_px_y"],
        elo=elo,
        ehi=ehi,
        max_nr_laue_spots=max_spots,
        n_orientations=n_orientations,
    )


def _forward_exists(folder: Path) -> bool:
    forward = folder / FORWARD_NAME
    try:
        bounded(forward, forward.stat)
    except FileNotFoundError:
        return False
    return True


def _cache_problem(folder: Path, key: CacheKey) -> str | None:
    """Why an existing cache does not fit ``key``, or ``None``."""
    record = folder / f"{FORWARD_NAME}.meta.json"
    bounded(record, lambda: open_check(record))
    return record_mismatch(read_record(folder), key)


def check_laue_matching(issues: Issues, settings: Settings, checked: Checked, params: LaueMatchingParamsV1) -> None:
    """The energy range, the data folder, and the named cache against the run's crystal, detector and energies."""
    if checked.usable("elo", "ehi") and params.elo >= params.ehi:
        issues.error(["ehi"], "energy_range", "Max Energy must be above Min Energy")
    root = settings.laue_matching.data_root
    if root is None:
        issues.error(["forward_cache"], "not_configured", NOT_SET)
        return
    try:
        n_orientations = _database_orientations(root)
    except NotResponding:
        issues.error(["forward_cache"], "not_responding", "The LaueMatching data folder is not responding")
        return
    except (OSError, ValueError) as error:
        issues.error(["forward_cache"], "no_database", f"The orientation database cannot be read: {error}")
        return
    names = ("geometry_file", "crystal_file", "elo", "ehi", "max_nr_laue_spots", "forward_cache")
    if not checked.usable(*names) or issues.has_error(*names):
        return
    try:
        values = _instrument_values(params.geometry_file, params.crystal_file)
    except NotResponding:
        issues.error(["geometry_file"], "not_responding", "The geometry or crystal file is not responding")
        return
    except READ_ERRORS as error:
        issues.error(["geometry_file"], "unreadable", f"The geometry or crystal file cannot be read: {error}")
        return
    folder = cache_folder(root, params.forward_cache)
    name = params.forward_cache
    try:
        exists = _forward_exists(folder)
        if params.create_forward_cache:
            if exists:
                issues.error(
                    ["forward_cache"], "cache_exists", f"Forward cache {name} already exists: choose a new name"
                )
            else:
                issues.warning(
                    ["forward_cache"],
                    "cache_build",
                    f"The first frame builds forward cache {name}, which can take many minutes",
                )
            return
        if not exists:
            issues.error(["forward_cache"], "cache_not_found", f"Forward cache {name} was not found")
            return
        key = _key(values, params.elo, params.ehi, params.max_nr_laue_spots, n_orientations)
        problem = _cache_problem(folder, key)
    except NotResponding:
        issues.error(["forward_cache"], "not_responding", "The forward cache is not responding")
        return
    except (OSError, ValueError) as error:
        issues.error(["forward_cache"], "unreadable", f"Forward cache {name} cannot be read: {error}")
        return
    if problem is not None:
        issues.error(["forward_cache"], "cache_mismatch", f"Forward cache {name} does not fit this run: {problem}")


def _suggested_name(geometry_file: str, crystal_file: str, elo: float, ehi: float, taken: Sequence[str]) -> str:
    stem = f"{Path(crystal_file).stem}_{Path(geometry_file).stem}_{elo:g}-{ehi:g}"
    base = _UNSAFE.sub("_", stem).strip("._-")[:120] or "cache"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}_{number}", number + 1
    return name


def list_forward_caches(
    settings: Settings, *, geometry_file: str, crystal_file: str, elo: float, ehi: float, max_nr_laue_spots: int
) -> ForwardCacheList:
    """The caches in the data folder whose record exactly fits the form's values, and a free name for a new one. A
    problem is part of the answer: the form shows it, and offers a new cache."""

    def unavailable(problem: str) -> ForwardCacheList:
        return ForwardCacheList(caches=[], suggested_name=None, problem=problem)

    root = settings.laue_matching.data_root
    if root is None:
        return unavailable(NOT_SET)
    try:
        geometry = str(resolve_inside_roots(geometry_file, settings)[1])
        crystal = str(resolve_inside_roots(crystal_file, settings)[1])
    except PathNotAllowed as refused:
        return unavailable(str(refused.problem.detail))
    except NotResponding:
        return unavailable("The geometry or crystal file is not responding")
    cap = settings.files.listing_cap
    try:
        n_orientations = _database_orientations(root)
        values = _instrument_values(geometry, crystal)
        names = bounded(
            root, lambda: sorted(entry.name for entry in os.scandir(root) if entry.is_dir() and entry.name[0] != ".")
        )
    except NotResponding:
        return unavailable("The LaueMatching data folder is not responding")
    except READ_ERRORS as error:
        return unavailable(f"The geometry, crystal or orientation files cannot be read: {error}")
    key = _key(values, elo, ehi, max_nr_laue_spots, n_orientations)
    fitting: list[str] = []
    for name in names[:cap]:
        folder = cache_folder(root, name)
        try:
            if _forward_exists(folder) and _cache_problem(folder, key) is None:
                fitting.append(name)
        except NotResponding:
            return unavailable("The LaueMatching data folder is not responding")
        except (OSError, ValueError):
            continue  # a folder that holds no readable cache is not offered
    return ForwardCacheList(
        caches=fitting, suggested_name=_suggested_name(geometry, crystal, elo, ehi, names), problem=None
    )
