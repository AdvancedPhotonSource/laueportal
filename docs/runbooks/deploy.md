# Deploying the portal

Manual deployment on the portal host: one supervisord runs the API (which serves the built SPA too) and the worker (with
remote execution, the remote poller is its thread; the job table is the queue). Where the portal replaces the earlier
portal, step 4 migrates the earlier portal's database. Everything listens on `127.0.0.1`; users come through an SSH
tunnel or a proxy on the host. Run every step as the portal account (never root): it owns the installation, the database
and the run directories it writes.

**Decide before the first start** (render values, step 3): the installation root `ROOT` on a local disk (e.g.
`/local/laueportal`), the runs root `RUNS_ROOT` on NFS (with the earlier portal's database, the earlier portal's
`DEFAULT_VARIABLES.root_path`), the API's port `API_PORT`, the host name users reach the portal by `HOST_NAME`
(`localhost` when they only tunnel), and whether remote execution is on (`REMOTE_ENABLED`). The defaults of the forms'
Geometry File and Structures File and the scan-log folder are set in `prod.toml` afterwards (left empty by the
template).

**When:** steps 1-3 ahead of time; with the earlier portal's database, its rehearsal (step 4) too, while the earlier
portal runs, and its migration (step 4) once the earlier portal has stopped, then the first start (5) and the conversion
(9). Copies of the portal's database are taken by hand (`laueportal db backup`: before an upgrade or a restore); the
earlier portal's stopped database file is copied with `cp -p`, never opened (step 4); the nightly crontab is optional
(step 10).

> **Never run the portal on the earlier portal's runs root while the earlier portal runs, nor the earlier portal on it
> after the portal made runs (a rollback).** Each portal hands out the run IDs its own database has not used yet, so the
> two would write the same run folders (where each run's Output Path puts them, usually
> `analysis/scan_<scan>/rec_<rec>/index_<run>` and `analysis/scan_<scan>/rec_<run>/data` under `RUNS_ROOT`). A rollback
> moves the portal's run folders aside first (step 4).

## Layout

| Path | What | Mode |
| --- | --- | --- |
| `<ROOT>/src` | A checkout of the reviewed commit (detached), with the built SPA in `frontend/dist` | |
| `<ROOT>/envs/laue-portal` | The conda env (Python, supervisor) with `base.lock`, lauelab and the backend | |
| `<ROOT>/config` | `prod.env` (the programs' environment), `prod.toml`, `supervisord.conf`, `bdp.yaml` (remote) | 0700 |
| `<ROOT>/db` | `laueportal.db` (and its `-wal`, `-shm`): on the local disk, in WAL mode | 0700 |
| `<ROOT>/logs` | supervisord's and each program's log, rotated by supervisor; the job processes' `jobs.log`, rotated by the worker when a job starts | 0700 |
| `<ROOT>/state` | supervisord's socket and pidfile | 0700 |

## 1. Install

From a checkout of the reviewed revision, as in the remote-execution runbook's "Installation" (its **Portal**
bullet; a host that is also the DM launcher follows that bullet too):

```bash
root=/local/laueportal                        # ROOT
rev=<the reviewed commit>
git clone <the repository> "$root/src" && git -C "$root/src" checkout --detach "$rev"
mkdir -p "$root/envs" && mkdir -p -m 0700 "$root/config" "$root/db" "$root/logs" "$root/state"
micromamba create -y -p "$root/envs/laue-portal" --file "$root/src/environment.lock"   # or -f environment.yml
py="$root/envs/laue-portal/bin/python"
uv pip install --python "$py" --require-hashes --no-deps -r "$root/src/requirements/base.lock"
micromamba run -p "$root/envs/laue-portal" uv pip install --python "$py" --no-deps \
    "lauelab @ git+https://github.com/AdvancedPhotonSource/lauelab.git@b846009a9628d9dfea2fd531eb5e6607d9b8094b"
uv pip install --python "$py" --no-deps -e "$root/src/backend"
uv pip check --python "$py"
```

**The SPA.** Node and pnpm are not in the production env: build it on the development machine at the same commit
(`make build`) and copy the build into the checkout, then check it is there:

```bash
rsync -a --delete frontend/dist/ <portal host>:/local/laueportal/src/frontend/dist/   # on the development machine
test -f "$root/src/frontend/dist/index.html"                                          # on the portal host
```

`laueportal serve` without `--reload` refuses to start (exit 2) while `app.static_dir` has no `index.html`.

## 2. The production environment file

`<ROOT>/config/prod.env` holds what the programs and every `laueportal` command need from the environment: the
settings file and the env's `PATH`. Create it once under `umask 077` in a subshell (the umask ends with it; in the
operator's shell it would make every later file and folder private, the run folders a conversion writes on NFS
too):

```bash
( umask 077
cat > "$root/config/prod.env" <<EOF
export LAUEPORTAL_CONFIG=$root/config/prod.toml
export PATH=$root/envs/laue-portal/bin:\$PATH
EOF
)
stat -c %a "$root/config/prod.env"                                       # 600
```

Every shell (and cron line) that runs a `laueportal` command or supervisord sources it first:
`. /local/laueportal/config/prod.env`.

## 3. Render the templates

`deploy/render-prod.py` writes `supervisord.conf` and `prod.toml` into the config folder (`<ROOT>/config`) from
`deploy/supervisor/prod.conf.template` and `deploy/config/prod.toml.template`, filling in the values (it refuses a missing, unused or unfit value, a path
ending in `/`, and an existing file without `--force`: once edited, `prod.toml` is yours). Keep the values: an
upgrade renders again with them (step 7).

```bash
"$py" "$root/src/deploy/render-prod.py" ROOT="$root" \
    RUNS_ROOT=<runs root on NFS> API_PORT=<port> HOST_NAME=<host name> REMOTE_ENABLED=false
```

`prod.toml` then has the production values: the runs root, the database at `<ROOT>/db/laueportal.db` in WAL mode,
the execution values (35 threads, 50,000 MB, 4 workers), the file probe's bounds and the cache sizes, every key
commented. Edit it: the data roots users browse in `[files.roots]`, one line each (`name = "/absolute/path"`) in place
of the `data` placeholder (until it is replaced, the settings do not load: `root 'data' must be an absolute path`), the
forms' `geometry_file` and `crystal_file`, and `[files] scan_log_dir`; then check the result:

