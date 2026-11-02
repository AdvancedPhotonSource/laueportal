"""Allowed file roots: every path that comes from a request goes through ``resolve_inside_roots``.

``settings.files.roots`` names the allowed roots. A path from a request is accepted only if it is absolute, has no
``..`` component, is at most ``MAX_PATH_BYTES`` long with no name longer than ``MAX_NAME_BYTES``, lies by its text
inside a root (inside the root's text or its real path), and, once every symlink is resolved, lies inside one of
the roots' real paths. ``~`` is never expanded (the server's home is not
the user's), relative paths are refused (they would be relative to the server's working directory), and with no
roots configured nothing is accepted.

The check is made when the path is resolved: a symlink changed afterwards can still redirect a
later open. Callers resolve again right before they read (``files.browser.read_selected_file`` does).

Mounts that may hang (``/net``, ``files.probe``): a path outside every root by its text is refused before any
stat of it. The path is then resolved one component at a time (``real_path``, what ``os.path.realpath`` does):
each check stats the components on one mount and is keyed by that mount, and a symbolic link's target is followed
under its own keys (at most ``MAX_LINKS`` links), so a hang marks the mount where it blocks, never another mount
the path's text names; the walk stops once the path it has walked passes ``MAX_PATH_BYTES`` (nested links can
lengthen a short path's resolution far beyond what the kernel would open). A request's checks are ``requested``
(the probe's budget for them, below its threads). The
roots are resolved the same way on every resolution (a root link pointed elsewhere counts at once): a root that
does not answer is its last real path while it does not answer, and holds nothing if it never answered; when no
root holds the path and a root did not answer, the answer is ``NotResponding`` (a 503), never inside. So a
resolution waits at most one probe timeout for the path, and one for each root whose mount does not answer until
the probe remembers that mount.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from laueportal.core.problems import ProblemError
from laueportal.core.settings import Settings
from laueportal.core.text import quoted, shortened
from laueportal.files.probe import NotResponding, bounded, default_probe

if TYPE_CHECKING:
    from laueportal.files.probe import FileProbe

SHOWN_PATH_LIMIT = 300
"""The longest path from a request that a message repeats, in characters."""
MAX_LINKS = 40
"""The most symbolic links a resolution follows (Linux's own limit): a path that needs more is refused."""
MAX_PATH_BYTES = 4096
"""The longest path a request or a run's output may give, in bytes (Linux's ``PATH_MAX``): a longer one is refused
before any stat, so a resolution's work stays bounded."""
MAX_NAME_BYTES = 255
"""The longest name such a path may hold, in bytes (``NAME_MAX``)."""


class PathNotAllowed(ProblemError):
    """A path from a request is malformed or outside every allowed root (a 400 problem)."""

    def __init__(self, detail: str) -> None:
        super().__init__(400, "Path not allowed", detail)


class PathNotFound(ProblemError):
    """An allowed path that does not exist, e.g. ``/net/...`` on a machine without the mount (a 404 problem)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(404, "Path not found", f"{shown(path)} does not exist or is not available on this host.")


def is_utf8(text: str) -> bool:
    """Whether a name or path from the file system is valid UTF-8, so the API can send it.

    ``os`` keeps the bytes of a name that is not UTF-8 as lone surrogates (surrogate escapes, ``\\udcfc`` for
    Latin-1's ``ü``). UTF-8 cannot encode a lone surrogate, so the API's UTF-8 JSON cannot send such a name. A
    request can still spell one, with a JSON escape (``"\\udcfc"``), but ``core.api.check_request_text`` refuses
    any lone surrogate in a request (a 422), and ``resolve_inside_roots`` refuses a path it cannot encode."""

    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def shown(path: str | os.PathLike[str]) -> str:
    """A path for a message: bytes that are not UTF-8 show as U+FFFD, so the problem can still be sent."""

    return os.fspath(path).encode("utf-8", "surrogateescape").decode("utf-8", "replace")


class _TooManyLinks(Exception):
    """A resolution would follow more than ``MAX_LINKS`` symbolic links."""


class _TooLong(Exception):
    """A resolution's walked path would pass ``MAX_PATH_BYTES``."""


def _past_path_limit(text: str) -> bool:
    """Whether ``text`` (a walked path, encodable) is longer than ``MAX_PATH_BYTES`` bytes. A character is at most
    four bytes, so a path of at most a quarter of that many characters is not encoded to tell."""
    return len(text) * 4 > MAX_PATH_BYTES and len(os.fsencode(text)) > MAX_PATH_BYTES


def _too_long(text: str) -> str | None:
    """Why ``text`` (encodable) is refused for its length (``MAX_PATH_BYTES``, ``MAX_NAME_BYTES``), or None."""
    encoded = os.fsencode(text)
    if len(encoded) > MAX_PATH_BYTES:
        return f"is longer than {MAX_PATH_BYTES:,} bytes"
    if any(len(name) > MAX_NAME_BYTES for name in encoded.split(os.fsencode(os.sep))):
        return f"has a name longer than {MAX_NAME_BYTES} bytes"
    return None


class _Walk:
    """``os.path.realpath`` (not strict) of an absolute path, one component at a time (Python 3.13's walk): the steps
    that need no file system access in the caller's thread (``pending``), the stats and link reads in checks
    (``advance``). A loop of links ends at the link that closes it, as ``realpath`` leaves it."""

    def __init__(self, text: str) -> None:
        self.rest: list[str | None] = [*text.split(os.sep)[::-1]]
        """What is left to resolve, last first; a None above a link's path once its target was pushed."""
        self.parts = len(self.rest)
        """The names left in ``rest``."""
        self.path = os.sep
        """Resolved so far: no link, no ``..``, so its text is where it lies."""
        self.seen: dict[str, str | None] = {}
        """A link's resolved target by the link's path (None while it is being resolved: a loop)."""
        self.links = 0
        self.looped = False
        """A link led back to itself (``realpath`` leaves the link as it is there)."""
        self.abandoned = False
        """Its caller stopped waiting: a check stuck in it stops at its next step."""

    def pending(self) -> str | None:
        """The next path to stat, after the steps that need none (``.``, ``..``, a link's end); None when done.
        ``_TooLong`` when that path is longer than ``MAX_PATH_BYTES`` (no stat could open it: ``ENAMETOOLONG``)."""
        while self.parts:
            name = self.rest[-1]
            if name is None:  # a link's target is resolved
                self.rest.pop()
                self.seen[cast(str, self.rest.pop())] = self.path
                continue
            if name and name != os.curdir and name != os.pardir:
                newpath = self.path + name if self.path == os.sep else self.path + os.sep + name
                if _past_path_limit(newpath):
                    raise _TooLong(newpath)
                return newpath
            self.rest.pop()
            self.parts -= 1
            if name == os.pardir:
                self.path = self.path[: self.path.rindex(os.sep)] or os.sep
        return None

    def _step(self, newpath: str) -> None:
        self.rest.pop()
        self.parts -= 1
        try:
            if not stat.S_ISLNK(os.lstat(newpath).st_mode):
                self.path = newpath
                return
            if newpath in self.seen:
                known = self.seen[newpath]
                self.path = newpath if known is None else known  # None: a loop
                self.looped = self.looped or known is None
                return
            self.links += 1
            if self.links > MAX_LINKS:
                raise _TooManyLinks(newpath)
            target = os.readlink(newpath)
        except OSError:  # as realpath: missing, unreadable, not a folder… the rest is taken as written
            self.path = newpath
            return
        if target.startswith(os.sep):
            self.path = os.sep
        self.seen[newpath] = None
        self.rest += [newpath, None]
        names = target.split(os.sep)[::-1]
        self.rest += names
        self.parts += len(names)

    def advance(self, files: FileProbe, key: str) -> None:
        """In a check keyed ``key``: stat the pending component, then the next ones while they lie on ``key`` or on
        the root file system (``FileProbe.place``)."""
        newpath = self.pending()
        while newpath is not None and not self.abandoned:
            self._step(newpath)
            newpath = self.pending()
            if newpath is not None:
                next_key, on_root = files.place(newpath)
                if next_key != key and not on_root:
                    return


def real_path(path: str | os.PathLike[str], *, requested: bool = True) -> Path:
    """``os.path.realpath(path)`` through ``files.probe``: each check stats the components that lie on one mount
    and is keyed by it (``FileProbe.mount`` of the component, whose folder is already resolved), with those on the
    root file system on its way, so a hang marks the mount where it blocks; ``requested``: checks of a path a
    request gave (the probe's budget). Raises
    ``NotResponding``, and ``PathNotAllowed`` for a path that needs more than ``MAX_LINKS`` links or whose walk
    passes ``MAX_PATH_BYTES`` (its links lead to a longer path: the walk stops there)."""

    return Path(_walked(os.fspath(path), requested).path)


def _walked(path: str, requested: bool) -> _Walk:
    """``real_path``'s walk, done."""
    text = _absolute(path)
    files = default_probe()
    walk = _Walk(text)
    try:
        newpath = walk.pending()
        while newpath is not None:
            key = files.place(newpath)[0]
            files.run(newpath, lambda key=key: walk.advance(files, key), requested=requested)
            newpath = walk.pending()
    except _TooManyLinks:
        raise PathNotAllowed(
            f"{shortened(text, SHOWN_PATH_LIMIT)} leads through more than {MAX_LINKS} symbolic links."
        ) from None
    except _TooLong:
        raise PathNotAllowed(
            f"{shortened(text, SHOWN_PATH_LIMIT)} resolves to a path longer than {MAX_PATH_BYTES:,} bytes."
        ) from None
    finally:
        walk.abandoned = True
    return walk


def recorded_real_path(path: str | os.PathLike[str]) -> Path:
    """``Path(path).resolve()`` of a path a run recorded, through ``files.probe`` (``real_path``, its checks not
    requested): ``remote.paths.portal_path``'s ``resolve`` on request. A path over ``MAX_PATH_BYTES`` or with a
    name over ``MAX_NAME_BYTES`` is an ``OSError`` (``ENAMETOOLONG``) before any stat; a loop of links (as
    ``Path.resolve`` reports one: the stat of the result fails with ``ELOOP``), a path that needs more than
    ``MAX_LINKS`` links and one whose walk passes ``MAX_PATH_BYTES`` are a ``RuntimeError`` (unavailable, for
    ``remote.paths.portal_path``); ``NotResponding`` when its file system does not answer."""

    text = os.fspath(path)
    reason = _too_long(text)
    if reason is not None:
        raise OSError(errno.ENAMETOOLONG, f"The path {reason}", text)
    try:
        walk = _walked(text, False)
    except PathNotAllowed as refused:
        raise RuntimeError(refused.problem.detail) from None
    resolved = Path(walk.path)
    if walk.looped and bounded(resolved, lambda: _loops(resolved)):
        raise RuntimeError(f"Symlink loop from {walk.path!r}")
    return resolved


def _loops(path: Path) -> bool:
    """Whether a stat of ``path`` fails for a loop of links (``ELOOP``)."""
    try:
        path.stat()
    except OSError as error:
        return error.errno == errno.ELOOP
    return False


_kept_roots: dict[str, Path] = {}
"""Each root's last real path, by its text (``real_roots``)."""
_kept_roots_lock = threading.Lock()


def _root(root: str | os.PathLike[str], requested: bool) -> tuple[Path | None, bool]:
    """A root's real path (``real_path``) and True; while it does not answer, its last real path (None if it never
    answered) and False."""

    text = os.fspath(root)
    try:
        real = real_path(text, requested=requested)
    except NotResponding:
        with _kept_roots_lock:
            return _kept_roots.get(text), False
    with _kept_roots_lock:
        _kept_roots[text] = real
    return real, True


@dataclass(frozen=True)
class RealRoots:
    """The roots' real paths for one answer (``real_roots``)."""

    paths: dict[str, Path]
    """By name: each root's real path, or while it does not answer its last one (none if it never answered)."""
    silent: tuple[str, ...]
    """The names of the roots that did not answer."""


def real_roots(settings: Settings, *, requested: bool = True) -> RealRoots:
    """Every root's real path, resolved now through ``files.probe`` (``real_path``; ``requested`` as there)."""

    paths: dict[str, Path] = {}
    silent: list[str] = []
    for name, root in settings.files.roots.items():
        real, answered = _root(root, requested)
        if real is not None:
            paths[name] = real
        if not answered:
            silent.append(name)
    return RealRoots(paths, tuple(silent))


def real_root(root: str | os.PathLike[str], *, requested: bool = True) -> Path:
    """A root's real path, resolved now (``real_roots``); while it does not answer, its last one, and
    ``NotResponding`` if it never answered."""

    real, answered = _root(root, requested)
    if real is None:
        raise default_probe().not_responding(root)
    return real


def _absolute(text: str) -> str:
    """``text`` made absolute as ``realpath`` does (the working directory is a real path)."""
    return text if text.startswith(os.sep) else os.path.join(os.getcwd(), text)


def _lexical(text: str) -> str:
    """``text`` (absolute) normalized by its text alone: no ``.``, empty or (by text) ``..`` component."""
    return os.sep + os.path.normpath(text).lstrip(os.sep)


def _holds(folder: str | Path, text: str) -> bool:
    base = os.fspath(folder)
    return text == base or text.startswith(base.rstrip(os.sep) + os.sep)


def _outside(text: str, settings: Settings, roots: RealRoots) -> ProblemError:
    """The answer for a path no root holds: ``NotResponding`` when a root did not answer (it may hold it), else
    ``PathNotAllowed``."""
    if roots.silent:
        return default_probe().not_responding(settings.files.roots[roots.silent[0]])
    return PathNotAllowed(
        f"{shortened(text, SHOWN_PATH_LIMIT)} is outside the allowed roots ({', '.join(sorted(settings.files.roots))})."
    )


def resolve_inside_roots(
    path: str | os.PathLike[str], settings: Settings, *, recorded: bool = False, roots: RealRoots | None = None
) -> tuple[str, Path]:
    """``(root name, resolved path)`` for a path from a request, or ``PathNotAllowed``.

    A path with a ``..`` component, longer than ``MAX_PATH_BYTES``, with a name longer than ``MAX_NAME_BYTES``, or
    outside every root by its text (the roots' texts and real paths), is refused before any stat of it. Symlinks
    (in the path and in the roots) are then resolved before the containment check, so a link inside a root that
    points outside every root is refused, and a link to another root is reported under that root. When roots nest, the innermost one names the path. The path need not exist.
    ``NotResponding`` (a 503) when the path's file system does not answer, or no root holds it and a root did not
    answer (the module's docstring).

    ``recorded``: a path named inside a run's outputs (mapped: the geometry, the
    point sources and images, the detector candidates), not one a request gave: still confined to the roots and to
    the length limits, but its checks are not ``requested``, and a ``..`` in it is resolved on the file system, as
    the run's reader would, not refused. The job's own files are not read through here (``jobs.summary``).
    ``roots``: the roots resolved already for this answer (``real_roots``), for a caller that checks many paths.
    """

    if not settings.files.roots:
        raise PathNotAllowed("No file roots are configured (files.roots in the settings), so no path is allowed.")
    text = os.fspath(path)
    if not isinstance(text, str):
        raise PathNotAllowed("The path must be text.")
    text = text.strip()
    if not text:
        raise PathNotAllowed("Enter a path.")
    if "\x00" in text:
        raise PathNotAllowed("The path contains a NUL character.")
    try:
        os.fsencode(text)
    except UnicodeEncodeError:  # a lone surrogate that is not a surrogate escape (e.g. from a JSON "\ud800")
        raise PathNotAllowed(f"{quoted(text)} cannot be a file name: it holds a lone surrogate.") from None
    reason = _too_long(text)
    if reason is not None:
        raise PathNotAllowed(f"{quoted(text, SHOWN_PATH_LIMIT)} {reason}.")
    if text.startswith("~"):
        raise PathNotAllowed(
            f"{quoted(text, SHOWN_PATH_LIMIT)}: '~' is not expanded; enter an absolute path inside an allowed root."
        )
    if not os.path.isabs(text):
        raise PathNotAllowed(f"{quoted(text, SHOWN_PATH_LIMIT)} is not an absolute path.")
    if not recorded and os.pardir in text.split(os.sep):
        raise PathNotAllowed(f"{quoted(text, SHOWN_PATH_LIMIT)} has a '..' component: enter the path without it.")

    requested = not recorded
    if roots is None:
        roots = real_roots(settings, requested=requested)
    lexical = _lexical(text)
    folders = [_lexical(_absolute(os.fspath(root))) for root in settings.files.roots.values()]
    if not any(_holds(folder, lexical) for folder in (*folders, *roots.paths.values())):
        raise _outside(text, settings, roots)  # never stat a path outside every root
    resolved = real_path(text, requested=requested)
    match: tuple[str, Path] | None = None
    for name, real in roots.paths.items():
        if resolved.is_relative_to(real) and (match is None or len(real.parts) > len(match[1].parts)):
            match = (name, real)
    if match is None:
        raise _outside(text, settings, roots)
    return match[0], resolved


def is_file_here(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` is a file this host can reach: one under a folder the server may not open is not (and
    ``Path.is_file`` raises ``PermissionError`` there, since Python 3.12)."""

    try:
        return Path(path).is_file()
    except OSError:
        return False


def is_dir_here(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` is a folder this host can reach (``is_file_here``)."""

    try:
        return Path(path).is_dir()
    except OSError:
        return False


def is_inside_roots(path: str | os.PathLike[str], settings: Settings) -> bool:
    """Whether ``resolve_inside_roots`` accepts ``path`` (``NotResponding`` when it cannot tell)."""

    try:
        resolve_inside_roots(path, settings)
    except PathNotAllowed:
        return False
    return True
