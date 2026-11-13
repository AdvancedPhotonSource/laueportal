# Development setup

A development machine from scratch, and what to do when something breaks. The commands are in the
[README](../../README.md); the rules in [`AGENTS.md`](../../AGENTS.md).

## Prerequisites

- Linux x86-64. On a machine with little memory, run at most two heavy processes at once (builds, full test suites,
  browsers).
- On the `PATH`: `git`, `curl`, `uv` (0.12) and Node 26 (through nvm). Nothing else is installed globally: `make setup` brings
  micromamba, the conda env (Python 3.12, compilers, GSL, HDF5, supervisor) and pnpm 12 into the scratch space.
- Network access to conda-forge, PyPI, the npm registry and GitHub (lauelab and pnpm releases).
- No local data: every development input is synthetic (the committed fixtures under `backend/tests/fixtures/`
  and what `laueportal seed demo` makes from them with `laueportal.science.synthetic`).
- Playwright 1.64's Chromium headless shell (`chromium_headless_shell-1248`) in `~/.cache/ms-playwright`, installed
  once by hand: `make e2e` never downloads browsers (`pnpm -C frontend exec playwright install --dry-run chromium`
  shows what is needed and where).

## Fresh machine

```bash
git clone https://github.com/advancedphotonsource/laueportal.git
cd laueportal
make setup
make dev-up && make dev-status
make dev-down
make check
```