```bash
. "$root/config/prod.env" && laueportal config show
```

- **Host names** (`[app] allowed_hosts`): a request whose `Host` (port aside) is not listed is a 400 "Invalid host
  header", so that a page whose DNS name was pointed at the host cannot read the API. Add every name users reach
  the portal by; `localhost` and `127.0.0.1` cover an SSH tunnel.
- **One origin:** the API serves the SPA, and the API refuses a change from another origin (`Sec-Fetch-Site`
  other than `same-origin`, including another port or subdomain of the same site). A proxy forwards the whole
  site (`/`, the SPA and `/api`) to `127.0.0.1:<API_PORT>` under one name; never put the SPA and the API on
  different names or ports.
- **Behind a proxy** (nginx or Apache on the host), the proxy must pass the `Host` header as the browser sent it,
  which neither does by default: nginx sends `127.0.0.1:<API_PORT>` (`$proxy_host`) and Apache too
  (`ProxyPreserveHost Off`). `allowed_hosts` admits that name, so the rebinding guard is then off without a sign.
  Set it beside the proxy line, then check it through the proxy (step 5):

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:<API_PORT>;
      proxy_set_header Host $host;
  }
  ```

  ```apache
  ProxyPreserveHost On
  ProxyPass        / http://127.0.0.1:<API_PORT>/
  ProxyPassReverse / http://127.0.0.1:<API_PORT>/
  ```

  Add the proxy's name to `allowed_hosts` (`HOST_NAME`). A refused name shows in `<ROOT>/logs/api.log` as
  "Refused a request for host '...'".
- **Memory:** check the host's RAM (`free -g`) against the API's caches. At `prod.toml`'s sizes the one API process can
  reach about 4.5 GB (`[indexing] dataset_cache_size = 6` datasets of up to about 540 MB each, a large run's with its
  tables, plus `[reconstruction] product_cache_mib = 1024` of wire-viewer products, plus the frames in flight), beside
  the worker's runs (`[execution] memory_limit_mb`, 50,000). On a host with less to spare, lower `dataset_cache_size` (1
  to 16; each dataset less saves up to about 540 MB) and `product_cache_mib`, then restart the API; a smaller cache only
  reloads more often.
- **Remote execution:** `REMOTE_ENABLED=true` sets `[remote] enabled` in `prod.toml`: the portal offers the remote
  targets and the worker runs the remote poller as a thread (no program of its own); change it later in step 6. Put the
  private `bdp.yaml` at `<ROOT>/config/bdp.yaml` (`docs/runbooks/remote-execution.md`). The main DM workflow's stages
  are `01-SUBMIT`, `02-MONITOR`, `03-PERMISSIONS` and `04-CHECK`: re-render it and update the registered `dm.workflow`
  once (`remote-execution.md`, "Returned-file permissions"); `dm.permissions_timeout_seconds` (300) bounds that stage's
  DM call.

## 4. The database

The API never creates the database (without it, a 503): the `db` commands do, on `database.path`
(`<ROOT>/db/laueportal.db`), or on the file `--db` names. Each sources `prod.env` first (`. "$root/config/prod.env"`).

**A new database:** `db upgrade` creates it at head.

```bash
. "$root/config/prod.env"
laueportal db upgrade      # creates <ROOT>/db/laueportal.db at head
laueportal db check
```

**The earlier portal's database** is migrated instead: a copy of it, its own file left as it is.

**Ahead of time, optional: a rehearsal on a copy** of the earlier portal's database, to see that today's data migrates
(the earlier portal keeps running and its database is only read). The copy lies on the local disk (WAL), and every
command names it with `--db`:

```bash
. "$root/config/prod.env"
mkdir -p -m 0700 "$root/db/rehearsal" && copy="$root/db/rehearsal/laueportal.db"
"$py" - <the earlier portal's database> "$copy" <<'EOF'   # a consistent copy (SQLite's backup), the earlier portal's file opened read-only
import pathlib, sqlite3, sys
source = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
with sqlite3.connect(sys.argv[2]) as copy:
    source.backup(copy)
