"""The ``LaueMatchingGPUStream`` daemon as one run's matcher: started once, fed frames in order, stopped at the end.

The daemon loads the orientation database and the forward cache once, then matches each image it receives over TCP:
a little-endian ``uint16`` image number and the image as ``float32`` pixels (``laue_index.pipeline.laue_stream_utils``'s
wire format). It appends each frame's rows to ``solutions.txt`` and ``spots.txt`` in its result folder, the CPU
binary's columns with an ``ImageNr`` column first (``laue_index.records``' ``stream`` layout), and ends each frame
with one log line, ``[Image N] Total: ...`` or ``[Image N] No matches, skipping fitting.``, written after its rows.

The daemon listens on every interface (laue-index 0.8.0 has no bind option), so it gets a free high port of its own
and the client connects only through ``127.0.0.1``. It runs in this job process's group, so a stop of the job reaches
it, and every exit of :class:`StreamMatcher` ends it. Its image number is 16 bits: one daemon matches at most
``MAX_IMAGES`` frames.

Standard library and numpy only, as the rest of the compute side (import-linter rule 3).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

BINARY_NAME = "LaueMatchingGPUStream"
BINARY_ENV = "LAUEMATCHING_BIN"
MAX_IMAGES = 65535
"""The image number is a ``uint16``; 0 is not used."""
POLL_SECONDS = 0.05
SEND_SECONDS = 1.0
"""How long one send to the daemon may block before a stop and the daemon's life are checked again."""
TERMINATE_SECONDS = 10.0
CONNECT_SECONDS = 1.0
RESULT_DIRNAME = "results_stream"
LOG_NAME = "daemon.log"
_DONE = re.compile(r"^\[Image (\d+)\] (?:Total:|No matches, skipping fitting\.)")


class DaemonFailed(RuntimeError):
    """The daemon exited, or did not start; the run cannot go on with it."""


class StopRequested(Exception):
    """A stop or the time limit came while the daemon started or matched a frame."""


def free_port() -> int:
    """A port no process on this host listens on now. Another process may take it before the daemon does: the
    daemon then fails to bind, and :meth:`StreamMatcher.start` reports that."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _TableTail:
    """A table the daemon appends to, read incrementally: complete lines only, grouped by their image number."""

    def __init__(self, path: Path, columns: int) -> None:
        self.path = path
        self.columns = columns
        self.offset = 0
        self.partial = ""
        self.header: str | None = None
        self.by_image: dict[int, list[list[float]]] = {}

    def read(self) -> None:
        try:
            with open(self.path, encoding="utf-8", errors="replace") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
        except FileNotFoundError:
            return
        lines = (self.partial + chunk).split("\n")
        self.partial = lines.pop()
        for line in lines:
            if not line:
                continue
            if line.startswith("%"):
                self.header = self.header or line
                continue
            values = [float(value) for value in line.split()]
            if len(values) != self.columns + 1:
                raise DaemonFailed(f"{self.path.name} has a row of {len(values)} columns, not {self.columns + 1}")
            self.by_image.setdefault(int(values[0]), []).append(values[1:])

    def take(self, image: int) -> np.ndarray:
        rows = self.by_image.pop(image, [])
        return np.array(rows, dtype=np.float64).reshape(len(rows), self.columns)

    def columns_header(self) -> str | None:
        """The header without its ``ImageNr`` column: the CPU binary's header for the same columns."""
        if self.header is None:
            return None
        names = self.header.lstrip("%").split()
        return "%" + "\t".join(names[1:])


