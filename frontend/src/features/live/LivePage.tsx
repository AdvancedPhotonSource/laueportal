// The Live page: one live session at a time from the form kit. The header strip shows the latest session
// (its state, its runs' counters and arrival status, their links) with Validate, Start and Stop; once a session a
// user stopped has ended it shows none. The Scan card and the program cards make the session's request. While a
// session is open the form is read-only and shows it; once it has ended, the form is its clone, ready for the next
// Start (no Resume).
import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  type Control,
  useForm,
  useFormState,
  useWatch,
  type UseFormReturn,
} from "react-hook-form"
import {
  cancelJob,
  stopJobNow,
  type CreatedLiveRuns,
  createLiveRuns,
  getGetLiveSessionQueryKey,
  type LiveSession,
  validateLiveRuns,
} from "@/api"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { EmptyState } from "@/components/EmptyState"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { UnsavedChangesGuard } from "@/components/UnsavedChangesGuard"
import { toastReport } from "@/features/jobs/report-toast"
import {
  jobRef,
  LIVE_STOP_EXPLANATION,
  LIVE_STOP_NOW_EXPLANATION,
  runEach,
  stopReport,
} from "@/features/jobs/run-monitor"
import {
  applyPrefill,
  type FormValues,
  formValuesOf,
  type PlacedIssues,
  type SubmissionRequests,
  useSubmission,
  ValidationSummary,
} from "@/features/program-form"
import {
  blankScan,
  cardValuesOf,
  conflictJobs,
  followedOutput,
  type LiveSpecs,
  type LiveValues,
  namedSessionEnded,
  openRuns,
  outputFollows,
  type Part,
  mirroredOutput,
  placeSessionIssues,
  qualified,
  scanValuesOf,
  sessionRequest,
  sessionState,
  sessionSwitches,
  shownSession,
  sourceRun,
  splitQualified,
  summaryTitle,
  type Switches,
  waitingReason,
} from "./live-form"
import { LiveHeader } from "./LiveHeader"
import { ProgramCard, ScanCard } from "./LiveCards"
import {
  useDaqList,
  useLiveSession,
  useLiveSpecs,
  useSessionClones,
} from "./use-live"

const PARTS: readonly Part[] = ["scan", "reconstruction", "indexing"]
const NO_IDS: ReadonlySet<string> = new Set()

export function LivePage() {
  const { programs, specs } = useLiveSpecs()
  let body
  // A failed read again of the programs keeps the last known (the navbar's Live does too).
  if (programs.isPending) body = <LoadingState variant="cards" count={3} />
  else if (programs.isError && !programs.data) {
    body = (
      <ErrorState
        error={programs.error}
        onRetry={() => void programs.refetch()}
        retrying={programs.isFetching}
      />
    )
  } else if (!specs) body = <EmptyState title="Live processing is off" />
  else body = <LiveForm specs={specs} />
  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Live</h1>
      {body}
    </section>
  )
}

/** The fields a form's user changed or left (React Hook Form's dirty and touched ones), by qualified name. */
function useTouched(part: Part, control: Control<FormValues>): string[] {
  const { dirtyFields, touchedFields } = useFormState({ control })
  // React Hook Form updates these objects in place: the names, as one text, say when they change.
  return [
    ...new Set(
      [dirtyFields, touchedFields].flatMap((fields) =>
        Object.entries(fields).flatMap(([name, on]) =>
          on ? [qualified(part, name)] : []
        )
      )
    ),
  ].sort()
}

function defaultsOf(specs: LiveSpecs): LiveValues {
  return {
    scan: blankScan(specs.scan),
    reconstruction: formValuesOf(specs.reconstruction),
    indexing: formValuesOf(specs.indexing),
  }
}

/** A session's values for the forms: its Scan card and its runs' clones (a part without one keeps `kept`). */
function sessionValues(
  specs: LiveSpecs,
  session: LiveSession,
  clones: ReturnType<typeof useSessionClones>,
  kept: LiveValues
): LiveValues {
  return {
    scan: scanValuesOf(specs.scan, session),
    reconstruction: clones.reconstruction
      ? cardValuesOf(specs.reconstruction, clones.reconstruction.values)
      : kept.reconstruction,
    indexing: clones.indexing
      ? cardValuesOf(specs.indexing, clones.indexing.values)
      : kept.indexing,
  }
}

