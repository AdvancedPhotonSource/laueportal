// The form's validation and Submit rules together: the live issues placed on the fields, each field's shown
// once it is touched (changed or left) and every one after a Submit attempt or Validate, while the summary
// counts them all from the start. Submit stays clickable (but while a submission runs and once the run is
// created): it checks the current values first (not the last report's); errors reveal every issue, focus the
// first (`onBlocked`) and send nothing; warnings ask for a confirmation; a refused create's issues go back
// onto the fields. A submission's message shows while the values it was for are on screen. `useSubmission`
// takes the requests (validate, create, the issues' placement): a create form's program (`useRunSubmission`)
// or the Live page's session.
import type { CreatedRun, ProgramKind } from "@/api"
import { useCallback, useMemo, useRef, useState } from "react"
import type { RequestValues } from "./form-values"
import {
  type IssueLike,
  placeIssues,
  type PlacedIssues,
  shownIssues,
} from "./issues"
import type { ProgramSpec } from "./program-spec"
import { createProgramRun, validateProgram } from "./requests"
import { useLiveValidation, type ValidateRequest } from "./use-live-validation"
import {
  type CreateRequest,
  type SubmitState,
  useSubmitRun,
} from "./use-submit-run"

/** What a submission asks of the server, and where its issues go (keep the object stable). */
export interface SubmissionRequests<Created> {
  validate: ValidateRequest
  create: CreateRequest<Created>
  /** The issues placed on the form's fields (`placeIssues` for one program's form). */
  place: (issues: readonly IssueLike[]) => PlacedIssues
}

export interface Submission<Created> {
  /** Every issue of the last check (the summary's counts). */
  issues: PlacedIssues
  /** The issues shown: a touched field's (and the form's own once one is), or all once revealed. */
  shown: PlacedIssues
  /** Every issue shows (after Validate or a Submit attempt). */
  revealed: boolean
  /** The current values wait for their check. */
  checking: boolean
  /** The last check failed (the summary says so; Submit still runs). */
  checkFailed: boolean
  submitState: SubmitState<Created>
  /** Why the last submission was refused or failed, while its values are on screen (a 409's until New Submission). */
  message: string | null
  /** The last submission met a key in use (409): a new submission is offered. */
  conflict: boolean
  /** The run that key created (a 409 after a lost answer, the values then changed): shown with New Submission. */
  existing: Created | null
  canSubmit: boolean
  /** Check the current values now (Validate), not after the pause, and show every issue. */
  validateNow: () => Promise<void>
  /** Submit as the button does: check, then send, or ask to confirm the warnings. */
  requestSubmit: () => Promise<void>
  /** The warnings' confirmation is open. */
  confirming: boolean
  confirm: () => Promise<void>
  cancelConfirm: () => void
  /** Start another submission of these values: a new Idempotency-Key, the last answer cleared. */
  newSession: () => void
  idempotencyKey: string
}

/** A create form's submission (its program's run). */
export type RunSubmission = Submission<CreatedRun>

export interface SubmissionOptions {
  /** The fields the user changed or left (React Hook Form's dirty and touched fields); prefilled ones are not. */
  touched?: ReadonlySet<string>
  /** A Submit found errors: every issue is shown now; focus the first. */
  onBlocked?: (issues: PlacedIssues) => void
}

/** A submission's options with what a created answer starts (the Live page's: its session read again). */
export interface CreateOptions<Created> extends SubmissionOptions {
  /** The create answered 201 (after the state shows it). */
  onCreated?: (created: Created) => void
}

const NONE: ReadonlySet<string> = new Set()

/** A create form's submission: its program's validate and create endpoints, its issues on its fields. */
export function useRunSubmission(
  kind: ProgramKind,
  spec: ProgramSpec,
  body: RequestValues,
  options: SubmissionOptions = {}
): RunSubmission {
  const requests = useMemo<SubmissionRequests<CreatedRun>>(
    () => ({
      validate: (values, signal) => validateProgram(kind, values, signal),
      create: (values, key) => createProgramRun(kind, values, key),
      place: (issues) => placeIssues(spec, issues),
    }),
    [kind, spec]
  )
  return useSubmission(requests, body, options)
}

export function useSubmission<Created>(
  requests: SubmissionRequests<Created>,
  body: RequestValues,
  { touched = NONE, onBlocked, onCreated }: CreateOptions<Created> = {}
): Submission<Created> {
  const { place } = requests
  const validation = useLiveValidation(requests.validate, body)
  const run = useSubmitRun(requests.create)
  const [confirming, setConfirming] = useState<RequestValues | null>(null)
  const [revealed, setRevealed] = useState(false)
  // One Submit at a time, from the click on (the check before the request included).
  const flow = useRef(false)

  const issues = useMemo(
    () => place(validation.issues ?? []),
    [place, validation.issues]
  )
  const shown = useMemo(
    () =>
      revealed
        ? issues
        : shownIssues(issues, (field) =>
            field === null ? touched.size > 0 : touched.has(field)
          ),
    [issues, revealed, touched]
  )

  const { submit } = run
  const { accept, flush } = validation
  const send = useCallback(
    async (values: RequestValues) => {
      const outcome = await submit(values)
      if (outcome.kind === "refused") accept(outcome.issues, outcome.sent)
      if (outcome.kind === "created") onCreated?.(outcome.created)
    },
    [submit, accept, onCreated]
  )

  const requestSubmit = useCallback(async () => {
    if (flow.current) return
    flow.current = true
    setRevealed(true)
    try {
      const found = place(await flush())
      if (found.errors > 0) {
        onBlocked?.(found)
        return
      }
      if (found.warnings > 0) {
        setConfirming(body)
        return
      }
      await send(body)
    } finally {
      flow.current = false
    }
  }, [place, flush, body, send, onBlocked])

  const confirm = useCallback(async () => {
    if (confirming === null || flow.current) return
    flow.current = true
    setConfirming(null)
    try {
      await send(confirming)
    } finally {
      flow.current = false
    }
  }, [confirming, send])

  const state = run.state
  const conflict = state.status === "failed" && state.conflict
  const message =
    (state.status === "refused" || state.status === "failed") &&
    (conflict || state.sent === JSON.stringify(body))
      ? state.message
      : null
  return {
    issues,
    shown,
    revealed,
    checking: validation.pending,
    checkFailed: validation.failed,
    submitState: state,
    message,
    conflict,
    existing: conflict ? state.existing : null,
    canSubmit: state.status !== "submitting" && state.status !== "created",
    validateNow: async () => {
      setRevealed(true)
      await flush()
    },
    requestSubmit,
    confirming: confirming !== null,
    confirm,
    cancelConfirm: () => {
      setConfirming(null)
    },
    newSession: run.newSession,
    idempotencyKey: run.idempotencyKey,
  }
}
