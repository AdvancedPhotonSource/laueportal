# Frontend conventions

The rules for the React app `laueportal-web` in `frontend/`: its stack, server data, state, tables, program forms, the
Live page, visualization, UI, routes, components, tests and builds. [`AGENTS.md`](../../AGENTS.md) has the rules for the
whole repository. Paths are relative to `frontend/`, apart from those that start with `backend/`, `deploy/` or `docs/`,
which are from the repository root.

## Stack

Exact pins in `package.json`:
- React 19, Vite 8, TypeScript 6 (strict);
- Tailwind ≥ 4.3.3, shadcn on Base UI;
- TanStack Router (file routes in `src/routes`) and TanStack Query; Orval 8 (hooks and Zod);
- AG Grid Community 36 (`ag-grid-community`, `ag-grid-react`), only through `src/components/DataGrid`;
- React Hook Form 7 with `@hookform/resolvers` (its Zod resolver) for forms (the scan page's catalog,
  `src/features/scans/ScanCatalogForm.tsx`);
- VTK.js (`@kitware/vtk.js`), only in the 3D view's lazy chunk (`src/viz/cloud/scene.ts`; nothing else imports it
  at runtime: the rule is about the bundle, so tests may); its unused required peers (autoprefixer, wslink) are
  marked optional in `pnpm-workspace.yaml` (`packageExtensions`), so they are not installed;
- Zustand 5 for the viewers' persisted settings (`src/features/indexing/viewer-store.ts`,
  `src/features/reconstruction/viewer-store.ts`);
- Apache Arrow (`apache-arrow`) decodes the viewer's Arrow products, only in `src/api/products.ts` (`useMapProduct`,
  `usePoleFigureProduct`, re-exported by `@/api`): each product is decoded once into typed arrays (`MapProduct`,
  `PoleFigureProduct`: the columns and the metadata's `product` JSON) and revalidated by its ETag (`apiRevalidate`: on
  a 304 the decoded object itself is kept, so the views re-pack nothing); a failed product is not retried. A tab keeps
  its last good product on screen while another loads or when one fails (the stale warning, `ErrorState stale`;
  `useLastGood` in `src/features/indexing/use-last-good.ts`); its misdeclared `@types/node` dependency is dropped
  (`overrides` in `pnpm-workspace.yaml`), so Node's globals stay out of the app's type-check;
