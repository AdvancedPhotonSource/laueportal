# LaueMatching on the Pioneer GPU queue

A LaueMatching run can compute on the `pioneer` target, the APS cluster's GPU queue, through DM (the form's **Run on:
Pioneer GPU**). This page says how the run works there and what it does not do yet. The operator's procedure is in the
[remote-execution runbook](runbooks/remote-execution.md#the-pioneer-gpu-target).

## How a run works

- **Transport.** As an `aps` run: the plan (`remote.plan`) points the PBS job at the run directory, which the portal,
  the DM host and the GPU nodes mount at the same path; the launcher submits with `qsub` over SSH, follows the job with
  `qstat` and stops it with `qdel` (`jobs.backends.TRANSPORTS`: `shared`). Nothing is transferred.
- **Shared storage and staged files.** The shared storage is `storage.aps.filesystem_root`, which the portal, the DM
  host and the GPU nodes mount at the same path. The run's frames, and its input folder, must be on it (validation and
  the plan check each path with its links resolved, never by its text); frames are never copied. A geometry, crystal,
  mask or background file on it is read where it is, also outside the experiment folder. One that is not (a geometry in
  the portal host's calibration folder) is copied at submission into the run's `staged-inputs/<field>/`, through a
  synced partial file, and the plan (`bdp.json`'s `staged_inputs`) records its source, its real path, its size and its
  sha256; the copy is read-only and the original is never written. The runner reads the copy, checked against that
  checksum, and `request.json` keeps the path the run was created with. A file over 1 GiB is refused rather than
  copied. `crux` keeps its own rule: every file inside the experiment folder.
- **The data folder.** The target's `data_root` holds the orientation database and the installed forward caches. The
  plan checks that the run's cache there fits the run's frozen values exactly, and maps the portal's data folder to the
  target's (`path_map`); `request.json` keeps the portal's path. With **New…**, the portal's existing worker builds
  the cache on the portal CPU before submitting this same run to DM. This needs the local and Pioneer data folders
  to resolve to the same shared folder. The build uses a blank internal image and processes no detector input;
  `cache-build/` keeps its parameters, log and provenance. Stop or a failed build submits no GPU allocation. The
  run waits in the local batch queue, builds, then returns to Queued for remote execution. A reused cache goes
  straight to DM. Sites with separate folders can still copy a CPU cache with `laueportal laue-matching install-cache`.
- **The indexer.** `LaueMatchingGPUStream`, the persistent daemon (`indexing.laue_matching.stream`): it loads the
  database and the cache once per allocation, then matches the frames the run sends it in manifest order. It gets a
  free port and is reached on `127.0.0.1` (laue-index 0.8.0 binds every interface); it ends with the allocation. Its
  image number is 16 bits, so one daemon (one allocation) matches at most 65,535 frames. Preprocessing, thresholds,
  the watershed and the postprocessing are the CPU path's code; the daemon receives each prepared image as `float32`,
  the CPU binary as `float64`. `LAUEMATCHING_BIN` must be unset: the daemon is found by name. The run refuses to go on
  when `nvidia-smi` does not list the daemon on a GPU, and records the GPU, the binary's sha256 and the build
  information in the run's provenance (each allocation's GPU in its `result.json`).
- **Segments.** The run commits its frames every `segment_seconds` into `segments/<n>/` (`indexing.laue_matching.output`):
  each segment's `output.h5` and `laue_matching.h5`, closed, with its failures' full records (`failures.jsonl`) and a
  `segment.json` naming every input it attempted in manifest order, which succeeded and which failed, the checksum of
  each file and the run's identity. When the run ends the segments are merged once into the run's two files, which are
  validated against the manifest, the failures of every segment are reported once in order, and the segments are
  removed. The launcher reports the committed frames as the run's progress (Queued "Waiting for PBS allocation", then
  Running "Processing: n of N frames"); the count is the whole run's and never goes back.

## One run over several allocations

