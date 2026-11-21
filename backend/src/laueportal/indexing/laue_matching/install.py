"""Install the portal's forward caches and orientation database into a remote target's LaueMatching data folder.

A remote LaueMatching run reads an installed cache and never builds one (``remote.plan``), so a cache built by a run on
the portal's worker is copied once into the target's ``data_root`` (``laueportal laue-matching install-cache``). The
copy is published only whole and checked: it is written under a hidden name beside the destination
(``.<name>.partial-<id>``), its sha256 compared with the source's, then renamed into place; a lock file
(``.<name>.install.lock``) keeps two installs of one name apart. An installed cache or database is never replaced: one
already there is accepted only when its bytes are the source's.

Standard library only.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from laueportal.indexing.laue_matching.cache import (
    DATABASE_NAME,
    FORWARD_KIND,
    FORWARD_NAME,
    HKL_NAME,
    RECORD_NAME,
    forward_bytes,
    read_record,
)

CHUNK = 1 << 22
CACHE_FILES = (FORWARD_NAME, HKL_NAME, RECORD_NAME)


class InstallError(RuntimeError):
    """The install was refused or failed; nothing was published."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(source: Path, destination: Path) -> str:
    """Copy ``source`` to the new file ``destination``, synced; the sha256 of the bytes written."""
    digest = hashlib.sha256()
    with open(source, "rb") as reader, open(destination, "xb") as writer:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest()


def complete_problem(folder: Path) -> str | None:
    """Why the cache in ``folder`` is not a complete one to install, or ``None``: its three files, a forward-cache
    record, and a forward file of the size the record gives."""
    for name in CACHE_FILES:
        if not (folder / name).is_file():
            return f"{name} is missing"
    try:
        record = read_record(folder)
    except (OSError, ValueError) as error:
        return f"its record cannot be read ({error})"
    if record.get("kind") != FORWARD_KIND:
        return f"its record is of kind {record.get('kind')!r}"
    artifact = record.get("artifact")
    size = artifact.get("size") if isinstance(artifact, dict) else None
    actual = (folder / FORWARD_NAME).stat().st_size
    if size != actual:
        return f"{FORWARD_NAME} is {actual} bytes, and its record says {size}"
    config = record.get("config")
    try:
        needed = forward_bytes(config["n_orientations"], config["MaxNrLaueSpots"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return "its record names no orientation or spot count"
    if actual != needed:
        return f"{FORWARD_NAME} is {actual} bytes, and its record's values need {needed}"
    return None


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> _Lock:
        try:
            with open(self.path, "x", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
        except FileExistsError:
            raise InstallError(
                f"{self.path} exists: another install is running, or one was interrupted (remove the file once no "
                "install runs)"
            ) from None
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_database(source_root: Path, target_root: Path, *, log: Callable[[str], None] = print) -> str:
    """Install the orientation database; its sha256. Accepted as it is when the target holds the same bytes."""
    source = source_root / DATABASE_NAME
    destination = target_root / DATABASE_NAME
    if not source.is_file():
        raise InstallError(f"{source} does not exist")
    target_root.mkdir(parents=True, exist_ok=True)
    with _Lock(target_root / f".{DATABASE_NAME}.install.lock"):
        if destination.exists():
            if destination.stat().st_size != source.stat().st_size or _sha256(destination) != _sha256(source):
                raise InstallError(f"{destination} holds another orientation database; it is never replaced")
            log(f"{destination} is already installed")
            return _sha256(destination)
        partial = target_root / f".{DATABASE_NAME}.partial-{uuid.uuid4().hex}"
        try:
            written = _copy(source, partial)
            if written != _sha256(source) or _sha256(partial) != written:
                raise InstallError(f"the copy of {source} does not read back as its bytes")
            os.rename(partial, destination)
            _sync_directory(target_root)
        finally:
            partial.unlink(missing_ok=True)
    log(f"Installed {destination} (sha256 {written})")
    return written


def install_cache(source_root: Path, target_root: Path, name: str, *, log: Callable[[str], None] = print) -> str:
    """Install the forward cache ``name``; its forward file's sha256. Accepted as it is when the target holds the
    same three files."""
    source = source_root / name
    destination = target_root / name
    problem = complete_problem(source)
    if problem is not None:
        raise InstallError(f"Forward cache {name} cannot be installed: {problem}")
    target_root.mkdir(parents=True, exist_ok=True)
    with _Lock(target_root / f".{name}.install.lock"):
        if destination.exists():
            same = all(
                (destination / file).is_file() and _sha256(destination / file) == _sha256(source / file)
                for file in CACHE_FILES
            )
            if not same:
                raise InstallError(f"{destination} holds another cache named {name}; it is never replaced")
            log(f"Forward cache {name} is already installed in {target_root}")
            return _sha256(destination / FORWARD_NAME)
        partial = target_root / f".{name}.partial-{uuid.uuid4().hex}"
        partial.mkdir()
        try:
            digests = {}
            for file in CACHE_FILES:
                digests[file] = _copy(source / file, partial / file)
                if digests[file] != _sha256(source / file) or _sha256(partial / file) != digests[file]:
                    raise InstallError(f"the copy of {source / file} does not read back as its bytes")
            _sync_directory(partial)
            os.rename(partial, destination)
            _sync_directory(target_root)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
    log(f"Installed forward cache {name} in {target_root} (forward.bin sha256 {digests[FORWARD_NAME]})")
    return digests[FORWARD_NAME]
