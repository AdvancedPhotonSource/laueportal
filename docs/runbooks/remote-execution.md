# Remote execution: DM, Globus and PBS deployment

The portal freezes a request and manifest, then submits a DM workflow. The launcher transfers inputs, invokes Globus
Compute to submit and monitor PBS, and returns results. Science runs in the allocation; the portal validates artifacts
before publication. Local processing uses the portal's worker (the `local` target; the job table is its queue); the APS
choice uses DM once its `aps` target is enabled.

## Source and environments

| Component | Source |
| --- | --- |
| Requests and lifecycle | `backend/src/laueportal/jobs/`, `indexing/`, `reconstruction/` |
| DM, Globus, PBS, return publication | `backend/src/laueportal/remote/` |
| Native adapters and standalone/MPI runners | `*/compute.py`, `jobs/runner.py`, `jobs/mpi_runner.py`, `science/merge.py` |
| DM workflow templates and launcher | `deploy/remote/workflow-bdp*.json.template`, `deploy/remote/dm-stage.sh` |
| Compute endpoint control and probe | `deploy/remote/globus/` |
| The files that cross the boundary | `backend/src/laueportal/jobs/rundir.py`, `backend/tests/fixtures/remote_goldens/` |

Deploy the same reviewed source revision to the enabled execution components. Each environment is the conda-forge env of
`environment.lock` (`environment.yml` solved: Python 3.12 and the native build prerequisites) plus a hash-locked set
(the commands are under "Installation" below):

| Lock | Where |
| --- | --- |
| `requirements/base.lock` | The portal (API, worker, remote poller) and local processing |
| `requirements/launcher.lock` | The DM-host launcher (gladier, globus-sdk, globus-compute-sdk, PyYAML) |
| `requirements/compute.lock` | The compute nodes: the standalone runner and the MPI runner (no database or Globus) |

A host that runs both the portal and the launcher installs `base.lock` and `launcher.lock` (they share versions).
Install lauelab from GitHub at the commit `scripts/setup.sh` pins (`d5de751`) with `--no-deps`, as a regular
(non-editable) install, wherever the compute side or the portal runs. The launcher and portal can share a package recipe
while retaining independent installed copies. The analysis and Compute endpoint environments remain separate.

### Installation

