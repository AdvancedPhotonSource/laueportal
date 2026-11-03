# Backend conventions

The rules for the Python package `laueportal` in `backend/`: its layout, import rules, endpoints, paths, long work,
viewers, remote execution, jobs and live runs, parameters, the database and tests. [`AGENTS.md`](../../AGENTS.md) has
the rules for the whole repository; [the architecture](../architecture.md) shows how the parts fit together. Module
paths (`core/api.py`) are relative to the package, `backend/src/laueportal/`, and `tests/` to `backend/`; the other
paths are from the repository root.

## Layout

- domain packages `scans`, `indexing`, `reconstruction`, `jobs`, `files` and `system`, each with the modules it needs of
  `models.py` / `schemas.py` / `service.py` / `api.py` (all have `schemas.py` and `api.py`; `files` and `system` have no
  tables and no `service.py`);
- shared code in `core`;
- lauelab access in `science`; remote execution in `remote` and the runners in `jobs`;
- programs as plain modules: `indexing/lauego/{params,compute}.py`, `reconstruction/wire/{params,compute}.py`, and
  their live schemas and computes (`*/live_params.py`, `*/live_compute.py`);
- development datasets in `seed` (`laueportal seed demo`, all synthetic: the committed fixtures and
  `science.synthetic`); vendored helpers in `util` (excluded from ruff and pyright);
- how it fits together: [`docs/architecture.md`](../architecture.md).

## Import rules

import-linter checks these (its contracts are in `backend/pyproject.toml`; `make check` runs it):
- nothing imports `redis` or `rq` (the contract `no-redis-rq`; the job table is the queue);
- only `api.py` modules (including `core/api.py`, the shared dependencies), `app.py` and `core/errors.py` touch FastAPI;
- only `science`, the `*/compute.py` modules and the `*/live_compute.py` modules import lauelab;
- `jobs.contract`, `jobs.rundir`, `jobs.backends`, the remote runners (`jobs.runner`, `jobs.mpi_runner`,
  `jobs.processes`), `remote.paths`, `remote.observation`, the launcher-side `remote` modules (`settings`, `plan`,
  `transport`, `stage`, `dm`, `flow`, `pbs`, `auth`), `*/compute.py` and `*/live_compute.py` never import `core.db` or
  `core.settings`, nor `sqlalchemy`, `fastapi`, `starlette` or `pydantic_settings`, not even inside a function (the
  remote-execution seam; pydantic is allowed, for the `bdp.yaml` model);
- what a compute node loads (`jobs.contract`, `jobs.rundir`, the remote runners, `remote.paths`, `*/compute.py`,
  `*/live_compute.py`) never imports pydantic either: a compute node has only `requirements/compute.in`, lauelab and its
  conda-forge packages (the slow runner tests check it, `tests/remote_support.py`'s `COMPUTE_PACKAGES`). So the compute
  modules never import the parameter models (`wire.constants` contains what both need).

### Private lauelab names

The portal uses these lauelab names, which are not in lauelab's `__all__`. Each is listed with the tests that
fail when it changes; a lauelab upgrade checks each again, and a new one is added here with a test:
`indexing.ResultsWriter._write_geometry(self, target)`, overridden by `indexing.lauego.live_compute`'s
`_SessionResultsWriter`, and `lauelab._results_layout.write_dataset(target, path, values)`, which the override writes
with (`tests/indexing/test_live_lauego_compute.py`:
`::test_the_geometry_text_is_written_with_lauelabs_own_private_dataset_writer`,
`::test_the_shards_and_the_preview_embed_the_geometry_read_at_the_start_through_lauelabs_writer`);
`lauelab.reconstruct.scan.EXPECTED_POINT_ERRORS`, the errors that fail one point of a live wire run
(`tests/reconstruction/test_live_wire_compute.py`:
`::test_the_errors_that_fail_one_point_are_lauelabs_private_tuple_as_pinned`); `lauelab.reconstruct._scan_layout`
(`POINT_COLUMNS`, `PointStatus`, `RunStatus`, `RECONSTRUCTION`, `SCAN_FILENAME`, `SCAN_DATASETS`, `accumulator_dtype`,
`point_path`, `unsafe_stem`) and `._scan_writer` (`parameter_values`, `write_catalog`), the wire catalogs in
`science/recon.py` and `science/merge.py`, which the viewer and merge tests build through them
(`tests/recon_support.py`); and `lauelab.reconstruct.inspection`'s names in `science/recon.py` (a public module whose
names `lauelab.reconstruct` does not export).

## Endpoints

