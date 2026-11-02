# `v1_schema_cb1159c.json`: v1 `cb1159c`'s database schema

`objects` is v1 `cb1159c`'s (`bdp_integration`, live processing and remote monitoring) `Base.metadata.create_all`
on an empty SQLite file: every `sqlite_master` row (`type`, `name`, `tbl_name`, `sql`), ordered by type and name, in
the format of `v1_schema_3ad1557.json`. `tests/migration_support.py::create_v1_cb1159c_database` builds the
`create_all` shape of `tests/test_migrations.py::test_a_v1_cb1159c_job_keeps_its_observations_to_head` from it (all
but the live tables), and `::test_v1_cb1159cs_create_all_is_v1_3ad1557s_with_the_live_tables_and_the_observation`
pins how it differs from `v1_schema_3ad1557.json`.

It differs from `v1_schema_3ad1557.json` in two ways:
- `job` has one more column, `remote_observation JSON` (nullable), after `external_url` (revision `0008`);
- three more tables, `live_session`, `live_point` and `live_checkpoint`, with their nine indexes (the ones
  `laueportal db stamp --detect` drops).

## How it was made (2026-10-25)

With `v1_schema_3ad1557.md`'s `capture_v1_schema.py` (saved outside the exported tree), from v1's own models, run
from an exported tree of the scratch clone, never a checkout (Python 3.12.15, SQLAlchemy 2.1.5, SQLite 3.54.0: the
dev env):

```bash
V1_CLONE=/path/to/v1-clone                         # a clone with v1's bdp_integration branch
WORK="$TMPDIR/v1-schema" && mkdir -p "$WORK/v1src" "$WORK/cfg"
git -C "$V1_CLONE" archive cb1159c166d48dfccc5097afb65b85ad6771be56 | tar -x -C "$WORK/v1src"
cp "$WORK/v1src/config.yaml.template" "$WORK/cfg/config.yaml"
LAUE_PORTAL_CONFIG="$WORK/cfg/config.yaml" PYTHONPATH="$WORK/v1src" PYTHONDONTWRITEBYTECODE=1 \
  python capture_v1_schema.py v1_schema_cb1159c.json \
  "a v1 database's schema with the live tables and the job column of revision 0008: v1's models' create_all on an empty SQLite file, as sqlite_master rows, captured from an exported tree (v1_schema_cb1159c.md)" \
  "$WORK/v1src"
```

**Checks of the method:**
- Run twice, the two files are byte for byte the same.
- The same script and env on an export of `3ad1557` reproduces `v1_schema_3ad1557.json`'s `objects` exactly.
- v1 `cb1159c` still pins `SQLAlchemy==2.0.41` (`requirements.txt`); that version's `create_all` was found
  identical to the dev env's for `3ad1557` and `dde1bde` (`v1_schema_3ad1557.md`). It was not run again for `cb1159c`.
