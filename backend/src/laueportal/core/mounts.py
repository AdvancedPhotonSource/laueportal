"""The mount table, ``/proc/self/mountinfo`` (standard library only; reading it never blocks on a mount).

``files.probe`` keys its file checks by mount point; ``core.db`` finds the file system of the database
(``database_filesystem``) to refuse WAL on a network file system.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

MOUNTINFO = "/proc/self/mountinfo"


@dataclass(frozen=True)
class Mount:
    """One line of the mount table: where it is mounted, and its file-system type (``ext4``, ``nfs4``, ``autofs``)."""

    point: str
    fstype: str


def unescaped(field: str) -> str:
    """A mountinfo field: space, tab, newline and backslash are octal escapes (``\\040``)."""
    if "\\" not in field:
        return field
    out, index = [], 0
    while index < len(field):
        if field[index] == "\\" and field[index + 1 : index + 4].isdigit():
            out.append(chr(int(field[index + 1 : index + 4], 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def read_mountinfo(path: str | os.PathLike[str] = MOUNTINFO) -> tuple[Mount, ...]:
    """Every mount of ``path`` (``/proc/self/mountinfo``), in its order: a point mounted twice, its top mount last.
    Raises ``OSError`` when it cannot be read (no ``/proc``)."""
    mounts = []
    with open(path, encoding="utf-8", errors="surrogateescape") as file:
        for line in file:
            # ID, parent, device, root, mount point, options, optional fields…, "-", type, source, options
            fields = line.split()
            if len(fields) < 8 or "-" not in fields[6:]:
                continue
            separator = fields.index("-", 6)
            fstype = fields[separator + 1] if separator + 1 < len(fields) else ""
            mounts.append(Mount(unescaped(fields[4]), fstype))
    return tuple(mounts)


def mount_holding(path: str, mounts: Iterable[Mount]) -> Mount | None:
    """The mount that holds the absolute, normalized ``path``, by its text (no link is followed): the longest mount
    point that is ``path`` or one of its folders, the top one of a point mounted twice. None when none holds it."""
    found: Mount | None = None
    for mount in mounts:
        point = mount.point.rstrip(os.sep) or os.sep
        inside = path == point or path.startswith(point if point == os.sep else point + os.sep)
        if inside and (found is None or len(point) >= len(found.point.rstrip(os.sep) or os.sep)):
            found = mount
    return found
