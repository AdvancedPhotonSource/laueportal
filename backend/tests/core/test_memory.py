"""The API process's allocator (``core.memory``): freed frame buffers go back to the system."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from laueportal.core import memory
from tests.core.test_cli import SOURCE_DIR, write_config

# glibc's own accounting (mallinfo2, glibc 2.33+): ``hblks`` counts the blocks mapped on their own.
MAPPED_BLOCKS = """
import ctypes, sys
class MallInfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost")]
libc = ctypes.CDLL(None)
libc.mallinfo2.restype = MallInfo2
libc.malloc.restype, libc.malloc.argtypes, libc.free.argtypes = ctypes.c_void_p, (ctypes.c_size_t,), (ctypes.c_void_p,)
if sys.argv[1] == "serving":
    from laueportal.app import serve_app
    serve_app()
libc.free(libc.malloc(8 << 20))  # a frame's buffer, freed: glibc's own threshold rises past its size
before = libc.mallinfo2().hblks
block = libc.malloc(2 << 20)
print(libc.mallinfo2().hblks - before)
libc.free(block)
"""


def has_mallinfo2() -> bool:
    """Whether this process's C library is glibc 2.33 or later, which counts mapped blocks in ``mallinfo2``."""
    return memory.is_glibc() and hasattr(ctypes.CDLL(None), "mallinfo2")


@pytest.mark.skipif(not has_mallinfo2(), reason="glibc 2.33's mallinfo2 counts the blocks mapped on their own")
def test_the_serving_process_maps_each_large_buffer_on_its_own_so_a_freed_one_goes_back(tmp_path: Path) -> None:
    """By default glibc serves a 2 MiB buffer from its heap once an 8 MiB one was freed (and keeps it when it is
    freed); the process ``laueportal serve`` runs (``app.serve_app``) maps it on its own, so freeing it unmaps it."""
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("LAUEPORTAL_") and name != memory.MMAP_THRESHOLD_ENV
    }
    environ |= {"LAUEPORTAL_CONFIG": str(write_config(tmp_path)), "PYTHONPATH": str(SOURCE_DIR)}

    def mapped(process: str) -> str:
        run = subprocess.run(
            [sys.executable, "-c", MAPPED_BLOCKS, process], env=environ, capture_output=True, text=True, timeout=60
        )
        assert run.returncode == 0, run.stderr[-2000:]
        return run.stdout.strip()

    assert (mapped("default"), mapped("serving")) == ("0", "1")


@pytest.mark.parametrize("case", ["administrator", "another C library", "no mallopt"])
def test_the_administrators_setting_and_another_c_library_are_left_as_they_are(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    calls: list[tuple[int, int]] = []

    def mallopt(parameter: int, value: int) -> int:
        calls.append((parameter, value))
        return 1

    def missing() -> None:
        raise AttributeError("mallopt")

    monkeypatch.setattr(memory, "is_glibc", lambda: case != "another C library")
    monkeypatch.setattr(memory, "_mallopt", missing if case == "no mallopt" else lambda: mallopt)
    environ = {memory.MMAP_THRESHOLD_ENV: "131072"} if case == "administrator" else {}

    assert memory.return_freed_buffers(environ) is False
    assert calls == []
