# Backup and restore of the portal database

The live database is one SQLite file on the portal host's local disk (`database.path`, e.g.
`/local/laueportal/db/laueportal.db`, the layout of `deploy.md`) in WAL mode. Copies to NFS are taken by hand with
`laueportal db backup`, before an upgrade (`deploy.md` step 7) or a restore (below); a nightly crontab is optional. The
migration of the earlier portal's database backs up its stopped file instead (`cp -p`, [`deploy.md`](deploy.md) step 4):
`db backup` would open it, and leave a `-wal` and `-shm` beside it.

**A database is up to four files:** `laueportal.db` and, beside it, `laueportal.db-wal`, `laueportal.db-shm` and
`laueportal.db-journal`. SQLite finds the WAL and the journal by the database's name, and a WAL can hold committed
transactions that are not in `laueportal.db` yet (always after a crash or a killed process). Never copy, move,
rename or delete one of them without the others; to copy a database, use `laueportal db backup`.

## The rule: WAL only on a local disk

The API, the worker, its job processes, `jobs reconcile`, `remote poll` and the `laueportal db` commands refuse to
start when `journal_mode = "wal"` (the default) and `database.path` is on a network file system: NFS, CIFS/SMB,
sshfs, 9p, a cluster file system (Ceph and ceph-fuse, GlusterFS, Lustre, GPFS, BeeGFS, GFS2, OCFS2, ...) or an
automounter path such as `/net/...` (`core.db.NETWORK_FILESYSTEMS`). `db check` reports it as a problem instead
(below). The file system is the one holding the file's real location (links resolved, bind mounts by their own
mount), read from `/proc/self/mountinfo`. They exit with status 2 and say what to change:

```
laueportal: database.path /net/fileserver/laueportal.db is on a network file system (nfs4, mounted at /net/fileserver), where SQLite's WAL journal is unsafe (journal_mode = "wal"): put the database on a local disk (e.g. /local/laueportal/laueportal.db) or set journal_mode = "delete" in [database]
```

`journal_mode = "delete"` (the rollback journal) is accepted anywhere; WAL on a local disk is the intended setup (the
pages poll while the worker writes).

## Backups by hand

`laueportal db backup <dest>` copies the database with SQLite's online backup API: one read transaction, so the copy
is one consistent state, the WAL's committed transactions included, while the API and the worker keep writing (in
WAL mode they are never held). The copy is written next to `<dest>` (`.<name>.partial-<pid>`), switched to the
rollback journal (one self-contained file, safe on NFS), checked with `quick_check`, given the live database's
permission bits, and only then renamed to `<dest>`. It prints the pages and bytes copied. It exits 1, with one line
naming the side that failed (the database, or the copy), and leaves nothing under `<dest>`'s name when the copy
cannot be written or does not pass `quick_check`.

It refuses (exit 1, nothing written): the live database or the `--db` source and any of their files (by any path or
a hard link), a protected original (a path `database.protected` lists, or under one), a folder, a missing folder, and an existing file unless
`--force`.

