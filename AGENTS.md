# Laue Portal

Laue Portal (`laueportal`) is the web portal of the 3D X-ray micro-diffraction instrument at beamline 34-ID-E of the
Advanced Photon Source. Beamline scientists and users run their analysis in it during and after beam time: they
browse and import scans, reconstruct wire scans into one frame per depth, index Laue patterns with LaueGo, inspect the
results in viewers, follow and stop runs, process a scan while it is measured, and run the computation on the APS
cluster or at ALCF. lauelab does the scientific computation; the portal manages the runs, their files, the queue and
the pages.

The backend is FastAPI with SQLite (`backend/`, the Python package `laueportal`); the frontend is React 19 with shadcn,
AG Grid, Canvas2D and VTK.js (`frontend/`, the package `laueportal-web`). Read the conventions of the part you change:
the [backend's](docs/contributing/backend.md) or the [frontend's](docs/contributing/frontend.md).

If an `AGENTS.local.md` exists at the root of the main checkout, read it too. It contains the rules of the machine you
work on: its paths, its ports, and the data and processes you must not touch. Git ignores it; never commit it. In a
[worktree](docs/runbooks/dev-setup.md#branches-and-worktrees) (only for a side experiment), the main checkout is the
parent of the folder that `git rev-parse --path-format=absolute --git-common-dir` prints; `scripts/env.sh` finds
`scripts/env.local.sh` in the same way.

## Principles

These six principles come before the other rules here and in `docs/contributing/`; each gives its reason and the
practice that follows. When one of them conflicts with your task, say so and ask a maintainer to decide.

1. **The portal is intuitive, fast and dependable.** Users choose the portal over scripts when it is easier to use and
   they can rely on it, so ease of use and reliability come before new features. A page is clear without
   instructions, answers promptly and stays responsive with a large run: the grids page, sort and filter on the
   server, the viewers draw with Canvas2D, and VTK.js loads only in the 3D view. The portal keeps answering while a
   `/net` mount hangs, a process is killed or a computation is slow: a file that is missing, unreadable or not
   responding is a state in the answer, never a 500, and a request that waits holds no thread and no database
   connection (the second of the [three ways to hurt yourself](#three-ways-to-hurt-yourself)). Send slow work
   (compute, conversion) to the worker; `jobs.reconciler` marks a run whose job process died Failed, so that no run
   stays Running without a process. A change that makes a page slower or harder to use needs a reason.
2. **Every result can be traced to its run.** Scientists decide whether to trust a number by the run that computed it.
   A run directory contains the frozen request (`request.json`), the input manifest (`inputs.jsonl`), the outputs, the
   failure report and the run summary (`run.json`). Deployed launchers and compute nodes read these files, and the
   portal reads those of runs created long ago, so their names, keys, time formats and atomic writes are a fixed
   format (`jobs.rundir`), recorded in the goldens in `backend/tests/fixtures/remote_goldens/`; never edit a golden by
   hand, because the tests compare the code's output with it. A changed parameter key is a new schema version
   (`schema_id`, such as `lauego/v1`). The tag of a viewer's product contains the identities of the files read to
   compute it and of the code (`core.etag`). Get a maintainer's decision before you change a run directory's format, a
   parameter schema, or a job status or phase transition.
3. **Few parts, and no work that nothing needs.** The portal shares DM, Globus, PBS and the `/net` file servers with the
   rest of the facility, and each part it adds is one more that operators run and that can fail. Supervisor runs two
   programs, the API and one worker process, on one SQLite database; the job table is the queue (the import contract
   `no-redis-rq` forbids Redis and RQ). Before you add a part, a setting or a fallback, check whether deleting code
   solves the problem. A background activity runs only while something needs it: the remote poller makes no DM call
   while no remote run is Queued or Running, and one call per pass for all of them; `tests/remote/test_poller_thread.py`
   checks that each of its activities stops.
4. **Data is never put at risk.** A database and its run directories contain the users' records and results. Every
   schema change is a hand-written Alembic revision in `backend/migrations/versions/`, tested in
   `backend/tests/test_migrations.py`, and a merged revision is frozen, because deployed databases were migrated
   through it. A migration refuses, changing nothing, where it would lose data, and `laueportal db stamp` refuses a
   database whose tables are not those of the revision. Back up the database before an upgrade
   (`laueportal db backup`), and work on copies (the first of the three ways to hurt yourself).
5. **Users decide what runs and when it stops.** A run that starts, ends or moves to another computer without a user's
   action can waste a cluster allocation or lose results. A live run has no time limit: it ends when a user stops
   it on the Live page (Stop, then Stop now), when it fails, or when the worker stops. A remote submission is never
   repeated automatically: `remote.submission.submit_run` records its identity before it contacts DM, and after a lost
   reply the remote poller looks for the DM job by that identity. A run computes on the portal host unless remote
   execution is enabled (`[remote] enabled`, off by default) and the user chooses `aps` or `crux`.
6. **The UI shows facts.** Users return to the same pages every day and know their layout. The UI shows labels, values,
   statuses and errors, not sentences about how it works: a hint is a tooltip, and an empty state is one short line.
   Each region of a page has a visible boundary (a `Card`, or `surface` from `frontend/src/lib/styles.ts`). One kind of
   thing looks the same and has the same name on every page (a run's status is a `RunStatusBadge`). Check every UI
   change in the light and the dark theme.

## Glossary

- A **scan** is one measurement of the sample, as described in its scan log: an ID, a folder of frames, and a catalog
  (sample, notes) that users edit in the portal.
- A **frame** is one detector image (an HDF5 file at 34-ID-E).
- A **point** is one measurement location of a wire scan; reconstruction converts its frames into one frame per depth.
- A **reconstruction** (`R<id>`; a wire run in the code) is a run of the wire program. Its artifact is a lauelab scan
  catalog (`scan.h5`) with one point file per point.
- An **indexing run** (`I<id>`) is a run of an indexing program, LaueGo or LaueMatching (`method`), over frames or over
  a reconstruction's depths. Its artifact is the results file (`output.h5`; a LaueGo run also has an Output XML).
- A **forward cache** is LaueMatching's precomputed simulation of every orientation, named by users and shared by the
  runs whose geometry, crystal, energies and spot count it exactly fits.
- A **job** is the execution record of one run: its status (Queued, Running, Finished, Failed, Cancelled) and phase.
- A **run directory** is the folder that contains a run's frozen request, input manifest, outputs and history.
- An **execution target** is where a run computes: `local` (the worker), `aps` (the APS cluster, through DM and PBS) or
  `crux` (ALCF's Crux, through DM, Globus and PBS), as defined in `jobs.backends`.
- **DM** is the APS Data Management system, which provides workflows, the DAQ catalog and transfers.
- A **live run** processes frames while a DAQ writes them; a **live session** is one or two live runs started together.
  A **follower** is a live indexing run that indexes the points of a live reconstruction.
- A **conversion** creates an indexing run's results file from its Output XML; it is a worker task, not a run.
- An **artifact** is a run's output as the viewers read it. A **product** is one view that lauelab computes from it (a
  map, a pole figure, a detector image); its tag contains the identities of the files read to compute it.
- The **scope** is the set of records that a viewer shows (lauelab's `DataScope`). A **step** is one frame of a run, and
  a **pattern** is one orientation found in a step.
- A **turn** is a bounded slot for capped work in the API (`core.api.turn`); a request that finds the turn's queue
  full gets a 503 with `Retry-After`.
- The **roots** are the folders that the portal may read and write (the `[files]` settings); the **probe** is the set
  of bounded checks of paths under them (`files.probe`).
- A **migrated run** was created by the earlier portal and brought over with its database (the deploy runbook, step 4).

## Three ways to hurt yourself

1. **Writing a real database or deleting data.** A database copied from a deployment contains users' records, and
   `laueportal seed demo --force` builds a database again and empties its runs root. Work on copies. List the
   originals you keep in the setting `database.protected` (or `LAUEPORTAL_DATABASE__PROTECTED`):
   `laueportal db backup`, `seed demo` and `seed slow-runs` refuse to write them, but the API, the worker and
   `db upgrade` do not check the list, so set `database.path` to a copy before you start any of them.
2. **A wait that hangs or starves the server.** A stat on a hung NFS mount blocks in the kernel, and no timeout in
   the API can end it: during a request, never stat, list or open a requested or recorded path outside
   `files.probe.bounded`. A request that holds one of the API's 40 threads or 15 database connections while it waits
   blocks the others; when sixteen waiting requests kept their connections, every other route answered 500. A route
   that waits for capped work is `async def` and waits in `core.api.turn`; before it waits, it reads the database in a
   session of its own (`core.api.SessionsDep`), and it never uses a threading semaphore. Test it with a
   one-connection pool and a hung path (`tests.probe_support`).
3. **Leaving or killing the wrong process.** The dev and e2e stacks both use port 2100. Stop what you start
   (`make dev-down`; `make e2e` stops its own stack) and check with `make dev-status` or `ss -ltnp`. A job process
   runs in a session of its own, so a killed worker leaves it running; the worker records its process ID and start
   time on the job's row and stops it at a service stop. Never kill processes by a name pattern
   (`pkill -f laueportal`): the pattern also matches another checkout's stack and job processes, and the reconciler
   marks the run of a killed job process Failed.

## Hit every surface

The most common defect in this code is a change that works where it was tested and is missing elsewhere. Before you
call a change to a program or to a run's behaviour done, decide for each entry whether it applies, and list those
that applied in the pull request.

- Batch and live runs: `params.py` and `live_params.py`, `compute.py` and `live_compute.py`, `jobs.follower`.
- Each execution target: `local` (the job process); `aps` and `crux` (`remote.plan`, the launcher `remote.stage`,
  the runners `jobs.runner` and `jobs.mpi_runner`, and `remote.poller`).
- Every page that shows runs (the job page, the Run Monitor, the run lists, the scan page, the Status page, the Live
  page and both viewers), in the light and the dark theme.
- The API and the generated client: run `make gen` and commit `frontend/openapi.json` and `frontend/src/api/generated`
  with the change (`make check` fails on a stale client).
- Migrated runs, whose run directories can contain other files (per-depth files instead of `scan.h5`; a
  `history.json` and no `request.json`) and other values (a `nan` depth).
- The reverse operation: if you add a way in, add the way out and a way to see the state (every run that can be
  started can be stopped, and a queued run that a live session holds is shown as Held).
- The docs ([Docs](#docs)).

## Common mistakes

1. **A status or phase change outside `jobs.state`.** A job's status and phase change only through `jobs.state`, and
   reconciliation runs only through `jobs.reconciler`. When a concurrent stop or submission could invalidate a check,
   the check and the write that depends on it run in one transaction that starts with `core.db.begin_write`, with
   no slow I/O inside it.
2. **A path from a request used as given.** Every path from a request goes through
   `files.roots.resolve_inside_roots`, which refuses a path outside the roots or with a `..` component. Test a path
   from a request or a record with `files.roots.is_file_here` or `is_dir_here`, never with `Path.is_file`, which
   raises `PermissionError` under a folder that the API may not open.
3. **User text in a shell command, or a secret in a log.** Shell commands take only UUIDs, administrator settings
   and values that the portal derived from them, as separate quoted arguments (`remote.dm.launcher_command`).
   Credentials, `bdp.yaml` and Globus state are kept outside the repository and are never printed or logged.
4. **A gate passed by weakening a test.** Never weaken, skip or delete a test to make a gate pass; report the failure
   verbatim. A fix is done when a test of its failure case passes, and a performance claim needs a measured number.

## Build and test

Source `scripts/env.sh` in every shell of your own; the `make` targets that need it source it themselves. `make help`
lists the targets. `make setup` installs the conda env (conda-forge only), lauelab and `node_modules` into the
development workspace ([dev setup](docs/runbooks/dev-setup.md#the-workspace)); `make setup-backend` (the conda env, the
locked Python packages, lauelab, the backend and the dev config) and `make setup-frontend` (pnpm and `node_modules`)
install one part each, as CI's jobs do. `make dev-up` starts the dev stack: the API (which also serves the built SPA) on
port 2100, the worker, and Vite on 2101. Every development input is synthetic: the committed fixtures and the data that
`laueportal seed demo` generates from them.

The gates for a change are `make check` (every fast gate; run it before every merge), the e2e specs of the pages you
changed (`make e2e SPECS="e2e/scans.spec.ts"`, paths from `frontend/`) and the slow tests of the area you changed
(`make test-slow TESTS="tests/jobs/test_faults.py"`, paths from `backend/`). `make check` leaves out Vitest's slow set,
the heaviest page tests (`SLOW_TESTS` in `frontend/vite.config.ts`): run `make test-frontend-slow` when you change a
file they test (the Live, scan and create-run pages, `DataGrid`, the router, the depth view) or the frontend's lock. The
full `make e2e`, `make test-slow` and `make test-frontend-slow` run before a release. To run one test file, use
`cd backend && pytest -q tests/jobs/test_state.py` or `pnpm -C frontend exec vitest run src/lib/title.test.ts`. The test
conventions are in [backend.md](docs/contributing/backend.md#tests) and
[frontend.md](docs/contributing/frontend.md#tests).

On every push to `main` and every pull request, CI (`.github/workflows/ci.yml`) runs the parts of `make check` as
parallel jobs: `make check-hygiene`, `check-backend`, `test-jobs-remote`, `test-others` (the backend's fast tests in two
halves), `check-frontend` and `check-contract`; reproduce a failed job with its target. The manual workflow
`.github/workflows/full.yml` runs the full `make test-slow`, `make test-frontend-slow` and `make e2e`.

- Never print the output of `env` or `printenv`; check a process's environment for a variable's name only. Never copy a
  secret into another file, a log, a commit or a report.
- Dependencies have exact pins and hash locks (`requirements/*.lock`, `environment.lock`, the pnpm lock); lock again
  (`make lock`, `make lock-env`) only for an approved change. Use pnpm, never npm or yarn (it resolves only releases at
  least 7 days old); add shadcn components with `frontend/scripts/shadcn-add.sh`. A new dependency needs a reason and a
  maintainer's approval.
- Stop and ask a maintainer after three failed attempts at the same gate, and before you delete anything outside the
  repository and the workspace.

## Code layout

- `backend/src/laueportal/` has one package per domain (`scans`, `indexing`, `reconstruction`, `jobs`, `files`,
  `system`), the shared `core`, `science` (the only package that calls lauelab, apart from the programs' `compute.py`
  and `live_compute.py` in `indexing/lauego/`, `indexing/laue_matching/` and `reconstruction/wire/`), `remote` (remote execution and the launcher
  that runs on the DM host), `seed` (the synthetic demo data) and `util` (vendored helpers, not linted).
- `backend/migrations/` contains the Alembic revisions; `backend/tests/` contains the tests and their fixtures.
- `frontend/src/` contains `routes`, `features` (one folder per page area), `viz` (the Canvas2D and VTK.js views),
  `components` (`ui` contains shadcn's components, edited here), `api` (`generated` is Orval's output) and `lib`;
  `frontend/e2e/` contains the Playwright specs.
- `deploy/` contains the supervisor stacks and the settings and remote templates; `scripts/` contains what `make` runs.
- [`docs/architecture.md`](docs/architecture.md#layout) describes each package and folder.

## Docs

Change `README.md` or the core philosophy, principles and policies in `AGENTS.md` only when explicitly instructed.
Keep factual references in `AGENTS.md`, such as code layout, paths and commands, up to date when a relevant change
requires it. Explicitly note all edits to either file in the change summary and, when applicable, the pull request
description.

Operators deploy from the runbooks, and scientists read the user docs to decide whether to trust what the viewers show.
If a change alters what a user, an operator or a contributor can observe, change the docs in the same commit.

[`docs/architecture.md`](docs/architecture.md) describes the processes, the layout, the data flow, the job lifecycle,
live runs and where to add a program. [`docs/contributing/`](docs/contributing/) contains the backend and frontend
conventions and the [writing style](docs/contributing/writing-style.md) for docs, UI text, comments and messages.
[`docs/runbooks/`](docs/runbooks/) contains the procedures for development setup, deployment (with the migration of the
earlier portal's database), backups, remote execution and the live deployment test. [`docs/user/`](docs/user/) describes
the workflows, for scientists.

State what the code does in plain words: what is stored where, what a function returns, which unit. Comments explain
why the code is as it is, never its history. Every relative link must resolve (`make check` runs the link check). Write
commit messages in the imperative, one change per commit. A pull request lists the commands you ran and their results,
the tests you added, screenshots or Playwright traces for a UI change, numbers for a performance claim, and migration
notes.