EOF
laueportal db stamp --detect --db "$copy"   # 0001 (v1 dde1bde's schema) or 0002 (v1 3ad1557's or cb1159c's)
laueportal db upgrade --db "$copy"
laueportal db check --db "$copy"            # ends with "<copy>: OK"
rm -r "$root/db/rehearsal"
```

`db stamp --detect` refuses a database older than v1 `dde1bde`'s schema: run the earlier portal's
`scripts/migrate_workflow_database.py <legacy> <out>` (at `dde1bde` or later) on a copy first, then stamp `<out>`. The
script at `cb1159c` gives `<out>` `job.remote_observation` and none of the live tables below (only the earlier portal's
app creates them, at its start): `db stamp --detect` takes it as v1 `cb1159c`'s schema without its live tables, records
`0002`, drops nothing and keeps the column (`stamped 0002 (detected: v1 cb1159c's schema without its live tables)`).

A database of v1 `cb1159c` (live processing: v1 `3ad1557`'s schema with `job.remote_observation` and the tables
`live_session`, `live_checkpoint` and `live_point`) is stamped `0002` too: `db stamp --detect` refuses it, changing
nothing, while one of its live sessions has not ended (its state is not finished, failed or cancelled; the message names
each, `live session <id>: '<state>' (R<id>, I<id>)`): the portal does not continue live sessions the earlier portal
started. A rehearsal takes another copy once no session is open (a session is not ended just to rehearse); for the
migration, the earlier portal's session is ended first (below, 1). Otherwise it drops the three live tables and records
`0002` in one transaction, and says so
(`stamped 0002 (detected: v1 cb1159c's schema; its live tables live_point, live_checkpoint, live_session dropped)`).
Nothing in the portal reads those tables (the earlier portal's live tables go); the sessions' runs and jobs stay as
history, ordinary finished runs (their queue IDs `live_<session>_…` never make them live runs), and
`job.remote_observation` stays with its values. Nothing else of v1 `cb1159c`'s schema is dropped by the stamp: the rest
is v1 `3ad1557`'s, which the chain migrates as before (its `calib` table goes at `0004`). Without the live tables (v1
`cb1159c`'s migration script's output, above) there is nothing to drop and no session: `0002` is recorded and the column
kept. A `cb1159c`-like database with anything the stamp does not know (another table or column, an index on a live table
or the earlier portal's own on other columns, a view or a trigger; with or without the live tables) is refused with what
differs. `laueportal db stamp 0002` refuses either `cb1159c` schema and says to use `--detect`.

**The migration:**

1. **The earlier portal stopped, with nothing in flight.** End its live session if one is open (its Live page's
   **Stop**, until its header reads **Ready to start**; a session in Recovery too), then stop its app (its web process)
   so that nothing new is submitted, and keep its worker and its `bdp_poller` running. Wait until no run is Queued or
   Running in it, remote runs included, and no live session is open: a read-only look at its database, run while its
   worker runs, prints nothing then (status 0 is Queued, 1 Running; `backend` and `external_state` are v1 `3ad1557`'s
   columns, `live_session` is v1 `cb1159c`'s table, and a session in Recovery has Failed jobs, so only its own state
   shows it):

   ```bash
   "$py" - <the earlier portal's database> <<'EOF'   # prints nothing once drained
   import pathlib, sqlite3, sys
   db = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
   remote = db.execute("SELECT 1 FROM pragma_table_info('job') WHERE name = 'backend'").fetchone()
   columns = "job_id, status, backend, external_state" if remote else "job_id, status"
   for row in db.execute(f"SELECT {columns} FROM job WHERE status IN (0, 1)"):
       print(row)
   if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'live_session'").fetchone():
       for row in db.execute("SELECT id, state FROM live_session WHERE state NOT IN ('finished', 'failed', 'cancelled')"):
           print("live session", row)
   EOF
   ```

   Then stop its worker and its `bdp_poller`. Do not run the query again once the earlier portal has stopped: on a
   stopped database even a read-only open leaves a `-wal` and `-shm` beside it, which the check below reads as an
   unclean stop. The portal is not running. No `-journal` or `-wal` file may lie beside the earlier portal's database
   (`ls <the earlier portal's database>*`): one means the earlier portal did not stop cleanly; start and stop it once,
   then look again.
2. **Back up** its file by hand, kept apart (the optional crontab's rotation never deletes a `laue-before-*` name), and
   check the backup, never the earlier portal's file: it prints `ok` and leaves nothing beside the backup
   (`immutable=1`: SQLite neither writes nor locks the file). From here on the earlier portal's own file is never opened
   by SQLite (a read-only open would leave a `-wal` and `-shm` beside it; `laueportal db backup` would open it): `cp -p`
   and `install` only copy its bytes.

   ```bash
   cp -p <the earlier portal's database> <BACKUPS>/laue-before-migration.db
   "$py" - <BACKUPS>/laue-before-migration.db <<'EOF'   # the backup; prints ok
   import pathlib, sqlite3, sys
   copy = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
   print(copy.execute("PRAGMA quick_check").fetchone()[0])
   EOF
   ```

3. **Copy, stamp, upgrade and check:** copy the earlier portal's database to where `database.path` names it and migrate
   it there (no `--db`), then note the earlier portal's last run IDs (the rollback needs them):

   ```bash
   install -m 0600 <the earlier portal's database> "$root/db/laueportal.db"   # a copy, on the local disk
   . "$root/config/prod.env"
   laueportal db stamp --detect
   laueportal db upgrade
   laueportal db check                                        # ends with "<ROOT>/db/laueportal.db: OK"
   "$py" - "$root/db/laueportal.db" <<'EOF'                   # the earlier portal's last run IDs: note them
   import sqlite3, sys
   database = sqlite3.connect(sys.argv[1])
   for table in ("indexing_run", "reconstruction_run"):
       print(table, database.execute(f"SELECT max(id) FROM {table}").fetchone()[0])
   EOF
   ```

   Mode `0600` keeps the database to the portal account (its backups copy the mode). If `db stamp --detect` refuses the
   copy for a live session that has not ended (`live session <id>: '<state>'`), nothing was changed: delete the copy
   (`rm -f "$root/db/laueportal.db"{,-wal,-shm}`), start the earlier portal again (its worker and its `bdp_poller`,
   then its app), end the session, and take steps 1-3 again. A database older than v1 `dde1bde`'s schema goes through
   the earlier portal's migration script first (above).
4. **Point the portal at it:** `laueportal config show` gives `database.path` = `<ROOT>/db/laueportal.db` on the local
   disk with `journal_mode` `wal`, `files.runs_root` = the earlier portal's runs root (`RUNS_ROOT`; its run directories
   are read in place), and `files.roots` = the production data roots (step 3) and `runs`, the runs root. Then the first
   start (step 5) and the conversion of the XML-only runs (step 9).
5. **Rollback** (if needed). Stop the portal (`supervisorctl -c "$root/config/supervisord.conf" shutdown`) and keep its
   database (`$root/db/`), for whatever its runs did. Before the earlier portal starts, set aside the run folders of the
   portal's runs (IDs above the last ones noted at 3): the earlier portal hands out their IDs again, into the same run
   folders. A run's folder is where its Output Path put it, inside the runs root or not: list them, then move each
   listed folder that exists aside on its own mount, keeping its path below a set-aside folder, as `backup.md`'s Restore
   step 6 does:

   ```bash
   "$py" - "$root/db/laueportal.db" <last indexing run ID> <last reconstruction run ID> <<'EOF'   # the portal's runs
   import sqlite3, sys
   database = sqlite3.connect(sys.argv[1])
   for table, last in (("indexing_run", sys.argv[2]), ("reconstruction_run", sys.argv[3])):
       for run_id, folder in database.execute(f"SELECT id, output_path FROM {table} WHERE id > ?", (int(last),)):
           print(table, run_id, folder)
   EOF
   ```

   Then look at the earlier portal's own file. Nothing has opened it since the backup (2), so it holds the backup's
   bytes: `ls` shows no side file beside it and `cmp` prints nothing. Only if either shows otherwise (the file was
   opened since), restore the backup into it, as the earlier portal's account (the owner of that file; it must be able
   to read the backup): `cp` without `-p` writes into the existing file, which keeps its owner and mode. Then start the
   earlier portal on it (its worker, its `bdp_poller`, its app).

   ```bash
   ls <the earlier portal's database>*                                      # the file alone
   cmp <BACKUPS>/laue-before-migration.db <the earlier portal's database>   # prints nothing
   # only if either shows otherwise, as the earlier portal's account:
   rm -f <the earlier portal's database>-wal <the earlier portal's database>-shm <the earlier portal's database>-journal
   cp <BACKUPS>/laue-before-migration.db <the earlier portal's database>
   ```

## 5. First start

The database must be in place (step 4) and `laueportal db check` must end with `OK`.

```bash
. "$root/config/prod.env"
supervisord -c "$root/config/supervisord.conf"
supervisorctl -c "$root/config/supervisord.conf" status   # api, worker RUNNING
```

**Smoke checks** (`<port>`: `API_PORT`):

```bash
curl -fsS http://127.0.0.1:<port>/api/v1/health                 # "db": "ok", "worker": "running"
curl -fsS http://127.0.0.1:<port>/scans | grep -c 'id="root"'    # 1: the SPA
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: rebound.example' http://127.0.0.1:<port>/api/v1/health   # 400
curl -sI http://127.0.0.1:<port>/ | grep -i -E '^(x-frame-options|content-security-policy|x-content-type-options):'
```

Behind a proxy, the same refusal through it (`<proxy URL>`: what users open):

```bash
curl -s -H 'Host: rebound.example' <proxy URL>/api/v1/health    # the 400 problem "Invalid host header"
```

A `{"status": "ok", ...}` answer there means the proxy replaces the `Host` header (step 3); the proxy's own error
page (another of its sites answering that name) is fine too.

Then in a browser through the tunnel or the proxy: the Scans list, a scan's page, an indexing run's viewer, the
Run Monitor, and a file browser listing under each root (a 404 for a root this host does not mount).

- **Logs:** `<ROOT>/logs/{api,worker,supervisord}.log` (the remote poller's lines are the worker's), rotated by
  supervisor (10-20 MB, 5-10 kept). `supervisorctl -c "$root/config/supervisord.conf" tail -f api`. The job
  processes write their own, `<ROOT>/logs/jobs.log` (`[execution] job_log`: each run's and conversion's lines, a live
  run's catalog and follower lines among them; appended by every job process, so a job process whose worker was
  killed still logs). Supervisor does not rotate it: the worker does, only when it starts a job (a run or a
  conversion) and the file has reached 20 MB, 10 kept. So a long run or a live session writes on past 20 MB until the
  next start, and a job process alive across a rotation goes on writing into the renamed file (`jobs.log.1`):
  `tail -F "$root/logs/jobs.log"` (`-F` follows the new file after a rotation).
- **Restart a program:** `supervisorctl -c "$root/config/supervisord.conf" restart api` (the worker lets a running run
  drain for up to `execution.shutdown_grace_seconds`, 600 s, then kills its job process; its `stopwaitsecs` is 660; a
  second stop kills at once). Stopping or restarting the worker ends an open live session: its runs start nothing more
  and end Failed (interrupted; the files waiting are not run), there is no Resume, and a new DAQ session admits and
  processes the folder's files again from its first. End the session first (the Live page's Stop, until its header says
  it has ended), or accept losing it. After a change to `prod.env`, restart supervisord itself
  (`supervisorctl ... shutdown`, then start it as above): its programs keep the environment it started with. The job
  table is the queue: a run created while the worker is stopped waits Queued and starts once it is back. One
  worker serves a database (`laueportal.db.worker.lock`): a second one exits 1 and names the first. A job process whose
  worker was killed (the OOM killer, `kill -9`) runs its run to the end, its lines in `jobs.log`, and the restarted
  worker starts nothing beside it; until it restarts, the status page and the navbar say the worker is not running (on
  the API's host; a worker on another host shows late). A program that `status` shows FATAL is started by hand:
  `supervisorctl ... start <program>`.
- **At boot** nothing starts unless you add it, e.g. the portal account's crontab:
  `@reboot . /local/laueportal/config/prod.env && supervisord -c /local/laueportal/config/supervisord.conf`.

## 5b. LaueMatching's data folder

LaueMatching needs a folder of its own: the orientation database and one folder per named forward cache. Create it where
the worker can read and write it, download the database into it, and set `[laue_matching] data_root` in `prod.toml`:

```sh
laue-index fetch-db --dest <data folder>     # 7.2 GB; checks its SHA-256
```

```text
<data folder>/100MilOrients.bin
<data folder>/Ni_geoN_5-30/forward.bin        a named forward cache: 3 to 12 GB, by Max Spots per Orientation
<data folder>/Ni_geoN_5-30/forward.bin.meta.json
<data folder>/Ni_geoN_5-30/hkls.csv
```

Until it is set, the form refuses a LaueMatching run with one line, and LaueGo and wire runs are unaffected. Restart the
API and the worker after a change.

- **Speed.** The indexer maps these files into memory only when their path starts with `/dev/shm`; from anywhere else,
  each frame reads the whole database and forward cache, about 19 GB at 30 spots. On a development workstation (Ryzen
  AI MAX+ 395, 16 cores, unified memory) with both files in `/dev/shm`, a frame took about 1.4 s; production hardware
  will differ. Measure one run on the production host and folder (a NAS such as `/gdata`) before relying on it, and set
  `threads` from that run.
  If it is too slow, set `data_root` to a folder under `/dev/shm`: a copy of the folder, or a symbolic link to it (the
  check is on the path's text, so the files are then mapped through the page cache; not yet measured on a NAS).
- **Memory.** The database and a 30-spot cache take about 19 GB of RAM while mapped.
- **Caches.** A cache fits one geometry, crystal, energy range and spot count exactly: any change, however small, needs
  another cache. The portal lists the ones that fit, never overwrites one and has no way to delete one: remove a cache's
  folder by hand when no run is using it. A finished run keeps the cache's record in its `run.json`.
- **Install.** `laue-index` compiles its C indexer when the locked requirements are installed, so the portal's host
  needs a C compiler with OpenMP, as `make setup` provides.

## 5c. The geometry folder

The Calibration page saves geometry files into one shared folder, and starts a calibration from its newest file. Choose a
folder inside a file root (`[files.roots]`) that the API's user can write, put the current geometry file in it with a
name of the form `geoN_YYYY-MM-DD_HH-MM-SS.xml`, and set it in `prod.toml`:

```toml
[calibration]
geometry_dir = "<geometry folder>"
```

Restart the API. Until it is set, the page calibrates but cannot save, and its Save card shows why. A file is never
replaced: a second save in the same second is refused.

## 6. Remote execution on or off

The switch is `prod.toml`'s `[remote] enabled`, rendered from `REMOTE_ENABLED`: with it, the API offers the remote
targets and the worker runs its remote poller thread. Both read it at start, so restart both. Before turning it on:
`bdp.yaml` in place and `laueportal remote poll --once` passing (`remote-execution.md`; run it before the worker's
restart, as the worker's thread then holds the poller lock and the command is refused). While another poller holds the
DM inbox's lock, the worker's thread waits for it (the status page shows the poller late; the worker's log names the
lock) and the rest of the worker runs on.

```bash
on=true                                                     # or false
sed -i "/^\[remote\]/,/^\[/ s/^enabled = [a-z]*/enabled = $on/" "$root/config/prod.toml"
sed -n "/^\[remote\]/,/^\[/ s/^enabled = \([a-z]*\).*/\1/p" "$root/config/prod.toml"           # the same word
supervisorctl -c "$root/config/supervisord.conf" restart worker api   # the worker drains first (up to 600 s)
grep 'remote poller' "$root/logs/worker.log" | tail -2      # on: "The remote poller holds .../.poller.lock; ..."
```

A later render (step 7) takes `REMOTE_ENABLED` as it is now.

### Live processing on or off

The Live page's API (`GET /live/daqs`, `GET /live/session`, `POST /live-runs/validate`, `POST /live-runs`)
answers only while `[live] enabled = true` (a 404 otherwise, and `GET /api/v1/programs` lists only the batch
programs; the navbar's Live, and the page at `/live`, show only while it is on). It is off in the rendered
templates. The worker does not read it: its live rules, and a live run already Queued or Running, are the same either
way. `GET /programs` is a 503 while remote execution is enabled with unusable settings: a browser that read it
before keeps Live and its form; one that never did shows no Live, and `/live` the error. A live session whose source is
notifications (`laueportal live notify`) needs nothing more; one whose source is a DAQ needs DM (below). A notify
keeps an admission index per run in `<database>.notify-index/` beside the database (`jobs.arrival_index`): a cache of
`arrivals.jsonl`, rebuilt from it when lost; an index unchanged for a week is removed when a later notify run's index
is made (nothing is removed when a run ends or is deleted); the folder is a cache, safe to delete; a backup leaves it
out.

```bash
on=true                                                     # or false
sed -i "/^\[live\]/,/^\[/ s/^enabled = [a-z]*/enabled = $on/" "$root/config/prod.toml"
sed -n "/^\[live\]/,/^\[/ s/^enabled = \([a-z]*\).*/\1/p" "$root/config/prod.toml"         # the same word
supervisorctl -c "$root/config/supervisord.conf" restart api   # the api reads [live] at start
curl -s http://127.0.0.1:<port>/api/v1/programs | grep -o '"kind":"[a-z-]*"'   # on: lauego-live and wire-live too
```

Before the beamline relies on it: the live deployment test, [`live-deployment-test.md`](live-deployment-test.md) (a
DAQ source and `notify` on the facility's runs root; which source to keep).

### The `[live]` settings

All in `prod.toml`'s `[live]` (rendered with these defaults; each background rate runs only while a live run needs it).
The api reads `enabled`, `daq_list_seconds`, `daq_name_exclude` and the `dm_*` settings; the worker and its job
processes read the rest. Each reads them at start: restart the api and the worker after a change (with no live session
open: a worker restart ends it, step 5's "Restart a program"). A value outside its range is refused: the api, the
worker and every `laueportal` command exit with "invalid settings (…)", naming it.

| Setting | Default | Range | What it bounds |
|---|---|---|---|
| `enabled` | `false` | `true`, `false` | The Live page and its API, and the live programs in `GET /programs` (off: a 404, and only the batch programs). A live run already Queued or Running runs either way. |
| `batch_stop_seconds` | `60` | > 0 | How long a running local batch run stopped for a live run has to drain before the dispatcher kills its job process's group and ends it Cancelled ("stopped for a live run"). |
| `cpu_share` | `0.75` | > 0, ≤ 1 | The live runs' cores: `floor(cores × cpu_share)`, at least 1, given to each live job process (`--cores`). |
| `wire_share` | `0.67` | > 0, < 1 | With a wire run and its follower, the wire run's part of those cores (16 of 24); the follower takes the rest, each at least 1. |
| `catalog_seconds` | `5` | > 0 | A DAQ run's catalog thread: one DM call per cycle at this cadence while files arrive. |
| `catalog_idle_seconds` | `30` | ≥ `catalog_seconds` | The cadence the thread doubles up to while nothing arrives (at least `catalog_seconds`): how late the first file after a pause can be taken. |
| `catalog_rescan_seconds` | `300` | > 0 | How often the thread reads the folder's whole catalog again: the latest a file its cursor missed (a clock more than 120 s off, below) is taken. |
| `catalog_page_size` | `1000` | an integer > 0 | Rows per DM catalog request, one request at a time within a cycle's call. |
| `catalog_max_rows` | `5000` | an integer > 0 | The most rows one cycle's call reads; a query it stops goes on at the next cycle, at once. |
| `follow_seconds` | `5` | > 0 | How often a follower looks at its wire run: the run's row, then a stat of its `completed.jsonl` (read only when it changed). |
| `publish_seconds` | `10` | > 0 | The most often a live run republishes its snapshot (a wire run's `scan.h5`, a LaueGo run's preview `output.h5`), so how fresh the viewers are; also how often a waiting follower rewrites its `arrival-status.json`. |
| `arrivals_seconds` | `1` | > 0 | The most often a live run looks at its `arrivals.jsonl` (a stat, a read only when it changed): the delay between a file's admission and its processing and count. |
| `dm_setup_file` | `""` | a path, or empty | DM's setup file for the DAQ list, the bind check and the catalog. Empty: no DM call at all, and no DAQ source (the DAQ list says DM is not configured). |
| `dm_ssh_host` | `""` | a host not starting with `-`, or empty | The host the DM operations run on, over SSH in batch mode; empty: the portal host. |
| `dm_timeout_seconds` | `15` | > 0 | One DM operation (a shell and DM's API), killed past it; a catalog call starts no page request after half of it. |
| `daq_list_seconds` | `30` | > 0 | The Live page's DAQ list: one answer shared by every browser, at most one DM call this often, and only while a form asks. |
| `daq_name_exclude` | `["logs", "current"]` | a list of text without a blank entry (each is stripped), or `[]` | The Live page's DAQ list leaves out a DAQ whose Source option shows one of these, ignoring case: its experiment name, or its data directory's last two names (`e1/raw` for `/detector/current/e1/raw`: a name further up never counts). Only the list: a session already on such a DAQ still validates and starts, and the page shows it by its ID. `[]`: every DAQ. |

### DM access for live runs

A live run whose source is a DAQ reads DM from the portal host: the api asks for the DAQ list (at most once per
`[live] daq_list_seconds`, and only while a Live page's form asks) and for a DAQ's record at each live Start (the bind
check); the live run's job process reads the DAQ's file catalog, one DM call per cycle (every page since its cursor, one
request of `catalog_page_size` rows at a time, at most `catalog_max_rows` a call). Each is one shell that is no login
shell (`bash -c`: the setup file gives DM's environment, and a login profile's banner could corrupt the call's JSON)
that sources `[live] dm_setup_file` and runs DM's Python API, read-only (`ExperimentDaqApi.listDaqs`, `getDaqInfo`,
`FileCatApi.getExperimentFiles`), bounded by `dm_timeout_seconds` (15), on the portal host, or over SSH to `dm_ssh_host`
(batch mode: a key, no prompt). So the portal's service account needs:

- DM's setup file and the login file it names (`DM_LOGIN_FILE`), readable by it, for a DM account that may read the
  station's DAQs and its experiments' file catalogs;
- DM's services, each operation building only its own API: for live runs, the DAQ web service
  (`dm.daq_web_service`, the list and the bind check) and the catalog web service (`dm.cat_web_service`, the
  catalog thread), installed where the calls run and reachable from there; for remote runs (`[remote]`, the `dm`
  section of `bdp.yaml`), the processing web service (`dm.proc_web_service`'s workflow API: the submission, the
  poller's records, the cancel and clean-up workflows), and on the DM host, for the launcher's `03-PERMISSIONS`
  stage, the storage service (`dm.ds_web_service`'s `FileDsApi.restorePermissions`, `remote-execution.md`). A setup
  for one kind of run alone need not provide the other's;
- the experiments' storage directories mounted on the portal host at the paths DM gives (`storageDirectory`): a
  catalogued file is that directory joined to its `experimentFilePath`, admitted once its size there is the
  catalog's;
- the portal host's clock within 120 s of DM's (NTP on both): until a live run's first file is catalogued, its
  catalog queries start 120 s before the earlier of the portal's clock at the last full scan and the folder's newest
  catalogued time, so with the portal's clock further ahead (and no newer file in the folder) the run's first files
  wait for the next full re-scan (at most `[live] catalog_rescan_seconds`, 300 s); none is lost.

Without `dm_setup_file` nothing calls DM: the DAQ list says DM is not configured, and a DAQ run's
`arrival-status.json` says why. Like `[remote]`, the api and the worker read `[live]` at start: restart both after a
change, with no live session open (a worker restart ends it).

### The acquisition hook's key

A live session whose source is notifications takes each file from a hook on the acquisition host, which runs
`laueportal live notify <job id> <file> --complete` on the portal host over SSH once the file is complete. notify writes
the database and the run folders, so it runs as the portal account, and an ordinary key would give the acquisition host
that account's shell: the database, `dm_setup_file`'s DM login, the SSH key to the DM host. So the hook gets a key of
its own that runs one command: `laueportal.jobs.notify_hook`, the key's forced command. It allows exactly
`laueportal live notify <job id> <path> --complete`: a job ID of digits, and one absolute path of letters, digits and
`. _ - + , = @ % : /` with no `.` or `..` component. It runs notify without a shell, in an environment of its own:
`prod.toml`, the env's `PATH`, `PYTHONNOUSERSITE=1` and the session's `HOME`, nothing else of the session's. `HOME` is
the portal account's own, kept for what looks a home up (`~` in a path); `PYTHONNOUSERSITE=1` keeps that home's user
site (`~/.local/lib/python3.*/site-packages`) out of notify, whose `laueportal` script runs a plain `python`. It gives
back notify's output and status (0 admitted, or a repeat; 1 refused or failed; 2 the settings or the database).
Anything else is refused with one line and status 2, and nothing runs. A folder or file name with other characters
cannot be notified (a space cannot pass through SSH's command line).

On the acquisition host, as the hook's account: `ssh-keygen -t ed25519 -N "" -C "laue notify hook" -f
~/.ssh/laue_notify` (no passphrase: the hook runs unattended). On the portal host, as the portal account, one line in
`~/.ssh/authorized_keys` with that public key, `restrict`, the forced command, and `from=` the acquisition host's IP
address (or an address pattern: `192.0.2.*`, or `192.0.2.0/24`). Never its name: sshd matches `from=` against the
client's address alone unless `UseDNS yes` is set, and its default is `no` (OpenSSH 6.8 and later), so a host name
never matches and the key is refused from everywhere. The fix for that is the address, never dropping `from=`, the
one option that ties the key to the acquisition host:

```
# one line: authorized_keys has no continuation lines
restrict,command="/local/laueportal/envs/laue-portal/bin/python -I -m laueportal.jobs.notify_hook /local/laueportal/config/prod.toml",from="<acquisition host's IP address>" ssh-ed25519 AAAA... laue notify hook
```

`restrict` refuses a terminal, every forwarding and `~/.ssh/rc`, and whatever later OpenSSH versions add to it. An sshd
older than OpenSSH 7.2 has no `restrict`; there, spell out the options it stands for today:

```
# the fallback before OpenSSH 7.2, one line
command="/local/laueportal/envs/laue-portal/bin/python -I -m laueportal.jobs.notify_hook /local/laueportal/config/prod.toml",from="<acquisition host's IP address>",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-user-rc ssh-ed25519 AAAA... laue notify hook
```

The hook runs, for each complete file:

```bash
ssh -i ~/.ssh/laue_notify -o BatchMode=yes <portal account>@<portal host> \
    laueportal live notify "$job" "$file" --complete
```

`$file` is the file's path as the portal host sees it: in the run's Folder, by its text or its real path (notify
refuses a file that is "not directly in the run's folder"). An acquisition host that mounts the data under another
path maps it to the portal host's first.

Check it from the acquisition host: `ssh -i ~/.ssh/laue_notify <portal account>@<portal host> id` prints
`laueportal notify hook: refused: not ...` and exits 2. The live deployment test notifies through the key once (its
step 3).

## 7. Upgrade

An upgrade starts with a copy of the database by hand (consistent while the API runs):

```bash
. "$root/config/prod.env"; ctl="supervisorctl -c $root/config/supervisord.conf"
laueportal db backup <BACKUPS>/laue-before-<new commit>.db   # backup.md; the rotation keeps it
$ctl stop api worker                                         # the worker drains (and ends its remote poller thread)
# (a live session still open ends Failed there, with no Resume: end it on the Live page first)
# an installation with redis or remote_poller programs (earlier versions): $ctl stop api worker redis remote_poller
git -C "$root/src" fetch <the repository>                   # as step 1's clone
git -C "$root/src" checkout --detach <new commit>
git -C "$root/src" diff --stat <old commit> <new commit> -- environment.lock requirements/ deploy/
```

- **A changed lock** (`environment.lock`, `requirements/base.lock`): the install lines of step 1 again.
- **The SPA:** the new commit's build, copied as in step 1 (`test -f .../index.html`).
- **Changed templates** (`deploy/`): render the new commit's beside the installed files, with the values of step 3
  (`REMOTE_ENABLED` as it is now), and compare. Carry what changed into `prod.toml` by hand (you edited it; keep your
  data roots over the render's `data` placeholder); take the new `supervisord.conf` whole unless you changed it. An
  installation of an earlier version may run a `remote_poller` program or a `redis` program (both stopped above with the
  api and the worker); the new `supervisord.conf` has neither (the worker polls; the job table is the queue), so take it
  and restart supervisord whole:

  ```bash
  "$py" "$root/src/deploy/render-prod.py" --output-dir "$root/config/next" ROOT="$root" RUNS_ROOT=... \
      API_PORT=... HOST_NAME=... REMOTE_ENABLED=...   # "not the installed files": expected
  for f in prod.toml supervisord.conf; do diff -u "$root/config/$f" "$root/config/next/$f"; done
  mv "$root/config/next/supervisord.conf" "$root/config/"   # when taken whole
  rm -r "$root/config/next"
  ```
- **From a version with Redis** (Redis and RQ gone; `REDIS_PORT` is no longer a render value): drop the Redis
  password from `prod.env`, the `[redis]` section from `prod.toml` (until then every start logs one warning line
  that it is ignored) and Redis's files. Step 7's first line exported the password into this shell, and deleting
  the line does not take it out, so unset it before supervisord is restarted whole (below); otherwise the new
  supervisord and every program and job process inherit it:

  ```bash
  sed -i '/^export LAUEPORTAL_REDIS__PASSWORD=/d' "$root/config/prod.env"
  grep -c '^export LAUEPORTAL_REDIS__PASSWORD=' "$root/config/prod.env"   # 0
  sed -i '/^\[redis\]$/,/^\[/{/^\[redis\]$/d; /^\[/!d}' "$root/config/prod.toml"   # up to the next section
  grep -c '^\[redis\]' "$root/config/prod.toml"                           # 0
  rm -r "$root/state/redis" "$root/config/redis.conf"
  unset LAUEPORTAL_REDIS__PASSWORD
  [ -z "${LAUEPORTAL_REDIS__PASSWORD+set}" ] && echo unset                # unset
  ```

Then migrate and start again:

```bash
laueportal db upgrade && laueportal db check   # ends with OK
$ctl start api worker                          # with remote execution on, the worker polls too
```

When `supervisord.conf` or `prod.env` changed, restart supervisord whole instead (`$ctl shutdown`,
then start it as in step 5). Then the smoke checks (step 5).

## 8. Hung `/net` mounts: check on a real NFS mount

The API bounds every stat, listing and open of a path on request (`files.probe`): when a mount does not answer within
`[files] probe_timeout_seconds` (5 s) the answer is a 503 `urn:laueportal:problem:not-responding` (or the state
`not_responding` in a page), and the mount answers so at once for `hung_mount_seconds` (30 s). This was tested with a
simulated hang only; check it once on the deployment host, with the system administrator, on a mount the beamline can
spare for a minute (a `hard` NFS mount, as `/net`'s are):

1. Make the mount hang: block its NFS server from the host (e.g. `iptables -I OUTPUT -d <server> -j DROP`, as
   root).
2. Stat: `time curl -s 'http://127.0.0.1:<port>/api/v1/files/browse?path=<a folder on that mount>'`: a 503
   `not-responding` problem in about 5 s, and at once when asked again.
3. Open: the job page or the viewer of a run whose directory is on that mount: "Not responding", in about 5 s.
4. Unblock the server; within `hung_mount_seconds` the listing answers again.

**Known limit:** NFS's attribute cache can answer a check's stat while the read after it hangs (a mount that hangs
between a check and its read): that request then holds one of the API's 40 request threads, and an HDF5 read holds
every HDF5 read in the API (h5py's lock). Each reload of a page of such a run adds stuck requests (the job page
polls every 2 s), so once the threads are taken the whole API stops answering, not only that run's pages.

- **Symptom:** every page spins and requests time out, `/health` included (`curl -m 10
  http://127.0.0.1:<port>/api/v1/health` gets no answer), while `supervisorctl ... status` still shows `api`
  RUNNING. The navbar shows **API unreachable** once its health check has had no answer for 10 s.
- **Remedy:** once the mount answers again, `supervisorctl -c "$root/config/supervisord.conf" restart api`. It takes
  about 20 s: the stuck threads keep the API from ending on SIGTERM, and supervisor kills it after its
  `stopwaitsecs` (20 s); the new process answers at once.

**Known limit, live runs:** a live run's input whose read hangs (a frame or a wire stack on a mount that stops
answering) keeps its worker waiting with no time limit (a live run has none). The run stays Running with fresh
heartbeats, so nothing ends it by itself, and no batch run or conversion starts meanwhile. The seal's lock has no time
limit either: on a runs root whose NFS lock manager stops answering, the `flock` on the run's `arrivals.lock` waits,
and Stop hangs the same way (as does a notify); the remedy below is the same.

- **Symptom:** a live run Running whose counters stop at an input (a LaueGo run's preview stops at the frame before it;
  a wire run's other points go on), and Stop does not end it: the run is sealed but cannot drain.
- **Remedy:** the Live page's **Stop now** (while the session is Stopping, Stop becomes Stop now): the worker's
  dispatcher kills the job process's group of each run whose stop was already asked and ends it Failed (interrupted)
  once it is gone, within a dispatch cycle, the files written so far kept; a run of the session not stopped yet gets its
  stop (it seals and drains). The job's messages end "Stop now requested", then "Stopped now: its job process was
  killed; what it published is kept". Without the page (or for a process the worker cannot reach): end its job process's
  group: `pgrep -f "laueportal.jobs.process run <job id> --cores"` gives the job process's pid (a live run's command
  always goes on with ` --cores <n>`, so the pattern names that job alone, never a longer ID such as the other run of
  the session), which leads its own session, then `kill -KILL -- -<pid>`. The reconciler ends the run Failed
  (interrupted) once its heartbeat is stale, after up to `execution.stale_heartbeat_seconds` plus `reconcile_seconds`
  (300 s + 60 s: about 6 minutes; the session stays open meanwhile, and Start is refused), without `run.json`, the
  Output XML or the job's results path; the files written so far (a LaueGo run's preview and shards, a wire run's
  catalog and point files) stay on disk. A follower of a wire run ended so seals by itself once that run's row is
  Failed, with every point the wire run completed (it reads that run's `completed.jsonl` to its end first). A worker
  blocked in an NFS read can stay until the mount answers. Restarting the worker ends it too, but only after
  `execution.shutdown_grace_seconds` (600 s; the worker waits for the running input first), with the same result. Once
  the mount answers, start a new live run for the inputs that are left (there is no Resume).

**Known limit, a follower:** a point the wire run completed whose file the follower cannot read (deleted or moved, a
mount that refuses it, a line of the wire run's `completed.jsonl` naming a file that does not exist) holds the
follower: it tries that point again at each look (the wait doubling up to 60 s), admits nothing after it, and does not
seal, even once its wire run has ended. It stays Running, so no batch run or conversion starts meanwhile.

- **Symptom:** a follower (a live LaueGo run following a wire run) Running after its wire run has ended. Its run
  directory's `arrival-status.json` says why and since when: `error` "point '<point ID>' (line <n> of the followed run's
  completed.jsonl) not admitted yet: <its path> does not exist" (or "cannot be read: <reason>"), `error_at` the first
  look that failed so. The job log (`<ROOT>/logs/jobs.log`) has "the follower's look failed, tried again:
  ArrivalNotReady: ...".
- **Remedy:** when the file comes back (the mount answers, the file is restored), the follower admits it at its next
  look and goes on by itself. Otherwise the Live page's Stop now (the session is Stopping, its waiting reason on the
  Stopping badge and in its Arrivals cell), or the follower's job page's Stop Run: it seals with what it has
  admitted and drains, and the point and the ones after it are not indexed. For them, index the wire run's
  reconstruction with a batch LaueGo run (there is no Resume).

**Known limit, a follower of a very deep wire run:** a follower takes a point of at most 100,000 depths
(`jobs.arrivals.MAX_POINT_DEPTHS`: it makes one input per depth). The Live page's Reconstruction card, as the wire
form, only warns about a range over 500 µm or a resolution under 0.1 µm, so a session whose reconstruction has more
depths (e.g. -0.5 to 999.5 µm at 0.01 µm: 100,001) starts. Its follower then ends Failed at the first point, with
"point '<point ID>' has 100,001 depths; at most 100,000", and the wire run goes on. Index such a reconstruction with a
batch LaueGo run once it has ended (its Depth Indices a part of the range). It is not refused when the session starts:
only lauelab knows a range's exact count of depths (it rounds the ends to the resolution), and such a range is 500
times the default's 201 depths (-50 to 150 µm at 1 µm).

## 9. The migrated XML-only runs

A database migrated from the earlier portal may hold indexing runs whose results are only an Output XML (no
`output.h5`). Convert them once the database is in place and the `/net` mounts answer (the CLI; the page's Convert
does one run at a time). It is safe to run again: a converted run is reported as already converted.

```bash
. "$root/config/prod.env"
laueportal results convert --all --dry-run --report "$root/logs/convert-dry-run.json"   # expect the XML-only runs
laueportal results convert --all --report "$root/logs/convert.json"
```

`output.h5` goes beside each run's XML; where the portal account may not write the historical folders, add
`--destination-root <a folder inside a root>`. It exits 1 when a run failed (the report says why): a run whose XML names
a geometry that is not available is refused unless given `--geometry <file>` (convert those by their IDs,
`laueportal results convert I12 I15 --geometry ...`).

## 10. Backups

Copies of the portal's database by hand with `laueportal db backup` (`backup.md`, "Backups by hand"): before an upgrade
(step 7) and a restore. The migration backs up the earlier portal's stopped database file with `cp -p` instead, then a
`quick_check` of the copy (step 4), so that the earlier portal's file is never opened. A nightly crontab is optional: if
the beamline wants one, the crontab in `backup.md` ("Optional: a nightly crontab"), sourcing `<ROOT>/config/prod.env`;
check the next morning that `backup.log` has a `backed up` line.
