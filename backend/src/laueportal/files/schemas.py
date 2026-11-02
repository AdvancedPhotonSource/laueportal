"""Typed results of the files domain: directory listings for the roots-restricted path picker, the allowed roots,
the query of ``GET /api/v1/files/browse``, and the create forms' file helpers (``POST /api/v1/files/patterns`` and
``/files/indices``). The API names are part of the contract (Orval type names)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from laueportal.core.text import quoted
from laueportal.core.timeutil import UTCTimestamp


class EntryKind(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"


class FileEntry(BaseModel):
    """One entry of a directory listing, with raw values (the frontend formats sizes and dates)."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    name: str
    path: str
    """Absolute and resolved (a symlink shows its target, which is inside an allowed root); an entry that is not
    available shows its own path."""
    kind: EntryKind | None
    """Null for a symbolic link whose target's file system does not answer (``available`` false)."""
    size: int | None
    """Bytes; null for directories, and for an entry that is not available."""
    modified: UTCTimestamp | None
    """The last modification (UTC); null for an entry that is not available."""
    available: bool = True
    """False for a symbolic link or a mount point in the folder whose file system does not answer (through the
    probe, ``files.browser``): listed, never opened."""


class DirectoryListing(BaseModel):
    """One level of a folder inside the allowed roots (``GET /api/v1/files/browse``)."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    root: str
    """The name of the allowed root (``files.roots``) the directory is in."""
    root_path: str
    """That root's folder, absolute and resolved: ``path`` is inside it (the path picker's breadcrumbs start
    there)."""
    path: str
    """The directory, absolute and resolved."""
    entries: list[FileEntry]
    """Directories first, then files, each in natural name order, then the symbolic links whose file system does
    not answer; hidden (dot) entries are left out. Any entry may be ``available: false`` (a mount point that does
    not answer stays among the directories)."""
    truncated: bool
    """True when the folder holds more than is listed: more entries matched than ``files.listing_cap`` allows, or
    more than ten times that many files did, of which only that many (the first the file system gives) were sorted,
    so the listed files are the first, in natural order, of those. Every folder is always considered."""
    invalid_names: int
    """Entries left out because their name, or a link's target, is not valid UTF-8, which the API cannot send
    (renamed, they are listed)."""


class FileRoot(BaseModel):
    """An allowed root (``files.roots``): where the path picker starts."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    name: str
    """The root's name in the settings (``files.roots.<name>``): ``GET /api/v1/files/browse?root=<name>``."""
    path: str
    """Its folder, absolute and resolved."""
    available: bool
    """Whether the folder exists on this host (production's ``/net/...`` roots are absent elsewhere); browsing
    an unavailable root is a 404."""


class ScanLogSource(BaseModel):
    """What the scan-log import (``/scans/import``) accepts, and where its picker opens: the settings
    the page reads instead of copying them."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    max_bytes: int
    """The largest scan log the import reads, in bytes (``files.max_scan_log_mb`` MiB): a larger upload is a 413, a
    larger server file a 422."""
    extensions: list[str]
    """The scan-log extensions (``files.scan_log_extensions``), lower case with their dot (``.xml``)."""
    start_folder: str | None
    """Where the scan-log picker opens: ``files.scan_log_dir`` (resolved) when it is inside the roots, else the
    first root's folder; null without roots. It need not exist on this host."""