`make setup` (`scripts/setup.sh`) is idempotent: each step checks its result and skips when it is in place. Everything
it makes goes into the development workspace ([below](#the-workspace)), by default `.workspace/` in the checkout, and it
downloads only from conda-forge, PyPI, the npm registry and GitHub (lauelab and the release binaries).

1. The workspace directories in `$LAUE_DEV_WORKSPACE`.
2. micromamba (2.9.0-0), the binary from its GitHub release (checksum checked), in `tools/`, then the conda env in
   `env/` from `environment.lock` (Python 3.12, compilers, GSL, HDF5, supervisor).
3. The locked Python packages (`requirements/dev.lock`, installed with `--no-deps`, then checked).
4. lauelab from GitHub at `b846009`, built inside the env (its compilers and conda-forge's GSL), then an import
   check of its merge APIs and its incremental `PreparedScan` (live runs).
5. The backend, editable.
6. pnpm 12.10.1, the standalone binary from its GitHub release (checksum checked), in `tools/`.
7. `pnpm -C frontend install --frozen-lockfile` (refused when `node_modules` came from another workspace's store).
8. A native lauelab smoke test on a committed synthetic frame (`scripts/lauelab_smoke.py`); nothing is copied.
9. The dev config: `config/dev.toml` from `deploy/config/dev.toml.template`, its paths filled in for this workspace and
   this checkout (`scripts/render-dev-config.py`; never overwritten: edit this copy).

`make setup-backend` runs only steps 1-5, 8 and 9, and `make setup-frontend` only steps 1, 6 and 7; each CI job installs
only the parts that its checks use.

## Python lock sets

`requirements/` has one hash-locked set per place the Python code runs:

| Lock (from) | Where it runs | `make setup` installs it |
|---|---|---|
| `base.lock` (`base.in`) | The portal | as part of `dev.lock` |
| `launcher.lock` (`launcher.in`) | The DM-host launcher of remote runs, Python 3.12 (the deployed launcher's pins) | as part of `dev.lock` |
| `compute.lock` (`compute.in`) | The compute nodes of remote runs: the standalone and MPI runners (the deployed compute environment's pins) | no |
| `dev.lock` (`dev.in`) | This dev env: `base.in`, `launcher.in` and the dev tools | yes |

- **One cutoff.** `make lock` compiles all four with the cutoff in their `# exclude-newer:` header (pass the same
  `LOCK_DATE=` to re-lock without moving it). The Makefile's `LOCK_EXEMPT` lists the approved exceptions to the
  cutoff; it is empty while the cutoff is past every pin (globus-compute-sdk 4.19.0 is the deployed launcher's).
- **Consistent versions.** `dev.lock` is compiled first; `base` and `launcher` are constrained to its versions, so a
  host can install both (as the combined APS env does). So is `compute`, except for `compute.in`'s own pins (PyYAML is
  6.0.3 there and 6.0.2 elsewhere, as the deployed compute environment has it).
- **Not in the locks:** numpy, h5py, cffi and pycparser are conda-forge's (constrained to the env's versions through
  `conda-pins.txt`), and lauelab is installed from GitHub at the commit pinned in `scripts/setup.sh`. Moving that pin
  needs a maintainer's decision, the batch parity suite again, and a check of each private lauelab name the portal uses
  ([the backend's conventions](../contributing/backend.md#import-rules), "Private lauelab names", with their
  tests).

## The workspace

Scratch space, never committed. `scripts/env.sh` points at it through `$LAUE_DEV_WORKSPACE`: an exported value wins;
else the main checkout's `scripts/env.local.sh`, when present, sets it; else it is `.workspace/` in the main checkout.
Git ignores both `scripts/env.local.sh` and `.workspace` (so `git clean -x` would delete the workspace). To keep it
elsewhere for every shell and `make` target of a clone and its worktrees:

```bash
echo 'export LAUE_DEV_WORKSPACE=/path/to/workspace' > scripts/env.local.sh   # in the main checkout, before make setup
```

`scripts/env.sh` sources `scripts/env.local.sh` in every shell, `LAUE_DEV_WORKSPACE` exported or not, so it also holds
the machine's other settings. Originals that must stay protected whatever `LAUEPORTAL_CONFIG` names go there as
`export LAUEPORTAL_DATABASE__PROTECTED='["/path/to/original.db"]'` (a JSON list; it replaces the configuration's
`database.protected`).

| Path | What |
|---|---|
| `env/`, `mamba/` | The conda env (Python, compilers, supervisor) and micromamba's root prefix |
| `tools/` | micromamba, pnpm (with `pnpm-dist/`) |
| `pnpm-store/`, `pnpm-cache/` | pnpm's package store and cache |
| `config/dev.toml` | The dev settings (`LAUEPORTAL_CONFIG`; edit this copy, not the template) |
| `db/` | `dev.db` (the dev stack), `e2e.db` (`make e2e`), `crux-demo.db` ("A demo with a run on CRUX"), and any original you keep: list it in `config/dev.toml`'s `database.protected` (absolute paths: a database, with its `-wal`/`-shm`/`.sha256` files, or a folder), and `seed demo`, `seed slow-runs` and `db backup` never write it, even with `--force` |
| `runs/`, `e2e/runs/`, `crux-demo/runs/` | The run directories of the dev, e2e and CRUX demo databases (`files.runs_root`); staged scan-log uploads in `runs/.uploads/` |
| `logs/` | Stack logs (`dev-<program>.log`, `e2e-<program>.log`, `supervisord-*.log`) |
| `tmp/` | `TMPDIR` for everything, plus the supervisor sockets and pidfiles |
| `cache/frontend/` | `make check-frontend`'s tsc build info and ESLint results (ESLint's for one state of `frontend/`: its type-aware rules read other files' types) |
| `worktrees/` | Optional git worktrees (side experiments) |

## The path check

`make check` starts with `scripts/check-paths.sh`, which fails, listing them, on tracked lines that name a home path
(`/home/<name>/...`, `/Users/<name>/...`, `~<name>/...`) or the folder `proj` in a home (after `~`, `$HOME`, `${HOME}`
or `/root`, with or without a slash after it): a machine's paths belong in its workspace and `scripts/env.local.sh`.
Tests and examples use the neutral name `user` (`/home/user/...`, `~user/...`); an example that needs another such path
marks its line `path-check: allow` (in a comment, e.g. `<!-- path-check: allow -->`).

## A demo with a run on CRUX

R4 shows a `crux` run's remote phase on the job page's Remote Run card and in the run monitor's Status cell: R1's
request on CRUX, recorded as running there with a golden observation. No DM job stands behind it, so only
`laueportal seed demo --crux-run` adds it (the default demo, `dev.db`, has none; `make e2e` seeds `e2e.db` with it),
into a database of its own, served with remote execution off:

```bash
. scripts/env.sh                                # in the checkout
make dev-down                                   # one stack at a time
export LAUEPORTAL_REMOTE__ENABLED=false         # whatever dev.toml says (--crux-run refuses otherwise)
export LAUEPORTAL_DATABASE__PATH="$LAUE_DEV_WORKSPACE/db/crux-demo.db"
export LAUEPORTAL_FILES__RUNS_ROOT="$LAUE_DEV_WORKSPACE/crux-demo/runs" LAUEPORTAL_FILES__ROOTS__RUNS="$LAUE_DEV_WORKSPACE/crux-demo/runs"
export LAUEPORTAL_FILES__UPLOADS_DIR="$LAUE_DEV_WORKSPACE/crux-demo/runs/.uploads"
laueportal seed demo --crux-run                 # the demo plus R4 (job 6); --force builds it again
make dev-spa                                    # stack.sh does not rebuild a stale SPA; make dev-up does
scripts/stack.sh up --seed=never deploy/supervisor/dev.conf
make dev-down                                   # when done; then close this shell (the next make dev-up is on dev.db)
```

- **Never enable remote execution on this database** (or on any database that holds R4): the worker's remote poller
  would ask DM about R4 on every pass, for ever, logging an error each time, and a Stop on R4 would never complete.
  Start its stack only from a shell with the variables above.
- R4 stays Running, so the lists, the status page's Active Runs and scan 990001's cards keep polling every 5 s here;
  the idle states are `dev.db`'s.
- Remove it with the stack stopped: `rm -r "$LAUE_DEV_WORKSPACE/db/crux-demo.db" "$LAUE_DEV_WORKSPACE/crux-demo"`.

## Another workspace (`LAUE_DEV_WORKSPACE`)

`scripts/env.sh` uses the main checkout's workspace ([above](#the-workspace)) unless `LAUE_DEV_WORKSPACE` is exported.
To work against another workspace (for example, to run `make setup` from an empty one), use a **separate worktree or
clone** for it, and export `LAUE_DEV_WORKSPACE` in every shell of that checkout:

```bash
git worktree add --detach /path/to/checkout main   # from the main checkout
cd /path/to/checkout
export LAUE_DEV_WORKSPACE=/path/to/other-workspace   # before . scripts/env.sh and every make target
make setup
```

Never point an existing checkout at another workspace: its `frontend/node_modules` is linked from the pnpm store of the
workspace it was installed with, and `pnpm install` would purge it and re-link it from the other workspace's store, tying
the checkout to that workspace (and breaking it once that workspace is wiped). `make setup` refuses step 7 when
`frontend/node_modules` was installed from another store (`scripts/pnpm-store-check.sh`); `SETUP_ALLOW_STORE_SWITCH=1`
re-links the checkout on purpose.

## Branches and worktrees

Work on a branch in the main checkout. A worktree is useful for a side experiment:

```bash
. scripts/env.sh                              # in the main checkout
git worktree add "$LAUE_DEV_WORKSPACE/worktrees/spike-grid" -b spike/grid-timing main
cd "$LAUE_DEV_WORKSPACE/worktrees/spike-grid"
. scripts/env.sh                              # every shell; make targets source it themselves
pnpm -C frontend install --frozen-lockfile    # once: node_modules belong to the checkout
```

- **One env for all checkouts.** The conda env, the config and the databases are shared: a worktree's
  `scripts/env.sh` uses its main checkout's workspace (and `scripts/env.local.sh`). It sets
  `REPO_ROOT` to the checkout it lives in and puts that checkout's `backend/src` first on `PYTHONPATH`, so `laueportal`,
  pytest and the stacks run the worktree's code even though the env has the main checkout installed editable.
- **One stack at a time.** The dev and e2e stacks share port 2100. A stack started from another checkout is reported,
  never touched: stop it from the checkout that started it. A worktree's stack serves its own `frontend/dist`.
- **Tests need no stack.** The live worker tests start their own `laueportal worker` on a test database
  (`backend/tests/worker_support.py`).

## Troubleshooting

| Symptom | What to do |
|---|---|
| lauelab fails to link (`relocation ... can not be used when making a shared object; recompile with -fPIC`, from `libgsl.a` or `libgslcblas.a`) | It was built against Ubuntu's `libgsl-dev`, whose static libraries are not position-independent. Build it only inside the env, as `make setup` does (`micromamba run`: conda-forge's GSL 2.8 is PIC). Check that `which gsl-config` points into `$LAUE_DEV_WORKSPACE/env/bin` after `. scripts/env.sh`; never install lauelab with the system's compilers or pip. |
| lauelab is at another commit, or half-installed | `make setup` reinstalls it when its recorded commit is not `d5de751`; the smoke test of step 8 checks the native code. |
| `make setup` stops at step 7 ("installed from another store") | `node_modules` was linked from another workspace's pnpm store. Use a checkout per workspace (below); `SETUP_ALLOW_STORE_SWITCH=1 make setup` re-links this one on purpose. |
| `pnpm install` refuses a package as too new, or a build script | `frontend/pnpm-workspace.yaml` resolves only releases at least 7 days old (`minimumReleaseAge`) and runs only the build scripts it allows (`allowBuilds`); adding either is a dependency change for a maintainer ([`AGENTS.md`](../../AGENTS.md)). |
| `pnpm: command not found` | Source `scripts/env.sh`: pnpm is `$LAUE_DEV_WORKSPACE/tools/pnpm`, on the `PATH` only through it. |
| `make dev-up` or `make e2e` says a port is busy, or a stack runs elsewhere | The dev and e2e stacks share 2100: stop the other one from the checkout that started it (`make dev-down`); `ss -ltnp` shows what listens. |
| `make dev-status` fails | Exit 3: the stack is not running; exit 1: a program is not `RUNNING`, or `/api/v1/health` does not report the database OK and the worker running. Read `make dev-logs` (`$LAUE_DEV_WORKSPACE/logs/dev-<program>.log`). |
| The API answers 503 "database missing", or a database "migrated with the early numbering of the revisions" is refused | With the stack stopped, `laueportal seed demo --force` rebuilds the demo database and empties its runs root (`make dev-up` seeds only a missing one). |
| A build, the test suite or the browser is killed (OOM) | Run at most two heavy processes at once (builds, full test suites, browsers). |
