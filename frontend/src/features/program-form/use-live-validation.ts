// Live validation: the form's values are checked by the validate endpoint 500 ms after the last change; a
// newer change cancels the pending check and aborts the request in flight. The report is kept with the
// values it is for, so a report never shows for other values, and Submit can check the current values at
// once (`flush`) instead of waiting. One check per values at a time: a flush shares the check already
// asking for the same values, and an answer older than the last report kept is dropped. The check is the
// caller's: a create form's validate endpoint, or the Live page's session-level one.
import type { ValidationReport } from "@/api"
import { useCallback, useEffect, useRef, useState } from "react"
import type { RequestValues } from "./form-values"
import type { IssueLike } from "./issues"

/** Ask the server to validate `body` (keep the function stable: a new one is not a new check). */
export type ValidateRequest = (
  body: RequestValues,
  signal: AbortSignal
) => Promise<ValidationReport>

export const VALIDATE_DELAY_MS = 500

interface Report {
  /** The values (serialized) the issues are for. */
  key: string
  issues: IssueLike[]
  /** The check failed (the server could not be reached, or answered an error). */
  failed: boolean
}

/** A check asking the server. */
interface Check {
  key: string
  answer: Promise<IssueLike[]>
  controller: AbortController
}

export interface LiveValidation {
  /**
   * The last report's issues, kept while newer values wait for theirs (so they do not flicker away);
   * undefined before the first.
   */
  issues: IssueLike[] | undefined
  /** The current values are not checked yet (waiting or asking). */
  pending: boolean
  /** The last check failed; Submit still runs (the create validates again). */
  failed: boolean
  /** Check the current values now, unless they are already; resolves to their issues. */
  flush: () => Promise<IssueLike[]>
  /** Keep issues found otherwise (a refused create's) for the values sent, while they are on screen. */
  accept: (issues: IssueLike[], key: string) => void
}

export function useLiveValidation(
  validate: ValidateRequest,
  body: RequestValues
): LiveValidation {
  const key = JSON.stringify(body)
  const [report, setReport] = useState<Report | null>(null)
  const reportRef = useRef<Report | null>(null)
  // The values a flush checks: the ones on screen when Submit or Validate is clicked.
  const keyRef = useRef(key)
  useEffect(() => {
    keyRef.current = key
  }, [key])
  const asking = useRef<Check | null>(null)
  // The checks a flush waits for: a change of values does not abort them.
  const awaited = useRef(new WeakSet<Check>())
  // Each check's turn, in the order they started; the report kept is the latest's.
  const started = useRef(0)
  const kept = useRef(0)

  const keep = useCallback((turn: number, next: Report) => {
    if (turn < kept.current) return // a newer check answered first
    kept.current = turn
    reportRef.current = next
    setReport(next)
  }, [])

  const start = useCallback(
    (checked: string): Check => {
      const controller = new AbortController()
      const turn = ++started.current
      const answer = validate(
        JSON.parse(checked) as RequestValues,
        controller.signal
      ).then(
        (report) => {
          keep(turn, { key: checked, issues: report.issues, failed: false })
          return report.issues
        },
        (error: unknown) => {
          if (controller.signal.aborted) throw error
          keep(turn, { key: checked, issues: [], failed: true })
          return []
        }
      )
      const check: Check = { key: checked, answer, controller }
      asking.current = check
      const done = () => {
        if (asking.current === check) asking.current = null
      }
      void answer.then(done, done)
      return check
    },
    [validate, keep]
  )

  useEffect(() => {
    if (reportRef.current?.key === key) return
    const waited = awaited.current
    const timer = setTimeout(() => {
      // Checked meanwhile, or being checked (Submit's flush): nothing to ask.
      if (reportRef.current?.key === key || asking.current?.key === key) return
      start(key).answer.catch(() => undefined)
    }, VALIDATE_DELAY_MS)
    return () => {
      clearTimeout(timer)
      const check = asking.current
      if (check?.key === key && !waited.has(check)) {
        check.controller.abort()
        asking.current = null
      }
    }
  }, [key, start])

  const flush = useCallback(async () => {
    const current = keyRef.current
    const last = reportRef.current
    if (last?.key === current) return last.issues
    const check =
      asking.current?.key === current ? asking.current : start(current)
    awaited.current.add(check)
    return check.answer
  }, [start])

  const accept = useCallback(
    (issues: IssueLike[], sent: string) => {
      if (sent !== keyRef.current) return // edited meanwhile: those values get their own check
      keep(++started.current, { key: sent, issues, failed: false })
    },
    [keep]
  )

  return {
    issues: report?.issues,
    pending: report?.key !== key,
    failed: report?.failed ?? false,
    flush,
    accept,
  }
}
