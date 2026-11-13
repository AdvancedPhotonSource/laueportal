// The only import point for server data (docs/contributing/frontend.md). Features import hooks and types from
// "@/api", never from ./generated, so the generated client can be replaced in this folder alone.
// Regenerate with `make gen` after an API change.

export {
  ApiError,
  apiUrl,
  NetworkError,
  type Problem,
  type ProblemIssue,
} from "./fetcher"

// system
export {
  getGetHealthQueryKey,
  getHealth,
  useGetHealth,
  useGetSystemStatus,
  getRemoteReadiness,
  useGetRemoteReadiness,
} from "./generated/endpoints"
export type {
  ActiveRun,
  Health,
  HealthDb,
  HealthWorker,
  QueueCounts,
  QueuedItem,
  RemoteRunState,
  SystemStatus,
  WorkerStatus,
  WorkerThread,
  RemoteReadiness,
  RemoteCheck,
} from "./generated/model"

export { getRemoteReadinessWithin } from "./remote-readiness"

// scans
export {
  getGetScanQueryKey,
  getGetScanQueryOptions,
  listScans,
  useAppendScanNote,
  useGetScan,
  useUpdateScanCatalog,
} from "./generated/endpoints"
export type {
  ListScansParams,
  ScanCatalog,
  ScanCatalogUpdate,
  ScanDetail,
  ScanDimension,
  ScanMetadata,
  ScanNote,
  ScanPage,
  ScanPositioner,
  ScanSummary,
} from "./generated/model"

// the scan-log import (/scans/import): index a log, import its New scans
export { useImportScans, useIndexScanLog } from "./generated/endpoints"
export {
  ScanImportDefaultsAperture,
  ScanImportOutcome,
  ScanLogStatus,
} from "./generated/model"
export type {
  ScanImportDefaults,
  ScanImportItem,
  ScanImportReport,
  ScanImportRequest,
  ScanImportSummary,
  ScanLogIndex,
  ScanLogRow,
} from "./generated/model"

// The API's own Zod schemas (Orval), to check what a form sends against the contract.
export { UpdateScanCatalogBody } from "./generated/zod/index.zod"

// the programs' forms (the form kit, src/features/program-form): the parameter schemas, the prefill,
// the file helpers, validation and run creation (the Idempotency-Key goes through the request options)
export {
  createIndexingRun,
  createLaueMatchingRun,
  createReconstructionRun,
  findFileIndices,
  findFilePatterns,
  getListProgramsQueryKey,
  listPrograms,
  prefillProgram,
  useListForwardCaches,
  useListPrograms,
  usePrefillProgram,
  validateIndexingRun,
  validateLaueMatchingRun,
  validateReconstructionRun,
} from "./generated/endpoints"
import type { ProgramKind as ListedProgramKind } from "./generated/model"
/** Every program `GET /programs` lists: the batch ones and, while live processing is enabled, the live ones. */
export type { ListedProgramKind }
/** A create form's program (the batch ones); the live ones are the Live page's. */
export type ProgramKind = Extract<
  ListedProgramKind,
  "lauego" | "laue_matching" | "wire"
>
/** The Live page's programs (listed while live processing is enabled). */
export type LiveProgramKind = Exclude<ListedProgramKind, ProgramKind>
export type {
  CreatedRun,
  FileIndices,
  FilePattern,
  FilePatterns,
  KeyConflict,
  Program,
  ProgramList,
  ProgramPrefill,
  ProblemItem,
  ValidationReport,
} from "./generated/model"
export {
  CreateIndexingRunBody as CreateIndexingRunBodySchema,
  CreateLaueMatchingRunBody as CreateLaueMatchingRunBodySchema,
  CreateReconstructionRunBody as CreateReconstructionRunBodySchema,
  GetIndexingRunResponse as IndexingRunDetailSchema,
  ValidateIndexingRunBody as ValidateIndexingRunBodySchema,
  ValidateLaueMatchingRunBody as ValidateLaueMatchingRunBodySchema,
  ValidateReconstructionRunBody as ValidateReconstructionRunBodySchema,
} from "./generated/zod/index.zod"