class StreamMatcher:
    """One daemon for one run (or one allocation of it). ``start`` waits until it listens; ``match`` sends one
    prepared image and returns that frame's solutions and spots in the CPU binary's columns; ``close`` ends it."""

    name = BINARY_NAME

    def __init__(
        self,
        binary: Path,
        *,
        params: Path,
        database: Path,
        hkls: Path,
        threads: int,
        folder: Path,
        solution_columns: int,
        spot_columns: int,
        should_stop: Callable[[], bool],
    ) -> None:
        self.binary = binary
        self.command = [str(binary), str(params), str(database), str(hkls), str(threads)]
        self.folder = folder
        self.should_stop = should_stop
        self.log = folder / LOG_NAME
        self.solutions = _TableTail(folder / RESULT_DIRNAME / "solutions.txt", solution_columns)
        self.spots = _TableTail(folder / RESULT_DIRNAME / "spots.txt", spot_columns)
        self.process: subprocess.Popen[bytes] | None = None
        self.socket: socket.socket | None = None
        self.port: int | None = None
        self.images = 0
        self.started_seconds: float | None = None
        self._log_offset = 0
        self._log_partial = ""
        self._done: set[int] = set()

    def start(self) -> None:
        """Start the daemon and connect to it. ``StopRequested`` when a stop comes first (the daemon is ended);
        ``DaemonFailed`` when it exits first."""
        shutil.rmtree(self.folder / RESULT_DIRNAME, ignore_errors=True)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        environment = {**os.environ, "LAUE_STREAM_PORT": str(self.port)}
        started = time.monotonic()
        with open(self.log, "wb") as output:
            self.process = subprocess.Popen(
                self.command, cwd=self.folder, stdout=output, stderr=subprocess.STDOUT, env=environment
            )
        while True:
            self._check_alive()
            if self.should_stop():
                self.close()
                raise StopRequested
            try:
                self.socket = socket.create_connection(("127.0.0.1", self.port), timeout=CONNECT_SECONDS)
                break
            except OSError:
                time.sleep(0.5)
        self.socket.settimeout(SEND_SECONDS)
        self.started_seconds = time.monotonic() - started

    def match(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Match one prepared image. ``StopRequested`` when a stop comes while it is matched (its rows are not
        read); ``DaemonFailed`` when the daemon exits or its tables are malformed."""
        if self.socket is None:
            raise DaemonFailed("the daemon is not started")
        if self.images >= MAX_IMAGES:
            raise DaemonFailed(f"one daemon matches at most {MAX_IMAGES} frames")
        self.images += 1
        number = self.images
        payload = memoryview(struct.pack("<H", number) + np.ascontiguousarray(image, dtype=np.float32).tobytes())
        while payload:
            try:
                payload = payload[self.socket.send(payload) :]
            except TimeoutError:  # the daemon reads nothing now: a stop or its end must not wait for it
                self._check_alive()
                if self.should_stop():
                    raise StopRequested from None
            except OSError as error:
                self._check_alive()
                raise DaemonFailed(f"the daemon refused frame {number}: {error}") from None
        while number not in self._done:
            self._read_log()
            if number in self._done:
                break
            self._check_alive()
            if self.should_stop():
                raise StopRequested
            time.sleep(POLL_SECONDS)
        self.solutions.read()
        self.spots.read()
        return self.solutions.take(number), self.spots.take(number)

    def columns(self) -> tuple[str, str] | None:
        """The tables' headers in the CPU binary's columns, once the daemon wrote them."""
        solutions, spots = self.solutions.columns_header(), self.spots.columns_header()
        return None if solutions is None or spots is None else (solutions, spots)

    def pid(self) -> int | None:
        return None if self.process is None else self.process.pid

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=TERMINATE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _check_alive(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            raise DaemonFailed(f"{BINARY_NAME} exited with code {code}: {self._log_tail()}")

    def _read_log(self) -> None:
        try:
            with open(self.log, encoding="utf-8", errors="replace") as stream:
                stream.seek(self._log_offset)
                chunk = stream.read()
                self._log_offset = stream.tell()
        except FileNotFoundError:
            return
        lines = (self._log_partial + chunk).split("\n")
        self._log_partial = lines.pop()
        for line in lines:
            found = _DONE.match(line)
            if found:
                self._done.add(int(found.group(1)))

    def _log_tail(self) -> str:
        try:
            return self.log.read_text(errors="replace")[-500:]
        except OSError:
            return "(no log)"


def stream_binary(require: Callable[[str], Path]) -> Path:
    """The daemon's binary, found by name (``require``: ``laue_index.indexer.require_binary``). Refused while
    ``LAUEMATCHING_BIN`` is set: that names one concrete binary (a CPU wrapper on the portal host), which must not
    stand in for the daemon."""
    if os.environ.get(BINARY_ENV):
        raise RuntimeError(f"{BINARY_ENV} is set; the GPU stream daemon is found by name, so unset it for GPU runs")
    binary = Path(require("STREAM"))
    if binary.name != BINARY_NAME:
        raise RuntimeError(f"{binary} is not {BINARY_NAME}")
    return binary


def gpu_evidence(pid: int | None) -> dict[str, object] | None:
    """What ``nvidia-smi`` says of the GPUs and of the daemon's process on them; ``None`` without ``nvidia-smi``."""
    tool = shutil.which("nvidia-smi")
    if tool is None or pid is None:
        return None

    def query(*arguments: str) -> list[list[str]]:
        done = subprocess.run(
            [tool, *arguments, "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=30
        )
        if done.returncode != 0:
            return []
        return [[cell.strip() for cell in line.split(",")] for line in done.stdout.splitlines() if line.strip()]

    gpus = [
        {"uuid": row[0], "name": row[1], "memory_total_mib": row[2], "driver_version": row[3]}
        for row in query("--query-gpu=uuid,name,memory.total,driver_version")
        if len(row) == 4
    ]
    processes = [
        {"pid": int(row[0]), "gpu_uuid": row[1], "used_memory_mib": row[2]}
        for row in query("--query-compute-apps=pid,gpu_uuid,used_memory")
        if len(row) == 3 and row[0].isdigit()
    ]
    return {
        "gpus": gpus,
        "daemon_on_gpu": [process for process in processes if process["pid"] == pid],
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }
