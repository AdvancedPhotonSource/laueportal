# LaueMatching on a remote GPU: the plan

Not built. LaueMatching computes on the portal's worker, on the CPU; the form offers no target and `remote.plan` refuses
the run kind. This page says what a remote GPU workflow needs, so the local code does not close it off.

## Why a GPU

On a development workstation (Ryzen AI MAX+ 395, 16 cores, unified memory) a frame takes about 1.4 s, and the rate stops
at about 0.7 frames per second however the cores are divided. A 40,000-frame raster is then about 16 hours there; a
server's memory bandwidth may change this, so measure the target host. LaueMatching's manuals report 0.19 s per frame on one GPU.

## What it takes

- **The indexer.** Use `LaueMatchingGPUStream`, the persistent daemon: it loads the database once and takes frames over
  TCP. `LaueMatchingGPU` reloads the database for every frame. Each daemon needs about 19 GB of host memory.
- **The compute step.** `indexing.laue_matching.compute` starts one process per frame. A GPU compute starts the daemon,
  feeds it frames in manifest order, and keeps the current rules: stop ends the daemon, each frame's failure is its own,
  and the output is `output.h5` and `laue_matching.h5`. The daemon writes one solutions table with an image-number
  column; `laue_index.records` has its layout.
- **The PBS script.** `remote/pbs.py` writes `ngpus=0` and exports `OMP_NUM_THREADS=1`; a GPU target needs its own
  resources.
- **The compute environment.** `requirements/compute.in` gains `laue-index`, `scipy`, `opencv-python-headless`,
  `scikit-image` and `diplib`, built with CUDA for the target's cards; `jobs.contract` already imports the compute
  module only for its kind.
- **The data folder.** The database and forward caches stay on the remote side; a run must not transfer 7.2 GB.
  `remote/plan.py` lists the files a transferred run sends and must leave these out.
- **The run kind.** `jobs/runner.py`, `jobs/mpi_runner.py`, `remote/pbs.py` and `remote/poller.py` test only for
  `lauego_indexing`; each needs this kind. Add it to `remote.plan.REMOTE_KINDS` last, with goldens in
  `backend/tests/fixtures/remote_goldens/`.
- **The form.** Add the `backend` field and its target check, as `lauego/v1` has.
