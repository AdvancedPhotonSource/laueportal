"""Whether lauelab loads a run's calibration files: the geometry (and, for indexing, the crystal), a live create check
(lauelab loads them before anything is created). The Live page's validation (``jobs.live_session``) asks, once the file
exists (``jobs.validation.check_file``) and is no larger than ``MAX_CALIBRATION_BYTES`` (``size_problem``: lauelab
parses the whole file in the api's process, so a large file picked by mistake must never reach it); a batch run's
validation does not. The bound holds on the read too: a file is opened once, its descriptor stat'ed, and at most
``MAX_CALIBRATION_BYTES`` + 1 bytes read, so a stat that understated its size or a file that grew after it is refused
unread as well; lauelab's loaders take a path, so they load a private copy of the bytes read (``_private_copy``), and a
message names the user's file, never the copy. Only ``science`` imports lauelab (import-linter rule 2).
"""

from __future__ import annotations

import math
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from lauelab.indexing import Geometry, load_crystal

from laueportal.core.text import quoted

MAX_CALIBRATION_BYTES = 1024 * 1024
"""The largest geometry or crystal file the Live page's validation loads (1 MiB). The real ones are a few KB (the
samples' geometries 2.3 KB, their crystals 0.5 KB); an Output XML shares the extension and the folders (a large
run's is 344 MB, beside its ``geometry.xml``), and lauelab's parse of a 40 MiB XML took 9-10 s and 409 MiB."""

WIRE_DETECTOR = 0
"""The detector a wire reconstruction reads (``reconstruction.wire.compute``: ``Reconstructor(geometry, 0)``)."""


@dataclass(frozen=True)
class CalibrationProblem:
    """Why a calibration file is not used: ``too_large`` (over ``MAX_CALIBRATION_BYTES``, never parsed) or
    ``does_not_load`` (lauelab refused it), and the message, which names the user's file."""

    code: str
    message: str


class _TooLarge(Exception):
    """The file holds more than ``MAX_CALIBRATION_BYTES``: its size by its descriptor's stat, None when that stat
    said less (only the read found it larger)."""

    def __init__(self, size: int | None) -> None:
        super().__init__(size)
        self.size = size


def _reason(error: BaseException, path: str, shown: str) -> str:
    """lauelab's message, the path it names (``path``) shown as the user's file name (``shown``), shortened
    (``core.text.quoted``)."""
    text = str(error) or type(error).__name__
    return text.replace(path, quoted(os.path.basename(shown)))


def size_problem(size: int | None) -> str | None:
    """Why a calibration file of ``size`` bytes is not loaded (larger than ``MAX_CALIBRATION_BYTES``), or ``None``;
    its size in MiB rounded up, so a file just over the bound never reads as the bound. ``None``: larger than the
    bound by its read, its size unknown."""
    bound = f"a calibration file is at most {MAX_CALIBRATION_BYTES // 1024 // 1024} MiB"
    if size is None:
        return f"Is over {MAX_CALIBRATION_BYTES // 1024 // 1024} MiB: {bound}"
    if size <= MAX_CALIBRATION_BYTES:
        return None
    mib = math.ceil(size * 10 / 1024 / 1024) / 10
    return f"Is {mib:.1f} MiB: {bound}"


def _private_copy(path: str, directory: str) -> str:
    """At most ``MAX_CALIBRATION_BYTES`` of ``path``, read through one descriptor (fstat'ed: a regular file, its size
    within the bound) and written to a file of the same name in the private ``directory``: its path. ``_TooLarge``
    when the descriptor's stat or the read (at most the bound + 1 byte) finds more; ``OSError`` as the open or the
    read raises it (not a regular file: ``IsADirectoryError``-like, an ``OSError`` too)."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    with open(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise OSError(f"{quoted(os.path.basename(path))} is not a regular file")
        if status.st_size > MAX_CALIBRATION_BYTES:
            raise _TooLarge(status.st_size)
        data = handle.read(MAX_CALIBRATION_BYTES + 1)
    if len(data) > MAX_CALIBRATION_BYTES:
        raise _TooLarge(None)
    copy = os.path.join(directory, os.path.basename(path))
    with open(os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600), "wb") as handle:
        handle.write(data)
    return copy


def _load(path: str, what: str, load: Callable[[str], str | None]) -> CalibrationProblem | None:
    """``load`` (lauelab's, on a path; its own refusal as a message, or None) on a private copy of ``path``'s bytes,
    read within the bound (``_private_copy``): the problem, or None."""
    with tempfile.TemporaryDirectory(prefix="laueportal-calibration-") as directory:  # 0700, removed after
        try:
            copy = _private_copy(path, directory)
        except _TooLarge as large:
            return CalibrationProblem("too_large", size_problem(large.size) or "")
        except OSError as error:
            return CalibrationProblem("does_not_load", f"Does not load as a {what} file: {_reason(error, path, path)}")
        try:
            refusal = load(copy)
        except Exception as error:  # a file the user names: any failure to read it is its refusal, never a 500
            return CalibrationProblem("does_not_load", f"Does not load as a {what} file: {_reason(error, copy, path)}")
    return None if refusal is None else CalibrationProblem("does_not_load", refusal)


def geometry_problem(path: str, *, wire: bool) -> CalibrationProblem | None:
    """Why lauelab cannot use ``path`` as a run's geometry, or ``None``: it must load (``Geometry``), and for a wire
    reconstruction have a complete wire section and an active detector 0, as lauelab's reconstructor requires."""

    def load(copy: str) -> str | None:
        geometry = Geometry(copy)
        if wire:
            if geometry.wire is None:
                return "Has no complete wire section"
            geometry.detector(WIRE_DETECTOR)
        return None

    return _load(path, "geometry", load)


def crystal_problem(path: str) -> CalibrationProblem | None:
    """Why lauelab cannot load ``path`` as an indexing run's crystal (``load_crystal``), or ``None``."""

    def load(copy: str) -> str | None:
        load_crystal(copy)
        return None

    return _load(path, "crystal", load)
