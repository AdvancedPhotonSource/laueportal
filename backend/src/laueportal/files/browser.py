"""The file browser behind the path pickers, restricted to the allowed roots.

Every path goes through ``files.roots.resolve_inside_roots``. A listing shows one directory level:
directories first, then files, each in natural name order (``files.inputs.natural_key``; a mount point whose file
system does not answer stays among the folders, ``available`` false), then the symbolic links whose file system
does not answer (``available`` false, ``kind`` null). Hidden (dot) entries are left out, and so are symlinks that
lead outside every root, special files (FIFOs, sockets, devices), broken links, and entries whose name or link
target is not valid UTF-8 (UTF-8 JSON cannot send it; the listing counts them in ``invalid_names``). Listings
stop at ``settings.files.listing_cap`` entries and say when they were truncated.

A listing's work is bounded too, whatever the folder's size. It reads every entry's name and type, which is cheap
(about 0.1 s for 200,000), and keeps every folder; of the files it would list (the extension filter applied), it
sorts only the first ``READ_FACTOR`` times ``listing_cap`` the file system gives. Sorting all 200,000 entries by
natural key would cost 2 s a request; the bounded listing takes about 0.25 s. With more files than that, the listing
is ``truncated`` and its files are the first, in natural order, of those sorted. Entries are typed
(``files.schemas.FileEntry``): size in bytes, modification time as an aware UTC datetime.

``browse`` serves ``GET /api/v1/files/browse`` (a root by name, a folder by path, or a folder checked to be in a
named root; a folder read: at most ``core.api.FOLDER_READS_AT_ONCE`` at once, waited for outside the thread pool)
and ``list_roots`` ``GET /api/v1/files/roots`` (the path picker; the scan detail's Files Path uses it
first).

Errors are problems: ``PathNotAllowed`` (400, also for an unknown root), ``PathNotFound`` (404) and
``InvalidSelection`` (422); a folder or file whose file system does not answer is ``files.probe.NotResponding``
(503): its resolution and its first stat run through the probe (requested checks), then the listing or the read in
the request's thread, so a 200,000-file listing is not cut short by the probe's timeout. A listing's entries that
may lie on another mount, the symbolic links (their targets) and the children keyed apart from the folder (mount
points, ``files.probe.FileProbe.keys_apart``), are resolved and stat'ed through the probe too (``_EntryChecks``):
one that does not answer is listed as not available, and once one has waited out the probe's timeout the rest
are listed so unchecked, so a listing waits at most one timeout for them. The others are stat'ed in the
request's thread, on the folder's own mount, which just answered.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar

from laueportal.core.problems import ProblemError
from laueportal.core.settings import Settings
from laueportal.core.text import quoted, shortened
from laueportal.files.inputs import natural_key
from laueportal.files.probe import NotResponding, bounded, default_probe
from laueportal.files.roots import (
    SHOWN_PATH_LIMIT,
    PathNotAllowed,
    PathNotFound,
    RealRoots,
    is_dir_here,
    is_utf8,
    real_path,
    real_roots,
    resolve_inside_roots,
    shown,
)
from laueportal.files.schemas import DirectoryListing, EntryKind, FileEntry, FileRoot, FileRoots, ScanLogSource

T = TypeVar("T")

READ_FACTOR = 10
"""A listing sorts at most this many times ``files.listing_cap`` of a folder's files (every folder is kept)."""


class InvalidSelection(ProblemError):
    """A path inside the roots that is not what was asked for (a 422 problem)."""

    def __init__(self, detail: str) -> None:
        super().__init__(422, "Invalid selection", detail)


def _extension_set(extensions: Iterable[str] | None) -> set[str]:
    return {extension.lower() for extension in extensions or ()}


def _existing(path: Path) -> os.stat_result:
    """``path``'s status (a requested check through ``files.probe``: ``NotResponding``); a 404 when it is not
    there, a 422 when it cannot be read."""
    try:
        return bounded(path, path.stat, requested=True)
    except FileNotFoundError:
        raise PathNotFound(path) from None
    except OSError as error:
        raise InvalidSelection(f"{shown(path)} cannot be read: {error.strerror or type(error).__name__}.") from None


