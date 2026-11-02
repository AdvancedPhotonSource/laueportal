// Submit: one Idempotency-Key per form session, so a repeated request (a double click, a retry after a lost
// answer) answers the same run and never creates a second one. A refused create (422) releases the key on
// the server, so the corrected form sends it again; a created run ends the session (Submit stays off until
// `newSession`, which draws a new key). A 409 (the key in use: by another body after a lost answer, by a
// request that did not finish, or by one still running) offers `newSession` too, the form's values kept;
// Submit stays for the "try again" case. A 409 for other values after the key's run was created (its answer
// lost, the form then edited) is of type `KEY_CREATED` and carries that run (`KeyConflict.created`): it
// shows beside New Submission. The creation is the caller's: a create form's run (`CreatedRun`), or the Live
// page's session (`CreatedLiveRuns`).
import { ApiError, type CreatedRun } from "@/api"
import { describeError } from "@/lib/errors"
import { useCallback, useRef, useState } from "react"
import type { RequestValues } from "./form-values"
import type { IssueLike } from "./issues"

/** Send the create request with its Idempotency-Key (keep the function stable). */
export type CreateRequest<Created> = (
  body: RequestValues,
  idempotencyKey: string
) => Promise<Created>

export type SubmitState<Created = CreatedRun> =
  | { status: "idle" }
  | { status: "submitting" }
  /** 201: the run exists; `created.failure` says why it could not be queued, if it could not. */
  | { status: "created"; created: Created }
  /** 422: the issues are back on the fields; `message` is the problem's; `sent` the values (serialized). */
  | { status: "refused"; message: string; sent: string }
  /**
   * 409, 503 and any other failure: the message to show; nothing was created. `conflict` (409): the key is
   * in use, so a new submission (a new key) is offered; `existing`: the run the key created, if it did.
   */
  | {
      status: "failed"
      message: string
      sent: string
      conflict: boolean
      existing: Created | null
    }

/** What a submit gave: the refused create's issues for the form, or nothing more to do. */
export type SubmitOutcome<Created = CreatedRun> =
  | { kind: "ignored" }
  | { kind: "created"; created: Created }
  | { kind: "refused"; issues: IssueLike[]; sent: string }
  | { kind: "failed" }

/**
 * A new random (v4) UUID. `crypto.randomUUID` exists only in a secure context (HTTPS or localhost): a portal
 * served over plain HTTP on a host name builds it from `crypto.getRandomValues`, which works in any context.
 */
export function newKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40 // version 4
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80 // the RFC 4122 variant
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"))
  return [
    hex.slice(0, 4),
    hex.slice(4, 6),
    hex.slice(6, 8),
    hex.slice(8, 10),
    hex.slice(10),
  ]
    .map((part) => part.join(""))
    .join("-")
}

/** The problem type of a 409 for other values after the key's run was created: it carries that run. */
export const KEY_CREATED = "urn:laueportal:problem:key-created"

/**
 * What a 409's key created (`KeyConflict.created`, `LiveRunsConflict.created`; type `KEY_CREATED`), when it
 * did; null otherwise.
 */
function existingOf(error: unknown): unknown {
  if (!(error instanceof ApiError) || error.problem?.type !== KEY_CREATED) {
    return null
  }
  return (error.problem as { created?: unknown }).created ?? null
}

function messageOf(error: unknown): string {
  const { title, detail } = describeError(error)
  return detail ? `${title}: ${detail}` : title
}

export function useSubmitRun<Created>(create: CreateRequest<Created>) {
  const [key, setKey] = useState(newKey)
  const [state, setState] = useState<SubmitState<Created>>({ status: "idle" })
  // Set at once (not on the next render), so a second click in the same tick is ignored too.
  const busy = useRef(false)
  const done = useRef(false)

  const submit = useCallback(
    async (body: RequestValues): Promise<SubmitOutcome<Created>> => {
      if (busy.current || done.current) return { kind: "ignored" }
      busy.current = true
      const sent = JSON.stringify(body)
      setState({ status: "submitting" })
      try {
        const created = await create(body, key)
        done.current = true
        setState({ status: "created", created })
        return { kind: "created", created }
      } catch (error) {
        if (error instanceof ApiError && error.status === 422) {
          const issues = error.problem?.errors ?? []
          setState({ status: "refused", message: messageOf(error), sent })
          return { kind: "refused", issues, sent }
        }
        const conflict = error instanceof ApiError && error.status === 409
        setState({
          status: "failed",
          message: messageOf(error),
          sent,
          conflict,
          existing: existingOf(error) as Created | null,
        })
        return { kind: "failed" }
      } finally {
        busy.current = false
      }
    },
    [create, key]
  )

  /** Start another submission of this form: a new key, the last answer cleared. */
  const newSession = useCallback(() => {
    done.current = false
    setKey(newKey())
    setState({ status: "idle" })
  }, [])

  return { idempotencyKey: key, state, submit, newSession }
}