As the account that runs the portal, with the production environment file (`deploy.md`, step 2), name each copy
for what it comes before (`laue-before-<what>-<date>.db`: the crontab's rotation below never deletes such a name):

```
. /local/laueportal/config/prod.env
laueportal db backup /net/.../laueportal-backups/laue-before-upgrade-$(date +%F).db
```

A copy is the size of the database (a beamline's few thousand runs: well under a megabyte). Who can read it: a copy has
the live database's permission bits (`0600` keeps it to the portal account); the folder's own mode decides who can list
it.

## Optional: a nightly crontab

Not needed (copies by hand are enough); for a beamline that wants one. The crontab of the account that runs the portal
(the owner of the database file; never root, whose copies other accounts could not replace). Each line sources the
production environment file (`deploy.md`, step 2: the `laueportal` command's `PATH` and `LAUEPORTAL_CONFIG`; not a
checkout's `scripts/env.sh`, which points at the development workspace); `%` must be escaped in a crontab:

```
SHELL=/bin/bash
MAILTO=<who reads the failures>
BACKUPS=/net/.../laueportal-backups
0 2 * * * . /local/laueportal/config/prod.env && laueportal db backup "$BACKUPS/laue-$(date +\%F).db" >> "$BACKUPS/backup.log" 2>&1 && ls -1 "$BACKUPS"/laue-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].db | head -n -30 | xargs -r rm -- || echo "laueportal db backup failed on $(hostname): see $BACKUPS/backup.log" >&2; find "$BACKUPS" -maxdepth 1 -name '.laue-*.partial-*' -mtime +0 -delete
```

- **Where results show:** every run appends `db backup`'s line to `$BACKUPS/backup.log` (`<dest>: backed up ...`,
  or `laueportal db backup: ...` with the reason). A failure also prints one line on stderr, which cron mails to
  `MAILTO`; on a host without mail, read `backup.log`: a night without a `backed up` line is a missed backup.
- **Retention:** only after a successful backup, the dated copies (`laue-YYYY-MM-DD.db`) but the newest 30 are
  deleted, so failed nights never cost a copy; change `30` as the beamline needs. The copies by hand
  (`laue-before-<what>-<date>.db`) are never deleted by it.
- **Partial copies:** a backup killed mid-copy (SIGKILL, the OOM killer) leaves `.laue-<date>.db.partial-<pid>`; the
  `find` removes those older than a day.

Check a copy: `laueportal db check --db <copy>`. A copy on NFS is checked with the rollback journal in the settings,
since `db check` reports WAL on a network file system as a problem (the copy itself is in rollback-journal mode
either way):

```
LAUEPORTAL_DATABASE__JOURNAL_MODE=delete laueportal db check --db /net/.../laueportal-backups/laue-2026-10-23.db
```

## Restore

Restoring replaces the live database: every run, scan and edit since the copy was taken is lost from the database, and
the runs created since will have their IDs given again. Like an upgrade, it starts with a copy of the current database
by hand (step 2, `laueportal db backup`, taken once the programs are stopped so that it holds every transaction). Run
every step as the account that runs the portal (the owner of `/local/laueportal/db` and of the database file), never as
root. The commands assume `cd /local/laueportal/db` and `. /local/laueportal/config/prod.env` (`deploy.md`);
`supervisorctl` stands for `supervisorctl -c /local/laueportal/config/supervisord.conf`.

1. **Stop** everything that opens the database, and check that nothing still holds it:
   ```
   supervisorctl stop api worker                  # the programs of supervisord.conf (deploy.md); the worker polls too
   fuser laueportal.db*                           # no output: no process has any of its files open
   ```
2. **Copy the current database** while it still opens (read-only; its WAL's transactions included). If this fails
   because the database is damaged, go on: step 3 keeps its files.
   ```
   laueportal db backup /net/.../laueportal-backups/laue-before-restore-$(date +%F).db
   ```
3. **Move the current database's files together** into a folder, keeping their names (the database and whichever of
   `-wal`, `-shm` and `-journal` exist):
   ```
   mkdir before-restore-$(date +%F) && mv laueportal.db laueportal.db-* before-restore-$(date +%F)/
   ```
   (After step 2, `laueportal.db-*` matches an empty `-wal` and a `-shm`, which its read-only open of the stopped
   database left. Without step 2, it matches nothing when the stack stopped cleanly: `mv` then says so about that
   name, and moves `laueportal.db`.)
4. **Copy the backup into place** (one file in rollback-journal mode; the API switches it to WAL when it first opens
   it):
   ```
   cp /net/.../laueportal-backups/laue-2026-10-23.db laueportal.db
   ```
5. **The queue comes with the database.** The job table and the conversion table are the queue (no Redis), and the
   idempotency keys of run creation are in the database (revision `0007`), so the backup brings back its own queue with
   its rows. The worker runs the copy's Queued runs and queued conversions once the stack starts.
6. **Set aside the run directories of the lost runs.** Their IDs will be given again, and a new run whose directory
   exists fails ("a run directory is never reused"). A run's directory is where its Output Path put it, anywhere inside
   the roots: usually under `files.runs_root`, not always (a migrated database may hold runs outside the runs root). So
   list the lost runs and their directories from the copy of step 2, which has every run up to the stop (if step 2
   failed, the moved `before-restore-<date>/laueportal.db` if it opens, else the `find` below alone): the runs with an
   ID above the restored database's largest, as `deploy.md` step 4's rollback does:
   ```
   python - /net/.../laueportal-backups/laue-before-restore-<date>.db <<'EOF'
   import pathlib, sqlite3, sys
   def read_only(path): return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)
   restored, before = read_only("laueportal.db"), read_only(sys.argv[1])
   for table in ("indexing_run", "reconstruction_run"):
       last = restored.execute(f"SELECT coalesce(max(id), 0) FROM {table}").fetchone()[0]
       print(table, "restored up to", last)
       for run_id, folder in before.execute(f"SELECT id, output_path FROM {table} WHERE id > ?", (last,)):
           print(table, run_id, folder)
   EOF
   ```
   Look at the list, then move each folder that exists aside on its own mount, keeping its path below a set-aside
   folder (e.g. `mkdir -p <mount>/laueportal-set-aside-<date>/<its parent>`, then `mv`). A folder listed inside
   another listed one moves with it. As a cross-check, the folders under the runs root with an ID above the
   restored ones (`[output_templates]`: `index_<ID>` and `rec_<ID>` by default) should all be in the list:
   ```
   find <runs_root> -type d \( -name 'index_*' -o -name 'rec_*' \) | awk -F/ -v i=<indexing max> -v r=<reconstruction max> '{ split($NF, part, "_") } (part[1] == "index" && part[2] + 0 > i) || (part[1] == "rec" && part[2] + 0 > r)'
   ```
   A remote run submitted after the copy may still return its results from DM into such a folder: the poller leaves
   a submission the database does not know alone.
7. **Check** the restored database. If the code is newer than the copy's revision, `db check` says so: upgrade it.
   Right after a restore, "runs stuck in created or published" is expected for runs the copy caught being created:
   the worker fails them once it starts (step 8). Any other problem: stop and undo.
   ```
   laueportal db check
   laueportal db upgrade          # only if db check says the revision is behind head
   ```
8. **Start** the services: `supervisorctl start worker api`. The worker's reconciler settles the runs
   the copy recorded as unfinished: a Running run without a live job process becomes Failed (interrupted), a running
   conversion without one becomes failed (it can be converted again), a Queued run stays queued (its row is its queue
   entry, which the dispatcher takes), and a run still being created fails after
   `execution.creation_timeout_seconds`. Remote runs are the poller's (the worker's thread, with remote execution
   enabled).

**Undo the restore** (before new runs were made on it): stop as in step 1; move the restored database's files aside
together as in step 3 (into another folder); move the kept files back, together:
`mv before-restore-<date>/laueportal.db* .`; move the run directories of step 6 back;
start (step 8). Delete the `before-restore-<date>` folder only once the restored database has run for a while; the
copy of step 2 stays in the backup folder.

## `laueportal db check`

Read-only; run it after a restore, an upgrade or a migration. It prints what it measured, then `<path>: OK`, or the
problems on stderr with exit status 1 (here a portal's database at `<ROOT>/db`, the layout of `deploy.md`):

```
journal: wal on ext4 (local, mounted at /local)
revision: 0008 (head)
quick_check: ok
foreign_key_check: 0 violations
timestamps: 105 values, 0 not UTC
rows: catalog 1, conversion 0, idempotency_key 0, indexing_run 8, job 17, lauego_indexing_parameters 8, metadata 1, reconstruction_run 9, scan 2, wire_reconstruction_parameters 9, worker 1
runs stuck in created or published: 0
/local/laueportal/db/laueportal.db: OK
```

- the journal mode is safe on the database's file system (above);
- the revision is head and the tables match the models and head's DDL;
- SQLite's `quick_check` and `foreign_key_check` pass;
- from revision 0005 on, every timestamp is stored as UTC (`2026-10-15T12:34:56.000000Z`); a migrated database's naive
  local times are converted by 0005's migration, and `laueportal db stamp` refuses 0005 or later while one is left:
  stamp the revision the values are at, then `db upgrade`;
- no run is stuck in created or published for longer than `execution.creation_timeout_seconds` (the reconciler's
  rule; the worker fails such runs, or `laueportal jobs reconcile --once`);
- the counts: every table's rows.