def existing_folder(
    path: str | os.PathLike[str], settings: Settings, *, roots: RealRoots | None = None
) -> tuple[str, Path]:
    """The root and the resolved folder of a requested ``path``: inside the roots (else a 400), on this host (else a
    404), a folder that can be read and whose path is valid UTF-8 (else a 422). ``roots``: as resolved for this
    answer (``files.roots.real_roots``)."""

    root, directory = resolve_inside_roots(path, settings, roots=roots)
    if not is_utf8(str(directory)):
        raise InvalidSelection(f"{shown(directory)} cannot be listed: its path is not valid UTF-8.")
    if not stat.S_ISDIR(_existing(directory).st_mode):
        raise InvalidSelection(f"{shown(directory)} is not a directory.")
    return root, directory


class _Unavailable(Exception):
    """An entry whose file system does not answer, or was not asked (``_EntryChecks``)."""


class _EntryChecks:
    """A listing's checks of the entries that may lie on another mount than its folder: a symbolic link (resolved,
    then its target stat'ed) and a child keyed apart (``FileProbe.keys_apart``), each through ``files.probe`` as
    requested checks. Once one has waited out the probe's timeout, the rest are not made (``_Unavailable``)."""

    def __init__(self, directory: Path, settings: Settings, roots: RealRoots) -> None:
        files = default_probe()
        self.directory = directory
        self.settings = settings
        self.roots = roots
        self.timeout = files.timeout
        self.apart = files.keys_apart(directory)
        self.waited = False

    def _checked(self, check: Callable[[], T]) -> T:
        if self.waited:
            raise _Unavailable
        started = time.monotonic()
        try:
            return check()
        except NotResponding:
            if self.timeout is not None and time.monotonic() - started >= self.timeout:
                self.waited = True
            raise _Unavailable from None

    def link(self, name: str) -> tuple[Path, os.stat_result] | None:
        """A link's target and its status; None when it leads outside the roots or nowhere (left out)."""
        try:
            _, target = self._checked(
                lambda: resolve_inside_roots(self.directory / name, self.settings, roots=self.roots)
            )
            return target, self._checked(lambda: bounded(target, target.stat, requested=True))
        except (PathNotAllowed, OSError):
            return None

    def status(self, name: str) -> os.stat_result:
        """A child's status: through the probe when it is keyed apart from the folder, else in this thread."""
        path = self.directory / name
        if self.apart(name):
            return self._checked(lambda: bounded(path, path.stat, requested=True))
        return path.stat()


def list_directory(
    path: str | os.PathLike[str],
    settings: Settings,
    *,
    extensions: Iterable[str] | None = None,
    roots: RealRoots | None = None,
) -> DirectoryListing:
    """One level of a directory inside the roots; files are limited to ``extensions`` when given. Every folder is
    listed first; of the files, only the first ``READ_FACTOR`` × ``listing_cap`` the file system gives are sorted
    (with more, the listing is truncated), and so of the symbolic links, checked first (``_EntryChecks``).
    ``roots``: as resolved for this answer (``files.roots.real_roots``)."""

    roots = real_roots(settings) if roots is None else roots
    root, directory = existing_folder(path, settings, roots=roots)
    wanted = _extension_set(extensions)
    cap = settings.files.listing_cap
    file_limit = READ_FACTOR * cap
    folders: list[str] = []  # every one
    files: list[str] = []  # the first file_limit the file system gives
    links: list[str] = []  # the first file_limit the file system gives
    invalid_names = 0
    unsorted_files = False  # files or links beyond file_limit: neither sorted nor listed
    try:
        with os.scandir(directory) as children:
            for child in children:
                if child.name.startswith("."):
                    continue
                try:
                    is_link = child.is_symlink()  # the entry's own type: its target is not looked at here
                    is_directory = not is_link and child.is_dir()
                    is_file = not is_link and not is_directory and child.is_file()
                except OSError:
                    continue
                if is_link:
                    if len(links) < file_limit:
                        links.append(child.name)
                    else:
                        unsorted_files = True
                    continue
                if not (is_directory or is_file):
                    continue
                if is_file and wanted and os.path.splitext(child.name)[1].lower() not in wanted:
                    continue
                if not is_utf8(child.name):
                    invalid_names += 1
                    continue
                if is_directory:
                    folders.append(child.name)
                elif len(files) < file_limit:
                    files.append(child.name)
                else:
                    unsorted_files = True
    except OSError as error:
        raise InvalidSelection(
            f"{shown(directory)} cannot be read: {error.strerror or type(error).__name__}."
        ) from None

    checks = _EntryChecks(directory, settings, roots)
    # (group: 0 a folder, 1 a file, 2 a link that does not answer; natural key; name; a link's target and status)
    candidates: list[tuple[int, tuple[object, ...], str, tuple[Path, os.stat_result] | None]] = []
    candidates += [(0, natural_key(name), name, None) for name in folders]
    candidates += [(1, natural_key(name), name, None) for name in files]
    for name in links:
        try:
            checked = checks.link(name)
        except _Unavailable:
            if is_utf8(name):
                candidates.append((2, natural_key(name), name, None))
            else:
                invalid_names += 1
            continue
        if checked is None:
            continue
        target, status = checked
        is_directory, is_file = stat.S_ISDIR(status.st_mode), stat.S_ISREG(status.st_mode)
        if not (is_directory or is_file):
            continue
        if is_file and wanted and os.path.splitext(name)[1].lower() not in wanted:
            continue
        if not (is_utf8(name) and is_utf8(str(target))):
            invalid_names += 1
            continue
        candidates.append((0 if is_directory else 1, natural_key(name), name, checked))
    candidates.sort(key=lambda candidate: candidate[:3])

    entries: list[FileEntry] = []
    truncated = unsorted_files
    for group, _, name, checked in candidates:
        if len(entries) >= cap:
            truncated = True
            break
        entry_path, status = checked if checked is not None else (directory / name, None)
        kind = None if group == 2 else EntryKind.FILE if group == 1 else EntryKind.DIRECTORY
        if status is None and kind is not None:
            try:
                status = checks.status(name)
            except _Unavailable:
                pass
            except OSError:
                continue
        if status is None:
            entries.append(
                FileEntry(name=name, path=str(entry_path), kind=kind, size=None, modified=None, available=False)
            )
            continue
        entries.append(
            FileEntry(
                name=name,
                path=str(entry_path),
                kind=kind,
                size=status.st_size if kind == EntryKind.FILE else None,
                modified=datetime.fromtimestamp(status.st_mtime, UTC),
            )
        )
    return DirectoryListing(
        root=root,
        root_path=str(roots.paths[root]),
        path=str(directory),
        entries=entries,
        truncated=truncated,
        invalid_names=invalid_names,
    )


