"""The layout of LaueMatching's data folder (``[laue_matching] data_root``) and whether a forward cache fits a run.

The folder holds the orientation database and one folder per named forward cache::

    <data_root>/100MilOrients.bin
    <data_root>/<cache name>/forward.bin            the forward simulation of every orientation
    <data_root>/<cache name>/forward.bin.meta.json  its record, written by the indexer
    <data_root>/<cache name>/hkls.csv               the reflections it was built from

A cache fits a run only when its record holds exactly the run's values: the indexer keys a cache on a hash of the raw
values, and any other value is a different cache. A file of the wrong size would be rebuilt over by the indexer without
looking at its record, so the portal checks the record before every launch. Standard library only: the API and the
compute process both use it, and a compute process has no pydantic (import-linter rule 3b).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATABASE_NAME = "100MilOrients.bin"
FORWARD_NAME = "forward.bin"
HKL_NAME = "hkls.csv"
RECORD_SUFFIX = ".meta.json"
RECORD_NAME = FORWARD_NAME + RECORD_SUFFIX
ORIENTATION_BYTES = 72
"""One orientation in the database: nine doubles."""
FORWARD_KIND = "forward_cache"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def name_problem(name: str) -> str | None:
    """Why ``name`` cannot name a cache (it becomes a folder name), or ``None``."""
    if _NAME.fullmatch(name):
        return None
    return "must start with a letter or digit and hold only letters, digits, '.', '_' and '-' (at most 128 characters)"


def cache_folder(data_root: str | os.PathLike[str], name: str) -> Path:
    return Path(data_root) / name


@dataclass(frozen=True)
class CacheKey:
    """The values a forward cache is built for, as its record holds them."""

    space_group: int
    lattice_parameters: tuple[float, ...]
    p_array: tuple[float, ...]
    r_array: tuple[float, ...]
    px_x: float
    px_y: float
    nr_px_x: int
    nr_px_y: int
    elo: float
    ehi: float
    max_nr_laue_spots: int
    n_orientations: int

    def expected(self) -> dict[str, Any]:
        """The record's ``config`` keys and this key's values."""
        return {
            "SpaceGroup": self.space_group,
            "LatticeParameter": list(self.lattice_parameters),
            "P_Array": list(self.p_array),
            "R_Array": list(self.r_array),
            "PxX": self.px_x,
            "PxY": self.px_y,
            "NrPxX": self.nr_px_x,
            "NrPxY": self.nr_px_y,
            "Elo": self.elo,
            "Ehi": self.ehi,
            "MaxNrLaueSpots": self.max_nr_laue_spots,
            "n_orientations": self.n_orientations,
        }


def key_from_parameters(parameters: Mapping[str, Any]) -> CacheKey:
    """The key of a run from its ``request.json`` parameters."""
    return CacheKey(
        space_group=int(parameters["space_group"]),
        lattice_parameters=tuple(float(value) for value in parameters["lattice_parameters"]),
        p_array=tuple(float(value) for value in parameters["p_array"]),
        r_array=tuple(float(value) for value in parameters["r_array"]),
        px_x=float(parameters["px_x"]),
        px_y=float(parameters["px_y"]),
        nr_px_x=int(parameters["nr_px_x"]),
        nr_px_y=int(parameters["nr_px_y"]),
        elo=float(parameters["elo"]),
        ehi=float(parameters["ehi"]),
        max_nr_laue_spots=int(parameters["max_nr_laue_spots"]),
        n_orientations=int(parameters["n_orientations"]),
    )


def record_mismatch(record: Mapping[str, Any], key: CacheKey) -> str | None:
    """How a forward cache's record differs from ``key``, or ``None`` when every value is exactly the same."""
    if record.get("kind") != FORWARD_KIND:
        return f"its record is of kind {record.get('kind')!r}"
    config = record.get("config")
    if not isinstance(config, Mapping):
        return "its record has no configuration"
    different = [name for name, value in key.expected().items() if config.get(name) != value]
    return f"it was built for other values of {', '.join(different)}" if different else None


def read_record(folder: str | os.PathLike[str]) -> dict[str, Any]:
    """The forward cache's record in ``folder`` (``OSError``, ``ValueError`` when it cannot be read)."""
    with open(Path(folder) / RECORD_NAME, encoding="utf-8") as stream:
        record = json.load(stream)
    if not isinstance(record, dict):
        raise ValueError("the record is not a JSON object")
    return record


def reuse_problem(folder: str | os.PathLike[str], key: CacheKey) -> str | None:
    """Why the cache in ``folder`` cannot be reused for ``key``, or ``None``: both files are there, its record exactly
    matches, and the forward file has the size its record gives. A compute process's check (it reads the files
    directly), made just before the indexer starts."""
    path = Path(folder)
    for name in (FORWARD_NAME, HKL_NAME):
        if not (path / name).is_file():
            return f"{name} is missing"
    try:
        record = read_record(path)
    except (OSError, ValueError) as error:
        return f"its record cannot be read ({error})"
    size = record.get("artifact", {}).get("size") if isinstance(record.get("artifact"), Mapping) else None
    actual = (path / FORWARD_NAME).stat().st_size
    if size != actual:
        return f"{FORWARD_NAME} is {actual} bytes, and its record says {size}"
    return record_mismatch(record, key)
