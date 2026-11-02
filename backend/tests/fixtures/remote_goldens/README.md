# Goldens for remote execution

What the portal and the remote side write for each case of `tests.remote_support.CASES`, normalized, from run
creation to publication; the files that cross the portal/remote boundary, which the deployed remote side reads.
The portal must reproduce them exactly:

- the portal's files (`request.json`, `inputs.jsonl`), the plan (`bdp.json`) and its DM inbox copy
  (`submission.json`): `tests/remote/test_remote_goldens.py` (`make check`), also through `jobs.queue.enqueue_run`;
- the remote side (`laueportal.jobs.runner`, `laueportal.jobs.mpi_runner`): `tests/jobs/test_runner.py`,
  `tests/jobs/test_mpi_runner.py` (`make test-slow`);
- the publication of the returned directory (`<case>/published/`) and its republication:
  `tests/remote/test_remote_goldens.py` (`make test-slow`); `tests/remote/test_publication.py` adds the refused
  returns and an interrupted publication.

They were captured once, with the case's files made by the reference implementation's run creation, plan, runner and
publication on the same requests; they are committed, and a change to one is a change of the boundary's format. The
LaueGo cases' `threshold_ratio` is unset (`null`): the `lauego/v1` parameter model refuses a negative ratio, and compute
reads an unset one as automatic.

## Normalization

On the files' text, so the formatting is compared byte for byte:

- the temporary base directory becomes `<BASE>`;
- every digit of `heartbeat_at`, `started_at`, `finished_at` and `recorded_at` becomes `0` (their format is
  kept: `0000-00-00T00:00:00+00:00`);
- this host's name becomes `<host>` (MPI ranks report `node-<n>`, which is kept);
- `lauelab_version`, `lauelab_commit` and `lauelab_source` become placeholders, and so does every number
  under a key ending in `seconds` and the Output XML's `executionTime`;
- `.runner.lock` (the runner's PID) becomes `<pid>`; the plan's `request_sha256` and `manifest_sha256`
  and `request.json`'s `manifest.sha256` become `<request.json sha256>` and `<inputs.jsonl sha256>` only
  when they are the digests of the run's files (a wrong digest stays and fails the comparison);
- the portal's `request.json` loses its `schema_id` line (`request`'s, which the parameter models add; the goldens
  have none: `remote_support.without_schema_id`).

HDF5 outputs are summarized (`outputs.json`): every group, dataset and attribute, with dtype, shape and a
sha256 of the values (strings listed, normalized as above). Values that change from run to run are
`<volatile>`: the `created` attributes, `source_mtime_ns`, library versions and timings (`*_seconds` except
`exposure_seconds`). A fixed-length string dataset holding a path has the dtype `|S<length of a path>`.

## Files per case

| File | What |
|---|---|
| `request.json`, `inputs.jsonl` | the run's request and manifest, as run creation writes them |
| `bdp.json`, `submission.json` | the plan in the run directory and its inbox copy |
| `listing.json` | everything in the control directory, the output directory and (MPI) `shards/` |
| `run.json`, `progress.json`, `failures.jsonl`, `ranks.json` | the runner's files, as written (when present) |
| `runner.lock` | `.runner.lock` |
| `outputs.json` | the science outputs (`output.h5`, the Output XML, `reconstruction/`) |
| `published/listing.json` | everything in the run directory after the publication |
| `published/run.json`, `published/publication.json` | the published summary and the marker `.bdp-publication.json` |
| `published/outputs.json` | the published science outputs: input paths rewritten, remote originals in `/provenance/remote_input_images` |

## Cases

| Case | What |
|---|---|
| `aps-lauego` | LaueGo, 3 synthetic Ni frames, `aps` (the control directory is the run directory; output in `incoming/<submission>/`) |
| `aps-lauego-failure` | the same with frame 2 unreadable: `failures.jsonl`, status Failed |
| `crux-lauego` | LaueGo on `crux` (inputs staged under the Eagle workspace, paths remapped by `path_map`) |
| `aps-wire`, `crux-wire` | wire reconstruction, 2 synthetic points |
| `crux-wire-cancelled` | `cancel.request` before the start: status Cancelled, nothing computed |
| `crux-lauego-mpi` | the MPI runner, 2 nodes x 2 indexing ranks (two ranks get no input) |
| `crux-wire-mpi` | the MPI runner, 3 points on 2 nodes |

A change of lauelab's outputs or of the cases changes them: review the diff.
