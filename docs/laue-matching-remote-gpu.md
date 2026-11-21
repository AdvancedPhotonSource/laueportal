# LaueMatching on the APS GPU queue

A LaueMatching run can compute on the `pioneer` target, the APS cluster's GPU queue, through DM (the form's **Run on:
APS GPU**). This page says how the run works there and what it does not do yet. The operator's procedure is in the
[remote-execution runbook](runbooks/remote-execution.md#the-aps-gpu-target).

## How a run works

- **Transport.** As an `aps` run: the plan (`remote.plan`) points the PBS job at the run directory, which the portal,
  the DM host and the GPU nodes mount at the same path; the launcher submits with `qsub` over SSH, follows the job with
  `qstat` and stops it with `qdel` (`jobs.backends.TRANSPORTS`: `shared`). Nothing is transferred. The run's files
  (inputs, geometry, crystal, mask and background) must be inside the experiment folder, as on `crux`.
- **The data folder.** The target's `data_root` holds the orientation database and the installed forward caches. The
  plan checks that the run's cache there fits the run's frozen values exactly, and maps the portal's data folder to the
  target's (`path_map`); `request.json` keeps the portal's path. A remote run never builds a cache: build it with an
  APS run, then install it (`laueportal laue-matching install-cache`).
- **The indexer.** `LaueMatchingGPUStream`, the persistent daemon (`indexing.laue_matching.stream`): it loads the
  database and the cache once per job, then matches the frames the run sends it in manifest order. It gets a free
  port and is reached on `127.0.0.1` (laue-index 0.8.0 binds every interface); it ends with the run, a stop or the
  time limit. Preprocessing, thresholds, the watershed and the postprocessing are the CPU path's code; the daemon
  receives each prepared image as `float32`, the CPU binary as `float64`. `LAUEMATCHING_BIN` must be unset: the daemon
  is found by name. The run refuses to go on when `nvidia-smi` does not list the daemon on a GPU, and records the
  GPU, the binary's sha256 and the build information in the run's provenance.
- **Segments.** The run commits its frames every `segment_seconds` into `segments/<n>/` (`indexing.laue_matching.output`):
  each segment's `output.h5` and `laue_matching.h5`, closed, with a `segment.json` of its frames and checksums. A
  frame's failure is reported when its segment is committed. At the end the segments are merged into the run's two
  files, which are validated against the manifest, and removed. The launcher reports the committed frames as the run's
  progress (Queued "Waiting for PBS allocation", then Running "Processing: n of N frames").
- **The time limit.** A PBS job ends at its walltime. A run starts no frame after `soft_deadline_seconds` (which must
  leave 300 s of the walltime), then merges and ends with the frames it did; the rest are not run, and the run is Failed
  ("Not all inputs completed successfully", with the warning "Stopped at the allocation's time limit after n of N
  frames"). Its results are published. Choose runs that fit one allocation.
- **Stop.** A queued job is deleted. A running job is left to drain: the runner sees the stop request in the run
  directory, ends the daemon, merges and keeps the frames done, and the run is Cancelled. The launcher deletes the job
  (`qdel`) if it still runs 10 minutes after the stop (`remote.stage.drains`).

## Not built yet

- **Continuation.** A run larger than one allocation ends at the time limit. Resuming it in a further PBS job (the
  launcher's `monitor` stage submitting the next job after the committed segments, with a DM workflow of its own for
  the longer monitoring) is the next step. The segments are written so that a later job could resume after them.
- **Estimates.** No measured profile exists for LaueMatching on any target, so the form shows no estimate.
- **A loopback-only daemon.** laue-index has no option to bind the daemon to `127.0.0.1` only.
