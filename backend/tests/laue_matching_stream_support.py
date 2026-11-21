"""A stand-in for ``LaueMatchingGPUStream``: the daemon's wire protocol, result tables and log lines, with a CPU
binary matching each frame. It lets the GPU path of a LaueMatching run be tested on a machine with no GPU.

``fake_daemon(tmp_path, cpu)`` writes an executable that takes the daemon's arguments (parameter file, orientation
database, reflection list, threads), listens on ``127.0.0.1:$LAUE_STREAM_PORT``, and for each image it receives (a
little-endian ``uint16`` number and ``float32`` pixels) writes the image as doubles, runs ``cpu`` on it as
``indexing.laue_matching.compute`` runs the CPU binary, appends that binary's rows to ``results_stream/solutions.txt`` and
``spots.txt`` with the image number first, and logs ``[Image N] Total: ...`` (or ``No matches``) after them.
``FAKE_STREAM_EXIT_AFTER=<n>`` makes it exit after ``n`` images; ``FAKE_STREAM_START_SECONDS`` delays its listening.
"""

from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

SCRIPT = """\
#!{python}
import os, signal, socket, struct, subprocess, sys, time
import numpy as np

params, orient, hkl, threads = sys.argv[1:5]
values = dict(line.split(None, 1) for line in open(params) if line.strip() and len(line.split(None, 1)) == 2)
nx, ny = int(values["NrPxX"]), int(values["NrPxY"])
exit_after = int(os.environ.get("FAKE_STREAM_EXIT_AFTER", "0"))
time.sleep(float(os.environ.get("FAKE_STREAM_START_SECONDS", "0")))
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
os.makedirs("results_stream", exist_ok=True)
tables = {{"solutions": open("results_stream/solutions.txt", "w"), "spots": open("results_stream/spots.txt", "w")}}
headers = {{}}
server = socket.socket()
server.bind(("0.0.0.0", int(os.environ["LAUE_STREAM_PORT"])))
server.listen(1)
print("listening", flush=True)
client, _ = server.accept()

def exact(n):
    data = bytearray()
    while len(data) < n:
        chunk = client.recv(n - len(data))
        if not chunk:
            sys.exit(0)
        data.extend(chunk)
    return bytes(data)

images = 0
while True:
    (number,) = struct.unpack("<H", exact(2))
    image = np.frombuffer(exact(4 * nx * ny), dtype=np.float32)
    print(f"Received image {{number}}", flush=True)
    image.astype(np.float64).tofile("frame.bin")
    subprocess.run([{cpu!r}, params, orient, hkl, "frame.bin", threads], check=True, stdout=subprocess.DEVNULL)
    found = 0
    for name, stream in tables.items():
        lines = open(f"frame.bin.{{name}}.txt").read().splitlines()
        if name not in headers:
            headers[name] = "%ImageNr\\t" + lines[0].lstrip("%")
            stream.write(headers[name] + "\\n")
        for line in lines[1:]:
            if line.strip():
                stream.write(f"{{number}}\\t{{line}}\\n")
                found += name == "solutions"
        stream.flush()
    for name in ("frame.bin", "frame.bin.solutions.txt", "frame.bin.spots.txt"):
        os.remove(name)
    if found:
        print(f"[Image {{number}}] Total: 0.001 s (GPU: 1 ms, CPU: 1 ms)", flush=True)
    else:
        print(f"[Image {{number}}] No matches, skipping fitting.", flush=True)
    images += 1
    if exit_after and images >= exit_after:
        sys.exit(7)
"""


def fake_daemon(folder: Path, cpu: Path | str) -> Path:
    """An executable named ``LaueMatchingGPUStream`` in ``folder`` that matches each frame with ``cpu``."""
    path = folder / "LaueMatchingGPUStream"
    path.write_text(textwrap.dedent(SCRIPT.format(python=sys.executable, cpu=str(cpu))))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def on_gpu(pid: int | None) -> dict[str, Any]:
    """What ``stream.gpu_evidence`` returns when the daemon runs on a GPU."""
    return {
        "gpus": [{"uuid": "GPU-test", "name": "Test GPU", "memory_total_mib": "1", "driver_version": "0"}],
        "daemon_on_gpu": [{"pid": pid, "gpu_uuid": "GPU-test", "used_memory_mib": "1"}],
        "cuda_visible_devices": "0",
    }