// the Live page: the DM DAQ list, a live session's validation and its Start (the Idempotency-Key
// through the request options, as the create forms'), the latest session (its runs' counters and
// arrival status, its Scan card); present while `[live] enabled` (GET /programs then lists the live
// programs), a 404 otherwise
export {
  createLiveRuns,
  getGetLiveSessionQueryKey,
  getListLiveDaqsQueryKey,
  listLiveDaqs,
  useGetLiveSession,
  useListLiveDaqs,
  validateLiveRuns,
} from "./generated/endpoints"
export type {
  CreatedLiveRuns,
  JobLive,
  LiveArrivalStatus,
  LiveDaqEntry,
  LiveDaqList,
  LiveRunsConflict,
  LiveSession,
  LiveSessionRun,
  LiveSessionScan,
  LiveSessionState,
  OpenLiveRun,
} from "./generated/model"

// reconstruction runs
export {
  getListReconstructionRunsQueryKey,
  listReconstructionRuns,
  useListReconstructionRuns,
} from "./generated/endpoints"
export type {
  ReconstructionRunPage,
  ReconstructionRunSummary,
} from "./generated/model"

// indexing runs
export { listIndexingRuns } from "./generated/endpoints"
export type { IndexingRunPage, IndexingRunSummary } from "./generated/model"

// the wire reconstruction viewer (the run and its stored parameters, its artifact, its points, a
// point's trace)
export {
  getGetReconstructionRunQueryOptions,
  getListReconstructionPointsQueryKey,
  useGetReconstructionArtifact,
  useGetReconstructionRun,
  useGetReconstructionTrace,
  useListReconstructionPoints,
} from "./generated/endpoints"
export {
  OutputPathState,
  ReconstructionArtifactState,
  ReconstructionFormat,
  ReconstructionPointStatus,
  ReconstructionReferenceKind,
} from "./generated/model"
export type {
  ReconstructionArtifact,
  ReconstructionPoint,
  ReconstructionPointList,
  ReconstructionRelativePaths,
  ReconstructionRoi,
  ReconstructionRoiTrace,
  ReconstructionRoiTraces,
  ReconstructionRoiTracesRequest,
  ReconstructionRunDetail,
  ReconstructionTrace,
  RootRelativePath,
  WireParameters,
} from "./generated/model"

// a point's ROI traces (a POST read as a query)
export {
  useReconstructionRoiTraces,
  type RetryRule,
  type RoiTracesAnswer,
} from "./roi-traces"

// the indexing viewer (the run and its stored parameters, its artifact, the XML choice, the
// conversion, the summary)
export {
  getGetIndexingArtifactQueryKey,
  getGetIndexingRunQueryOptions,
  useConvertIndexingResults,
  useGetIndexingArtifact,
  useGetIndexingRun,
  useGetIndexingSummary,
  useSelectIndexingXml,
} from "./generated/endpoints"
export {
  ArtifactState,
  ConversionStatus,
  MapColor,
  OrientationSymmetry,
  XmlSource,
} from "./generated/model"
export type {
  AxisChoice,
  ColorModeChoice,
  ConversionJob,
  GetIndexingMapParams,
  GetIndexingPoleFigureParams,
  GetIndexingSummaryParams,
  IndexingArtifact,
  IndexingResultsSummary,
  IndexingRunDetail,
  LaueGoParameters,
} from "./generated/model"

