"""Every grid list sorts and filters by each of its fields on the server as its rows show the field, on synthetic
rows: the run lists (``tests.run_lists_support.seed_run_lists``: three scans, four reconstructions, four runs in the
indexations list and ten jobs, with blanks, mixed case and every status) and the scans list's five scans
(``tests.scans.test_scans_api.seed``: users, days, techniques and dimensions apart).

The order a field sorts in: blanks first (ascending), text ignoring ASCII case, ``status`` by its code (queued,
running, finished, failed, cancelled), ties by the list's key in either direction. The translator's rules on their
own table are ``tests/core/test_paging.py``'s; that every published field is accepted, ``tests/test_api_sweep.py``'s.
"""

from __future__ import annotations

import json
import string
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from laueportal.app import create_app
from laueportal.core.paging import GridSpec
from laueportal.indexing.service import INDEXING_RUN_GRID
from laueportal.jobs import service as jobs_service
from laueportal.jobs.schemas import RunStatus
from laueportal.reconstruction.service import RECONSTRUCTION_RUN_GRID
from laueportal.scans.service import SCAN_GRID
from tests.api_support import PORTAL_HEADERS
from tests.run_lists_support import T0, seed_run_lists
from tests.scans.test_scans_api import seed as seed_scans
from tests.settings_support import make_database

GRIDS: dict[str, tuple[str, GridSpec]] = {
    "scans": ("/api/v1/scans", SCAN_GRID),
    "reconstruction-runs": ("/api/v1/reconstruction-runs", RECONSTRUCTION_RUN_GRID),
    "indexing-runs": ("/api/v1/indexing-runs", INDEXING_RUN_GRID),
    "jobs": ("/api/v1/jobs", jobs_service.JOB_GRID),
}
NOCASE = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
"""SQLite's NOCASE collation and ``lower()``: ASCII letters only."""
WHITESPACE = " \t\n\r\f\v"
"""What a blank text value may consist of (``core.paging``)."""
LIFECYCLE = [status.value for status in RunStatus]

Row = dict[str, Any]
Condition = tuple[dict[str, Any], Callable[[Any], bool]]


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The app on the seeded run lists, the jobs list's clock at ``T0`` (a running job's duration is fixed)."""
    settings, engine = make_database(tmp_path_factory.mktemp("grid-lists"))
    seed_run_lists(engine)
    seed_scans(engine)
    engine.dispose()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(jobs_service, "utcnow", lambda: T0)
        with TestClient(create_app(settings), headers=PORTAL_HEADERS) as test_client:
            yield test_client


def get(client: TestClient, path: str, **params: Any) -> Any:
    if "filter" in params:
        params["filter"] = json.dumps(params["filter"])
    response = client.get(path, params={"limit": 1000, **params})
    assert response.status_code == 200, f"{path} {params}: {response.status_code} {response.text}"
    return response.json()


def value(row: Row, name: str) -> Any:
    """A field's value in a row as the list shows it: a job's ``run`` by its label (``I12``)."""
    if name == "run":
        return None if row["run"] is None else row["run"]["label"]
    return row[name]


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def sort_key(spec: GridSpec, name: str) -> Callable[[Row], tuple[Any, ...]]:
    filter_type = spec.fields[name].filter_type

    def key(row: Row) -> tuple[Any, ...]:
        item = value(row, name)
        if item is None:
            return (0,)
        if name == "status":
            return (1, LIFECYCLE.index(item))
        if filter_type == "text":
            return (1, item.translate(NOCASE))
        if filter_type == "date":
            return (1, moment(item))
        return (1, item)

    return key


def text_conditions(values: list[Any]) -> list[Condition]:
    def blank(item: Any) -> bool:
        return item is None or item.strip(WHITESPACE) == ""

    shown = Counter(item for item in values if not blank(item))
    common = shown.most_common(1)[0][0] if shown else "e"
    folded = common.translate(NOCASE)
    part = next((item[:2] for item in shown if item[:2].strip()), "e").translate(NOCASE)
    return [
        ({"type": "blank"}, blank),
        ({"type": "notBlank"}, lambda item: not blank(item)),
        ({"type": "equals", "filter": common}, lambda item: item is not None and item.translate(NOCASE) == folded),
        ({"type": "notEqual", "filter": common}, lambda item: item is None or item.translate(NOCASE) != folded),
        ({"type": "contains", "filter": part}, lambda item: item is not None and part in item.translate(NOCASE)),
        (
            {"type": "startsWith", "filter": part},
            lambda item: item is not None and item.translate(NOCASE).startswith(part),
        ),
    ]


def number_conditions(values: list[Any]) -> list[Condition]:
    shown = sorted(item for item in values if item is not None) or [0]
    low, middle = shown[0], shown[len(shown) // 2]
    return [
        ({"type": "blank"}, lambda item: item is None),
        ({"type": "notBlank"}, lambda item: item is not None),
        ({"type": "equals", "filter": middle}, lambda item: item is not None and item == middle),
        ({"type": "greaterThan", "filter": middle}, lambda item: item is not None and item > middle),
        (
            {"type": "inRange", "filter": low, "filterTo": middle},
            lambda item: item is not None and low <= item <= middle,
        ),
    ]


def date_conditions(values: list[Any]) -> list[Condition]:
    """By UTC day (a date without an offset is a UTC day, ``core.paging``)."""
    shown = sorted(moment(item) for item in values if item is not None) or [datetime(2026, 1, 1, tzinfo=UTC)]
    first, day = shown[0].date(), shown[len(shown) // 2].date()
    since = datetime(first.year, first.month, first.day, tzinfo=UTC)
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    return [
        ({"type": "blank"}, lambda item: item is None),
        ({"type": "notBlank"}, lambda item: item is not None),
        (
            {"type": "equals", "dateFrom": day.isoformat()},
            lambda item: item is not None and start <= moment(item) < end,
        ),
        ({"type": "greaterThan", "dateFrom": day.isoformat()}, lambda item: item is not None and moment(item) >= end),
        (
            {"type": "inRange", "dateFrom": first.isoformat(), "dateTo": day.isoformat()},
            lambda item: item is not None and since <= moment(item) < end,
        ),
    ]


CONDITIONS = {"text": text_conditions, "number": number_conditions, "date": date_conditions}


@pytest.mark.parametrize("grid", GRIDS)
def test_a_grid_list_sorts_and_filters_by_each_field_as_its_rows_show_it(client: TestClient, grid: str) -> None:
    path, spec = GRIDS[grid]
    rows = get(client, path)["rows"]
    assert len(rows) >= 3
    by_key = sorted(rows, key=lambda row: row[spec.key])
    for name, field in spec.fields.items():
        if field.sortable:
            for sort in (name, f"-{name}"):
                expected = sorted(by_key, key=sort_key(spec, name), reverse=sort.startswith("-"))  # ties by the key
                shown = [row[spec.key] for row in get(client, path, sort=sort)["rows"]]
                assert shown == [row[spec.key] for row in expected], f"sort={sort}"
        if field.filterable:
            for condition, matches in CONDITIONS[field.filter_type]([value(row, name) for row in rows]):
                model = {name: {"filterType": field.filter_type, **condition}}
                page = get(client, path, filter=model, sort=spec.key)
                expected_keys = [row[spec.key] for row in by_key if matches(value(row, name))]
                assert (page["total"], [row[spec.key] for row in page["rows"]]) == (
                    len(expected_keys),
                    expected_keys,
                ), model
