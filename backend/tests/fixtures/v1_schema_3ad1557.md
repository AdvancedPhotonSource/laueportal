# `v1_schema_3ad1557.json`: v1 `3ad1557`'s database schema

`objects` is v1 `3ad1557`'s (`bdp_integration`) `Base.metadata.create_all` on an empty SQLite file: every
`sqlite_master` row (`type`, `name`, `tbl_name`, `sql`), ordered by type and name, in the format of
`v1_schema_dde1bde.json`. `tests/test_migrations.py::test_0002_builds_exactly_v1_3ad1557s_create_all`
compares an empty database upgraded to revision `0002` with it byte for byte.

It differs from `v1_schema_dde1bde.json` only in `job`: five nullable `VARCHAR` columns after
`queue_job_id` (`backend`, `submission_id`, `external_id`, `external_state`, `external_url`), as v1's
`git diff dde1bde 3ad1557 -- laue_portal/database` shows.

## How it was made (2026-10-16)

From v1's own models, run from an exported tree, never a checkout (Python 3.12.15, SQLAlchemy 2.1.4,
SQLite 3.54.0: the dev env). `upgraded_dde1bde_job` (below) was added to the file afterwards.

```bash
V1_CLONE=/path/to/v1-clone                         # a clone with v1's bdp_integration branch
WORK="$TMPDIR/v1-schema" && mkdir -p "$WORK/v1src" "$WORK/cfg"
git -C "$V1_CLONE" archive bdp_integration | tar -x -C "$WORK/v1src"    # 3ad1557a59fe…
cp "$WORK/v1src/config.yaml.template" "$WORK/cfg/config.yaml"
LAUE_PORTAL_CONFIG="$WORK/cfg/config.yaml" PYTHONPATH="$WORK/v1src" PYTHONDONTWRITEBYTECODE=1 \
  python capture_v1_schema.py v1_schema_3ad1557.json \
  "a v1 database's schema as revision 0002 builds it: v1's models' create_all on an empty SQLite file, as sqlite_master rows, captured from an exported tree (v1_schema_3ad1557.md)" \
  "$WORK/v1src"
```

`capture_v1_schema.py`, complete (save it outside the exported tree):

```python
"""Capture v1's create_all as sqlite_master rows (the format of backend/tests/fixtures/v1_schema_*.json).

Usage, with PYTHONPATH pointing at an exported v1 tree (git archive), never a checkout:
    python capture_v1_schema.py <out.json> <source text> <exported tree>
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine

import laue_portal.database.models  # noqa: F401  (v1: registers every model on Base.metadata)
from laue_portal.database.base import Base

out, source, tree = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
# v1's models must come from the exported tree, not from an installed or checked-out v1.
assert Path(laue_portal.database.models.__file__).resolve().is_relative_to(tree.resolve()), (
    laue_portal.database.models.__file__
)
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "v1.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    connection = sqlite3.connect(path)
    rows = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    connection.close()
objects = [{"type": t, "name": n, "tbl_name": tb, "sql": s} for t, n, tb, s in rows]
out.write_text(json.dumps({"source": source, "objects": objects}, indent=2) + "\n")
print(f"{out}: {len(objects)} objects")
```

**Checks of the method:**
- The same script on an export of `dde1bde` reproduces `v1_schema_dde1bde.json`'s `objects` exactly.
- **v1's pinned SQLAlchemy:** v1 pins `SQLAlchemy==2.0.41` (`requirements.txt`, both commits), the version
  that created the deployed databases. The same capture in a scratch venv with SQLAlchemy 2.0.41
  (Python 3.12.15, SQLite 3.54.0) gives identical `objects` for `3ad1557` and for `dde1bde` (18 each), so
  the fixtures are v1's deployed DDL, not only the dev env's rendering of it.

## `upgraded_dde1bde_job`

The `job` table as v1 `3ad1557`'s own `scripts/migrate_workflow_database.py` leaves it when it upgrades a
database already at `dde1bde`'s schema. v1's `deploy/README.md` migrates every database with this script.
The script detects the "compact" stage and adds the missing columns with
`ALTER TABLE "job" ADD COLUMN <name> VARCHAR`, so SQLite appends them after the last column
(`failure_report_path`), not where `create_all` puts them.

Made by running that script (from the same exported tree) on a scratch copy of a database in v1 `dde1bde`'s schema:

```bash
python "$WORK/v1src/scripts/migrate_workflow_database.py" snap.db v1-3ad1557-upgraded.db
#   Stages: upgrade; added column job.backend … job.external_url
```

Every other object, and every row of every table, was unchanged; the five columns were NULL in every job.
`tests/test_migrations.py::test_productions_job_table_after_v1s_own_upgrade_is_stamped_0002_and_comes_back_from_head`
rebuilds `dde1bde`'s `job` with production's quoted name and CHECK order (`ck_job_counters_bounded` first;
`create_all` puts it second), runs the same `ALTER TABLE` statements and checks that the result equals `sql`.
That database (every other object `dde1bde`'s) is then detected and stamped 0002, upgraded to head with
`db check`'s problems empty, and downgraded to 0002, which gives `sql` back with the rows kept.

So a database at v1 `3ad1557`'s schema has the five columns either after `queue_job_id` (a new database,
or a legacy database that the `3ad1557` script compacted, which rebuilds `job` from the models) or at the
end (a `dde1bde` database that the script upgraded). `laueportal db stamp 0002` and `--detect` accept
both, because column order is not part of the schema check.