def _named_root(name: str, settings: Settings, roots: RealRoots) -> Path:
    """The resolved folder of the root called ``name``, or ``PathNotAllowed``: an unknown root is outside them all
    (``NotResponding`` for one that never answered)."""

    configured = settings.files.roots
    if name not in configured:
        known = f"the allowed roots are {', '.join(configured)}" if configured else "no file roots are configured"
        raise PathNotAllowed(f"There is no root named {quoted(name)} ({known}).")
    if name not in roots.paths:
        raise default_probe().not_responding(configured[name])
    return roots.paths[name]


def _inside_named_root(
    path: str, root: str | None, root_folder: Path | None, settings: Settings, roots: RealRoots
) -> None:
    """``PathNotAllowed`` unless ``path`` resolves inside the roots and, when a root is named, inside that one."""

    _, resolved = resolve_inside_roots(path, settings, roots=roots)
    if root_folder is not None and not resolved.is_relative_to(root_folder):
        raise PathNotAllowed(
            f"{shortened(path.strip(), SHOWN_PATH_LIMIT)} is not inside the root {root!r} ({shown(root_folder)})."
        )


def browse(
    settings: Settings,
    *,
    root: str | None = None,
    path: str | None = None,
    extensions: Iterable[str] | None = None,
) -> DirectoryListing:
    """One level of a folder for the path picker: the folder of the root named ``root``, the folder ``path``, or
    ``path`` checked to be inside ``root``. The listing names the root asked for (with nested roots, a path may
    lie in several); without one, the innermost root that holds the path.

    Errors: an unknown root or a path outside the roots (or outside ``root``) is a 400, a missing folder a 404,
    a file instead of a folder a 422, and so is a request with neither a root nor a path.
    """

    roots = real_roots(settings)
    root_folder = None if root is None else _named_root(root, settings, roots)
    if path is not None:
        _inside_named_root(path, root, root_folder, settings, roots)
        listing = list_directory(path, settings, extensions=extensions, roots=roots)
    elif root_folder is not None:
        listing = list_directory(root_folder, settings, extensions=extensions, roots=roots)
    else:
        message = "Give a root (GET /api/v1/files/roots), a path, or both."
        raise ProblemError(
            422,
            "Validation failed",
            message,
            errors=[{"loc": ["query", "root"], "message": message, "code": "missing"}],
        )
    if root_folder is not None and listing.root != root:
        listing = listing.model_copy(update={"root": root, "root_path": str(root_folder)})
    return listing


