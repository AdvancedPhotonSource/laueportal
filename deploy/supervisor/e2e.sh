# Settings overrides of the e2e stack (deploy/supervisor/e2e.conf), sourced by scripts/stack.sh after
# scripts/env.sh, before it seeds or starts supervisord. On top of $LAUEPORTAL_CONFIG (dev.toml): a database
# and a runs root of its own, rebuilt by `laueportal seed demo --force` on every `make e2e`, so the dev
# database and its runs are never touched; this checkout's built SPA; and the execution settings, pinned so
# that a dev.toml edit does not change the e2e runs' timing or the stale limit the specs assert.
# Remote execution is pinned off: the e2e stack never reaches DM, whatever dev.toml's [remote] says, and its demo
# has R4 (--crux-run), a run recorded as running on CRUX with no DM job behind it, which the specs' Remote Run card
# and run monitor's Status cell read (seed demo refuses --crux-run while remote execution is enabled).
# scripts/stack.sh refuses to start the e2e stack without this file.
export LAUEPORTAL_DATABASE__PATH="$LAUE_DEV_WORKSPACE/db/e2e.db"
export LAUEPORTAL_FILES__RUNS_ROOT="$LAUE_DEV_WORKSPACE/e2e/runs"
export LAUEPORTAL_FILES__UPLOADS_DIR="$LAUE_DEV_WORKSPACE/e2e/runs/.uploads"
export LAUEPORTAL_FILES__ROOTS__RUNS="$LAUE_DEV_WORKSPACE/e2e/runs"
# The demo's synthetic inputs (`laueportal seed demo` stages them in the runs root's inputs): the samples root, the
# scan-log folder and the forms' default calibration files, whatever the dev config names.
E2E_INPUTS="$LAUE_DEV_WORKSPACE/e2e/runs/inputs"
export LAUEPORTAL_FILES__ROOTS__SAMPLES="$E2E_INPUTS"
export LAUEPORTAL_FILES__SCAN_LOG_DIR="$E2E_INPUTS/scan-logs"
export LAUEPORTAL_INDEXING__DEFAULTS__GEOMETRY_FILE="$E2E_INPUTS/calibration/geoN_2022-03-29_14-15-05.xml"
export LAUEPORTAL_INDEXING__DEFAULTS__CRYSTAL_FILE="$E2E_INPUTS/calibration/Ni.xml"
export LAUEPORTAL_RECONSTRUCTION__DEFAULTS__GEOMETRY_FILE="$E2E_INPUTS/calibration/geoN_2022-03-29_14-15-05.xml"
export LAUEPORTAL_APP__STATIC_DIR="$REPO_ROOT/frontend/dist"
export LAUEPORTAL_REMOTE__ENABLED=false
SEED_DEMO_OPTIONS=(--crux-run)
export LAUEPORTAL_EXECUTION__JOB_TIMEOUT_SECONDS=86400
export LAUEPORTAL_EXECUTION__HEARTBEAT_SECONDS=15
export LAUEPORTAL_EXECUTION__PROGRESS_SECONDS=5
export LAUEPORTAL_EXECUTION__CANCEL_POLL_SECONDS=2
export LAUEPORTAL_EXECUTION__STALE_HEARTBEAT_SECONDS=300
export LAUEPORTAL_EXECUTION__SHUTDOWN_GRACE_SECONDS=600
export LAUEPORTAL_EXECUTION__DISPATCH_SECONDS=1.0
export LAUEPORTAL_EXECUTION__RECONCILE_SECONDS=60
export LAUEPORTAL_EXECUTION__CREATION_TIMEOUT_SECONDS=600
export LAUEPORTAL_EXECUTION__WORKERS=1
export LAUEPORTAL_EXECUTION__MAX_IN_FLIGHT=2
export LAUEPORTAL_EXECUTION__MAX_RUN_INPUTS=1000000
export LAUEPORTAL_EXECUTION__JOB_LOG=""   # the job processes write to the worker's log (e2e-worker.log)
export LAUEPORTAL_EXECUTION__RECONSTRUCTION_WORKERS=1
export LAUEPORTAL_EXECUTION__NUM_THREADS=1
export LAUEPORTAL_EXECUTION__MEMORY_LIMIT_MB=512
export LAUEPORTAL_EXECUTION__VERBOSE=1
# Live processing on (e2e/live.spec.ts), pinned: a stand-in DM (below; no SSH host), the DM catalog
# read every second while files arrive and the DAQ list kept 5 s (its failures longer, up to 60 s), the arrivals read
# every second and the snapshots every 2 s, and a quarter of the host's cores for the live runs (at least 1).
export LAUEPORTAL_LIVE__ENABLED=true
export LAUEPORTAL_LIVE__DM_SSH_HOST=
export LAUEPORTAL_LIVE__CATALOG_SECONDS=1
export LAUEPORTAL_LIVE__CATALOG_IDLE_SECONDS=30
export LAUEPORTAL_LIVE__DAQ_LIST_SECONDS=5
export LAUEPORTAL_LIVE__ARRIVALS_SECONDS=1
export LAUEPORTAL_LIVE__PUBLISH_SECONDS=2
export LAUEPORTAL_LIVE__CPU_SHARE=0.25
# The stand-in DM: DM's setup file and API installed under $FAKE_DM by `up` (prepare_stack)
# with tests.remote_support.FakeDMInstall, so the real DM client and API script run on a stand-in `dm` package over
# its state.json (no network, DM or SSH). That state is not JSON until a spec writes one, so every DM call fails: the
# DAQ list is unavailable and a session's source is `laueportal live notify`; the DAQ session's spec writes a DAQ and
# its catalog's rows, then puts this state back (e2e/stack-tools.ts' setFakeDM).
FAKE_DM="$LAUE_DEV_WORKSPACE/e2e/fake-dm"
export LAUEPORTAL_LIVE__DM_SETUP_FILE="$FAKE_DM/dm-setup.sh"
prepare_stack() {
  rm -rf "$FAKE_DM"
  local install='import sys, pathlib, tests.remote_support as fake; fake.FakeDMInstall(pathlib.Path(sys.argv[1]))'
  PYTHONPATH="$REPO_ROOT/backend:$PYTHONPATH" python -c "$install" "$FAKE_DM" || return 1
  printf 'not JSON\n' >"$FAKE_DM/state.json"
}