The commands each host runs, from a checkout of the reviewed revision. They follow `make setup` (`scripts/setup.sh`):
the conda env from the explicit `environment.lock` (`environment.yml` where the lock's linux-64 packages do not
apply), then exactly one lock with `--require-hashes --no-deps` (numpy, h5py, cffi and pycparser stay conda's),
then lauelab from GitHub with `--no-deps`. `micromamba` stands for micromamba, mamba or conda; `uv` is 0.12 or
later. `<root>` is the host's installation root (`src/`, `envs/`, `config/`, `state/`, `logs/`).

```bash
root=/path/to/installation                    # the installation root of this host
rev=<the reviewed commit>
git clone <the repository> "$root/src" && git -C "$root/src" checkout --detach "$rev"
mkdir -p "$root/envs" "$root/logs" && mkdir -p -m 0700 "$root/config" "$root/state"
py="$root/envs/laue-portal/bin/python"        # dm-stage.sh and verify-dm-vm.py expect this prefix
micromamba create -y -p "$root/envs/laue-portal" --file "$root/src/environment.lock"   # or -f environment.yml
```

- **DM launcher** (the DM host; `dm.launcher_code` = `$root/src`, `dm.launcher_python` = `$py`, `dm.launcher_config` =
  `$root/config/bdp.yaml`). The launcher runs from the checkout (`dm-stage.sh` puts `$root/src/backend/src` on
  `PYTHONPATH`); the package is not installed. The DM host must see the runs root (`files.runs_root`, on GPFS) for
  `crux` runs too: `03-PERMISSIONS` (`dm-stage.sh permissions`) checks the returned `<run>/incoming/<submission>` there
  before it asks DM to restore its permissions ("Returned-file permissions", below), `04-CHECK` (`dm-stage.sh check`)
  reads its `run.json`, and the `cleanup` stage reads `<run>/run.json` (`jobs.rundir`):

  ```bash
  uv pip install --python "$py" --require-hashes --no-deps -r "$root/src/requirements/launcher.lock"
  uv pip check --python "$py"
  cp "$root/src/deploy/remote/bdp.yaml.template" "$root/config/bdp.yaml"   # then fill it in (private)
  "$py" "$root/src/deploy/remote/verify-dm-vm.py" --root "$root" --write-source-manifest   # see Validation
  ```

- **Compute** (the APS cluster's and Crux's analysis environment: each target's `analysis_python` and `code_directory`).
  The PBS job runs `python -m laueportal.jobs.runner` (or `jobs.mpi_runner`) with `code_directory` on `PYTHONPATH`; set
  `analysis_python` to `$py` and `code_directory` to `$root/src/backend/src`:

  ```bash
  uv pip install --python "$py" --require-hashes --no-deps -r "$root/src/requirements/compute.lock"
  micromamba run -p "$root/envs/laue-portal" uv pip install --python "$py" --no-deps \
      "lauelab @ git+https://github.com/AdvancedPhotonSource/lauelab.git@d5de751a8e8a01ac053ddb0b09c4d35a91a005da"
  uv pip check --python "$py"
  "$py" -c "from lauelab.indexing import merge_results_files, merge_xml_files"   # the MPI merge APIs
  ```

  lauelab is a regular install built with the env's compilers (`micromamba run` activates the env), never an
  editable link to a development checkout. mpi4py (in `compute.lock`) builds against the MPI it finds: on Crux,
  load the target's `module_commands` first, as its PBS jobs do (not verifiable from the development machine).
  The Globus Compute endpoint's own environment stays separate (`deploy/remote/globus/`).

- **Portal** (the portal host: the API and the worker, whose thread is the remote poller). The portal's set, lauelab
  (the result viewers and local runs read it) and the backend, which provides the `laueportal` command:

  ```bash
  uv pip install --python "$py" --require-hashes --no-deps -r "$root/src/requirements/base.lock"
  micromamba run -p "$root/envs/laue-portal" uv pip install --python "$py" --no-deps \
      "lauelab @ git+https://github.com/AdvancedPhotonSource/lauelab.git@d5de751a8e8a01ac053ddb0b09c4d35a91a005da"
  uv pip install --python "$py" --no-deps -e "$root/src/backend"
  uv pip check --python "$py"
  ```

  A host that is also the launcher installs `launcher.lock` into the same env as well (the two share `dev.lock`'s
  versions) and runs `verify-dm-vm.py --lock requirements/base.lock`.

## Private configuration

Keep filled-in configuration and the site runbook outside the checkout, backed up privately. Published templates
contain example values only. YAML values are literal; environment variables are not expanded.

- **The portal's settings** (TOML; `LAUEPORTAL_CONFIG`): `[remote] enabled = true` and
  `settings_file = "<the private bdp.yaml>"`, plus the database, `files.runs_root`, output templates and the web
  settings. Keep SQLite on local SSD.
- **The remote settings**: copy `deploy/remote/bdp.yaml.template`. Its keys are fixed, so a `bdp.yaml` written for an
  earlier deployment loads as it is, except for `targets.crux.observer` and a target's `computer_name`, which are
  refused by name: remove them from both copies (the portal's and the launcher's). Set:
  - `experiment_directory`: the portal's `files.runs_root`. Run directories live under `<experiment>/analysis/`;
    the portal refuses a different directory (`laueportal remote poll` says which). Input and calibration files
    must lie inside the experiment directory;
  - the enabled targets (`targets.aps.enabled`, `targets.crux.enabled`);
  - DM owner, workflow names, setup script, and the launcher's source, Python and config paths
    (`dm.user_account` optionally overrides the workflow owner's execution account);
  - Transfer collection IDs and filesystem/collection-root mappings, the Compute endpoint ID, the workspace,
    module commands, and explicit PBS and worker resources;
  - each target's `code_directory`: what the PBS job puts on `PYTHONPATH`, a portal checkout's `backend/src`.

  When the file is read, unknown keys are refused, and so are values the code could not use (a count that is not a
  whole number, a worker count that is not an integer, a section that is not a mapping), naming the key (never the
  value). An SSH host (`dm.ssh_host`, `dm.launcher_ssh_host`, `targets.aps.submit_host`) that starts with `-` is
  refused too: ssh would read it as an option. Values are kept exactly as written: a walltime may be text, seconds or
  a float, a count may be `4`, `4.0` or `"4"`, as the code reads them with `str()` or `int()`. No key is required
  when the file is read; the portal refuses a file without `dm.setup_file`, `dm.owner`, `dm.workflow`,
  `storage.aps.filesystem_root` or `submission_directory`, and a disabled target may be incomplete (nothing reads it).
  A run is refused at submission, before DM is called and with nothing written, when its target lacks a key every run
  on it reads (`analysis_python`, `code_directory`, `pbs.queue`, `pbs.walltime`, `execution.workers`,
  `execution.reconstruction_workers`, `execution.threads_per_worker`; on `crux` also `compute_endpoint` and
  `workspace`), or when the launcher could not build its PBS script from the plan (one node, and one LaueGo rank per
  node, on `aps`; one to four ranks per node; an allocation on `crux`; safe PBS values): such a submission's DM job
  would fail on the remote side. The portal's own `enabled` decides; the file's `enabled` is ignored (any scalar
  loads). `dm.timeout_seconds: null` means no timeout, and so does `dm.permissions_timeout_seconds: null` (300 s by
  default: the bound on the permissions stage's DM call). The portal also refuses a `poll_seconds`,
  `dm.timeout_seconds` or `dm.permissions_timeout_seconds` that is not a positive, finite number (the poller would call
  DM back to back; every DM call would time out); the launcher's copy is not checked for them. Two values a run could
  start with are refused on purpose: `auth_profile: null` (it would use the beamline profile with a
  `gladier-None.cfg`) and a target `enabled` that is not `true` or `false` (a truthiness test would read any non-empty
  value, even `"false"`, as enabled).
- **The launcher** reads its own copy (`dm.launcher_config`, `LAUE_BDP_CONFIG` for launcher commands). It
  needs only `submission_directory`, `state_directory`, `auth_profile` (and `credentials_file` for the beamline
  profile), for its permissions stage `experiment_directory` and `dm.setup_file` (with `dm.ssh_host: null` on
  the DM host), and, to authorize, both storage collection IDs; each launcher command names a key it lacks. Use
  `dm.ssh_host: null` on the DM API host, or configure an SSH bridge when needed. The portal need not have direct
  Globus connectivity.

Keep software and small operational files under an installation root with `src/` (a portal checkout), `envs/`,
`config/`, `state/` and `logs/`. Scientific data belongs in the configured experiment and compute workspace.
`deploy/remote/dm-stage.sh` derives its default installation root from its own location (`<root>/src/deploy/remote/`)
and expects `src/` and `envs/laue-portal/`; it runs `python -m laueportal.remote.stage` with `<root>/src/backend/src` on
`PYTHONPATH`. `LAUE_PORTAL_ROOT`, `LAUE_PORTAL_SOURCE`, `LAUE_PORTAL_PYTHON` and `LAUE_BDP_CONFIG` override it (the
launcher installation's variables; the launcher never reads the portal's own settings, `LAUEPORTAL_CONFIG`).

## Database and authorization

A database of the earlier portal (either schema, `dde1bde` or `3ad1557`) reaches the portal through
`laueportal db stamp --detect`, `laueportal db upgrade` and `laueportal db check` on a copy ([`deploy.md`](deploy.md)
step 4 has the procedure). Stop the earlier portal first, with no run Queued or Running (remote runs included) and no
live session open; never mix the two portals' workers on one queue.

Choose personal or confidential beamline authentication in the private configuration (`auth_profile`). The
identity needs permission to run the endpoint and access both collections. Reference an existing credential file
for the beamline profile (`credentials_file`); never copy it into the checkout. From the launcher installation,
authorize and register the Globus flow and function:

```bash
"$LAUE_PORTAL_SOURCE/deploy/remote/dm-stage.sh" auth
```

This obtains service, flow and collection consent without transferring data or submitting PBS jobs. Preserve
the private auth cache, Gladier registration files, and per-submission UUID records (`state_directory`) across
source and environment updates. Keep private state directories mode `0700`.

## DM registration

Render the workflow JSON from the private remote settings:

```bash
python deploy/remote/render-workflows.py --config "$LAUE_BDP_CONFIG" --output-dir "$LAUE_WORKFLOW_DEFINITIONS"
```

The renderer writes outside the source checkout, refuses existing definitions, and does not register workflows. It fills
owner/account, workflow names (`dm.workflow`, `dm.cancel_workflow`; the deployed names stay; and the portal's
`dm.cleanup_workflow` when it is set, "Leftovers of published runs" below) and the quoted launcher command
(`<dm.launcher_code>/deploy/remote/dm-stage.sh`); `02-MONITOR`'s regexes keep the launcher's `remoteStatus` and
`remoteObservation` ("Monitoring"), and `03-PERMISSIONS` runs only when its `runIf` holds ("Returned-file permissions").
Register the rendered files with DM's administrative tooling. The main workflow's stages are `01-SUBMIT`, `02-MONITOR`,
`03-PERMISSIONS`, `04-CHECK`. DM uses the frozen shared `request.json` as its input-file specification; the manifest
selects the scientific inputs. Cancellation has a separate UUID argument (`cancelSubmissionId`). Keep
`dm.cancel_workflow` configured (the DM VM then needs no SSH key to the launcher host). Without it the portal runs the
launcher's cancel stage itself over SSH (`dm.launcher_ssh_host`, else `dm.ssh_host`), with `dm.launcher_python` and
`dm.launcher_config`, in the checkout `dm.launcher_code`, as that checkout's `dm-stage.sh` would:
`laueportal.remote.stage` with `PYTHONPATH=<launcher_code>/backend/src` (the launcher environment does not install the
package).

### Returned-file permissions (`03-PERMISSIONS`)

After a completed Globus return (`runIf`: `"$analysisMachine" == "crux" and "$remoteStatus" == "SUCCEEDED"`, the
submission's own `analysisMachine` and `02-MONITOR`'s last status), DM runs `dm-stage.sh permissions <submission>`,
which calls DM's `FileDsApi.restorePermissions` on exactly `incoming/<submission>` within the run
(`<experiment>/analysis/<run>/incoming/<submission>`, named relative to the experiment folder, with the experiment
folder's name). That restores the experiment's managed ownership and the beamline account's ACL write access on the
returned files; it does not make that account their owner. The stage refuses, with no DM call, unless the launcher's
state file says `SUCCEEDED` for a `crux` plan and the plan's `incoming_directory` resolves to that directory and is one
(never a parent folder, another submission's return or a link elsewhere). It makes one DM call, through the launcher's
own `dm.setup_file` in a shell that is no login shell, bounded by `dm.permissions_timeout_seconds` (300 s by default;
the storage service updates the files' metadata recursively), and prints `Status: PERMISSIONS_RESTORED`. Any failure
fails the stage, so DM fails the job. The portal's poller needs nothing for it: while the stage runs, DM's job is still
running and the run stays Running (its last observation, Downlink "Returning results", reads "status delayed" after 90
s). If the stage fails, DM reports the job failed; the poller then publishes the returned run as usual when its
`run.json` is in the return inbox and finalizes it from that summary (Finished with DM State "failed"), or, when the
return cannot be read or moved without the restored permissions, logs "Could not reconcile remote job" on each pass and
retries, the run staying Running until the permissions are fixed (run the stage again by hand on the DM host as the DM
account: `dm-stage.sh permissions <submission>`); without a returned `run.json` it ends Failed ("DM job ... failed").
When the poller has published such a run, its outputs were moved out of `incoming/<submission>` into the run directory
(directories renamed, files hard-linked where the file system allows), so they keep the ownership Globus wrote them
with, without the restored ACL, and `incoming/<submission>` holds only `run.json`: running the stage again restores
nothing useful. Restore the run directory's permissions by hand with DM's tooling instead.

**Once, when switching to this version:** re-render the workflows (`render-workflows.py`, above) and update the
registered `dm.workflow` with DM's administrative tooling (an administrator does it; nothing registers it here). Until
then the registered workflow has no `03-PERMISSIONS` and its check stage is still `03-CHECK`, which works as before.

## The poller

The remote poller reconciles remote runs with DM and publishes their returned results; its listing also removes what
Finished, published runs left behind ("Leftovers of published runs"). It is a thread of the worker, which runs it only
when the settings enable remote execution (`[remote] enabled` and `settings_file`; never in the seed's burst worker);
there is no separate poller program. Enable it after authorization, workflow registration and the database upgrade, then
restart the worker (`make dev-down`, `make dev-up` in development; deploy.md step 6 in production), never on a database
that holds the demo's R4 (`laueportal seed demo --crux-run`, which refuses while remote execution is enabled;
dev-setup.md, "A demo with a run on CRUX"): no DM job stands behind R4, and the poller would ask DM about it on every
pass. Its lines are the worker's log (`$LAUE_DEV_WORKSPACE/logs/dev-worker.log` in development, `<ROOT>/logs/worker.log`
in production): "The remote poller holds <submission_directory>/.poller.lock; it polls every 10 s" once it runs. One
reconciliation pass by hand, beside no other poller (while the worker's thread holds the lock, the command exits 1
saying so):

```bash
laueportal remote poll --once
```

It checks the settings too: with remote execution disabled it says so and exits 0; unusable remote settings exit 2
and name the key. The command has no looping form any more (it would be a second poller beside the worker's).

Only one poller runs per DM inbox: a lock on `<submission_directory>/.poller.lock`, which the worker's thread holds from
its first pass until the worker stops. While another process holds it (a `remote poll --once` in progress, another
portal worker on the same inbox), the worker's thread waits for it: it tries again after `poll_seconds`, twice as long
each time up to 60 s, logs once that it waits ("Another process holds ...") and once that it holds the lock, and makes
no pass meanwhile, so the status page shows the poller late; the worker's dispatcher and reconciler carry on. Remote
settings the worker cannot use (`bdp.yaml` missing, an unknown key, an experiment directory other than
`files.runs_root`) leave the poller off with one error line in the worker's log ("Remote execution is enabled, but its
settings cannot be used ...") and the status page shows the poller late (never "Off", which means remote execution is
disabled): fix `bdp.yaml` (`laueportal remote poll --once` names the key) and restart the worker. Beside another
portal's running poller on the same DM inbox, start a development stack without remote execution
(`LAUEPORTAL_REMOTE__ENABLED=false make dev-up`, or `[remote] enabled = false`): with it, the development stack's
remote runs would wait unfollowed while that poller holds the lock.

**Calls to DM and the inboxes**: each pass (every `poll_seconds`, `bdp.yaml`, 10 s) makes one DM call for every active
remote run, whatever their number (`remote.dm.DMClient.fetch`: one subprocess, one shell and Python process, one SSH
session behind a bridge, bounded by `dm.timeout_seconds`; a call still running when the worker stops is killed with what
it started before the worker exits, and one whose worker was killed outright (an OOM kill, SIGKILL) gets SIGTERM 5 s
after its timeout and SIGKILL 5 s later: coreutils `timeout` wraps each of the worker's calls while `dm.timeout_seconds`
is set (null: no wrapper, and such a call runs on unbounded); GNU coreutils' `timeout` sends that SIGKILL to the whole
call, uutils' (0.10, Ubuntu's default) to its first process alone, so there a process of the call that survives SIGTERM
outlives the limit), and none while no remote run is Queued or Running (a pending stop or publication keeps its run
Running). A call that fails as a whole changes nothing; the wait then doubles, up to 60 s, logged when the calls start
failing, when the failure changes and when DM answers again. A record DM cannot give, or one that is not an object,
fails that run's pass alone (logged "Could not reconcile remote job ..."). A stop submits the cancel workflow once (a
call of its own, only while a stop is pending); its job's state comes with the following passes' call. A cancel
submission cut by the worker's stop (logged "The worker's end cut the delivery of remote job ...'s stop") or by its
timeout is submitted again by the next pass; should DM have taken the first, an `aps` run's second `qdel` fails ("DM
cancellation job ... failed", logged on each pass) until the run's DM job ends, which then finalizes it as usual.
`dm.timeout_seconds` (30 s by default) bounds the whole batch, not a run: with many active runs on a slow DM, a call
that keeps failing with "no answer within 30 s" stalls every remote run; raise it in `bdp.yaml` and restart the worker.
The listing of the DM inbox and the return inboxes comes at the thread's start, then at most every ten minutes while
remote runs are active or one ended since the last listing.

**Production** (`docs/runbooks/deploy.md`): `prod.toml`'s `[remote] enabled` (rendered from `REMOTE_ENABLED`) turns the
worker's poller thread on with the API's remote targets; restart the worker and the API after a change (deploy.md step
6). It needs the database upgraded to head (`laueportal db upgrade`, then `db check`), `settings_file`
(`<ROOT>/config/bdp.yaml`, private). Check it with `laueportal remote poll --once` (the production environment file
sourced) before the restart.

What it does each `poll_seconds`, for every submitted remote job that is Queued or Running: find a lost submission's DM
job by its submission ID (never a second submission); deliver a requested stop (`cancel.request` in the run directory,
then DM's cancel workflow; `.cancel-sent` once delivered); follow DM's state (the job turns Running when DM leaves
`pending`); and when DM is done, failed or aborted, validate and publish the returned directory and finalize the job
from its summary. A refused return finalizes the job Failed ("Returned output could not be validated/published: …");
other errors, the filesystem's among them (an unreadable file, another process's HDF5 file lock, `EIO`, `ESTALE`), are
logged and retried on the next pass. A run whose plan (`<run>/bdp.json`) is missing while the runs root answers (its
folder deleted, moved or never written) is skipped and left out of the pass's DM call, logged once ("its plan ... is
missing"), and after 10 minutes of that (`remote.poller.MISSING_PLAN_SECONDS`, counted by the worker's poller thread,
from zero again after the worker's restart) ends Failed: "Run folder missing: ... DM job <id> is no longer followed
(cancel it in DM if it still runs)". A plan that is there but cannot be read (not JSON or not a JSON object, a hand
edit or a damaged file system; a folder in its place; its read refused) is treated the same: "its plan ... cannot be
read (<reason>)", then "Run plan unreadable: ... (<reason>) ...". Its Stop ends with it: a stop is delivered through
the folder. While the runs root itself does not answer (an NFS mount down) nothing is counted: the run waits, logged
once, and a count begun before starts again once the root answers. A lost reply's submission that DM has several jobs
for ("Multiple DM jobs have the same submission identity ...; manual reconciliation required") is left as it is,
logged once per change of those jobs: cancel the extra DM jobs by hand. What a refused return leaves depends on when
it is refused:

- **By the validation** (the summary, its counters, the artifacts, the PBS exit status, an artifact whose bytes
  HDF5 rejects: not HDF5, truncated, h5py's errors without an `errno`), another publication or summary of another
  submission in the run, or an inbox entry whose name is already taken in the run directory ("Publication would
  overwrite"): nothing is published. The returned directory stays in `incoming/<submission>/`.
- **While the entries are moved** (after the marker `.bdp-publication.json` is written): a returned symlink, a
  returned directory holding one ("Returned output directory contains symlinks"), or, after a restart, a
  destination that exists and is not this publication's file (the error is its path). The run directory then holds
  the marker and the entries moved before the refused one (they are moved by name, in the marker's order); the
  refused entry and the rest stay in the inbox, with `run.json`; the run directory has no `run.json`.

**Recovering a return refused while moving.** Nothing is deleted, and the job stays Failed (a finished job is
never reopened; the statuses and transitions are unchanged): submit a new run for a Finished one. To complete the
run directory anyway (for example to keep expensive results), make the refused entry publishable, then run the
publication once by hand, as the portal account with the portal's environment; it continues from the marker,
skips what was moved and writes `run.json`:

- a destination that exists: move that file out of the run directory, after checking whose it is;
- a returned symlink (or a directory holding one): replace it in the inbox by a regular copy of the file it should
  be;

```bash
python -c 'import json, sys; from laueportal.remote.poller import publish_returned; publish_returned(json.load(open(sys.argv[1])))' <run>/bdp.json
```

A poller stopped in the middle of a publication finishes it on the next pass, also when it stopped between
publishing a file and removing its inbox copy: the copy is removed only when the destination is this
publication's file (the same file, the same bytes, or, for the files publication rewrites, `output.h5` and the
Output XML, exactly the bytes the rewrite makes of the inbox copy). A different file already at a destination is
never replaced: the job fails with "Publication would overwrite …" or the destination's path, and both files are
kept.

**A submission DM has no job for.** A job whose submission DM never received (the portal stopped between
recording it and calling DM: `external_state` `submitting`) or did not act on (a lost reply:
`submission_uncertain`) stays Queued, and the poller logs on every pass `Remote job <id>: DM has no job for
submission <uuid>`. To resolve it:

1. Look the submission ID up in DM (the processing jobs of the workflow, by `submissionId`). If DM has a job,
   the next pass finds it and reconciles the run as usual: nothing to do.
2. If DM has none, stop the run in the portal (Stop: the request is recorded). Once the stop is
   `UNSUBMITTED_STOP_GRACE_SECONDS` old (10 minutes, or twice `dm.timeout_seconds` when that is longer) and DM
   still has no job, the poller finalizes the job Cancelled ("DM has no job for submission <uuid> … after the
   stop; cancelled"). Should a DM job for the submission appear later, cancel it in DM; the portal no longer
   follows it. If DM's answer to that submission reaches the portal after the job ended (a portal process paused
   for longer than the grace), the job keeps its state and gets the message "DM accepted submission <uuid> as
   processing job <id> after this run ended; cancel it in DM" (also logged as an error): cancel that DM job.
   With `dm.timeout_seconds: null` (no timeout) nothing bounds how long a submission waits for DM's answer, so
   the poller never finalizes such a job (its warning says so): look the submission up in DM and, once sure no
   submission is still waiting (the portal process that submitted it has ended), set a timeout and restart the
   poller, or leave the run Queued.
3. Its plan stays in the run directory and in the DM inbox; nothing submits it again. Submit a new run instead.

A remote job without a run directory (only by editing the database) is skipped with an error on every pass.

A DM cancel job that failed or was aborted is logged on every pass
(`DM cancellation job <id> failed; inspect its stage diagnostics`) and never submitted again on its own:
`.cancel-job.json` keeps its ID. After inspecting the cancel job's stage diagnostics in DM, remove
`<run>/.cancel-job.json`; the next pass submits the cancel workflow again.

## Endpoint tools

Keep endpoint settings in the private deployment environment:

- `LAUE_ENDPOINT_ENV`: endpoint environment prefix.
- `LAUE_ENDPOINT_NAME`: configured endpoint name.
- `LAUE_ENDPOINT_HOST`: configured short hostname, required for start/stop.
- Optional `LAUE_ENDPOINT_TMPDIR` and `LAUE_PBS_BIN`: temporary directory and PBS binaries.

`bash deploy/remote/globus/endpoint.sh start|stop|list` uses those settings and checks the host before starting
or stopping the endpoint. The endpoint YAML template (`deploy/remote/globus/endpoint-config.yaml.template`)
contains generic service settings. Probe explicitly supplied paths from an authenticated launcher without
requesting nodes or transferring data:

```bash
python deploy/remote/globus/endpoint_smoke.py "$COMPUTE_ENDPOINT_ID" --workspace "$COMPUTE_WORKSPACE" --analysis-python "$ANALYSIS_PYTHON"
```

## Execution and state

Each run uses checksummed input batches, one PBS job, and one result transfer. Compact manifests and at most 100
files per batch bound Flows state. Node count, walltime, allocation, queue, and worker counts are configured
explicitly.

Submitting a `crux` run checks every input file while the plan is built (one stat each, slow on a slow mount).
Meanwhile the run is `published` with no submission, and the worker's reconciler fails a run left so longer than
`execution.creation_timeout_seconds` (600 s) after its last change; the plan's build records progress on the
run every minute, so only a build that stops progressing (a hung mount) is failed ("Creation did not
complete"; the build then stops before anything is written, and DM is never asked).

MPI distributes reconstruction points across nodes. Indexing keeps a point's depths on one node and distributes
them among its ranks. Each rank has a private writer; the coordinator checks host distribution and validates
merged outputs. Indexing merges use lauelab's public HDF5/XML APIs. Failed shards prevent a complete result.
Estimates use numeric calibration profiles and include remote round-trip transfer while excluding queue wait.

Run directories retain manifests, summaries, rank records, and scheduler logs. Returned input-image paths are translated
to the originating storage before publication; the other paths a remote run wrote stay remote and the portal maps them
when it reads them (`remote.paths.portal_path`). The portal shows workflow state and scientific counters after return.
Cancellation delivers a marker to the scheduler monitor, which requests `qdel`; uncertain submissions are reconciled
without automatic resubmission.

On `aps` the launcher keeps `qsub`'s output as the PBS job ID (`<state_directory>/<submission>.json`, for `qstat` and
`qdel`) only when it is one (digits, optionally `.<server>`: the pattern the `crux` PBS task checks). Anything else
fails the submit stage ("PBS returned an invalid job ID"; the DM job fails) and leaves the state `SUBMISSION_UNCERTAIN`,
so nothing submits it again and `monitor` asks for manual reconciliation: look the job up with `qstat` and `qdel` it if
it runs.

Retain remote run directories until returned results are validated and retention requirements are met. The
poller's clean-up ("Leftovers of published runs") removes only what a Finished run left, and the remote folders
only while `dm.cleanup_workflow` is set: leave the key unset while retention requires keeping them. Preserve
active and uncertain submission records, the portal database, queue persistence, and private authorization state
independently of source control.

## Monitoring

A `crux` run's job page (the Remote Run card's Phase row) and the run monitor's Status cell (while the run is queued or
running, the detail its tooltip) show where the run is: Uplink, Queued, Running or Downlink, with a detail, or Login
needed. The launcher's `monitor` stage (DM's `02-MONITOR`, every 10 s) prints it as one line, `Observation: <json>` (the
`remoteObservation` format), which `02-MONITOR`'s regex keeps as the DM job's `remoteObservation` variable; the poller
reads it from its one DM call per pass and keeps it in `job.remote_observation` while the run is active. `aps` runs have
none.

- From the Flow's step (`get_status`, as before): the input transfer is Uplink "Transferring inputs", the result
  transfer (and a Flow that succeeded) Downlink "Returning results"; the poller writes Downlink "Publishing results"
  before it publishes.
- **Login needed**: while the Flow is `INACTIVE` with `authorization_parameters` on the run or on any of its actions (a
  Globus session or consent requirement), in any step, the stage prints Login needed "Sign in to Globus and resume this
  run" (warning-tinted, as Queued), lists nothing and keeps only that flag: the parameters never reach the state file,
  the output or a log. Sign in to Globus as the identity the Flow runs as and resume the run in the Globus web app (the
  Remote Run card's Globus run link). Once the Flow no longer waits for a sign-in the run's phase is its step's again
  (none inherited from the pause; in `RunPbsJob` it is listed at once). An `INACTIVE` Flow without them (a paused
  action) shows its step's phase as before.
- While the Flow is in `RunPbsJob` (and has not ended), at most every `targets.crux.listing.interval_seconds` (30),
  one Globus Transfer listing of `<targets.crux.workspace>/runs/<submission>/results` in the Eagle collection, with
  the launcher's identity and the Transfer consent the `crux` cancel uses, waiting at most `timeout_seconds` (8)
  for the whole step (the Transfer client, its token's renewal and the request), with no retry. It never asks for a
  login: without a Transfer consent or token for the launcher's identity it fails at once (`LoginRefused`; run
  the `auth` stage as that account). A client identity's (`beamline`) token comes from its credentials, which asks
  nothing. No folder (Transfer's `NotFound` codes, or a 404 without a code), or none of the files below: Queued
  "Waiting for endpoint"; `pbs.json`: Queued "Waiting for PBS allocation"; `progress.json`: Running "Processing";
  `run.json`: Running "Finishing processing". When the newer of `pbs.json` and `progress.json` (both rewritten
  every 5 s while the job is in PBS) is more than `stale_after_seconds` (90) old, or the listing failed or did not
  answer in time, the observation keeps the last phase and is stale (the job page's "status delayed"); the stage
  never fails for it. A listing that shows `run.json` is not stale by the heartbeats' age (the runner has ended;
  the Flow may stay in `RunPbsJob` a while before it returns the results). The next listing comes 30 s later; after
  a failed one twice as long each time, up to 60 s (`listing_wait` in the state file), and 30 s again once one
  answers. The stage's stderr has one line when the listings start failing (the error's type and HTTP status
  only), when the failure changes, and when they answer again.
- `<state_directory>/<submission>.json` keeps the last listing's time (`listed_at`, saved before the listing), the
  back-off (`listing_wait`, `listing_failure`) and the last observation; the runs between listings print the kept
  one. Nothing is listed in another Flow step, after the Flow ended, or for `aps`. Every state file is the
  account's own (0600: the stage sets the umask 077 before any write). Do not run `dm-stage.sh monitor` by hand
  during a run: the two runs share no lock, so it may cost one extra listing (the later write of the state file
  wins). During a Globus outage it also makes a Flows call of its own once its wait has passed, and its later
  write can bring back an older failure start (`status_failing_since`), so the budget below may count from before
  an answer.
- **A Globus outage** (the Flow status call fails: a network error, a Globus 5xx, a token renewal that fails or does not
  answer, no answer within 10 s): the stage does not fail. Each status call is one request to Flows, with no retry, the
  whole step (the launcher's client, its token's renewal, the request) bounded by 10 s. It prints the last `Status:` and
  the kept observation, stale, and ends as a successful pass, so DM's `02-MONITOR` repeats. The job page's Phase row
  keeps the last phase and detail with "status delayed" (e.g. "Running · Processing · status delayed"; the tooltip's
  time is the last observation made from Flows' answer); a run whose first status call fails, with nothing kept yet,
  shows Queued "Workflow status unavailable", delayed. The next call comes 10 s after the failed one (DM's interval),
  then twice as long after each failure, up to 60 s; the runs in between make no Flows call and print the same line.
  Nothing is listed on Eagle meanwhile (the listing keeps its own back-off). Once Flows answers, its status is handled
  as before and the observation is fresh again (in `RunPbsJob`, from the next listing). The stage's stderr has one line
  when the calls start failing (the error's type and HTTP status only, never a token; the line names the budget), when
  the failure changes, and when Flows answers again. While the calls fail the state file keeps the back-off
  (`status_failure`, `status_wait`, `status_failed_at`, `status_failing_since`) and the observation as it was.
- **Not an outage, failing the stage at once as before:** a missing Flows consent or token for the launcher's
  identity (`LoginRefused` in the stage's diagnostics; the stage never starts a login: run the `auth` stage as that
  account), and an error building the launcher's client (the credentials file, `bdp.yaml`). A client build that
  hangs past the 10 s is a failed call.
- **When the run fails for it:** the first status call that starts more than
  `targets.crux.status_failure_budget_seconds` (600 s) after the first failed one, and fails, fails the stage as
  before (the error's traceback, exit 1, nothing on stdout), so DM ends the job as failed and the poller finalizes
  the run Failed ("DM job … failed; inspect DM stage diagnostics"). With the back-off that is at most about 80 s
  after the budget: one wait (60 s), DM's interval (10 s) and the failing call's own time (at most 10 s). The waits
  and the budget follow the DM host's clock: a step back does not lengthen the budget, but a step forward during
  an outage counts toward it (one larger than what is left of the budget fails the stage at the next call). As
  with any failed stage, the Flow and its PBS job are not cancelled.
- Not shown: Held (it reads "Waiting for PBS allocation"), the PBS ID while the job is in PBS (the returned
  `pbs.json` has it), live counters (`progress.json`'s are read after the return), a state history.
- The settings are the launcher's copy's `targets.crux.listing` (`interval_seconds`, `timeout_seconds`,
  `stale_after_seconds`) and `targets.crux.status_failure_budget_seconds` (positive numbers; the defaults above
  when absent). The portal does not read them; a run's plan copies the portal's `targets.crux` section as it is,
  `listing` and the budget included when they are there. Keep `interval_seconds` well
  under 90 s (the runs between listings print the kept observation, and the portal shows one observed more than
  90 s before as delayed) and `timeout_seconds` under `interval_seconds`.

**Live checks on the facility's systems**, once, in this order:

1. **The launcher the DM host runs:** `git -C <dm.launcher_code> log -1 --format='%h %s'` names the reviewed portal
   commit, and neither copy of `bdp.yaml` has `targets.crux.observer` or a target's `computer_name` (the portal's
   launcher has no observer; its settings refuse those keys by name: "Private configuration").
2. **A one-off listing as the launcher identity, outside a Flow** (a past `crux` run's Eagle folder stays while
   `dm.cleanup_workflow` is unset), with the stage's own code. Run it on the DM host as the account DM runs the stages
   as (the owner of `<root>/state`: the token storage and the credentials are that account's), from any directory, with
   the portal checkout `<root>/src`, the Python of `<root>/envs/laue-portal` (it has the portal's `launcher.lock`) and
   the launcher's `<root>/config/bdp.yaml`:

   ```bash
   portal=/path/to/portal-checkout  # <root>/src
   py=/path/to/python               # <root>/envs/laue-portal/bin/python
   config=/path/to/bdp.yaml         # <root>/config/bdp.yaml
   PYTHONNOUSERSITE=1 PYTHONPATH="$portal/backend/src" "$py" -c '
   import json, sys
   from laueportal.remote.plan import submission_path
   from laueportal.remote.settings import load_settings
   from laueportal.remote.stage import _transfer_client, eagle_run_folder, list_results
   settings = load_settings(sys.argv[1])
   plan = json.loads(submission_path(settings, sys.argv[2]).read_text())
   print(list_results(plan, settings))
   endpoint, folder = eagle_run_folder(plan, settings)
   try:  # what Eagle answers for a folder that does not exist
       _transfer_client(settings, timeout=8).operation_ls(endpoint, path=f"{folder}/results/no-such-folder")
   except Exception as error:
       print(type(error).__name__, getattr(error, "http_status", None), getattr(error, "code", None))
   ' "$config" <a crux run's submission ID>
   ```

   The first line is the status files with their times (`{'pbs.json': datetime(...), ...}`), `{}` when the folder is
   gone, or `None` with the reason on stderr. `LoginRefused` there means the account has no Transfer consent or token:
   run the launcher's `auth` stage as that account (`dm-stage.sh auth`), then the check again. The listing never asks
   for a login; should anything print a login URL or wait for a code, stop it (Ctrl-C) and report it. The second line is
   Eagle's answer for a missing folder (e.g. `TransferAPIError 404 ClientError.NotFound`): note it. The launcher reads a
   code ending in `NotFound`, or a 404 with no code, as "no folder"; any other answer would show every run as Queued
   "Waiting for endpoint", stale, until its `results/` exists: report it. (Or, for the consent only: a `crux` cancel has
   worked, which uses the same consent.)
3. **`laue-portal-bdp` registered as the portal's `render-workflows.py` renders it** ("DM registration"): its
   `02-MONITOR` `outputVariableRegexList` has `"Observation: (?P<remoteObservation>.*)"` after the `Status` one, and it
   has `03-PERMISSIONS` and `04-CHECK` ("Returned-file permissions"). Otherwise render it and register it again under
   the same name, between runs (no DM job of the workflow running).
4. **DM's job record carries `remoteObservation` where the poller reads it** (the record's top level; not yet verified
   on DM itself): during a `crux` run, `dm-get-processing-job --id <id> --display-keys=ALL` shows `remoteObservation`
   beside `remoteStatus`. If it is elsewhere, the job page shows no Phase row: report the layout.
5. **A `crux` run shows Uplink → Queued → Running → Downlink → Finished** on the job page and in the run monitor's
   Status cell. With the portal's launcher the printed `flow_state` follows the Flow (`InputTransfer`, `RunPbsJob`,
   `ResultTransfer`); one that stays `null` ("Awaiting workflow") means the Flow's run document names its step
   elsewhere than `details.action_statuses` or `details.details.state_name` (`remote.stage.flow_step`): report it.

## Leftovers of published runs

> **Leave `dm.cleanup_workflow` unset** until the clean-up stage (step 2 below) is deployed and tested on the remote
> side. Set, it would also clean the earlier portal's published `crux` runs migrated into the portal's database (their
> Eagle folders, launcher state files and inbox copies; see below).

A remote run leaves files outside its run directory. The poller removes them only for a Finished run: its job is
Finished in this database, and its run directory holds its publication of a Finished run (`run.json` names the
submission with the status "Finished", and `.bdp-publication.json` names the submission). It goes through the DM inbox's
plan copies (`remote.cleanup`; at its start, then every ten minutes) and, for each such run, removes:

1. the return inbox's remnants, `<run>/incoming/<submission>/`, when it holds only `run.json` and `.runner.lock`
   (and `incoming/` once empty); anything else there is kept and logged ("still holds ...; kept"), as are a
   removal the file system refuses (the transfer account's directory) and an `incoming/` or `<submission>` that
   is a link or leads outside the run directory: none of these stops step 2;
2. with `dm.cleanup_workflow` set (the portal's own key, null in the template): the DM clean-up workflow, submitted once
   (its DM job ID in `<run>/.cleanup-job.json`), runs the launcher's `cleanup` stage: a `crux` run's Eagle folder
   (`<workspace>/runs/<submission>`, a Globus delete task, waited for up to ten minutes), then
   `<state_directory>/<submission>.json`; the stage itself refuses a run whose `run.json` does not name the submission
   with the status "Finished". The poller removes the inbox copy `<submission_directory>/<submission>.json` at its first
   clean-up pass after DM reports the job done: within about twenty minutes of the publication.

A Failed or Cancelled run, and a run that was not published (a refused return, a DM job that failed without one), keep
everything for diagnosis: the return inbox's remnants, the inbox copy, the state file and, on `crux`, the Eagle folder,
where an MPI run's `shards/` hold its ranks' results and diagnostics (the run's message points to `ranks.json`). Remove
those leftovers by hand once done with them. The earlier portal's remote runs migrated into this database (the migrated
copy) are known to it and treated as its own: a Finished, published one loses its return inbox's remnants (step 1), and
with `dm.cleanup_workflow` set it would lose the rest too (step 2: a `crux` run's Eagle folder, its launcher state file
and its inbox copy); keep the key unset (above). Only a submission this database does not know (e.g. a run of the
earlier portal submitted after the copy was taken) is never touched. A pass makes at most ten DM calls, so a backlog
(the runs published before this) drains over several passes; a run whose DM call fails is retried at the next pass, then
backs off (skipped for up to about five hours), so it cannot hold up the others. `.cleanup-job.json` says `submitting`
before DM is asked (a record that cannot be written submits nothing), then holds the DM job's ID, then DM's verdict once
it failed. A failed clean-up job is logged once per poller process ("clean-up DM job ... failed") and never looked up or
resubmitted on its own: inspect its stage diagnostics, then remove `.cleanup-job.json` to retry. A record left
`submitting` (DM was asked, then the job's ID could not be recorded, or DM did not answer in time: it may have accepted
the job; only DM's refusal removes the record) is submitted again at most three times in all; then the poller asks for a
look in DM ("submitted 3 times without its DM job ID being recorded"). Without `dm.cleanup_workflow` only step 1 runs,
the inbox copies stay for a later clean-up to read, and the poller says so once per process. To turn it on (see the note
above): render and register the clean-up workflow (`render-workflows.py` writes `workflow-bdp-cleanup.json` when
`dm.cleanup_workflow` is set), then set the key in the portal's `bdp.yaml` and restart the worker.

## Validation

```bash
make check && make test-slow     # tests/remote/ (the remote tests, the workflow tests, the poller, the goldens)
"$LAUE_PORTAL_ROOT/envs/laue-portal/bin/python" deploy/remote/verify-dm-vm.py --root "$LAUE_PORTAL_ROOT" \
    --config "$LAUE_BDP_CONFIG" --network \
    [--lock requirements/base.lock]   # on a host that also runs the portal
```

The verifier (run with the installed env's Python) checks the installed source against its private source
manifest, the launcher's lock set (and each `--lock`), `pip check`, the private settings (read with
`remote.settings`, which refuses unknown keys), `dm-stage.sh --help` and the generated batched flow, without
authorizing or submitting jobs; `--network` adds the public Globus HTTPS endpoints. It writes
`<root>/state/setup/install-manifest.json`.

- **The source manifest** is `<root>/state/setup/source-manifest.json`: `{"files": {"<path relative to
  <root>/src>": "<sha256 of the file's bytes>", …}}` (other keys, such as `written_at`, are kept for the record and
  not checked). Write it once after installing a reviewed revision: `verify-dm-vm.py --root "$LAUE_PORTAL_ROOT"
  --write-source-manifest` hashes the checkout's tracked files (`git ls-files`; every regular file outside `.git`
  and `__pycache__` when `src/` is not a git checkout), writes the manifest (directory `0700`), then verifies.
  Every later run checks those files byte for byte ("Source checksum mismatch: <path>"); without a manifest the
  verifier stops and says how to write it. Re-write it after installing the next reviewed revision.
- **What it does not check:** the native lauelab checks (installed in the prefix, not editable) run only when
  lauelab is installed (a launcher-only environment has none); URL requirements get no VCS commit check, because
  the lock sets hold none (lauelab's commit is checked by `make setup` and the compute install above); and only
  `requirements/launcher.lock` is checked unless `--lock` names more.

Nothing here can be exercised live from the development machine (no DM, Globus or PBS): the tests use fakes and goldens
captured from the deployed remote side's code. After deployment changes, run the live smoke tests below as the portal
account before scaling up.

## Live smoke tests (on the real machines)

For each enabled target, `aps` and `crux`, one small reconstruction and one small indexing run, with the poller
running:

1. **Reconstruction** (2 to 3 points of a known wire scan) and **indexing** (3 to 5 frames with a known result),
   created with the target chosen ("Run on": APS for `aps`, ALCF for `crux`).
2. Right after submission: the job is Queued with `external_state` `pending` and a `submission_id`;
   `<run>/bdp.json` and `<submission_directory>/<submission>.json` exist and are identical; DM lists a job with
   that `submissionId`.
3. While it runs: the job turns Running; `crux`: the Globus run (its URL on the job) shows the input transfer,
   `RunPbsJob` and the result transfer; `qstat` shows the PBS job (`aps`: its ID in
   `<state_directory>/<submission>.json`; `crux`: in `pbs.json`).
4. After DM reports done: the job is Finished with every input succeeded; the run directory holds the outputs,
   `run.json` (with `provenance.remote_output_directory`), `.bdp-publication.json`, `progress.json`,
   `pbs-output.log` (`crux`: also `pbs.json`); `incoming/<submission>/` keeps only `run.json` and `.runner.lock`
   until the poller's next clean-up (at most ten minutes), which removes `incoming/`.
5. Indexing: `output.h5`'s `/frames/input_images` and the Output XML's `inputImage` name the portal's files
   (`crux`: `/provenance/remote_input_images` keeps the Eagle paths); the results open in the portal.
6. Reconstruction: the catalog and point files open in the portal, the Source column maps to the portal's files.
7. One stop on each target: stop a running run; `cancel.request`, `.cancel-job.json`, then `.cancel-sent` appear,
   PBS ends the job, and the run finishes Cancelled with the completed inputs kept.
8. Only once `dm.cleanup_workflow` is set (see "Leftovers of published runs"). With `dm.cleanup_workflow` set
   ("Leftovers of published runs"), for the Finished run of step 4: within ten minutes of its publication
   `<run>/.cleanup-job.json` appears and DM lists the clean-up job; once DM reports it done,
   `<state_directory>/<submission>.json` and, for `crux`, the Eagle folder `<workspace>/runs/<submission>` are gone
   (Globus shows the delete task "Laue cleanup <first 8 characters of the submission>"), and
   `<submission_directory>/<submission>.json` goes at the poller's next clean-up pass (within about twenty minutes of
   the publication); the run directory and the run's results in the portal are unchanged. The Cancelled run of step 7
   keeps all of them (`crux`: its Eagle folder too).

Record the run IDs, the DM job IDs and anything unexpected in the private site runbook.

Without the create forms (a scripted check), create and submit a run from Python on the portal host (with
`LAUEPORTAL_CONFIG` set), e.g. a reconstruction. The form's values start from the configured defaults
(`[reconstruction.defaults]`, `[output_templates]`); the threads, memory and `verbose` come from `[execution]` and the
host name and submission time from the portal (`jobs.programs.build_params`, which refuses a value for them). Run
creation given the settings refuses a path with white space at its start or end, an output folder outside the file
roots and more inputs than `[execution] max_run_inputs`; `reconstruction.validation.validate_reconstruction(form,
settings=settings, engine=engine).issues` (`indexing.validation.validate_indexing`) lists every other problem first:

```python
from laueportal.core.db import make_engine
from laueportal.core.settings import get_settings
from laueportal.jobs import programs
from laueportal.jobs.queue import enqueue_run
from laueportal.reconstruction.service import create_reconstruction
from laueportal.remote.settings import portal_settings

settings = get_settings()
engine = make_engine(settings)
remote = portal_settings(settings.remote, runs_root=settings.files.runs_root)
scan = 274038  # the form's values: the scan, its wire-scan folder, file names and points, the target
form = programs.form_defaults("wire", settings) | {
    "scan_number": scan,
    "input_path": "/net/fileserver/export/data1/path/to/Wire_scans/Twin2_wire",
    "filename_prefixes": ["Twin2_wire_%d.h5"],
    "scan_points": "1-3",
    "output_path_template": programs.output_path_template("wire", settings, scan_number=scan),
    "backend": "crux",  # ALCF; "aps" for APS
}  # also "geometry_file" when [reconstruction.defaults] sets none
run = create_reconstruction(programs.build_params("wire", form, settings), engine=engine, settings=settings)
print(run.id, enqueue_run(run.job_id, engine=engine, remote=remote))  # DM's job ID
```

An indexing run is the same with `"lauego"` and `create_indexing` (`laueportal.indexing.service`); its form also
takes `reconstruction_id`, the geometry and crystal files (unless `[indexing.defaults]` sets them) and, for a
reconstruction's frames, `depth_range`.