def list_roots(settings: Settings) -> FileRoots:
    """The allowed roots in the settings' order, resolved, each with whether its folder is on this host (one whose
    file system does not answer is not: its last real path, or its own text if it never answered)."""

    real = real_roots(settings)
    roots = []
    for name, text in settings.files.roots.items():
        folder = real.paths.get(name)
        if folder is None or name in real.silent:
            path = folder if folder is not None else Path(os.path.normpath(text))
            roots.append(FileRoot(name=name, path=shown(path), available=False))
            continue
        # A root whose folder's path is not UTF-8 cannot be browsed through the API.
        sendable = is_utf8(str(folder))
        roots.append(FileRoot(name=name, path=shown(folder), available=sendable and _folder_here(folder)))
    files = settings.files
    return FileRoots(
        roots=roots,
        scan_logs=ScanLogSource(
            max_bytes=files.max_scan_log_mb * 1024 * 1024,
            extensions=list(files.scan_log_extensions),
            start_folder=_scan_log_start(settings, roots, real),
        ),
    )


def _folder_here(folder: Path) -> bool:
    """Whether a root's folder is on this host (``is_dir_here``, through ``files.probe``: one that does not answer
    is not)."""
    try:
        return bounded(folder, lambda: is_dir_here(folder), requested=True)
    except NotResponding:
        return False


def _scan_log_start(settings: Settings, roots: list[FileRoot], real: RealRoots) -> str | None:
    """Where the scan-log picker opens: ``files.scan_log_dir`` resolved when it is inside the roots (and can be
    sent), else (also when its file system does not answer) the first root's folder (the setting's documented
    default), else None. A configured folder: resolved (``real_path``) wherever its text lies."""

    folder = settings.files.scan_log_dir
    if folder is not None:
        try:
            resolved = real_path(folder, requested=True)
        except (PathNotAllowed, NotResponding):
            pass
        else:
            if any(resolved.is_relative_to(root) for root in real.paths.values()) and is_utf8(str(resolved)):
                return shown(resolved)
    return roots[0].path if roots else None


def validate_selection(
    path: str | os.PathLike[str],
    settings: Settings,
    *,
    mode: Literal["file", "directory"] = "file",
    extensions: Iterable[str] | None = None,
    max_file_size_mb: float | None = None,
    roots: RealRoots | None = None,
) -> Path:
    """The resolved path of a file or directory the client selected, checked against the roots (``roots``: as
    resolved for this answer)."""

    _, selected = resolve_inside_roots(path, settings, roots=roots)
    status = _existing(selected)
    if mode == "directory":
        if not stat.S_ISDIR(status.st_mode):
            raise InvalidSelection(f"{shown(selected)} is not a directory.")
        return selected
    if mode != "file":
        raise ValueError(f"unsupported selection mode {mode!r}")
    if not stat.S_ISREG(status.st_mode):
        raise InvalidSelection(f"{shown(selected)} is not a regular file.")

    wanted = _extension_set(extensions)
    if wanted and selected.suffix.lower() not in wanted:
        raise InvalidSelection(
            f"{shown(selected.name)}: select a file with one of these extensions: {', '.join(sorted(wanted))}."
        )
    if max_file_size_mb is not None and status.st_size > max_file_size_mb * 1024 * 1024:
        raise InvalidSelection(f"{shown(selected.name)} exceeds the {max_file_size_mb} MB limit.")
    return selected


def read_selected_file(
    path: str,
    settings: Settings,
    *,
    root: str | None = None,
    extensions: Iterable[str] | None = None,
    max_file_size_mb: float | None = None,
) -> bytes:
    """Check a selected file again (roots, the named ``root`` when given, type, extension, size) immediately before
    reading it. The read stops past the size limit too, so a file that grew after the check is refused all the
    same (a 422), never read whole."""

    roots = real_roots(settings)
    if root is not None:
        _inside_named_root(path, root, _named_root(root, settings, roots), settings, roots)
    selected = validate_selection(
        path, settings, mode="file", extensions=extensions, max_file_size_mb=max_file_size_mb, roots=roots
    )
    limit = None if max_file_size_mb is None else int(max_file_size_mb * 1024 * 1024)
    try:
        with selected.open("rb") as stream:
            data = stream.read() if limit is None else stream.read(limit + 1)
    except OSError as error:
        raise InvalidSelection(
            f"{shown(selected)} could not be read: {error.strerror or type(error).__name__}."
        ) from None
    if limit is not None and len(data) > limit:
        raise InvalidSelection(f"{shown(selected.name)} exceeds the {max_file_size_mb} MB limit.")
    return data
