"""The API process's allocator: freed image buffers go back to the system.

glibc's malloc raises its mmap threshold each time a large block is freed (up to 32 MiB on 64-bit), so the API's
8-16 MiB frame buffers (a frame's bytes, the gzip middleware's copies) soon come from the per-thread arenas, which keep
them once freed: under quick depth stepping the API's resident memory reached about three times the wire viewer's
product cache (1427 MiB at 40 concurrent frames on the measured sample, 512 MiB of cache, and 1352 MiB still resident
at idle). A fixed threshold of 1 MiB (``mallopt(M_MMAP_THRESHOLD)``, what ``MALLOC_MMAP_THRESHOLD_=1048576`` sets at
start) maps every such buffer on its own and unmaps it when freed: 895 MiB at the peak, 783 MiB at idle, with the
same latency (measured before the frames took turns).

``laueportal serve`` sets it in the process that serves (``app.serve_app``), so the dev and e2e stacks and a
deployment get it however they start the command; ``MALLOC_MMAP_THRESHOLD_`` in the process's environment (glibc
read it at start) is left as the administrator set it. Only that variable is honoured: a threshold set through
``GLIBC_TUNABLES`` (``glibc.malloc.mmap_threshold``) is overridden. Another C library (musl, macOS) starts as it
always did.
"""

from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

M_MMAP_THRESHOLD = -3
"""glibc's ``mallopt`` parameter (``malloc.h``)."""
MMAP_THRESHOLD_BYTES = 1024 * 1024
"""Blocks of this size and larger are mapped on their own and returned to the system when freed (1 MiB: a frame
is 8 MiB as uint16, a gzip copy of one about 2.6 MiB)."""
MMAP_THRESHOLD_ENV = "MALLOC_MMAP_THRESHOLD_"
"""glibc's own environment variable for the same setting, read when the process starts."""


def is_glibc() -> bool:
    try:
        return os.confstr("CS_GNU_LIBC_VERSION") is not None  # "glibc 2.39"; no such name elsewhere
    except (AttributeError, ValueError, OSError):  # Windows has no confstr; other systems lack the name
        return False


def _mallopt() -> Callable[[int, int], int]:
    """glibc's ``mallopt`` (raises ``OSError`` or ``AttributeError`` where there is none)."""
    mallopt = ctypes.CDLL(None).mallopt  # the process's own C library
    mallopt.argtypes = (ctypes.c_int, ctypes.c_int)
    mallopt.restype = ctypes.c_int
    return mallopt


def return_freed_buffers(environ: Mapping[str, str] = os.environ) -> bool:
    """Fix glibc's mmap threshold at ``MMAP_THRESHOLD_BYTES`` for the rest of the process (module docstring); whether
    it did. Not on another C library, and not when ``MALLOC_MMAP_THRESHOLD_`` is set (the administrator's)."""
    if MMAP_THRESHOLD_ENV in environ:
        logger.info("%s is set: the allocator keeps the environment's mmap threshold", MMAP_THRESHOLD_ENV)
        return False
    if not is_glibc():
        logger.info("not glibc: the allocator keeps its own settings")
        return False
    try:
        done = _mallopt()(M_MMAP_THRESHOLD, MMAP_THRESHOLD_BYTES) == 1
    except (OSError, AttributeError):
        done = False
    if done:
        logger.info(
            "glibc's mmap threshold is %d bytes: freed frame buffers go back to the system", MMAP_THRESHOLD_BYTES
        )
    else:
        logger.warning("could not set glibc's mmap threshold: freed frame buffers may stay resident")
    return done
