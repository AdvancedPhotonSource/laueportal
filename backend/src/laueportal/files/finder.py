"""The create forms' file helpers, Find Matching Files and Find Indices, for one folder.

- :func:`find_patterns` (``POST /api/v1/files/patterns``): up to 10 file-name templates for the folder's HDF files
  (``files.hdf_extensions``), each number at a name's end made a ``%d`` (the last
  two for the LaueGo form, the last one for the wire form) and similar templates merged into wildcards
  (``files.patterns``), labelled with their index ranges, with the ranges themselves.
- :func:`find_indices` (``POST /api/v1/files/indices``): the scan points and depths of the files a template matches
  (the LaueGo form's Find Indices: a template's first ``%d`` is the point, its second the depth, matched as a run's
  inputs are, ``files.patterns.compile_filename_templates``); the wire form's are every HDF file's last number,
  whatever the template.

The folder is a request's path: inside the roots, on this host and a folder (``files.browser.existing_folder``: 400,
404, 422). The work is bounded: at most ``MAX_FILES`` HDF files are read (the first the file system gives; then
``truncated``), each by its name only, and names that are not valid UTF-8 are left out (JSON cannot send them).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Literal

from laueportal.core.problems import ProblemError
from laueportal.core.settings import Settings
from laueportal.core.text import quoted
from laueportal.files.browser import InvalidSelection, existing_folder
from laueportal.files.patterns import build_pattern_label, compile_filename_templates, extract_index_patterns
from laueportal.files.patterns import file_patterns as suggested_patterns
from laueportal.files.roots import is_utf8, shown
from laueportal.files.schemas import FileIndices, FilePattern, FilePatterns
from laueportal.util.srange import srange

FileProgram = Literal["lauego", "wire"]

MAX_FILES = 200_000
"""The most HDF files a helper reads in a folder: twice the largest migrated run (99,825 inputs). Reading
200,000 names takes about a quarter of a second."""
MAX_PATTERNS = 10
"""The most suggestions."""
INDICES: dict[FileProgram, int] = {"lauego": 2, "wire": 1}
"""How many numbers at a name's end each form takes, and so the most ``%d`` a LaueGo
template may have (the scan point and the depth; ``indexing.validation``'s ``placeholders``)."""


def hdf_files(settings: Settings, folder: str) -> tuple[list[str], bool]:
    """The names of the folder's HDF files (at most ``MAX_FILES``, sorted) and whether there were more."""

    _, directory = existing_folder(folder, settings)
    extensions = tuple(extension.lower() for extension in settings.files.hdf_extensions)
    names: list[str] = []
    truncated = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(extensions):
                    continue
                try:
                    if not entry.is_file():  # follows a symlink, as os.path.isfile does
                        continue
                except OSError:
                    continue
                if not is_utf8(entry.name):
                    continue
                if len(names) == MAX_FILES:
                    truncated = True
                    break
                names.append(entry.name)
    except OSError as error:
        raise InvalidSelection(
            f"{shown(directory)} cannot be read: {error.strerror or type(error).__name__}."
        ) from None
    return sorted(names), truncated


def _range(values: Iterable[int]) -> str | None:
    found = set(values)
    return str(srange(found)) if found else None  # pyright: ignore[reportArgumentType]  (srange takes integers too)


def find_patterns(settings: Settings, folder: str, program: FileProgram) -> FilePatterns:
    """The folder's file-name templates for the ``program`` form (Find Matching Files)."""

    names, truncated = hdf_files(settings, folder)
    patterns = []
    for pattern, indices in suggested_patterns(names, INDICES[program], MAX_PATTERNS):
        patterns.append(
            FilePattern(
                pattern=pattern,
                label=build_pattern_label(pattern, indices),
                scan_points=_range(index[0] for index in indices if index),
                depth_range=_range(index[1] for index in indices if len(index) > 1),
            )
        )
    return FilePatterns(patterns=patterns, truncated=truncated)


def _invalid_pattern(code: str, message: str) -> ProblemError:
    return ProblemError(
        422, "Invalid request", message, errors=[{"loc": ["body", "pattern"], "code": code, "message": message}]
    )


def find_indices(settings: Settings, folder: str, pattern: str | None, program: FileProgram) -> FileIndices:
    """The scan points (and, for ``lauego``, depths) of the folder's files (Find Indices and Load File
    Indices). A ``lauego`` request without a template, or with more than two ``%d`` (as a run's: each more makes
    the matcher's work on a long run of digits grow exponentially), is a 422."""

    template = (pattern or "").strip()
    if program == "lauego" and not template:
        raise _invalid_pattern("missing", "A file-name template is required to find its files' indices")
    if program == "lauego" and template.count("%d") > INDICES[program]:  # as validation refuses it
        raise _invalid_pattern("placeholders", f"The file-name template {quoted(template)} has more than two %d")
    names, truncated = hdf_files(settings, folder)
    if program == "wire":
        found = [indices for group in extract_index_patterns(names, 1).values() for indices in group if indices]
    else:
        matcher = compile_filename_templates([template])
        found = [list(match.indices) for name in names if (match := matcher.match(name)) is not None]
    return FileIndices(
        scan_points=_range(indices[0] for indices in found if indices),
        depth_range=_range(indices[1] for indices in found if len(indices) > 1),
        files=len(found),
        truncated=truncated,
    )