class FileRoots(BaseModel):
    """The allowed roots (``GET /api/v1/files/roots``), in the settings' order, and the scan-log import's source
    settings."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    roots: list[FileRoot]
    """Empty when no roots are configured: then no path is allowed."""
    scan_logs: ScanLogSource


_EXTENSION = re.compile(r"\.[^./\s]+")


class BrowseParams(BaseModel):
    """The query of ``GET /api/v1/files/browse``: ``params: Annotated[BrowseParams, Query()]``. A blank ``root`` or
    ``path`` counts as none."""

    model_config = ConfigDict(extra="forbid")

    root: str | None = Field(
        default=None,
        description=(
            "The name of an allowed root (`GET /api/v1/files/roots`). Alone, its folder is listed; with `path`, "
            "the path must be inside it (else a 400). An unknown root is a 400."
        ),
    )
    path: str | None = Field(
        default=None,
        description=(
            "An absolute folder inside the allowed roots (symlinks resolved; `~` is not expanded). Outside them, a "
            "400; missing on this host, a 404; a file, a 422."
        ),
    )
    extensions: str | None = Field(
        default=None,
        description=(
            "Comma list of the file extensions to list, e.g. `.xml,.txt` (any case). Folders are always listed. "
            "Without it, every file."
        ),
    )

    @field_validator("root", "path", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("extensions")
    @classmethod
    def _extension_list(cls, value: str | None) -> str | None:
        if value is None:
            return None
        items = [item.strip().lower() for item in value.split(",") if item.strip()]
        bad = [item for item in items if not _EXTENSION.fullmatch(item)]
        if bad:
            shown = ", ".join(quoted(item, 30) for item in bad[:5]) + (
                f" and {len(bad) - 5} more" if len(bad) > 5 else ""
            )
            raise ValueError(f"extensions must look like '.h5'; got {shown}")
        return ",".join(items) or None

    def extension_list(self) -> list[str] | None:
        """The extensions as a list, or None for every file."""
        return self.extensions.split(",") if self.extensions else None


FILE_PROGRAM = (
    "The create form asking: `lauego` takes a file's last two numbers as its scan point and depth (the indexing "
    "form), `wire` its last number as its scan point (the wire form)."
)


class PatternsRequest(BaseModel):
    """The body of ``POST /api/v1/files/patterns`` (Find Matching Files)."""

    model_config = ConfigDict(extra="forbid")

    folder: str = Field(description="An absolute folder inside the allowed roots (as `GET /api/v1/files/browse`).")
    program: Literal["lauego", "wire"] = Field(description=FILE_PROGRAM)


class FilePattern(BaseModel):
    """A suggested file-name template and the indices of the files it matches."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    pattern: str
    """A `%d` template, with `*` where similar templates differ (wildcards)."""
    label: str
    """The template with its index ranges, as the list shows it (`Twin2_wire_%d_%d.h5 (scanPoints: 2,745, depths:
    0-90x30)`)."""
    scan_points: str | None
    """The scan points of the files it matches, as a range (`1-10`); null when the template has no `%d`."""
    depth_range: str | None
    """Their depths (the second number; `lauego` only); null without one."""


class FilePatterns(BaseModel):
    """The suggestions for a folder (``POST /api/v1/files/patterns``)."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    patterns: list[FilePattern]
    """At most 10: the wildcard templates first (fewest `*` first), then the exact ones, most files first. Empty
    when the folder holds no HDF file (`files.hdf_extensions`)."""
    truncated: bool
    """True when the folder holds more HDF files than are read (200,000, the first the file system gives): the
    suggestions and their ranges are those of the files read."""


class IndicesRequest(BaseModel):
    """The body of ``POST /api/v1/files/indices`` (Find Indices, and the wire form's file indices)."""

    model_config = ConfigDict(extra="forbid")

    folder: str = Field(description="An absolute folder inside the allowed roots (as `GET /api/v1/files/browse`).")
    pattern: str | None = Field(
        default=None,
        max_length=1000,
        description=(
            "The file-name template (`%d` and `*`, as a run's file names), for `lauego` (required): its first `%d` "
            "is the scan point, its second the depth. The wire form takes every HDF file's last number "
            "and ignores it."
        ),
    )
    program: Literal["lauego", "wire"] = Field(description=FILE_PROGRAM)


class FileIndices(BaseModel):
    """The indices of a folder's files (``POST /api/v1/files/indices``), as the form's index fields take them."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    scan_points: str | None
    """The scan points of the matching files, as a range (`1-10`); null when none matched or gave one."""
    depth_range: str | None
    """Their depths (`lauego`, the template's second `%d`); null without one."""
    files: int
    """How many HDF files matched (`lauego`: the template; `wire`: those with a number)."""
    truncated: bool
    """True when the folder holds more HDF files than are read (200,000, the first the file system gives)."""