- Recharts 3 (`recharts`, with its `react-is` peer at React's version) for small charts, only through shadcn's chart
  component (`src/components/ui/chart.tsx`; the wire viewer's ROI traces). The shadcn CLI's registry pins an older
  `recharts` and asks to overwrite `card.tsx`: answer no (`yes n | scripts/shadcn-add.sh chart`), then re-pin ours with
  `pnpm add recharts@<version>`.

## Package manager

Only pnpm 12 (settings in `pnpm-workspace.yaml`), never npm or yarn.

## Server data

Server data comes only from the Orval hooks re-exported by `src/api/index.ts`.
- Import from `@/api`, never from a module inside `src/api` (ESLint's `no-restricted-imports` enforces it,
  for `@/api/...` and relative paths alike, so don't name another folder `api`).
- Never hand-write API types.
- Never copy server data into Zustand or component state.
- Every query of a run's results is keyed under the run's `/api/v1/indexing-runs/{id}/` (the generated keys are; a
  hand-written one starts with the endpoint's generated URL, as `getSelectionStatsQueryKey` does), or `reloadResults`
  (`src/features/indexing/artifact-bar.ts`: another XML, a conversion) will not refresh it. A grid of a run's results
  fetches outside React Query, so it listens with `onResultsReload` and refreshes itself (the Peaks and Patterns tabs,
  `ResultsTableTabs.tsx`).
- After an API change, run `make gen` and commit `openapi.json` plus `src/api/generated`.

## State placement

- the URL contains only coarse, non-default values, written after a pause, replacing history, except a viewer's tab
  and step, which push;
- search values are plain strings in the URL (`src/lib/search.ts`: no JSON quoting, readable commas); each route's
  `validateSearch` converts and checks them, and returns `undefined` for a value it drops;
- a viewer's route reads its search through `useViewSearch` (`src/lib/view-search.ts`: shown at once, written after 300
  ms, history keys pushed, back/forward change only them); the indexing viewer's search helpers are in
  `src/features/indexing/viewer-search.ts` (`searchPatch` drops defaults), the wire viewer's in
  `src/features/reconstruction/viewer-search.ts` (`tab` and `point`, both history keys; the default point, not written,
  is what a fresh load picks; a kept point that is no longer it is written with replace, `onSearchReplace`; `depth`, the
  Depth View's index, replaces and is not written at the point's brightest depth, and another point drops it,
  `searchPatch`);
- "Copy link to this view" is shared: each viewer describes its link once (`ViewerLinks`: version, pack, open, its
  store's settings) and wraps `useOpenViewLink` and `usePackedViewLink` (`src/lib/use-view-link.ts`); the header renders
  `ViewLinkButton link={...}` (`src/components/ViewLinkButton.tsx`, with the clipboard fallback); a viewer's store is
  its own (the wire viewer's `useWireViewerStore`, `laueportal:view:wire-viewer:v1`, has one slice per tab as the
  indexing viewer's: the Depth View's `depth`, its colormap and colorbar; the ROI Inspector's `roi`; its ROIs are
  `useRoiStore` (`src/features/reconstruction/rois.ts`), one localStorage key per run and point; tests call
  `resetWireViewerStore()`, `src/test/reconstruction.ts`, which resets both). The capped routes (a 503 with
  `Retry-After` when busy: the wire viewer's trace, reference, ROI traces and depth frames, the indexing viewer's
  detector image) take `BUSY_RETRY` (`src/lib/busy.ts`) and show `busyStatus` over their plot (`PlotStatusLine` in the
  wire viewer: retrying, then Retry; the detector tab shows the step's overlays while its image retries, then the
  image's notice with Retry). The folder reads are capped too, in one shared turn (4 at once, 16 waiting: the file
  browser's listing, Find Matching Files and Find Indices, a run's validation and creation), which one user never
  reaches: the path picker's dialog shows such a 503 as its alert, a file helper as its message, the live validation as
  "Validation unavailable" (the next change checks again; Submit still runs) and Submit as its failure (sent again, the
  same `Idempotency-Key` is still unclaimed). A 503 of the type `urn:laueportal:problem:not-responding` (a hung `/net`
  mount, the backend's `files.probe`) is not busy: `busy.ts` detects it by its problem type (`isNotResponding`;
  `isUnavailable` is either), never asks again by itself (the server answers the same until the mount does) and shows
  "Not responding" with Retry at once. A frame's 503 takes more than 40 frames in flight, but one browser's six
  connections do not bound that: an aborted request leaves the server's queue only at its next disconnect check (0.5 s),
  and several viewers step at once. The ROI traces are a POST read as a query (`useReconstructionRoiTraces`,
  `src/api/roi-traces.ts`);
- viewer settings and ROIs are in Zustand stores persisted to localStorage. The indexing viewer's store
  (`src/features/indexing/viewer-store.ts`, `useIndexingViewerStore`) has one slice per tab: add a field to
  `IndexingViewerSettings`, its default to `DEFAULT_SETTINGS`, its checks to `readSettings` (stored and linked values
  pass through it), and its actions; "Copy link to this view" packs every slice (`settingsOf`). The slices beyond the
  scope (`map`, `pole`, `detector`) have their shapes, defaults and readers in `viewer-slices.ts`; the pole's
  `reference` (the colour centre and the reference pattern, null for a centre placed with the "Set color center" tool;
  an earlier flat step and pattern index still read) and the Rodrigues reference step name their run (`poleReferenceOf`,
  `rodriguesStepOf`), since the store is shared by every run. A typed number that changes a request uses `NumberField`
  (`src/features/indexing/NumberField.tsx`: applies on Enter or blur); the tabs' sidebar rows are the shared `Choice`,
  `Control`, `Section` and `Note` (`src/features/indexing/controls.tsx`). Changing a slice's shape so that old values
  cannot be read bumps `VIEWER_SCHEMA_VERSION` (old values and links are then discarded); a field added with a default
  the reader falls back to does not (the clicked pole's `previousColor`). To make browsers that stored the old default
  take a new one, bump `VIEWER_STORE_VERSION` (the persisted value's version, under the same key) and add a step to
  `migrateStored`. The persist middleware applies the step once and stores the value again, so a later choice of the old
  value stays; links keep their version and their values (version 2: a stored 3D `axes3d` of the motors' X, Y, Z becomes
  the lab X, Y, Z, the default). Every product query takes the scope from `useScopeParams()` (the applied scope: a typed
  N applies after 300 ms or on Enter/blur), never from the stored `scope`. A typed numeric value that changes a server
  query applies on Enter/blur or after a pause, not on every keystroke. A selector that returns several fields wraps it
  in `useShallow` (`zustand/shallow`; zustand 5 re-renders forever on a new object each render), or selects each field
  alone. The store is a module singleton persisted to jsdom's localStorage, so a test that renders the viewer calls
  `resetIndexingViewerStore()` (`src/test/indexing.ts`) after each test;
- transient interaction state stays in components.

## Tables

Always use `src/components/DataGrid` (AG Grid Infinite Row Model on the server grid protocol). Give it typed columns
(`GridColumn<Row>`, exported from the feature's `columns.ts`, and listed in `src/features/grid-contract.test.ts`, which
checks each sortable and filterable column against the list's `x-grid-fields` in `openapi.json`), the list's generated
function from `@/api` as `fetchRows`, a `gridId` and a `getRowId`; `selection` for checkboxes, set per grid
(`"singleRow"` for a list whose create actions take one source: Scans, Reconstructions, Indexations, the scan page's run
cards; `"multiRow"` for bulk actions: the Run Monitor, the scan-log import); `live` (and `changedSince` where the list
takes it) for lists of runs: every 5 s while a visible row is queued or running, every 15 s for its changes otherwise
(`changedSince` only), backed off after failed polls and stopped by a 404 until an answer comes (`live.ts`,
`src/lib/polling.ts`, which every other poll shares: the job page's, the artifact bar's; a new `refetchInterval` uses
`backoff`, `isGone` and `failureStreak` too). Create actions build their prefill with `sourcePrefill`
(`src/lib/prefill.ts`: one ID per parameter, never a list). `fetchRows` may be inline: the grid calls the latest one,
and a new function alone reloads nothing. It gets an `AbortSignal` as its second argument, aborted once the answer is
not wanted (the block asked again, another sort or filter, another list, the grid gone): pass it on where a request is
costly (`(query, signal) => listIndexingPeaks(id, query, { signal })`). A page's column checklist calls the handle's
`setColumnsShown(fields, shown)` and reads `onShownColumnsChange` (visibility stays in the grid's column state). The
grid keeps its column order, widths and visibility per `gridId` in localStorage (`column-state.ts`), also a column
shown, hidden or moved while it starts (AG Grid holds its state events for a tick at each start-up step and ends each
with its own `gridInitializing` event; a width dragged in such a tick is not kept; a page that only opens stores
nothing). Persistence is tested in `DataGrid.test.tsx` only; a page's test that clicks its checklist waits for the first
rows (`rowIds`) first and seeds a kept state with `writeColumnState`. `onRowCount` gives a heading its count
(`{total, filtered}`). For a list that depends on the page's parameters (e.g. a scan's runs), pass `listKey` (the scan
ID): another `listKey` is another list, so the grid reloads with its sort and filters and drops its selection; another
`gridId` is another grid. Columns fit the grid from 900 px up where they can; a list that cannot fit them marks its ID
`pinned: "left"` and its status `pinned: "right"` (fixed widths), kept in view only while the grid scrolls sideways. A
small list the page loads whole (the scan-log import's index, `SCAN_LOG_COLUMNS`) passes `rows` instead of `fetchRows`
(one or the other, `GridRows`; `rows` from the first render, an empty array while loading: the row model is chosen
once): AG Grid's client-side model sorts and filters it in the browser (a date filter on the local day, ranges
inclusive, as on the server; `toColumnDefs` sets `cellDataType: false`, since AG Grid infers types, and a date column a
date-and-time filter, only on that model), its header box selects the filtered selectable rows, a new array updates the
rows by ID, and `listKey` still drops the selection; such a grid is listed in `grid-contract.test.ts`'s
`CLIENT_SIDE_GRIDS` (also the wire viewer's Points, `POINT_COLUMNS`). AG Grid escapes the `row-id` attribute (`&` is
`&amp;`): a test finds a row whose ID has such characters by its cells. `isRowSelectable` (stable) disables the other
rows' checkboxes; the handle's `selectRows(ids)` selects exactly those rows among those held (a quick select). A
`custom` column draws its cell with `cell(row)` (a badge) and sorts and filters the field's text; a `status` column may
take a `cell` too (the run monitor's `JobStatusCell`: a queued or running crux run's remote phase,
`src/features/jobs/remote-status.ts`, shared with the job page's Remote Run card), still sorting and filtering the
status and driving a live grid's poll (`statusField`), and falls back to `RunStatusValue` (the status badge and "Held").
Use shadcn `Table` only for small fixed tables. The path picker (`src/components/PathPicker`) also takes `startPath`
(where it opens when its value is empty) and `maxFileSize` (in file mode, larger files are listed but cannot be chosen):
the scan-log import passes `GET /files/roots`' `scan_logs` (`start_folder`, `max_bytes`, `extensions`); read such
settings from the API, never copy them.

## Program forms

Program forms (the form kit, `src/features/program-form`, import from its `index.ts`): a create page is `CreateRunPage`
(below), which renders `<ProgramForm kind values fills source />` (`values`: the prefill's `values`; keep the object
stable: a new one applies a new prefill, which replaces the fields it derives from its source, those whose value is not
the configured default, and keeps what the user changed otherwise, such as Author, Notes and parameters: `keptByUser`;
`fills`, the field names a scan's or a reconstruction's prefill or a cleared source sets (`source-mode.ts`), applies
only those and the source IDs and keeps every other field on screen: `applyPrefill`; `source`, the page's source block
drawn instead of the `scan`/`reconstruction` fields; without it each is a picker, `onSourceChange`): it loads `GET
/programs` (which also lists the live programs, `lauego-live` and `wire-live`, while live processing is on: `@/api`'s
`ProgramKind` is the create forms' two, `LiveProgramKind` the Live page's, `ListedProgramKind` every listed one;
`isProgram(kind)` finds any listed program and `programSpec` reads it as a `ProgramSpec<K>`, a create form's a
`ProgramSpec<ProgramKind>`) and draws `x-layout`'s sections as cards (`SectionCard`, also the Live page's cards and
the viewers' Parameters tabs, one column of read-only values in the form's order; `sectionAnchor(group)` ids for a
sidebar; each card's accent stripe, a bar that takes no layout space, and `SectionIcon` from
`sectionIdentity(title)`, `section-identity.ts`: one mapping for the create forms, their sidebar, the Live page's
cards and the Parameters tabs (their "Other recorded values" neutral), the accents the `--section-*` tokens; the
sidebar's icons are `SectionIcon muted`, in the muted text colour, so only the cards show the accents;
`section-identity.test.ts` fails for a title the programs use without an entry; Restore Default on a section of
several fields that all have a default), each field by its `x-widget` (`scan`/`reconstruction` source pickers, which
show a value set from outside as its ID; `folder`/`file` path pickers, a file's listing filtered by `x-extensions`;
`output-folder`, whose Browse picks a folder and keeps the template's part from its `%d` on; `filename-templates`
with Find Matching Files, consecutive `indices` with Find Indices (both off while the input is an HDF5 file, a
reconstruction's scan.h5, the reason in their tooltip), consecutive `hkl` as one row, numbers with their `x-unit`,
nullable ones blank for automatic, `select` from `x-options`, `checkbox`, `text`, `textarea`), the validation summary,
Run on (`backend`'s `x-options`) with Validate and Submit (`SubmitBar`), and the answer; the page's `layout`
(`FormLayout`, given the `FormParts`: `actions`, `message`, `summary`, `answer`, `sections`) places them inside the form
element, so Submit remains the form's submit button and Enter in a field submits. Titles are labels; a field's `x-hint`
(the user's words; the `description` is the API's, for developers) and its limits are the label's tooltip; a placeholder
is at most a terse value pattern (`x-placeholder`). The form stores text and booleans (`form-values.ts`; a
comma-separated Filename; a number that does not parse or is not finite is sent as typed); the server is the only
validator and words its messages without the field's label (`core.problems.field_message`): live validation calls
`…/validate` 500 ms after the last change (a check in flight for other values is aborted, Submit shares the one for its
values, an older answer never replaces a newer one), issues are placed by `loc[0]` (`[]` or a non-field name: the form's
own), errors first. A field's issues show once it is touched (changed or left; a prefilled value is not), all of them
after Validate or a Submit attempt; the summary counts every issue from the start, lists those shown and names each
field once (`summaryText`). Submit stays clickable: it checks the current values first, and on errors shows them all,
focuses the first and sends nothing; it confirms warnings. The create sends one `Idempotency-Key` per form session
(request options; after a 422 the key is kept, after a 201 the session ends: a run created and queued opens its page
after the render that ends the unsaved state, while one with a `failure` stays in the Submitted panel until New
Submission; a 409 offers New Submission, the values kept, beside the run and job the key created when it did
(`KeyConflict.created`, a problem of type `urn:laueportal:problem:key-created`, `KEY_CREATED`: an answer lost, then the
values edited); a key from `crypto.getRandomValues` where `crypto.randomUUID` is missing, plain HTTP on a host name); a
refused or failed submission's message (`SubmitMessage`) is on its own line under the header's row, above the summary,
while its values are on screen (a 409's until New Submission). The rule is in `useRunSubmission` (`touched`,
`onBlocked`), so a page that composes its own layout from the field components gets it too; `useSubmission(requests,
body, options)` is the same rule for other requests (`SubmissionRequests`: `validate`, `create` with its key, `place`
the issues on the page's fields; `onCreated` after a 201), the Live page's session-level validate and create;
`CloneField` is the labelled clone picker ("Clone of": an indexing run on the LaueGo form, a reconstruction on the wire
form). A create page is `<CreateRunPage kind title search />` (the route's `validateCreatePrefill` search; the page
removes it from the URL itself, with replace; `/reconstructions/new`, `/indexing/new`): the header (the title with Run
on, Validate and Submit beside it, a refused submission's message under them, then the validation summary), sticky below
the navbar from `md` up (its height is `--form-header`, which the sidebar's sticky offset and the scroll margins of the
cards and of every focusable element below it (the Submitted answer's, the form's column's) add, so a card or a focused
element is never under it; when the header grows over the focused element, that element scrolls to below it again, and
nothing else moves the page (a header that shrinks, an element the user scrolled under it); a long summary scrolls in
its own box), the Submitted answer under the header (scrolled into view when it appears), the sidebar of section anchors
(a link scrolls to its card and focuses it), Identity's source block (`SourceBlock`: the "Source" switch, the shadcn
`ToggleGroup`, with Scan and Clone on the wire form and Scan, Reconstruction and Clone on the LaueGo form, Scan by
default (`source-mode.ts`); one picker for the chosen kind (`SourceField` or `CloneField`; an empty Scan is an unlinked
run); the IDs the source sets read-only, as links: a reconstruction's scan, a clone's scan and reconstruction), and the
prefill of one source context: `useConsumedPrefill` (`src/lib/prefill.ts`) takes the URL's
`scan`/`reconstruction`/`indexing` once (a later link with others too) and removes them; `useProgramPrefill` reads GET
`/programs/{kind}/prefill` once per source and keeps the last one loaded while another loads or fails; the form stays
mounted while a prefill loads once it has been shown (a skeleton only for the URL's first source), so every new source
keeps the user's edits; a source picked is the new context and its kind follows each new context (`sourceModeOf`: the
URL's reconstruction opens Reconstruction on the LaueGo form and Clone on the wire form, an indexing run Clone); a clone
fills the form, a scan or a reconstruction its data fields and IDs (`SOURCE_FIELDS`: a scan Folder Path, Filename,
Output Path; a reconstruction also Scan Indices, Depth Indices, Geometry File); another kind (also one set from outside:
a later link), a cleared picker or a refused prefill clears the source, its IDs and the data fields (`CLEARED_FIELDS`:
Folder Path, Filename, Scan Indices, Depth Indices, Output Path; a typed value stays) before anything applies, so the
form never mixes two sources; the Structures File, the Output XML and the parameters stay; Clone of shows none for a
refused source; a refused source gets the API's message (`PrefillAlert`); a URL parameter that is not one ID is dropped
(`validateCreatePrefill`). Tests read the `/programs` fixture `src/test/programs.json` (`specOf`, `fieldOf`);
`backend/tests/jobs/test_programs.py` checks it against the served schema (all but the defaults), so refresh it from the
API when the models change.

## The Live page

The Live page (`src/features/live`, `/live`): the navbar shows "Live" only while `GET /programs` lists the live programs
(`useLiveEnabled`; `[live] enabled`; a failed read again keeps the last answer, there and on the page), as the Menu's
first item below 1024 px. Below 1024 px the main links also have 5.4 px of padding a side instead of 9 px, so that at
768 px the Worker indicator's dot keeps its 75.6 px link and a pill shows its state's words whole; its subject (Worker,
API) is dropped first, below about 800 px (`e2e/shell.spec.ts`).

The page reads the latest session
(`GET /live/session`, `useLiveSession`: every 5 s while it is open, as `polling.ts` backs off; once read, a failed read
again keeps the last answer and the form, with the header's one-line error and Retry), and the DM DAQ list only while
its form is open (no session open) and never sooner than the answer is kept (`useDaqList`: `kept_seconds`, or a 503's
`Retry-After`, plus a second). Its form is the kit's: three React Hook Form forms (the Scan card's fields, `scanFields`,
and the two program cards, the live programs' fields but those the Scan card sets, `cardSpec`/`SCAN_SETS`, the LaueGo
card's Depth Indices only while it follows the reconstruction), drawn with the kit's field components; a DAQ the user
picks fills Folder (the kit's `SelectField onPick`: never when the page applies a session's values); Output Folder
follows Folder (`followedOutput`: its mirror, `mirroredOutput`, the last `data` component made `analysis`; the
configured default for a Folder without one, a blank one too) until the user edits it (a `watch` of the Scan form's
`change` events: the page's own `setValue` and `reset` have none; a session's values applied count as an edit only when
their Output Folder is not the one their Folder gives, nor its names below its last `analysis` the Folder's below its
last `data`, `outputFollows`: the session records its Folder by its real path), and
its From data folder button (`PathPickerField actions`, its tooltip on a wrapper since a disabled button takes no
pointer events) mirrors it again; one request (`sessionRequest`: `scan`, each part switched on, else null) through
`useSubmission`, its issues placed by their part (`placeSessionIssues`: qualified names, `scan.folder`,
`indexing.geometry_file`; a DAQ's on Source). While a session is
open the cards are in a disabled `fieldset` (Base UI's switch and checkbox are not native controls: they get `disabled`
themselves, the kit's `ToggleField disabled`) and show the session (its Scan card as recorded, `LiveSession.scan`, and
each run's clone, the kit's prefill `GET /programs/{wire,lauego}/prefill`), but for this page's own Start, whose values
stay; once it has ended the clone applies with the kit's `applyPrefill` (what the user typed beyond it stays; for the
page's own session as the form's new baseline, not an unsaved edit) and Start is a new submission (a new key). A Start's
409 has a session (`conflictJobs`: the open run, or the runs its key created); its message shows until that session
has ended, then the submission is renewed (a new key). The header shows no session once a session a user stopped has
ended with no run Failed of its own (`shownSession`, `LiveSession.stopped`; a run with `stopped_now` and no failed
file is the Stop's end; the form still fills from it), while one that ended without a Stop, or with a run Failed
otherwise, stays shown until the next Start; an ended session applied to the form has its values checked again
(`revalidate`, the kit's `useSubmission`: it marks the report stale, never reused by a flush, and the debounced check
runs after the next render, for the values then on screen: one request). Stop
asks, then cancels the session's first run (`POST /jobs/{id}/cancel`; its dialog
and report show `LIVE_STOP_EXPLANATION`, as a live run's Stop on its job page and in the Run Monitor does:
`JobRef.live`, from `JobSummary.live` in the Run Monitor, `stopExplanation`; the job page's Live Run card from
`JobDetail.live`, `liveRows`); the session is Stopping from then or from that run's end until the
session ends (a follower drains after its wire run), and Stop is then Stop now (`LIVE_STOP_NOW_EXPLANATION`, a one-line
confirm): `stopJobNow` on every open run (`openRuns`: one not stopped yet is stopped, one stopping killed, Failed), and
the Stopping badge's tooltip is why the session waits (`waitingReason`: the follower's arrival-status error, a point it
cannot read; a DAQ's or a notify run's error never holds a Stop). A queued batch run behind an open session has "Held"
beside its status in the Run Monitor (`StatusCell`,
`JobSummary.held`, its tooltip `HELD_HINT`) and on its job page; the artifact bar shows a held conversion as held. The
rules are pure functions in `live-form.ts` (`live-form.test.ts`), the page's states in `LivePage.test.tsx`, sessions end
to end in `e2e/live.spec.ts` (the e2e stack has `[live]` on with a stand-in DM, `deploy/supervisor/e2e.sh`:
`tests.remote_support.FakeDMInstall` behind the real DM client, failing every call, so the DAQ list is unavailable,
until a test writes its state with `setFakeDM` from `e2e/stack-tools.ts`, as the DAQ session, the file's last test,
does: its clone would make a DAQ the next form's source). The switch is shadcn's (`src/components/ui/switch.tsx`, added
with the script; its focus ring made the opaque one).

## Visualization

- 2D views use the Canvas2D renderers in `src/viz` and must work **without WebGL**;
- the scatter (`ScatterView`, and `PoleFigureView` with the unit circle and HSV centre, and with `onCenterClick` the
  "Set color center" tool, the `center` ScatterTool: a click reports the pole under it, or null, and the point; import
  from `@/viz`) takes the product's typed arrays as `data` (x, y, colours as r,g,b bytes or a scalar through the
  caller's `lookup`, `indexed`, `step`, `patternIndex`). It memoizes on the arrays and the `lookup`, not on the objects
  holding them, so inline `data` and `colors` objects are fine; but new arrays or a new `lookup` function re-pack and
  re-stamp every point, so derive them once per product and memoize the lookup on its colour table. New x or y also
  rebuild the hover index and refit the automatic view (padded by half the marker size, so edge markers are whole; also
  at the pole figure's rim); after a pan or zoom the user's view stays (Reset refits), so give the view a new React
  `key` when the data becomes another plot (e.g. other axes). `onHover` fires only when the hovered point changes (the
  coordinates readout is the view's own). Its logic is in pure functions (`src/viz/scatter/*.ts`, tested without a
  canvas); the controller and renderer hold the canvas, and the component only wires props;
- the heatmap (`HeatmapView`, from `@/viz`) takes a `Frame` (`decodeFrame(body, X-Frame-Shape, X-Frame-Dtype)`: uint16
  or float32; the indexing viewer's come from `useDetectorFrame` in `src/api/products.ts`, decoded once and revalidated
  by their ETag as the Arrow products are, and dropped a minute after no view shows them, at most `FRAME_CACHE_LIMIT`
  (8) such frames kept, those used last (`FRAME_GC_TIME_MS` and `trimFrameCache`: a frame is 8-16 MiB; the wire viewer's
  depth frames do the same: `useReconstructionFrame`, which encodes the point ID, as every call of a point's route must,
  and never refetches a frame it holds; the ROI Inspector's reference images, `useReconstructionReference`, the same
  way)), the colormap, the colorbar's `range` and `scale`, `opacity`, and `overlays` from
  `overlayLayers(DetectorOverlays, toggles)` (memoize it on the payload and the toggles), and `boxes` (`OverlayBox`:
  half-open `[y0, y1, x0, x1]` pixels and a colour, outlined over a dark rim: the wire viewer's ROIs; a new list
  redraws, never recolours); `extent` shows the overlays without an image. A new range, scale or colormap only recolours
  (one table lookup a pixel); a new frame of the same size keeps the user's zoom (the wire viewer's depths), so give the
  view a new `key` for another kind of frame. The plot box has the image's aspect, centred (`imageBox`: the axes span
  only the image). Its logic is in pure functions (`src/viz/heatmap/{frame,image-view,overlays}.ts`,
  `src/viz/color/*.ts`);
- the interactive colorbar (`ColorBar`): the page keeps its `ColorbarSettings` (`colorbarSettings(FRAME_AUTO_RANGE)`
  for frames, `MAP_AUTO_RANGE` for the map's scalars), gets the data's distribution once with
  `const { distribution, error, retry } = useDistribution(array)` (a worker for large frames; pass a stable array; the
  previous one stays while a new array's is computed, so a new frame does not blank), gives the bar all three
  (`distribution`, `error`, `onRetry`: a failed worker shows on the bar, and Retry computes in the page) and passes
  `resolveRange(settings, distribution)` and `colorScaleOf(settings)` to the heatmap, or
  `scalarLookup(colormapRgb(palette, reverse), lo, hi, scale)` (memoized on those values) to the scatter's `lookup`.
  A typed range, or one kept across frames, applies as given; only drags are bounded. Map palettes are the backend's
  lower-case `Palette`, frame colormaps `FRAME_COLORMAPS`. Its logic is in `src/viz/colorbar/logic.ts`;
- linked highlighting: one store per page, created once (`const [store] = useState(createSelectionStore)`; never
  `createSelectionStore()` in a component body, which makes a new one each render), read with `useLinkedSelection`
  (`src/viz/linked-selection.ts`), keyed by (step, pattern_index); a view's `onSelect` writes its pairs, every view
  takes the store's selection as `selection`. In memory only: not the persisted viewer store, not the URL. Frame-only
  records (pattern_index -1) are highlighted but never sent to `selection-stats` (its pairs are ≥ 0). The pole figure's
  card asks `useSelectionStats` (`src/api/selection-stats.ts`, a query keyed on the endpoint's URL and the patterns,
  re-exported by `@/api`) with `statsRequest` (`pole-figure.ts`): the selected patterns that have a pole, each once,
  sorted; nothing below two or above 700 (the skip notice);
- the 3D view (`PointCloud3DView`, from `@/viz`) takes the map product's arrays with three axes as `data` (as the
  scatter, plus `z`; memoized the same way) and `axes` `{x, y, z}` (names). VTK.js loads with `import()` on its
  first mount (only `src/viz/cloud/scene.ts` imports it at runtime: keep it so, or VTK.js is in the main
  bundle). Without WebGL 2 it shows the "3D needs WebGL" message instead. The page's `renderTooltip(index)` gives
  the hovered point's details and the view adds its x, y, z. Hover picks on the CPU, once per frame
  (`src/viz/cloud/pick.ts`, on VTK.js's camera matrix: `pick.test.ts` pins its convention); the selection dims the
  others and draws the selected points over the cloud with rings. New coordinates refit the camera only while the
  user has not moved it (a drag past the click slop or a zoom does; a click on a point does not); other `axes`
  names always refit it, so the page needs no new `key` for them. Unmounting releases the WebGL context. Its logic
  is in pure functions (`src/viz/cloud/{pick,points,webgl}.ts`, `src/viz/frame-throttle.ts`);
- the unit-cell view (`UnitCellView`, from `@/viz`) draws a cell's rows a, b, c in beamline coordinates with Canvas2D,
  in perspective (front edges by depth, hidden ones dashed, the front faces tinted), with the beam's direction from
  the shared a, b, c origin and the X, Y, Z triad; its colours are the `--cell-a`, `--cell-b`, `--cell-c` and
  `--cell-beam` tokens. Its camera is its own and stays when the cell changes, or the caller's (`camera`,
  `onCameraChange`). The bases, the rotation from a reference cell (in the nearest of the crystal's
  `equivalent_settings`, from the point's details: `nearestSetting`),
  the camera and the cell's faces and edges are pure functions (`src/viz/cell/geometry.ts`). The Color Map tab's
  `UnitCellCard` reads the clicked point's details (`src/features/indexing/unit-cell.ts`) and keeps the camera, so it
  stays across points; its collapsed state and beam switch are the map slice's `cellCollapsed` and `cellBeam`;
- no Plotly.

## UI

- shadcn components are in `src/components/ui` (our own code). Add more with `scripts/shadcn-add.sh <names>`: it runs
  the pinned CLI, rewrites `cn` imports to `@/lib/utils`, drops packages that are not approved (`cn`, `shadcn`,
  `next-themes`, fonts) and fails if a component still imports one (re-adding `sonner` brings back `next-themes`:
  restore ours);
- use theme tokens (CSS variables), not hard-coded colours;
- surfaces: the page is `--canvas` (body); cards and panels are surfaces on it. Use `Card` (its `CardHeader` is the
  header strip: title, actions, a divider) or, for a region that is not a card (a sidebar, a plot, the viewer's header),
  `surface` from `src/lib/styles.ts`; a heading inside a card or panel is `subheading`. Line tabs
  (`TabsList variant="line"`) are on a full-width baseline. `card.tsx`, `tabs.tsx` and the fields' `bg-background`
  (input, textarea, select) contain these changes: keep them when the CLI re-adds a component;
- label/value rows in a card or panel are `DefinitionList` (`src/components/DefinitionList.tsx`, with its `Value` for a
  dash read as "none"): labels muted and smaller, values in the foreground in tabular figures, `mono` per row, only for
  paths, file names and code-like values (hashes, PVs, recorded JSON; the job page's run-file rows by `isCodeRow` in
  `src/features/jobs/job-page.ts`, the Parameters tab's by its file keys); the hover readouts and the big counters keep
  their own layout;
- text: the UI shows labels, values, statuses and errors, not sentences about how it works. A control's hint is
  its tooltip (`title`); an empty state is one short line (a title, no description); a blocked state shows what is
  missing in one line. A confirm dialog states what its action does, in one sentence;
- keyboard focus shows the opaque `--ring` (`focus-visible:ring-ring`; never shadcn's translucent `ring-ring/50`, which
  a component added with the CLI brings back), and nothing replaces it while focused: an invalid field's ring and
  border apply only without focus (`aria-invalid:not-focus-visible:…`). `src/focus-contrast.test.ts` checks that the
  token reaches 3:1 on every surface in both themes and that no source breaks these rules (no accessibility target
  beyond that);
- a page with edits a user could lose renders one `UnsavedChangesGuard when={...}` (`src/components`: TanStack
  Router's `useBlocker` and `beforeunload`; a change of the page's own search is not leaving it): the create
  forms (`ProgramForm`: edits not yet a created run), the scan page (the note box or the catalog form) and the
  scan-log import (New rows, a selection or catalog defaults no import took);
- an error toast is `toastError` (`src/lib/toast.ts`), on screen for `ERROR_TOAST_MS` (15 s, as a Stop or Move to Front
  report with an error; a toast that is not an API error passes `duration: ERROR_TOAST_MS`); `describeError` gives a
  problem's `instance` (the reference `ErrorState` and `RouteError` show) only when its detail does not name it already
  (the API's 500 does);
- every view handles loading, empty and error states; a viewer's artifact bar shows a run directory that is not on this
  host or not responding as a status of its own, apart from empty (the artifact's `not_on_host`, `not_responding`,
  `empty`; the wire viewer's tabs show it instead of a point, `noReconstruction`), and polls while the run writes or the
  directory does not respond, and (the indexing viewer) every 5 s while its run is an open live run (`live_open`),
  reading the results again when `results_version` changes (`resultsReplaced`, then `reloadResults`), the detector's
  default step kept (`detectorStep`'s `kept`); the wire viewer of an open live run offers "Follow newest point"
  (`newestComplete`, a followed point written with replace; the user's pick turns it off); the indexing viewer's results
  tabs (all but Parameters) render through the page's `ResultsGate`, which handles an unreadable artifact and the
  summary's loading and error (Retry) once for all of them, and gives the tab the summary;
- keep the layout familiar: users know it from the beamline; change it with a reason.

## Routes and titles

- a route file in `src/routes` sets its title with `head: titleHead("<page>")`, a detail page with
  ``head: ({ match, params }) => titleHeadFor(match, `Indexing I${params.indexingId}`)``; nothing else sets
  `<title>` (`src/app/router.test.tsx` checks every route);
- unknown URLs reach the catch-all `src/routes/$.tsx`, the 404 page; a loader that cannot find its record throws
  `notFound()`;
- `src/routeTree.gen.ts` is generated and committed, and it typechecks. After adding or removing a route, regenerate it
  with `pnpm exec vite build` (or a running `pnpm dev`), because `pnpm build` typechecks first.

## Components

`features/system/RemoteHealthIndicator` is the navbar's ALCF dot, compact status tooltip and details dialog. Its generated query reads
`GET /system/remote-status`, shared across browsers by the server: every five minutes while visible (two seconds while
a check runs), no automatic retries, and a ten-second API timeout. Refresh respects the server's cache/backoff.
`remote-readiness.ts` keeps presentation separate: stale answers never stay green, sign-in and denied access are
distinct from a confirmed offline endpoint, and remote-disabled deployments hide the indicator. Keep the panel
usable in both themes; monitoring must never submit work.

- component files export only components (keeps hot reload working);
- keep components small and typed;
- no `any`.

## Tests

- Vitest for logic (renderers, URL state, formatting);
- Vitest's slow set, the heaviest page tests (`SLOW_TESTS` in `vite.config.ts`, about half of the tests' time), runs in
  `make test-frontend-slow` and the Full tests workflow, not in `make check-frontend`; each of those pages keeps fast
  tests of its logic. A `.test.ts` file runs in Node, without a DOM; one whose code reads the DOM or the browser's
  storage, also on import (Node then warns that localStorage is not available), starts with
  `// @vitest-environment jsdom`;
- Vitest keeps no transformed modules between runs (`fsModuleCache: false` in `vite.config.ts`, about 2 s a run): its
  cache contained a typed `import.meta.glob`'s file list from an earlier run, so leave it off;
- Playwright for daily-use flows, Chromium, one worker: the `default` project runs every spec; `no-gpu` runs only the
  WebGL rendering checks, the tests tagged `@webgl` (`--disable-gpu`, `--disable-software-rasterizer`, without
  Playwright's `--enable-unsafe-swiftshader`, so no WebGL at all; a test branches on `testInfo.project.name`). A mocked
  test that waits on the app's polling timers uses Playwright's fake clock (`page.clock`, as `e2e/shell.spec.ts` does);
  a test on the seeded stack, whose timing comes from the worker, runs in real time. `make e2e` runs them against the
  real stack (`deploy/supervisor/e2e.conf` with the settings of `deploy/supervisor/e2e.sh`: its own `e2e.db`, remote
  execution off, live processing on with a stand-in DM) on a freshly seeded demo database (with R4, a `crux` run
  recorded as running: `seed demo --crux-run`; `e2e/smoke.spec.ts` mocks the health API; `e2e/stack.spec.ts` mocks
  nothing). `E2E_BASE_URL` overrides the base URL (default `http://127.0.0.1:2100`); the stack behind it must serve
  `e2e.db` (`e2e/global-setup.ts` checks `GET /system/status` before any spec). A mocked error answers through
  `e2e/problems.ts` (`contentType` problem+json, which a `headers` content type beside `json:` is not, and the backend's
  own titles and details); a mocked outage is the API stopped, `route.abort("connectionrefused")`. A test that changes
  data through the API directly (Playwright's `request`) sends `X-Laue-Portal: 1`, as `src/api/fetcher.ts` does: the API
  refuses a write without it (a 403).
- Playwright runs the Chromium build its pinned `@playwright/test` expects (in `~/.cache/ms-playwright`; installed
  as in [dev-setup](../runbooks/dev-setup.md#prerequisites)).

## Builds and dependencies

- build target `['chrome111','edge111','firefox140','safari16.5']`;
- no CDN assets;
- no new dependency without a reason in the pull request and a maintainer's approval.
