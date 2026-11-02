# Goldens for the runtime estimates

What the runtime estimates give for each case below: `count_selection` per run, `estimate_runtime` (seconds,
profile, notes, the summed selection), `estimate_transfers` and `format_duration`, or the message of the
`ValueError` raised. `laueportal.remote.estimates` must give exactly the same numbers and messages for the same
selections (`tests/remote/test_estimates.py` › `test_the_estimates_match_the_goldens[*]`, `make check`); the
measured profiles file is pinned by its digest (`test_the_measured_profiles_are_the_pinned_file`).

They were captured once from a reference implementation and are committed; a change to one is a change of the
estimates. Nothing is normalized: the estimates read no files, clocks or hosts.

## The cases

Each case is a list of runs in the typed form (`RunSelection`'s fields, `rows`, repeated `repeat` times) and a
layout (`ExecutionLayout`'s fields). They cover a basic selection with the `APS` and `CRUX` layouts, every layout the
profiles distinguish (one, two and four nodes; two local defaults; a layout no profile matches), the counting rules
(template placeholders, duplicates, literal names, wildcards, depth grids, skips, several runs, the 100-run limit) and
every refusal.

The cases with an HDF5 input (`indexing-hdf5-file-not-a-catalog`, `indexing-missing-catalog`) name files that do
not exist: the estimates try to read a catalog and fall back to counting the folder, or refuse a missing `scan.h5`,
once the path is inside the allowed roots (the test's roots contain `/not-mounted`).

| Key | What |
|---|---|
| `name`, `kind`, `backend`, `layout`, `rows`, `repeat` | the case |
| `counts` | `count_selection` for each run (when `repeat` is 1) |
| `estimate` | `estimate_runtime` for all the runs, or `{error, type}` |
| `transfer` | `estimate_transfers` of that estimate's selection (both directions, rate, seconds) |
| `duration` | `format_duration` of the estimate's seconds |
| `durations` (top level) | `format_duration` of a list of seconds |
