// The form kit, composed by the create pages: the program form scaffold driven by GET /programs, its
// field components, the source pickers, live validation and submit.
export { CreateRunPage } from "./CreateRunPage"
export { PrefillAlert } from "./PrefillAlert"
export { ProgramForm, ProgramFormBody } from "./ProgramForm"
export type { ProgramFormProps, SourceChange } from "./ProgramForm"
export { CloneField, SourceField, SourcePicker } from "./SourcePicker"
export {
  HklField,
  NumberField,
  OptionalNumberField,
  PathPickerField,
  RangeField,
  SelectField,
  TextField,
  ToggleField,
} from "./fields"
export { FilenamePatternField, IndicesField } from "./file-helpers"
export { SectionIcon } from "./SectionIcon"
export { sectionIdentity } from "./section-identity"
export { SubmitAnswer, SubmitBar, ValidationSummary } from "./submission-views"
export { blocksOf, isProgram, programSpec, sectionAnchor } from "./program-spec"
export type {
  FieldBlock,
  FieldSpec,
  ProgramSpec,
  SectionSpec,
} from "./program-spec"
export {
  applyPrefill,
  defaultsOf,
  formValuesOf,
  keptByUser,
  outputParts,
  requestOf,
  requestValueOf,
} from "./form-values"
export type { FormValues, RequestValues } from "./form-values"
export { placeIssues, shownIssues } from "./issues"
export type { FormIssue, IssueLike, PlacedIssues } from "./issues"
export { CLONE_SOURCE, searchSources, sourceLabel } from "./source-search"
export type { SourceKind, SourceOption } from "./source-search"
export { srangeCount } from "./srange"
export { useRunSubmission, useSubmission } from "./use-run-submission"
export { useProgramPrefill } from "./use-program-prefill"
export type {
  CreateOptions,
  RunSubmission,
  Submission,
  SubmissionOptions,
  SubmissionRequests,
} from "./use-run-submission"