- plain `def` (synchronous), never `async def`, but for a route that waits its turn for a capped computation or for a
  dataset another request loads (Long work, below; ruff's ASYNC rules keep blocking calls out of it);
- always set `response_model`;
- errors are problem+json via `core.problems.ProblemError` (FastAPI-free, so services raise it too; `core.errors` has
  the handlers); a 422's and a validation's messages are plain words that do not repeat the field's label
  (`core.problems.field_message`: "Required", "Must be a whole number"; a form's summary adds the label);
- stable camelCase `operationId`s, because they become frontend hook names. The operationId **is the route's `name`**:
  `@router.get("/health", name="getHealth", summary="Health", response_model=Health)`. `create_app` rejects a missing
  (snake_case default), non-camelCase or duplicate name at startup. One HTTP method per route; set `summary` yourself
  (FastAPI would derive "Gethealth" from the name);
- one `APIRouter(tags=["<domain>"])` per domain `api.py`, listed in `app._routers()`; `app.py` adds the `/api/v1`
  prefix;
- get settings, the engine and DB sessions through `core.api` (`SettingsDep`, `EngineDep`, `SessionDep`). `SessionDep`
  commits when the endpoint returns and rolls back if it raises, so build the response model inside the endpoint; a
  route that waits takes `SessionsDep` instead (Long work);
- timestamps in schemas are `core.timeutil.UTCTimestamp` (aware, UTC, `Z`); columns are `core.timeutil.UTCDateTime`; get
  the time from `core.timeutil.utcnow()`; run-directory files keep the run-directory format's times: the remote runners
  write UTC with `+00:00` to the second, the local executor's `run.json` naive local time to the second
  (`core.timestamps.local_timestamp()`); the portal's other times (reports) go through
  `core.timestamps.utc_timestamp()`;
- every route runs the app's request guards first (`core.api.REQUEST_GUARDS`): a request that changes data needs
  `X-Laue-Portal: 1` from the portal's own origin (else a 403), and no text input may hold a NUL or a lone surrogate
  (else a 422; a form's text fields too). A problem never repeats a request's value whole (`core.text.quoted`;
  `core.problems.sendable` bounds every answer). `tests/test_api_sweep.py` checks every route of the OpenAPI document,
  so a new route is covered by itself (a header parameter, such as `Idempotency-Key`, for its size only: a header cannot
  carry a NUL or a lone surrogate); a `TestClient` that posts sends `tests.api_support.PORTAL_HEADERS`. The sweep sends
  JSON bodies only: a form route tests its own fields. Before any route, the app answers a `Host` outside
  `[app] allowed_hosts` with a 400 problem and logs the refused name, at most once per name every five minutes
  (`app.HostCheckMiddleware`, on Starlette's `TrustedHostMiddleware`) and puts `app.SECURITY_HEADERS` (CSP
  `frame-ancestors 'none'`, `X-Frame-Options`, `nosniff`) on every answer; a `TestClient` sends `Host: testserver`,
  which `make_settings` allows (`tests.settings_support.TEST_HOST`);
- a record a client edits whole includes the version of its stored values (`core.etag.value_tag`, no column, no code
  version): the save sends the version it edited and is checked under the write lock (`core.db.begin_write`); another is
  a 409 that leaves the record as it is. The scan catalog does (`ScanCatalog.version`, `scans.service.update_catalog`,
  `urn:laueportal:problem:catalog-changed`); its note append, which only appends, takes none;
- FastAPI receives a route's whole body (a multipart file spooled to disk) before any dependency runs, the guards
  included. A route that takes an upload (`File`/`Form` parameters, still a plain `def`) gets a route class that
  runs the write guard and refuses an over-long `Content-Length` before that (413; 411 without one), as
  `scans.api.ScanLogUploadRoute` does (`route_class_override`), and reads the file only up to its limit. Every
  other body is at most 1 MiB (`core.api.BodyLimitMiddleware`, `JSON_BODY_LIMIT`: a 413 by its `Content-Length`
  before it is read, or once a chunked body passes it), so a JSON route needs nothing of its own;

## No import-time side effects

- settings come from `core.settings.get_settings()` and are injected; a section the settings no longer have
  (`core.settings.RETIRED_SECTIONS`: `[redis]`) is ignored with one warning line, file and variables alike;
- logging is configured only by the CLI and app entry points;
- never create files or connect to anything at import.

## Paths

- every path that comes from a request goes through `files.roots.resolve_inside_roots`, which strips the text it checks:
  a caller that keeps the text refuses one with white space at its start or end (`jobs.validation.padded`). It refuses a
  `..` component, a path longer than 4,096 bytes or with a name longer than 255 bytes (`MAX_PATH_BYTES`,
  `MAX_NAME_BYTES`) and a path outside every root by its text (a root's text or real path) before any stat of it, and
  stops its resolution once the path walked passes 4,096 bytes (links that lead to a longer path: a 400, unavailable for
  a recorded path). A path named inside a run's outputs, mapped as `remote.paths` maps a run's paths (the geometry, the
  point sources and images, the detector candidates), passes `recorded=True`: still confined to the roots and the length
  limits, its `..` resolved, its checks not requested. That is not the job's own files (below), which are read as
  recorded and not confined. A remote run's mapped value is confined to its portal directory through the probe too
  (`remote.paths.portal_path` with `resolve=files.roots.recorded_real_path`). A configured path (a root, `scan_log_dir`,
  `runs_root`) is resolved with `files.roots.real_path`, never by `os.path.realpath` or `Path.resolve` on request;
- `Path.is_file`, `is_dir` and `exists` raise `PermissionError` under a folder the server may not open (Python
  3.12): test a path from a request or a record with `files.roots.is_file_here` / `is_dir_here`, or stat it and
  catch `OSError` (as `files.browser._existing` and `jobs.validation` do), never a 500;
- artifacts are served by ID;
- XML from a user (a scan log) is parsed through `scans.importer.parse_scan_log`, which refuses a DTD (no entity
  is expanded or fetched), never with `ET.fromstring` directly; its size is bounded before it is read; so is a
  calibration file named in a request that the api loads (the Live page's validation: lauelab's geometry and crystal
  loaders, `science.calibration`): refused unread over `MAX_CALIBRATION_BYTES` (1 MiB; real ones are a few KB),
  by the size of the stat that found it (`jobs.validation.check_file` returns it) and by the read itself (one
  descriptor, fstat'ed, at most the bound + 1 byte read; lauelab, which takes a path, loads a private copy of the
  bytes read, and the messages name the user's file), so a stat that understates a file or a file that grows after
  it is refused unread too;
- the create forms' file helpers (`files.finder`: `POST /api/v1/files/patterns`, `/files/indices`) take a folder
  through `files.browser.existing_folder` (as the browser does) and read at most `finder.MAX_FILES` HDF file names,
  in the folder reads' turn (Long work);
- a requested path that is missing on this host (e.g. a `/net/...` path on a host without that mount;
  `GET /files/browse`) is a 404 with the reason, never a 500;
- the files recorded on a job (its run directory, failure report and history) are read as recorded, and a missing or
  unreadable one is a state in a 200 (`jobs.schemas.FileState`), never a 404 or a 500 (`docs/architecture.md`, Jobs and
  run directories, step 6); so are both viewers' run directories, a state of their artifact (`not_on_host`: absent or
  unreadable, apart from `empty`: there with nothing to read);
- a stat, a listing or an open of a recorded, mapped or requested path on request (`/net` mounts can hang in the kernel)
  goes through `files.probe.bounded(path, check)`, keyed by the path the check looks at (one `bounded` per path, never a
  folder's key for files elsewhere; a key is where a check blocks only when no link or `..` leads the path elsewhere, so
  resolve a path with `files.roots.real_path`, which stats one mount per check and follows a link's target under its own
  key): the check runs on the probe's own daemon threads (`[files] probe_threads`, 8: the most that can ever be stuck)
  and the request waits at most `probe_timeout_seconds` (5) for it, then gets `NotResponding` (a 503 problem
  `urn:laueportal:problem:not-responding`, `Retry-After` the `hung_mount_seconds`; or the state `not_responding` where
  an answer reports one: `FileState`, the artifacts, `OutputPathState`, a validation's error). The mount (by
  `/proc/self/mountinfo`, `/net/<host>` under autofs; a root-file-system path by its first two components, never `/`) is
  then remembered while its check is stuck and at least `hung_mount_seconds`, so later checks of it answer at once,
  without a thread. One mount has at most two checks in flight (`CHECKS_PER_MOUNT`), so a hung mount blocks two threads
  and the others stay free; once every thread is stuck, a check answers at once. A check of a path a request gave passes
  `requested=True`: those share `request_threads` (the threads less `RECORDED_THREADS`, 2), so the recorded paths'
  checks always keep two threads. So a request waits at most one timeout per distinct hung mount it looks at, and a
  points list on one hung mount one timeout in all; a path's resolution looks at the mounts of its components and of
  every root (the roots are resolved again on each resolution; one that does not answer is taken at its last real path,
  and contains no path if it never answered), and a listing waits one timeout in all for its links and mount points
  (`files.browser._EntryChecks`: the entries that do not answer are `available: false`). So the file browser, Find
  Matching Files and Find Indices, form validation, run creation, a scan's catalog and the scan-log import check a
  requested path first, then list or read it in the request's thread as before. The checks are bounded, not the reads
  after them (a catalog, a frame, a dataset, a JSON file, a large listing): those follow a check that just answered, and
  the run files the request reads next are opened in the check (`files.probe.open_check`; on NFS an open asks the
  server). Known limit, unverified on NFS here: the attribute cache can still answer a stat while the read after it
  hangs (a hung read holds its request's thread, and an h5py read holds h5py's process-wide lock, so every HDF5 read in
  the API waits; restart the API once the mount is back). Keep h5py's work out of a check (validate after it), so its
  lock cannot make a mount look hung. The API configures the probe at startup (`create_app`:
  `files.probe.configure(settings.files)`); the CLI and the worker run checks in their own thread, unbounded
  (`configure(bound=False)`; a slow but healthy mount must not fail a conversion). Tests hang a path with
  `tests.probe_support` (a stat, listing or open that blocks on an event) and install a probe of their own;

## Long work

- anything slow (compute, conversion) goes to the worker: the job table is the queue (a local run is queued by
  committing its phase `queued`), and the worker's dispatcher (`jobs.dispatch`) claims it and runs it in a fresh job
  process (`python -m laueportal.jobs.process`, a session of its own; never a fork; its stdout and stderr
  `execution.job_log` when set, `dispatch.JobLog`, so an orphan still logs), in the order of `jobs.order`'s queries,
  which the status page's queue counts, live slots in use and next items share (`jobs.order`, like `jobs.liveness`, the
  process checks, imports neither the worker nor the run lifecycle, so an `api.py` may reach it: import contract 4). It
  has one batch slot and two live slots; while a live run is Queued or Running no batch run or conversion is claimed
  (`claim_run` and `claim_conversion` check it in their transaction), the batch slot's run is stopped "stopped for a
  live run" (Cancelled) and its conversion killed (`failed`), and the live runs start once its process has exited, each
  with its cores (`dispatch.live_cores`, `--cores`, `RunRequest.cores`);
- `execute_run` runs only in a job process, on a run the dispatcher claimed (Running);
- a conversion is not a run: a row of the `conversion` table (`indexing.conversion`, `convert_results`, ID
  `convert_results_<indexing ID>`) and no `job` row, so it never changes a run's status or phase; the dispatcher takes
  queued conversions before any queued run, the oldest request first, with a timeout of their own
  (`CONVERSION_TIMEOUT_SECONDS`, one hour), and `GET /indexing-runs/{id}/artifact` reports the row.
- a slow computation a request runs itself (lauelab's simulation in the detector overlays; the wire viewer's depth
  products: a point's trace, reference images and ROI traces, which read all its frames) runs a bounded number at once,
  behind an `anyio.CapacityLimiter` the app holds (`app.state.simulations`, `indexing.detector.SIMULATIONS_AT_ONCE`, 1:
  lauelab's pure-Python compute holds the GIL, so more at once only slows each; `app.state.point_computations`,
  `reconstruction.viewer.COMPUTATIONS_AT_ONCE`, 2: h5py reads one file at a time, so more would mostly add memory), and
  is waited for outside the thread pool: the route is `async def`, waits in `core.api.turn` and runs its blocking work
  with `anyio.to_thread.run_sync`. At most a few requests wait (`turn(waiting=...)`; `detector.SIMULATIONS_WAITING` and
  `viewer.COMPUTATIONS_WAITING`, 4): the next is a 503 problem with `Retry-After` that the page shows. A waiter whose
  client goes leaves the queue (`request.is_disconnected()`, polled), and one whose client has gone by its turn is
  dropped unrun (a 499, `core.api.CLIENT_CLOSED_REQUEST`). The result is kept with its dataset (`DatasetCache.derived`,
  keyed on what it depends on: the step and the window, not the scope), or in the wire viewer's `ProductCache` (by the
  point file's identity), so a request for one being made waits for it, a revisited one reuses it, and a request for a
  kept one takes no turn. Never a threading semaphore in a sync route: each waiter would hold one of the pool's 40
  threads, and a full pool blocks every sync route. A depth frame (one read, about 40 ms, but 8-16 MiB and its copy as
  bytes) takes a turn too, kept or not (`app.state.frame_reads`, `viewer.FRAMES_AT_ONCE`, 6; `FRAMES_WAITING`, 34), so a
  browser's abandoned frames leave the queue unread and a storm of them never takes the pool; the indexing viewer's
  detector image takes one of its own the same way (`app.state.detector_images`, `indexing.detector.IMAGES_AT_ONCE`, 6;
  `IMAGES_WAITING`, 34); so does every folder read, in one shared turn (`core.api.read_folders`:
  `app.state.folder_reads`, `core.api.FOLDER_READS_AT_ONCE`, 4; `FOLDER_READS_WAITING`, 16): the file browser's listing
  (`GET /files/browse`), Find Matching Files and Find Indices (`POST /files/patterns`, `/files/indices`) and the run
  forms' validation and creation (`POST /{indexing,reconstruction}-runs/validate`, `/{indexing,reconstruction}-runs`),
  which list the input folder, and the Live page's (`POST /live-runs/validate`, `/live-runs`: the folder, the
  calibration files, and Start's DAQ bind check); a new route that lists or reads a requested folder takes it too.
  A creation takes its turn before it claims its `Idempotency-Key`, so its 503 can be sent again with the same key. The
  turn bounds the frames read and copied at once, not the bodies held: the turn ends when the route returns its
  `Response`, and the body is then gzipped (Starlette's own limiter of 40 threads) and sent. Measured on R2 with a 512
  MiB cache: 780 MiB resident at the peak with 40 frames in flight, 922 MiB with 80.
- the process `laueportal serve` runs fixes glibc's mmap threshold at 1 MiB (`core.memory.return_freed_buffers`,
  called by `app.serve_app`; `MALLOC_MMAP_THRESHOLD_` in its environment takes precedence, and another C library is left
  alone): freed frame buffers go back to the system instead of staying in the threads' arenas (the API at 40
  concurrent frames on the measured sample: 1.4 GiB resident, 1.3 GiB at idle, without it). Start the API with
  `laueportal serve` (or `app.serve_app`), never `create_app` in a server of its own. It is one process (the
  caches, turns and probe are its own; `workers=1`); without `--reload` (production, e2e) it refuses to start
  (exit 2) unless `app.static_dir` contains the SPA's `index.html` (`docs/runbooks/deploy.md`).
- a request that waits holds no database connection: before it waits for a dataset load, a result another request is
  making, or a capped computation's turn, it reads what it needs in a session of its own and closes it
  (`core.api.SessionsDep` with `core.db.session_scope`, as `indexing.viewer.viewer_source` does), never through
  `SessionDep`, whose connection stays checked out until the route returns; so does a route that touches a run's files
  (a hung `/net` mount: the artifacts, select-xml, convert, the job page, its log and failures;
  `indexing.viewer.read_run`, `jobs.summary.run_record`). The pool has 15 connections (SQLAlchemy's 5 plus 10 overflow,
  30 s timeout): sixteen waiters holding theirs made every other route answer 500.
  `tests/indexing/test_viewer_detector.py` checks it on a one-connection pool; the wire viewer's depth frames follow the
  same rule (`tests/reconstruction/test_viewer_images.py`: a point's computation, a request waiting for it in
  `science.recon.ProductCache`, one waiting for a turn and a frame waiting for one).

## Results viewer

The results viewer (`indexing.viewer`, `indexing.products`, `indexing/api.py`): every product endpoint that selects
records takes `ScopeDep` (lauelab's `DataScope`, the viewer's scope bar; not the legend or the selection stats) and
reads its dataset through `viewer.viewer_source` (in the thread pool) then `await viewer.load_viewer_dataset` with
`DatasetCacheDep` (the app's `science.cache.DatasetCache`, `indexing.dataset_cache_size`), never by a path from the
request: the product routes are `async`, so a request waiting for a dataset another request loads waits in the event
loop, holding no thread (`DatasetCache.load`, `science.once.Pending.wait_async`), and computes its product with
`anyio.to_thread.run_sync`; `viewer_source` validates the run's results file each time (its run directory looked at
through `files.probe`), and a valid one's summary is kept by its identity (`science.results.validate_results`: about 30
ms of h5py under the GIL per request, which 80 detector images in flight turned into a starved thread pool; the identity
includes the change time, so a file rewritten in place with its size and modification time kept is validated again).
lauelab computes each product (`science.products`); one lauelab cannot make is `products.ProductFailed` (422,
`urn:laueportal:problem:product-failed`). A GET product or the summary is tagged and revalidated before its dataset is
loaded: `core.api.revalidated(request, response, products.tag(source, name, scope, params))`, and a binary `Response`
gets `core.etag.headers(tag)` (no `max-age`). A cache key or a tag has the identity (real path, size, modification and
change times: `science.cache.file_identity`) of every file the output reads, and the tag the code's
(`core.etag.versions`). The artifact is tagged by its content after it is resolved (`viewer.artifact_tag`; its
`results_version` the results file's identity, `viewer.results_version`, and `live_open` an open live run's queue
identity). The detector image and overlays (`indexing.detector`) read only a file whose path is in the loaded dataset
(`inputImage`), so they load the dataset (cached) first and tag with that file's identity (`detector.image_tag`); every
candidate image path is mapped and confined before it is looked at (`detector.step_image`), and one not on this host is
a 404 with the reason. The peaks and patterns grids (`indexing.tables`) take the scope inside their grid parameters
(`schemas.ResultsGridParams`: `GridParams` admits no other query parameter), page with the pyarrow translator
(`core.arrow_paging.fetch_table_page`, `rows=` the scope's) over a table made once per dataset and kept with it
(`DatasetCache.derived`), and are tagged like the products, not `no-store`. The cache loads per key, at most
`science.cache.MAX_LOADS` (2) files at once and `MAX_XML_LOADS` (1) Output XML (a 344 MB Output XML peaks at 2.6 GB
while it loads, its `output.h5` at about 270 MB): never hold its lock during a load. A loaded dataset of that size uses
about 540 MB with its tables (the dev API: 1.07 GB RSS with one), which `indexing.dataset_cache_size` (6 by default)
multiplies.

## Wire viewer

The wire viewer (`reconstruction.viewer`, `reconstruction/api.py`; the readers in `science.recon`): the run directory is
the run's `output_path`, read as recorded; `reconstruction.views.find_artifact` finds the scan catalog or a migrated
run's per-depth files and whether the directory is on this host, through `files.probe` (`viewer.locate`). The artifact
and points routes read the run through `viewer.run_files` (`SessionsDep`, a session closed before any file is touched)
and then the files; every `science.recon` reader takes the app's cache as `cache=` (`ProductCacheDep`:
`app.state.reconstruction_products`, a `ProductCache` of `reconstruction.product_cache_mib`; never a module-level
cache), keyed by the catalog's or the point file's identity; a catalog's new snapshot (a per-depth folder's new listing)
replaces its old one there (`slot=`; one snapshot per watched live run), and so does a point's reader or
product kept under a new point-file or folder identity (`recon._product_slot`: the catalog or folder, the point and the
product). The artifact is tagged before it is read (`viewer.artifact_tag`: the run's status and the artifact's
identity), the points by their content (`viewer.points_tag`: their sources' states depend on other files). A point's
source is mapped with `indexing.viewer.mapped_path` and the run's plan read once; an unreadable catalog is a 409
(`urn:laueportal:problem:reconstruction-unreadable`), no artifact a state (`writing`, `empty`, `not_on_host`,
`not_responding`). A point's images (its depth frames, trace, reference images and ROI traces) go through
`viewer.point_images`: the point found in the cached table (unknown: 404; not complete: 409 with the reason), every
point file admitted before anything opens or stats it (the readers' `admit=`, `viewer._admission`: mapped with the run's
plan, inside the roots and a file here; else a 404 with the reason), then the readers with `cache=` and `admit=`. The
cache computes each product once per key (others wait for it; `science.once`, shared with `DatasetCache`: a failure
leaves no key computing, and each waiter raises its own copy of the error), and a frame, the trace and a reference are
tagged by the point file's identity before they are read (`viewer.images_tag`: a finished point's never changes, a
rewritten one's does); a per-depth folder's listing is kept by the folder's identity. The trace, reference and ROI-trace
routes take a turn (Long work) unless what they read is kept (`viewer.trace_kept`, `reference_kept`, `rois_kept`), and
then read it only if it still is (`kept_only=True`: one evicted since is `recon.NotKept`, and the route takes a turn
after all); the ROI traces not kept are summed together in one pass over the frames, never reading more than one box
holding them per depth (`science.recon.roi_traces`, lauelab's values exactly), and each is kept by its bounds.

## Remote execution

Remote execution (`remote/`, the DM/Globus/PBS integration; the runbook is
[`docs/runbooks/remote-execution.md`](../runbooks/remote-execution.md)):
- what crosses the portal/remote boundary (`request.json`, `inputs.jsonl`, `bdp.json`,
  the DM inbox copy, `progress.json`, `failures.jsonl`, `run.json`, the returned directory and its publication)
  stays byte-compatible with the deployed launchers (`tests/fixtures/remote_goldens/`, the committed goldens); the
  DM workflow names and the `bdp.yaml` keys stay too;
- the launcher side runs on the DM host from `bdp.yaml` alone (`remote.settings.load_settings`), the compute
  side from the control directory alone: neither may import the database, the portal settings or the web layer, and
  the launcher loads only packages of `requirements/launcher.lock` (`tests/jobs/test_alcf_seam.py`);
- the portal reads `bdp.yaml` through `remote.settings.portal_settings(settings.remote, runs_root=...)` (`None` when
  disabled); jobs reach DM only through `jobs.queue.enqueue_run` (or `enqueue_indexing`, `enqueue_reconstruction`),
  given those settings as `remote=` → `remote.submission.submit_run`, which records the submission identity before
  contacting DM and never submits a job twice;
- the execution targets are defined once, in `jobs.backends` (`BACKENDS`, `REMOTE_BACKENDS`, `is_remote`);
- a check and the write it guards that a concurrent stop or submission could invalidate run in one transaction
  that starts with `core.db.begin_write` (as `jobs.controls.cancel_run` and `submit_run` do), without slow I/O;
- remote runs are never the worker's dispatcher's; the remote poller (a thread of the worker,
  `remote.poller.RemotePoller`, only with `[remote] enabled`; `laueportal remote poll --once` for one pass) reconciles
  them, through `jobs/state.py`'s existing transitions; the worker's reconciliation skips them, but
  for one that never reached its submission (`created` or `published`, no `submission_id`), which it fails
  after `execution.creation_timeout_seconds` like a local one (counted from its last change: its plan's build
  records progress every minute, `remote.submission.plan_progress`, so a slow mount does not fail it);
- a remote run whose plan (`bdp.json`) is missing or cannot be read (not a JSON object, its read failing) while its runs
  root answers is skipped, left out of the pass's DM call, logged once per reason, and ended Failed after
  `poller.MISSING_PLAN_SECONDS` (600; `poller.MissingPlans`, the thread's own count); the runs root not answering never
  counts (`tests/remote/test_poller.py`). A per-run condition the poller cannot act on is logged once per change
  (`poller.LoggedOnce`), never a traceback each pass;
- the poller makes one DM call per pass for every active remote run (`DMClient.fetch`, the API script's `fetch`
  operation, one subprocess; a record DM cannot give, or one that is not an object, is reported per record) and none
  while no remote run is Queued or Running; a new per-run DM need goes into that call, never a call per run. Every
  background rate runs only while it is needed, with a test per "Only while" (`tests/remote/test_poller_thread.py`); DM
  calls in tests go through `tests.remote_support.FakeDMService` (the real client, processes counted, no DM) or a fake
  client with `BatchedFake`;
- a `crux` run's remote observation (`remoteObservation`, the launcher's printed line that DM keeps) is in DM's
  record of that call: `remote.observation.accept` checks it (and refuses what SQLite's JSON functions cannot read,
  `observation.unreadable`), and the poller keeps it in `job.remote_observation` (revision `0008`), in its one write of
  the job, while the job is active; a refused one is logged once per job and reason (`poller.RefusedObservations`) and
  dropped, never failing a run, and `aps` runs have none. Before it publishes a returned `crux` run the poller writes
  the Downlink "Publishing results" (`remote.observation.publishing`), and checks no record's observation while that one
  is stored. The job API (`remote.observation`) and the run monitor (the row's `observation` for its Status cell; no
  Remote Phase column or `remote_phase` field) show it only for a `crux` run that is queued or running
  (`jobs.service.observation_view`, one builder for both). Its phases are the launcher's (`PHASE_LABELS`: the four and
  Login needed, `login_required`). Goldens: `tests/fixtures/observation_goldens/` (`goldens.json`, captured once with
  the clock fixed; its cases in `cases.py`). The launcher prints it (`remote.stage.observe`): Login needed while the
  Flow waits for a Globus sign-in (`login_required`: only the flag kept, never the authorization parameters; no listing;
  not inherited once resumed), else one Transfer listing of the run's Eagle `results/` only while the Flow is in
  `RunPbsJob`, at most every `targets.crux.listing.interval_seconds` (doubling up to 60 s while it fails), the whole
  step (client, token renewal, request) bounded by `timeout_seconds` with no retry and no login (`LoginRefused`), its
  time saved first; a failed or slow listing is stale, never a failed stage, but one that shows `run.json` is not stale
  by the heartbeats' age; never the detail "Waiting for status" (the portal's "At ALCF"). The Flow status call
  (`remote.stage.flow_status`) is one Flows request with no retry (`_flows_client`: `RetryConfig(max_retries=0)`, no
  login), the whole step (client, renewal, request) bounded by `STATUS_TIMEOUT_SECONDS` (10). Both bounded steps'
  clients get an HTTP timeout 5 s past the bound (`stage.request_timeout`; equal, they raced), so no answer is the
  bound's `TimeoutError`. One that fails prints the kept observation stale (none kept: Queued "Workflow status
  unavailable"), the next call 10 s later, doubling up to 60 s (`status_wait` and the other `stage.STATUS_FAILURE_KEYS`
  in the state file, reset by an answer; no client built while waiting), one stderr line per change; it re-raises,
  failing the stage as before, only once the calls have failed for longer than
  `targets.crux.status_failure_budget_seconds` (600), or at once for `LoginRefused` or a client that cannot be built.
  `main` sets the umask 077 before any write (every state file 0600). The `permissions` stage (`03-PERMISSIONS`,
  `remote.stage.restore_permissions`) makes one DM call (`DMClient.restore_permissions`, bounded by
  `dm.permissions_timeout_seconds`) on exactly a `crux` run's `<run>/incoming/<submission>` once its Flow succeeded, its
  checks first; any failure fails it. Tests: `tests/remote/test_stage_monitor.py`, with a fake Transfer client listing
  the Eagle stand-in;