function LiveForm({ specs }: { specs: LiveSpecs }) {
  const queryClient = useQueryClient()
  const defaults = useMemo(() => defaultsOf(specs), [specs])
  const forms: Record<Part, UseFormReturn<FormValues>> = {
    scan: useForm<FormValues>({ defaultValues: defaults.scan }),
    reconstruction: useForm<FormValues>({
      defaultValues: defaults.reconstruction,
    }),
    indexing: useForm<FormValues>({ defaultValues: defaults.indexing }),
  }
  const [switches, setSwitches] = useState<Switches>({
    reconstruction: true,
    indexing: true,
  })

  const sessionQuery = useLiveSession()
  const session = sessionQuery.data?.session
  // Once read, the page goes on with the last answer while a read again fails (its header says so).
  const ready = sessionQuery.data !== undefined
  const open = session?.open === true
  const clones = useSessionClones(session)
  const daqs = useDaqList(ready && !open)

  // Output Folder follows Folder (typed, browsed or filled from a DAQ: `followedOutput`) until the user edits it.
  // A session's values applied count as an edit only when its Output Folder is not the one its Folder gives
  // (`outputFollows`): a hand-set one stays, a mirrored one keeps following. From data folder follows again.
  const outputEdited = useRef(false)
  const scanForm = forms.scan
  const configuredOutput = String(defaults.scan.output_folder ?? "")
  const follow = useCallback(
    (folder: string) => {
      if (outputEdited.current) return
      const output = followedOutput(folder, configuredOutput)
      if (output !== scanForm.getValues("output_folder")) {
        scanForm.setValue("output_folder", output, { shouldDirty: true })
      }
    },
    [scanForm, configuredOutput]
  )
  useEffect(() => {
    // A user's edit is a "change"; the page's own setValue and reset are not.
    const subscription = scanForm.watch((values, { name, type }) => {
      if (type !== "change") return
      if (name === "output_folder") outputEdited.current = true
      else if (name === "folder") follow(String(values.folder ?? ""))
    })
    return () => {
      subscription.unsubscribe()
    }
  }, [scanForm, follow])
  const fromDataFolder = () => {
    const output = mirroredOutput(String(scanForm.getValues("folder")))
    if (output === undefined) return
    outputEdited.current = false
    scanForm.setValue("output_folder", output, { shouldDirty: true })
  }

  // The session's values: shown read-only while it is open (but this page's own Start, whose values are on
  // screen), applied as the kit's clone once it has ended (what the user typed beyond it stays: keptByUser;
  // for this page's own session, what was typed went into it, so it is the form's new baseline, not an edit).
  const own = useRef<ReadonlySet<number>>(new Set())
  const applied = useRef<LiveValues>(defaults)
  const identity = session
    ? `${String(session.runs[0]?.job_id)}:${String(session.open)}`
    : null
  const shownIdentity = useRef<string | null>(null)
  useEffect(() => {
    if (!session || identity === null || !clones.settled) return
    if (shownIdentity.current === identity) return
    shownIdentity.current = identity
    setSwitches(sessionSwitches(session))
    const mine = own.current.has(session.runs[0]?.job_id ?? -1)
    if (session.open && mine) return
    const next = sessionValues(specs, session, clones, applied.current)
    for (const part of PARTS) {
      const form = forms[part]
      if (session.open) {
        form.reset(next[part])
      } else {
        const { values, kept } = applyPrefill(
          defaults[part],
          applied.current[part],
          next[part],
          form.getValues(),
          { ids: NO_IDS }
        )
        if (mine) {
          form.reset({ ...values, ...kept })
        } else {
          form.reset(values)
          for (const [name, value] of Object.entries(kept)) {
            form.setValue(name, value, { shouldDirty: true })
          }
        }
      }
      applied.current = { ...applied.current, [part]: next[part] }
    }
    const scan = forms.scan.getValues()
    outputEdited.current = !outputFollows(
      String(scan.folder),
      String(scan.output_folder),
      String(defaults.scan.output_folder ?? "")
    )
    // `forms` holds the same three forms on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, identity, clones, specs, defaults])

  const watched: LiveValues = {
    scan: { ...defaults.scan, ...useWatch({ control: forms.scan.control }) },
    reconstruction: {
      ...defaults.reconstruction,
      ...useWatch({ control: forms.reconstruction.control }),
    },
    indexing: {
      ...defaults.indexing,
      ...useWatch({ control: forms.indexing.control }),
    },
  } as LiveValues
  const bodyKey = JSON.stringify([watched, switches])
  // The request changes only when a value or a switch does (its text says when).
  const body = useMemo(
    () => sessionRequest(specs, watched, switches),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bodyKey, specs]
  )

  const touchedNames = [
    ...useTouched("scan", forms.scan.control),
    ...useTouched("reconstruction", forms.reconstruction.control),
    ...useTouched("indexing", forms.indexing.control),
  ].join("\n")
  const touched = useMemo(
    () => new Set(touchedNames ? touchedNames.split("\n") : []),
    [touchedNames]
  )
  const focus = useCallback(
    (name: string) => {
      const at = splitQualified(name)
      if (at) forms[at.part].setFocus(at.field)
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  )
  const onBlocked = useCallback(
    (found: PlacedIssues) => {
      const first = found.all.find(
        (issue) => issue.severity === "error" && issue.field !== null
      )
      if (first?.field) focus(first.field)
    },
    [focus]
  )
  // The session a Start's 409 names (the one open, or the one its key created), by its jobs.
  const [named, setNamed] = useState<readonly number[]>([])
  const requests = useMemo<SubmissionRequests<CreatedLiveRuns>>(
    () => ({
      validate: (values, signal) => validateLiveRuns(values, { signal }),
      create: async (values, key) => {
        try {
          return await createLiveRuns(values, {
            headers: { "Idempotency-Key": key },
          })
        } catch (error) {
          setNamed(conflictJobs(error))
          throw error
        }
      },
      place: (issues) => placeSessionIssues(specs, issues),
    }),
    [specs]
  )
  // A Start's answer: its runs are this page's own (their values are on screen), the session is read again,
  // and the next Start is another submission (a new key).
  const [started, setStarted] = useState<CreatedLiveRuns | null>(null)
  const renew = useRef<() => void>(() => undefined)
  const onCreated = useCallback(
    (created: CreatedLiveRuns) => {
      own.current = new Set(created.runs.map((run) => run.job_id))
      setStarted(created)
      renew.current()
      void queryClient.invalidateQueries({
        queryKey: getGetLiveSessionQueryKey(),
      })
    },
    [queryClient]
  )
  const submission = useSubmission(requests, body, {
    touched,
    onBlocked,
    onCreated,
  })
  useEffect(() => {
    renew.current = submission.newSession
  }, [submission.newSession])
  // An ended session, once applied to the form (the effect above, before this one): its values are checked again
  // (`revalidate`), not the report from before Start, since its runs are now in the Output Folder.
  const endedIdentity =
    session && !session.open && clones.settled ? identity : null
  const { revalidate } = submission
  useEffect(() => {
    if (endedIdentity !== null) revalidate()
  }, [endedIdentity, revalidate])
  // A refusal of a session open elsewhere (409): read the session again, which shows it. Its message stays
  // while that session is open; once it has ended, the next Start is a new submission (a new key: the one
  // refused may belong to that session).
  const { submitState, newSession } = submission
  const conflict = submitState.status === "failed" && submitState.conflict
  useEffect(() => {
    if (conflict) {
      void queryClient.invalidateQueries({
        queryKey: getGetLiveSessionQueryKey(),
      })
    }
  }, [conflict, queryClient])
  const conflictOver = conflict && namedSessionEnded(named, session)
  useEffect(() => {
    if (conflictOver) newSession()
  }, [conflictOver, newSession])
  const shownStart =
    started &&
    !started.runs.some((run) => session?.runs[0]?.job_id === run.job_id)
      ? started
      : null
  const awaiting =
    shownStart !== null && shownStart.runs.every((run) => run.failure === null)

  const [stopping, setStopping] = useState(false)
  const [stopNow, setStopNow] = useState(false)
  const stop = async () => {
    const run = session ? sourceRun(session) : undefined
    if (!run) return
    const outcomes = await runEach(
      [jobRef({ ...run, target: "local", live: true })],
      (id) => cancelJob(id)
    )
    toastReport(stopReport(outcomes, "Stop"))
    await queryClient.invalidateQueries({
      queryKey: getGetLiveSessionQueryKey(),
    })
  }
  // Stop now: every open run of the Stopping session; one already stopping is killed.
  const stopAllNow = async () => {
    const runs = session ? openRuns(session) : []
    if (runs.length === 0) return
    const outcomes = await runEach(
      runs.map((run) => jobRef({ ...run, target: "local", live: true })),
      (id) => stopJobNow(id)
    )
    toastReport(stopReport(outcomes, "Stop now"))
    await queryClient.invalidateQueries({
      queryKey: getGetLiveSessionQueryKey(),
    })
  }

  const issuesOf = (part: Part) => (name: string) =>
    submission.shown.byField[qualified(part, name)]
  const dirty =
    forms.scan.formState.isDirty ||
    forms.reconstruction.formState.isDirty ||
    forms.indexing.formState.isDirty
  const busy = submission.submitState.status === "submitting"
  // The header's session: none once a session a user stopped has ended (the form still filled from it above).
  const shown = shownSession(session)
  const state = sessionState(shown)
  const count = submission.issues.warnings
  const warnings = `${String(count)} ${count === 1 ? "warning" : "warnings"}`

  if (sessionQuery.isError && !sessionQuery.data) {
    return (
      <ErrorState
        error={sessionQuery.error}
        onRetry={() => void sessionQuery.refetch()}
        retrying={sessionQuery.isFetching}
      />
    )
  }
  return (
    <>
      <LiveHeader
        session={shown}
        state={state}
        started={shownStart}
        message={conflictOver ? null : submission.message}
        readError={ready && sessionQuery.isError ? sessionQuery.error : null}
        onReadAgain={() => void sessionQuery.refetch()}
        reading={sessionQuery.isFetching}
        canValidate={ready && !open}
        canStart={ready && !open && !busy && !awaiting}
        starting={busy || awaiting}
        canStop={open}
        stopNow={state === "stopping"}
        waiting={waitingReason(shown)}
        onValidate={() => void submission.validateNow()}
        onStart={() => void submission.requestSubmit()}
        onStop={() => {
          if (state === "stopping") setStopNow(true)
          else setStopping(true)
        }}
      />
      {!open && (
        <ValidationSummary
          issues={submission.issues}
          shown={submission.shown}
          checking={submission.checking}
          failed={submission.checkFailed}
          titleOf={(name) => summaryTitle(specs, name)}
          onFocusField={focus}
        />
      )}
      <fieldset
        disabled={open || !ready}
        aria-label="Live session form"
        className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0"
      >
        <ScanCard
          fields={specs.scan}
          control={forms.scan.control}
          setValue={forms.scan.setValue}
          issuesOf={issuesOf("scan")}
          daqs={daqs.data?.daqs ?? []}
          daqError={open ? null : daqs.error}
          readOnly={open || !ready}
          onFolderFilled={follow}
          onFromDataFolder={fromDataFolder}
        />
        <ProgramCard
          part="reconstruction"
          spec={specs.reconstruction}
          on={switches.reconstruction}
          onChange={(on) => {
            setSwitches((now) => ({ ...now, reconstruction: on }))
          }}
          follower={false}
          control={forms.reconstruction.control}
          issuesOf={issuesOf("reconstruction")}
          readOnly={open || !ready}
        />
        <ProgramCard
          part="indexing"
          spec={specs.indexing}
          on={switches.indexing}
          onChange={(on) => {
            setSwitches((now) => ({ ...now, indexing: on }))
          }}
          follower={switches.reconstruction}
          control={forms.indexing.control}
          issuesOf={issuesOf("indexing")}
          readOnly={open || !ready}
        />
      </fieldset>
      <UnsavedChangesGuard when={dirty && !open && !busy && !awaiting} />
      <ConfirmDialog
        open={submission.confirming}
        onOpenChange={(next) => {
          if (!next) submission.cancelConfirm()
        }}
        title="Start with warnings?"
        description={`The session starts despite ${warnings}.`}
        confirmLabel="Start"
        onConfirm={submission.confirm}
      >
        <ul className="flex list-disc flex-col gap-1 pl-5 text-sm text-warning">
          {submission.issues.all
            .filter((issue) => issue.severity === "warning")
            .map((issue, index) => {
              const title = summaryTitle(specs, issue.field)
              return (
                <li key={index}>
                  {title ? `${title}: ${issue.message}` : issue.message}
                </li>
              )
            })}
        </ul>
      </ConfirmDialog>
      <ConfirmDialog
        open={stopping}
        onOpenChange={setStopping}
        title="Stop the live session?"
        description={LIVE_STOP_EXPLANATION}
        confirmLabel="Stop"
        destructive
        onConfirm={stop}
      />
      <ConfirmDialog
        open={stopNow}
        onOpenChange={setStopNow}
        title="Stop the live session now?"
        description={LIVE_STOP_NOW_EXPLANATION}
        confirmLabel="Stop now"
        destructive
        onConfirm={stopAllNow}
      />
    </>
  )
}