A PBS allocation on the GPU queue lasts at most its walltime (one hour on the inference queue), and the queue runs one
job per account at a time. A run of any size is one portal run (one job, one indexing ID, one request and manifest,
one start time, one Stop) that the launcher continues over sequential PBS allocations, one at a time
(`remote.stage`'s "Continued runs").

- **An allocation's limits.** An allocation starts no frame after `soft_deadline_seconds` (which must leave 300 s of
  the walltime) or after `max_frames_per_allocation` frames (at most 65,535, one daemon's, and its default). It then
  commits its open segment and hands off: it writes `allocations/<n>/result.json` (the inputs committed) and no run
  summary, so nothing is published or failed in between. A pilot sets a few
  frames or a short deadline to see a small run continue.
- **The next allocation.** The launcher submits it only once PBS reports the previous one finished and its hand-off
  checks (more inputs committed than before). Between them the run stays Running, its observation Queued "Waiting for
  next PBS allocation". Other runs queue in PBS as usual. An allocation is recorded before its `qsub`, and a lost reply
  is reconciled from PBS's history by the allocation's job name: it is never submitted twice.
- **Resuming exactly.** Each allocation first checks the run's identity (`checkpoint.json`: the request's parameters,
  the manifest, the checksums of the geometry, crystal, mask and background files, the forward cache's record, size
  and reflections, the orientation database's size, the parameter file and the matcher's binary), then every committed
  segment against its record and checksums, and that the committed inputs are exactly the start of the manifest. It
  goes on from the first input no segment attempted: no input is computed, counted or published twice, and none is
  skipped. Frames of a segment that was not committed are computed again; they were never counted.
- **The background.** A raw-frame run's background is the median of its first raw frame, computed once: the
  allocation that computes it records it (`background.json`: the frame and the checksum of `background.bin`), and
  every later allocation reads that one back, checked. Reconstructed frames use none.
- **The end.** The allocation that attempts the last input, or that drains after a stop, merges every segment and
  writes the run's summary: Finished when every input succeeded, else Failed ("Not all inputs completed successfully")
  with every failed input reported, or Cancelled. The summary's `started_at` is the first allocation's, and its
  provenance lists every allocation (`allocations`) and segment. The output files are those one allocation would have
  written. The segments are removed only after the summary is written.
- **A stop.** On Stop no processing allocation is submitted any more, and a queued one is deleted at once. A running
  allocation drains: it commits, merges and ends the run Cancelled with the frames done (the launcher deletes it if it
  still runs 10 minutes after the stop or after it started, whichever is later). When the allocation in PBS at the
  stop ended without the run's summary, one *finalizing* allocation, with the target's `finalize_pbs` (a CPU request
  with no GPU, on a CPU queue, so it never waits behind the GPU queue), checks the run's identity and every committed
  segment, starts no daemon, processes no input, merges and ends the run Cancelled ("Stopping: waiting to merge", then
  "Stopping: merging results"). With nothing committed the run ends Cancelled at once.
- **Anything unexpected ends the run Failed, with its files kept.** A frame that fails is reported in the run's
  failure report, as on one allocation; that is not a failure of the run's allocations. But an allocation that ends
  without the run's summary or a hand-off that checks (the walltime, a node failure, a crash, a signal without a stop),
  a hand-off that committed no input, a refused `qsub`, a `qsub` whose job PBS's history never shows, an error of the
  runner (a checkpoint, input, cache or software that changed, the storage failing, a daemon that does not start, a
  merge that fails) or of the launcher, and a finalizing allocation that fails after a stop: each ends the run Failed
  with its reason, and nothing is submitted after it. The committed segments, `checkpoint.json` and every allocation's
  PBS output stay in the output folder; a summary the runner wrote says how many inputs are committed and that they
  are kept unmerged. Nothing is retried, rescued or resumed: submit a new run for the rest, and an operator can merge
  the committed frames into a new folder (the runbook's "Salvaging a failed run"). An end killed after it published its
  merged files is not finished later either: a stop's finalizing allocation that finds them trusts and replaces none,
  and ends the run Failed.
- **Software.** The run's identity includes the versions of lauelab, laue-index, numpy, scipy, h5py, scikit-image,
  diplib and Python, the matcher's sha256 and the checksum of the LaueMatching compute module. Never replace the
  software of an active run in place: its next allocation would end the run Failed rather than resume with other
  science. Install a new release beside the old one and switch only while no run is active.
- **What each allocation reads again.** Each allocation checks every committed segment's files against their
  checksums, and the end checks them once more before the merge: on the order of the committed output's size per
  allocation (tens of seconds for tens of gigabytes), while the frames themselves are never read again. The heartbeat
  (`progress.json`) runs during that check; a heartbeat that cannot be written is tried again and never stops the run.
- **PBS or the storage not answering.** While PBS or the shared storage does not answer, the run stays Running with
  its observation delayed, until DM's monitor stage ends (`dm.monitor_seconds`, 14 days by default): an allocation PBS
  cannot be asked about is never taken to have ended. After PBS reports an allocation finished, the launcher waits up
  to a minute for its files to appear on the shared storage before it takes them to be missing. A run must end within
  the monitoring horizon; DM ending it there ends the run Failed.

## Not built yet

- **Estimates.** No measured profile exists for LaueMatching on any target, so the form shows no estimate.
- **A loopback-only daemon.** laue-index has no option to bind the daemon to `127.0.0.1` only.
