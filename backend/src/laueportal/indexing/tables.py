"""The results viewer's peaks and patterns tables (``GET …/peaks`` and ``…/patterns``): the grid query
protocol over lauelab's ``indexed_peak_table`` and ``pattern_table`` (``science.tables``),
filtered, sorted and paged by the pyarrow translator (``core.arrow_paging``) within the scope.

Each table is made once per dataset, over every pattern, and kept with the cached dataset
(``DatasetCache.derived``); the scope selects its rows. Every field sorts and filters; ``frame_id`` filters as text
and sorts by step (a frame's identity may be a number or text). The default order is the results' (``id``).
"""

from __future__ import annotations

from laueportal.core import arrow_paging
from laueportal.core.paging import GridField, GridSpec
from laueportal.indexing.schemas import PatternPage, PatternRow, PeakPage, PeakRow, ResultsGridParams
from laueportal.indexing.viewer import ViewerDataset
from laueportal.science import tables
from laueportal.science.cache import DatasetCache
from laueportal.science.viewer import DataScope


def _grid(number_fields: tuple[str, ...], text_fields: tuple[str, ...]) -> GridSpec:
    fields = {name: GridField("number") for name in number_fields}
    fields.update({name: GridField("text") for name in text_fields})
    fields["frame_id"] = GridField("text", sort_by="step")
    return GridSpec(fields=fields, key="id", default_sort="id")


PEAK_GRID = _grid(tables.PEAK_NUMBER_FIELDS, tables.PEAK_TEXT_FIELDS)
PATTERN_GRID = _grid(tables.PATTERN_NUMBER_FIELDS, tables.PATTERN_TEXT_FIELDS)


def page_parameters(params: ResultsGridParams) -> dict[str, object]:
    """The grid parameters without the scope, for a page's entity tag (which holds the scope as lauelab's)."""
    return params.model_dump(include={"offset", "limit", "sort", "filter"})


def peaks_page(loaded: ViewerDataset, scope: DataScope, params: ResultsGridParams, *, cache: DatasetCache) -> PeakPage:
    table = cache.derived(loaded.dataset, "peak_table", tables.peak_table)
    page = arrow_paging.fetch_table_page(table.table, PEAK_GRID, params, rows=table.scope_rows(loaded.dataset, scope))
    return PeakPage(rows=[PeakRow.model_validate(row) for row in page.rows], total=page.total, as_of=page.as_of)


def patterns_page(
    loaded: ViewerDataset, scope: DataScope, params: ResultsGridParams, *, cache: DatasetCache
) -> PatternPage:
    table = cache.derived(loaded.dataset, "pattern_table", tables.pattern_table_of)
    page = arrow_paging.fetch_table_page(
        table.table, PATTERN_GRID, params, rows=table.scope_rows(loaded.dataset, scope)
    )
    return PatternPage(rows=[PatternRow.model_validate(row) for row in page.rows], total=page.total, as_of=page.as_of)