// the viewers' products: the Arrow ones, the detector frame and the wire viewer's depth frame decoded
// (./products.ts); the indexing viewer's legend image, point details, detector overlays and the
// selection's statistics
export {
  decodeDetectorFrame,
  decodeMapProduct,
  decodePoleFigureProduct,
  useCalibrationFrame,
  useDetectorFrame,
  useMapProduct,
  usePoleFigureProduct,
  useReconstructionFrame,
  useReconstructionReference,
  type DepthFrame,
  type MapProduct,
  type MapProductMeta,
  type PoleFigureProduct,
  type PoleFigureProductMeta,
  type ProductColors,
} from "./products"
export {
  getGetIndexingLegendUrl,
  useGetIndexingDetectorOverlays,
  useGetIndexingPoint,
} from "./generated/endpoints"
export { LegendColor } from "./generated/model"
export type {
  DetectorSummary,
  GetIndexingDetectorOverlaysParams,
  PatternDetails,
  PointDetails,
} from "./generated/model"
// the Peaks and Patterns tabs' grids (the grid protocol, fetched by the DataGrid; the totals as queries)
export {
  listIndexingPatterns,
  listIndexingPeaks,
  useListIndexingPatterns,
  useListIndexingPeaks,
} from "./generated/endpoints"
export type {
  PatternPage,
  PatternRow,
  PeakPage,
  PeakRow,
} from "./generated/model"
// the misorientation of a selection (read as a query: ./selection-stats.ts)
export { getSelectionStatsQueryKey, useSelectionStats } from "./selection-stats"
export type { SelectionStats } from "./generated/model"

// the indexing viewer's renderers (src/viz: the map's palettes, the detector overlays)
export { Palette } from "./generated/model"
export type {
  DetectedPeak,
  DetectorOverlays,
  DetectorPattern,
  IndexedPeak,
  SimulatedReflection,
} from "./generated/model"

// files (the roots-restricted path picker)
export {
  browseFiles,
  getBrowseFilesQueryKey,
  listFileRoots,
  useBrowseFiles,
  useListFileRoots,
} from "./generated/endpoints"
export type {
  BrowseFilesParams,
  DirectoryListing,
  FileEntry,
  FileRoot,
  FileRoots,
} from "./generated/model"
export { EntryKind } from "./generated/model"

// runs (the shared status, progress and link components)
export { RunKind, RunStatus } from "./generated/model"
export type { RunReference } from "./generated/model"

// jobs (the run monitor and the job page)
export {
  cancelJob,
  getGetJobLogQueryKey,
  getGetJobQueryKey,
  getGetJobQueryOptions,
  getListJobFailuresQueryKey,
  listJobs,
  moveJobToFront,
  stopJobNow,
  useListJobs,
  useGetJob,
  useGetJobLog,
  useListJobFailures,
} from "./generated/endpoints"
export type {
  CancelResult,
  FailurePage,
  FailureRow,
  JobDetail,
  JobLog,
  JobPage,
  JobProgress,
  JobRemoteRun,
  JobSummary,
  LogTail,
  MessageClass,
  RemoteObservation,
  RequestManifest,
  RunArtifact,
  RunConfiguration,
  RunDirectory,
  RunExecution,
  RunFile,
  RunHistory,
  SubmittedRequest,
} from "./generated/model"
export {
  ArtifactViewer,
  CancelState,
  FileState,
  RemotePhase,
} from "./generated/model"

// the Calibration page: its setup, frame, peaks, indexing (read as queries, ./calibration.ts), the
// fit and the saved geometry file
export {
  fitCalibrationPeak,
  getGetCalibrationSetupQueryKey,
  useGetCalibrationSetup,
  useRefineCalibration,
  useSaveCalibration,
} from "./generated/endpoints"
export {
  useCalibrationIndexing,
  useCalibrationInputs,
  useCalibrationPeaks,
} from "./calibration"
export type {
  CalibrationDetector,
  CalibrationDetectorValues,
  CalibrationHarmonic,
  CalibrationIndexing,
  CalibrationIndexRequest,
  CalibrationIndexSettings,
  CalibrationInputs,
  CalibrationInputsInfo,
  CalibrationMeasurement,
  CalibrationPeak,
  CalibrationPeakSettings,
  CalibrationPeaks,
  CalibrationPosition,
  CalibrationRefinedRow,
  CalibrationRefineRequest,
  CalibrationResult,
  CalibrationRow,
  CalibrationSaved,
  CalibrationSaveRequest,
  CalibrationSetup,
  CalibrationSurface,
  GeometryFolder,
} from "./generated/model"