- every process the poller starts (a DM call, the launcher's command run directly) goes through its client's
  `DMClient.run` (`own_session`): a session of its own, killed whole (the shell, Python, an SSH bridge) at its timeout
  and at the worker's end (`DMClient.abort`, from `RemotePoller.close`, before the poller lock goes), and, with a
  timeout (`dm.timeout_seconds` set), under coreutils `timeout` (`remote.dm.SELF_BOUND`, the timeout plus 5 s) so that
  it ends by itself after a worker killed outright (uutils' `timeout` sends its SIGKILL to the call's first process
  alone, GNU's to the whole call: `SELF_BOUND`'s note); never a bare `subprocess.run` that could outlive the worker; the
  API's submission keeps its call in the API's process group, which the API program's stop (`stopasgroup`) reaches;
- what a Finished, published remote run leaves behind is removed by the poller's listing, at its first pass and then at
  most every ten minutes while remote runs are active or one ended since (`remote.cleanup`, from the DM inbox's plan
  copies): the return inbox's remnants, and, with the optional `dm.cleanup_workflow` (unset by default: it would clean
  migrated runs too), the launcher's `cleanup` stage (a `crux` run's Eagle folder, the state file), then the inbox copy.
  Only when the job is Finished in the database and its published `run.json` contains the status "Finished" (the stage
  checks `run.json` too): nothing of a Failed or Cancelled run is removed (an MPI run's ranks' results exist only in its
  Eagle folder's `shards/`), nor of a run that was not published or a submission this database does not know;
- paths a remote run wrote that publication leaves remote are mapped when read:
  `remote.paths.portal_path(value, remote.paths.run_path_map(run_directory))`, with the map of the portal's own
  plan (`bdp.json`), never one from a returned file; `None` (outside the run directory or the APS root, no
  prefix, or an unusable plan) means unavailable, and a mapped path is opened only after
  `files.roots.resolve_inside_roots` (`jobs.summary.failure_records` does this for `failures.jsonl`'s sources,
  `indexing.viewer.map_output_path` for the viewer's: lauelab never resolves a geometry path in an output,
  `science.cache.load_dataset`; `reconstruction.viewer.points` for a scan catalog's point sources);
- shell commands (the SSH bridge, the DM API, `qsub`/`qstat`/`qdel`, the launcher command, the PBS script) take only
  submission UUIDs (checked with `UUID(...)`), administrator settings and what the portal or the launcher derived from
  them (the plan's paths, a PBS job ID), never user text, as separate arguments, quoted with `shlex` (`shlex.quote`,
  `shlex.join`) or base64-encoded: follow `remote.dm.launcher_command` and `remote.transport.shell_command`, which runs
  `bash -c` and never a login shell (for DM's API and the launcher's command: DM's setup file gives DM's environment; a
  login profile's banner would corrupt the JSON a DM call prints). Credentials, `bdp.yaml` and Gladier/Globus state stay
  outside the repository (`.gitignore` lists them) and are never printed or logged; settings errors name keys, never
  values;
- tests use fakes for DM, Globus and PBS, never the network.

## Job status and phase changes

Job status and phase changes go only through `jobs/state.py`. `state.initialize_registered` (a run computed
elsewhere, created Finished) is for `laueportal.seed` only (`tests/jobs/test_state.py` enforces it).

## Live runs

Live runs (`docs/architecture.md`, Live runs) are ordinary local runs and jobs, no new status, phase or column: a run is
live when its `queue_job_id` is exactly `live_<its job id>` and its run directory contains `live.json`
(`jobs.live.live_marker` in SQL is enough for Queued and Running rows, `is_live_run` for any row). `arrivals.jsonl` has
one writer, `jobs.arrivals`' `ArrivalWriter.admit` (every source: the catalog thread, `laueportal live notify`; a
follower's thread through `admit_point`), under the `flock` on `arrivals.lock` (each acquisition a read-write descriptor
of its own, as NFS needs; never share one), the stop flag read inside it; `notify` (a process per file) looks its path
up in the run's admission index (`jobs.arrival_index`, SQLite beside the database, the ledger its source of truth),
never reading the whole ledger under the lock (a test at 100,000 lines bounds the lines read), and a file it refuses
for having no number (no template, `jobs.arrivals.NumberMissing`) is the open run's `arrival-status.json` line, the
catalog thread's keys, written only when it changes (`record_number_missing`); the acquisition hook's
SSH key runs notify alone, through the forced command `jobs.notify_hook` (standard library only: exactly
`laueportal live notify <job id> <path> --complete`, its arguments checked, then `os.execve` with no shell and an
environment of its own; deploy.md); the executor reads it from its offset (whole lines only; at most every
`live.arrivals_seconds`, a stat first, a read only when it changed), counts each batch with `state.record_arrivals` (it
only raises a Running run's `n_inputs`) before the compute gets it, seals on the stop flag and applies the live end rule
(`executor._decide_live`). A live run has no time limit: the job process arms no alarm and the dispatcher kills none. A
live compute is registered with `contract.register_live_compute_function` and takes `(request, hooks)`, and sizes its
pools from `request.cores` (the dispatcher's share) and publishes at most every `request.publish_seconds`
(`live.publish_seconds`); `hooks.shutting_down()` is true at a service stop (start nothing more), false at the seal
(drain every input handed out). The live wire compute is `reconstruction.wire.live_compute` (its module docstring:
`PreparedScan` from `prepare_scan([])`, the geometry read once; `add` per arrival; `reconstruct_point` in a spawned pool
of `cores // num_threads`; `scan.h5` republished at most every `publish_seconds` and only after a point was recorded;
`completed.jsonl`'s lines after each publication, from the scan's outcomes, `rundir.CompletedPoint`; `finish()` at the
seal, `close()` on a run-level error, its workers ended first and its running points `interrupted`; tests
`tests/reconstruction/test_live_wire_compute.py`, through a real worker (the service stop and the run-level errors'
points in-process), and the live-against-batch parity in `tests/test_live_parity.py`). The live LaueGo compute is
`indexing.lauego.live_compute` (its module docstring), on raw frames or following a live wire run (below): the
calibrations read once (`Calibration.read`: the pool's indexers built from what was read, and the results files embed
that geometry text, `_SessionResultsWriter`); a spawned pool of `cores` workers, each building its indexer at its first
frame; `Indexer.index(..., keep_image=False)` per arrival; frames in arrival order into `shards/batch_<n>/output.h5`;
the preview `output.h5` merged and replaced at most every `publish_seconds` and no sooner than 9 times the last merge's
seconds after it, only after a new shard, each merge in a thread of its own (the hooks and the run's state stay the
loop's thread's); the Output XML stitched at the seal; a run-level error ends the workers first
(`jobs.processes.end_pool_workers`, as the wire's), takes the outcomes in by then, marks the running frames
`interrupted`, publishes the frames indexed; tests `tests/indexing/test_live_lauego_compute.py` (its fast ones run the
loop with threads for workers and stand-ins for the worker's task and the session's indexer) and the parity in
`tests/test_live_parity.py`. A follower (a live LaueGo run whose source is a live wire run): its job process runs
`jobs.follower`'s thread (`execute_run` starts it, as the catalog's for a DAQ run), which every `live.follow_seconds`,
only while its run is open, asks the wire run's job row whether it has ended, then stats the wire run's
`completed.jsonl` and reads it only when it changed, and admits each point in that file's order (`admit_point`: refused
for good, a point outside the wire run's directory by its text or its real path, an ID not its file's stem or another
file's, more than `MAX_POINT_DEPTHS`); it never writes the wire run's directory. Once the wire run has ended it reads
the file to its end and is done, and `LiveExecutorHooks.arrivals` seals the follower (as a Stop; its error, a line that
is not a point or a point refused, is the run's); its own `arrival-status.json` has its last check, the points read
and, while it waits for an unreadable point, why (the catalog's keys; [deploy.md](../runbooks/deploy.md) §8): written
when that reason changes, else at most every `live.publish_seconds` while it follows, and at its end with no reason
(`Follower.finish`, again at the thread's close when that write failed), so an ended follower's file has no reason. The
wire run writes a failed append's `completed.jsonl` lines again first, as they were, so a reader's offset stays right.
Its inputs are each point's selected depths, each a `ScanFrame` indexed in the same pool (never `iter_index`, whose
workers read the geometry's path), into `shards/<point_id>/output.h5`; tests `tests/indexing/test_live_follower.py` (a
linked pair through a real worker, its job processes under `tests/jobs/follower_probe.py`),
`tests/jobs/test_follower.py` and the linked pair's parity in `tests/test_live_parity.py` (indexable stacks:
`tests.wire_support.write_laue_wire_scan`). Both live computes check an input's identity as admitted
(`ManifestEntry.identity`, `rundir.unchanged_since_admission`) before they process it (a changed file is a failed
input), and a worker's own error is its input's, which failed (a lost worker's is no input's). While an input's read
hangs the run stays Running (no live time limit): [`docs/runbooks/deploy.md`](../runbooks/deploy.md) §8. A live run's
hooks write the progress a burst left pending at each look at the arrivals (`ProgressRecorder.flush_pending`). The
tests' stand-in is `tests.jobs.job_computes.live_compute`. The dispatcher's live rules (`jobs.dispatch`;
architecture.md, The worker): two live slots, each kind's orphans counted in its own; no batch item claimed while a live
run is open; a batch item stopped for one is ended by the dispatcher only once its killed process is gone, and stays the
worker's own for the reconciler until then. One live session: `jobs.live.check_one_live_session` (`LiveSessionOpen`; it
sees only queued live runs, so the Live page's Start, `jobs.live_session.start`, holds ONE lock, `app.state.live_start`,
across it, the creations and the queueing, and `jobs.creation.create_live_session` creates a session's runs, then queues
them together in one transaction, `jobs.queue.enqueue_live_runs`: a wire run is never claimed alone with the whole
share; keep both rules for any new way to start a session; the lock is one api process's, so `enqueue_live_runs` refuses
again in its write transaction while another live run is open: the database allows one session whatever starts it);
Move to Front of a live run is `MoveState.LIVE` (409). Stop now (`POST /jobs/{id}/stop-now`, `controls.stop_run_now`): a
live run not stopped yet is stopped as Stop does; one Running whose stop was requested gets `state.STOP_NOW_REQUESTED`
(a message line: no new status, phase or column), and the dispatcher kills its job process's group
(`Dispatcher._stop_now`: its child, or the recorded orphan alive here) and only once it is gone ends it
Failed/interrupted (`end_stopped_now`, `STOPPED_NOW`), holding it in `running_items` meanwhile, so the reconciler never
finalizes it while it is alive (tests: `tests/jobs/test_live_dispatch.py`, and through a real worker with
`job_computes.LIVE_HANG`, an input stuck past the stop, `tests/jobs/test_live_runs.py`). A queued batch run or
conversion behind an open live session is `held` (`JobSummary`, `JobDetail`, `ConversionJob`;
`jobs.live.live_session_open`). The Live page's API (`jobs.live_session`, routes in `jobs/api.py`) is present while
`[live] enabled` (`jobs.api.require_live`, a 404 otherwise; the routes stay in the contract): `GET /live/daqs` (the
app's `DaqList`, `app.state.daq_list`, awaited: no thread held, no DM call unless asked; `kept_seconds`, the time the
answer is still served, so the page asks no sooner; a failed answer a 503 `urn:laueportal:problem:dm-unavailable`;
less a DAQ whose option, its experiment and its data directory's last two names (`live_session.shown_names`), holds
one of `live.daq_name_exclude` (`live_session.excluded`; no blank entry), which the kept answer, Validate and Start
still take),
`GET /live/session` (`jobs.live_status`: the latest session, the newest live run by its queue identity and a follower's
wire run first, an ended row only with `live.json` in its run directory, `is_live_run`'s rule, looked at once the rows
are read; each run's status and counters from its job row and its `arrival-status.json`, and `stopped_now` (phase
`interrupted` and `state.stop_now_requested(job.messages)`: ended by its Stop now, `dispatch.end_stopped_now`),
`stopped` (the source run's stop flag, kept once it has ended: the page then shows no session unless a run Failed
other than by its Stop now), and the Scan card as recorded,
from the source run's `live.json` and rows; rows read in a session closed before any file, each file through the probe,
`jobs.summary.read_json_file`; the job page's `JobDetail.live` the same way: `live_status.live_row` in the job's
session, then `job_live`: `live.json`, `arrivals.jsonl`'s whole lines (`jobs.summary.count_arrivals`: the last whole
line's `n` + 1, from a bounded tail), and `arrival-status.json`), `POST /live-runs/validate` and `POST /live-runs` (its
`Idempotency-Key` claimed first, route `createLiveRuns`; a 409 `urn:laueportal:problem:live-session-open` naming the
open run), both in the folder-read turn, in a worker thread (Start's bind check, its one DM call, off the event loop;
Validate never asks DM: it checks a DAQ against the DAQ list's kept answer, `DaqList.last_seen`). The form is the Scan
card and the program parts (`jobs.live_session`'s docstring); its issues' `loc` start with their part, a run's issue on
a field the Scan card sets reported once on the Scan card's. Tests: `tests/jobs/test_live_api.py` (the race through real
concurrent requests, the Both rule through the real dispatcher). The DAQ catalog: each DM operation one call of
`remote.dm.DMClient` (`daqs`, `daq`, `catalog`) with the `[live]` settings' client (`jobs.daq.live_client`; no
`dm_setup_file`, no call); `jobs.daq.DaqList` (the Live page's list: one shared answer, at most one call per
`live.daq_list_seconds`, none unless asked) and `jobs.daq.bind_daq` (the bind check at a live create); `jobs.catalog`,
the catalog thread of a `daq` run's job process (`execute_run` starts it, none for `notify` or a follower; one DM call
per cycle at the background rates, every page since its cursor (a provisional one while its run has no row), one request
at a time, within `catalog_max_rows`; it admits through `admit` only; errors in `arrival-status.json`, never the run's),
closed before the seal (`LiveExecutorHooks.before_seal`), so no DM call follows it. Its tests drive the real client and
script: `FakeDMService` (`daqs`, `files`; `requests()`, the DM requests inside each call; `delay`, `start_delay`, a slow
DM; `workflow=False`, no workflow API) in-process, `tests.remote_support.FakeDMInstall` (DM's setup file and the
stand-in API installed, its calls logged by pid in the stand-in `dm` package, which every operation imports) for a job
process or a worker.

## Reconciliation and the dispatcher's claim

Reconciliation of local runs and conversions goes only through `jobs/reconciler.py`: `reconcile_once`, on the database
alone (no queue checks, no lock, no re-enqueue), run by the worker's reconciler thread at its start and then every
`execution.reconcile_seconds` (a `jobs.worker.Supervised` thread), and by `laueportal jobs reconcile --once`; each cycle
also deletes the expired `Idempotency-Key` rows (`jobs.idempotency.purge_expired`). It uses `jobs.state`'s existing
transitions only (`finalize` with `RunPhase.INTERRUPTED`, `record_failure`); a running conversion whose job process is
gone becomes `failed` ("Interrupted: the worker or job process died; convert the results again"; with none recorded,
once its start is older than the stale limit), so it can be converted again. A job process is alive as the dispatcher
counts its slot: the worker's own child that has not exited (`own=Dispatcher.running_items`, recorded or not yet), or
the one the dispatcher recorded on the row (`process_host`, `process_pid`, `process_since_boot`: its start in seconds
since boot, which a clock step does not move; `jobs.liveness.recorded_process_alive`, which `jobs.dispatch` imports)
existing on this host: an orphan whose worker was killed keeps its run, and none recorded, another host's or a reused
pid counts as dead. The candidates are read in one session and changed in one `begin_write` transaction only while still
as read (state and recorded process). The dispatcher claims a run atomically (`jobs.dispatch.claim_run`: one
`begin_write` transaction that marks it Running only while it is still Queued, or Cancelled when its stop flag is set;
the loser runs nothing), records its job process on the row, and only then lets it go: a job process runs nothing unless
its pid on this host is on the row (`jobs.process`). The worker holds `<database>.worker.lock` (one worker per database)
and its row (`jobs.worker`). A dispatch cycle that hits a database error (`OperationalError`: a write lock past the busy
timeout) ends logged and the next one tries again, as a reconciler or poller cycle does (`core.db.DatabaseErrors`: one
line, once per streak); a job process whose record failed waits at its go line, holding the slot, until a cycle records
it (at the service stop, `jobs.worker._service_stop` lets it go unrecorded, `Dispatcher.let_go_unrecorded`: it exits
unrun, and the worker's last write, `stop_row`, ends its item, `dispatch.stop_unstarted`). The dispatcher's kill of an
overdue job process counts from its go line, as the job process's own timeout does (`Child.started`).

## Run directories

Their files, keys and atomicity rules are a fixed format, byte for byte, that deployed launchers and migrated runs share
(`jobs.rundir`'s docstring; goldens in `tests/fixtures/remote_goldens/`); changing any of them needs a maintainer's
decision.

## Parameters

- the pydantic `LaueGoParamsV1` and `WireParamsV1` (`*/params.py`, on `core.params.ProgramParams`) have exactly the
  fields of the deployed request format, in their order (`backend` included), plus `schema_id`, and
  `jobs.rundir.request_document` writes them into `request.json` (`tests/jobs/test_programs.py` checks the keys against
  the goldens). They normalize their values and check the per-field rules (a `ValidationError`);
- a new run's parameters are validated by `indexing.validation.validate_indexing` and
  `reconstruction.validation.validate_reconstruction` (`POST /api/v1/{indexing,reconstruction}-runs/ validate`, a 200
  with `{issues}`), on `jobs.validation`: the model's rules through `build_params` (a failed field gets a stand-in, so
  every other field is still checked), then the cross-field and context checks; each issue is
  `core.problems.ProblemItem` (`{loc, severity, code, message}`), bounded like a problem's errors
  (`core.problems.bounded`, at most `jobs.validation.MAX_ISSUES`). A validation only reads (the database in a short
  session of its own, then the file system), and its input resolution is the one run creation makes, so a valid form's
  run can be created: the integers fit SQLite's INTEGER, a run reads at most `execution.max_run_inputs` inputs (counted
  from the ranges, never expanded first: `files.inputs.count_integer_range`), and a remote target is one the deployment
  offers, with an ALCF run's inputs inside the experiment folder. Run creation makes its own refusals for callers that
  do not validate (`jobs.validation.check_creatable`: padded paths, an output folder outside the roots given
  `settings=`, the input cap); pass it the settings. Validate checks only what the parameters contain (a clone's source
  run is prefill's 404). Its result (`Validated`: `params`, `issues`, `ok`) is what a create endpoint refuses with a
  422; a new check goes in the program's `*/validation.py` (shared ones in `jobs.validation`), with a case in its test's
  rule table (`tests/{indexing,reconstruction}/test_validation.py`);
- a new run is created and submitted through `jobs.creation.submit` (`POST /api/v1/{indexing,reconstruction}-runs`): its
  `Idempotency-Key` claimed first (`jobs.idempotency`: a row of `idempotency_key`, revision `0007`, kept 24 h; the claim
  is an insert in a short `core.db.begin_write` transaction of its own, so of two requests with one key one claims it; a
  row past `expires_at` counts as absent and is replaced, and the reconciler's cycle deletes it,
  `jobs.idempotency.purge_expired`; another body or a request still running is a 409, a `jobs.schemas.KeyConflict`
  problem whose `created` is the kept answer when the key's run exists, of type `urn:laueportal:problem:key-created`),
  then the validation (a 422 with every issue, errors first), the creation with `settings=`, and
  `jobs.queue.enqueue_run` on the run's target (with `remote.settings.portal_settings` for `aps` and `crux`). Once the
  run exists the answer is a 201 `CreatedRun`, kept with the key, whatever happened next: a publication or enqueue
  failure is its `failure`, and a run that never reached a queue is marked Failed (`jobs.state.record_enqueue_failure`).
  Once its rows are saved, run creation turns any failure into a `RunPublicationError` for that run, which is Failed
  (`jobs.records.publishing`): put a new step after the save inside it. Only a request known to have created nothing
  releases its key (the validation's 422, run creation's own refusal, `WorkflowValidationError`); any other failure
  keeps it claimed (a 409; past `jobs.idempotency.STALE_SECONDS`, one saying the request did not finish), never a second
  run;
- a create form's values from one source context come from `jobs.prefill` (`GET /api/v1/programs/{kind}/prefill`): one
  ID per parameter (anything else: a 422), an unknown source a 404; a clone copies only the run's form fields, with the
  clone mapping (`jobs.prefill`; a migrated run's `nan` depth is made null);
- each field's JSON Schema metadata contains where its value comes from (`x-source`: `form`, `config`, `derived`) and
  its setting (`x-config`, e.g. `indexing.fixed.detector_crop[0]`). A form field has a user hint (`x-hint`, the form's
  tooltip, required by `core.params.param`: what the value is, without lauelab or config names; the `description` is the
  developers'), at most a terse value pattern as `x-placeholder` (`Si_%d.h5`, `Automatic`), and a file widget its
  `x-extensions`; `tests/jobs/test_programs.py` checks them and the frontend's `/programs` fixture against the served
  schema. `jobs.programs` reads those settings: `fixed_values`, `form_defaults`, `output_path_template`, `build_params`
  (a new run's parameters from a client's form: the config and derived values come from the portal, and a client's value
  for one is refused, `set_by_portal`) and `GET /api/v1/programs`. The models never import the settings or the database
  either, and the compute side never imports the models;
- copy a model with `with_values(...)`, which checks and normalizes again, never `model_copy(update=...)`;
- changing a key means a new schema version, which needs a maintainer's decision.
- the live schemas `wire-live/v1` and `lauego-live/v1` (`*/live_params.py`) have exactly the batch models' fields, in
  their order, each a copy of the batch model's (`core.params.shared_field`, normalized alike), but `scan_points`
  (derived, `""`), `backend` (derived, `local`), `filename_prefixes` (at most one template, one `%d`; none, `[]`:
  every file whose name ends `.h5`, `jobs.arrivals.RunFiles`), LaueGo's `output_xml` (a
  name in the run's folder) and `schema_id`; `live.json`'s model is `jobs.live` (`tests/jobs/test_live_params.py`,
  `test_live_document.py`). They are `jobs.programs.LIVE_PROGRAMS`, listed by `GET /programs` as `lauego-live` and
  `wire-live` while `[live] enabled` (`programs.MODELS` and `AnyProgramKind`: `fixed_values`, `form_defaults`,
  `build_params`, `parameters_schema` and `jobs.validation.check_form` take a live kind too; a live `backend` is
  derived, so it has no `x-options`), and a live run is created by `jobs.creation.create_live_run` (the program
  services' `create_live_reconstruction`, `create_live_indexing`), a Live page's session by `create_live_session`. The
  Live page's validation (`jobs.live_session`) adds the live create checks; the calibration files' loading is
  `science.calibration` (lauelab).

## DB

- every schema change is an Alembic migration: a hand-written `backend/migrations/versions/NNNN_<slug>.py`
  (revision id `NNNN`, template `script.py.mako`), run through `laueportal db upgrade` (no `alembic.ini`);
- name constraints and indexes explicitly with `op.f(...)`; SQLite cannot alter a constraint, so rebuild the
  table (see `0004_drop_calib_and_ca.py`);
- test it on the v1-schema databases of `tests/test_migrations.py` (`dde1bde`, `3ad1557` and `cb1159c` shapes, built
  from the committed `tests/fixtures/v1_schema_*.json`);
- the models must match head (`tests/test_migrations.py`). `0001` (v1 `dde1bde`) and `0002` (v1 `3ad1557`'s five job
  columns) are the earlier portal's schemas, each exactly that commit's `create_all` (`tests/fixtures/v1_schema_*.json`;
  `v1_schema_cb1159c.json`, v1 `cb1159c`'s, is no revision's: the shape whose `job.remote_observation` `0008` keeps); a
  database of the earlier portal is stamped with its revision (`laueportal db stamp --detect`,
  `migrations.stamp_detected`). A v1 `cb1159c` database is refused, read-only, while a live session of the earlier
  portal has not ended (`migrations.open_live_sessions`), else its three live tables are dropped and `0002` recorded in
  one transaction; it must be `cb1159c`'s schema exactly (`migrations.cb1159c_differences`: the live tables' columns and
  indexes as `CB1159C_LIVE_DDL`, which a test compares with the fixture; no view or trigger): a stamp that changes a
  database refuses rather than guesses. The same schema without the live tables (the earlier portal's migration script
  at `cb1159c` never creates them) is stamped `0002` too, nothing dropped. `0001` and `0002` are frozen: change them
  only with a maintainer's decision;
- a dev database from the early numbering (when cleanups were `0002`–`0004`) is refused by `db upgrade` and
  `db check`; rebuild it (`laueportal seed demo --force`);
- WAL only on a local disk: making an engine (`core.db.database_engine`) first applies `core.db.guard_journal_mode`:
  `journal_mode = "wal"` with `database.path` on a network file system (the mount holding its real path in
  `/proc/self/mountinfo`, `core.mounts`; `NETWORK_FILESYSTEMS`) is `UnsafeJournalMode`, a `ConfigError` (the CLI exits
  2), so the API, the worker, its job processes and every command refuse to start. The `db` commands apply it before
  anything else (`cli._database`), the read-only ones too; `db check` reports it as one of its checks instead. Tests
  mock the mount table (`monkeypatch.setattr(core.db, "MOUNTINFO", ...)` with `tests.settings_support.mount_table`);
- `db check` (`migrations.examine`, read-only) prints what it measured and exits 1 on any problem: the revision and
  schema, `quick_check`, `foreign_key_check`, the journal-mode guard, every `UTCDateTime` value in the UTC storage
  format from revision `0005` on (`migrations.timestamp_counts`), the runs stuck in creation (the reconciler's rule,
  `jobs.reconciler.runs_stuck_in_creation`) and every table's rows; `migrations.check` is its problems. `db stamp`
  refuses `0005` or later while a timestamp is not UTC. `db backup <dest>` (`core.db.backup`: SQLite's backup API, the
  copy in rollback-journal mode, `quick_check`ed before it takes its name) never writes the live database, the `--db`
  source or a protected original. The protected originals are the setting `database.protected` (absolute paths, none by
  default: a database with its files and checksums, or a folder and everything under it; `cli._refuse_destination`,
  which `seed demo` and `seed slow-runs` apply too; the development config, `$LAUE_DEV_WORKSPACE/config/dev.toml`,
  contains the list of originals, and `scripts/env.local.sh` may export them as `LAUEPORTAL_DATABASE__PROTECTED`, which
  applies under any configuration). Operations: `docs/runbooks/backup.md`.

## Tests

- real SQLite (temp files), not mocks;
- API tests with TestClient;
- `@pytest.mark.slow` (`make test-slow`) for native lauelab, the live-against-batch parity, the demo seed end to end,
  and for wall-clock checks (the browse timing on a 200,000-file folder); everything else runs in `make check`;
- `tests/worker_support.py` runs `laueportal worker` as supervisor does, its job processes with the test computes when
  asked (`tests.jobs.job_computes`, through `jobs.dispatch.JOB_COMMAND`); the dispatcher's steps run in-process with
  stand-in job processes (`tests/jobs/test_dispatch.py`); `start_worker(prelude=...)` runs a test's hook in the worker
  first (a fault); `write_config(live=...)` writes a `[live]` section, and the `live` fixture's end kills the job
  processes its rows record that still run (`kill_recorded_job_processes`: a live run's has its own session and no time
  limit). The fault-injection tests (`tests/jobs/test_faults.py`) are one per failure case, in a fixed order; those with
  live workers or job processes are `slow`, the rest (the dispatcher in-process) fast; the poller thread's row (12) is a
  live worker whose poller raises; then three cases with a live run in the mix (slow). The dispatcher's live rules are
  `tests/jobs/test_live_dispatch.py`. The reconciler's rules are fast tests (`tests/jobs/test_reconciler.py`), the
  poller thread's in-process (`tests/remote/test_poller_thread.py`);
- every test builds its own `Settings` (`tests.settings_support.make_settings(tmp_path, ...)`). `Settings(...)` never
  reads the environment or a file; only `load_settings`/`get_settings` do;
- no test depends on a developer's local config or workspace: Git, in a test and in what a test starts, reads no user
  or system configuration (`tests/conftest.py`); the default workspace puts TMPDIR, and so `tmp_path`, in the
  checkout, so a test of a deploy renderer (which refuses to write in the checkout) writes in `outside_checkout`;
- a test that expects a probe check to start within a short timeout starts the probe's threads first
  (`tests.probe_support.install_probe` or `start_threads`) and runs with the `gc_disabled` fixture: a full garbage
  collection in a long test process takes about as long as the timeout.
