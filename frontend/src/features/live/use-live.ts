// The Live page's server data: the live programs (whether the page is on, its cards), the latest session
// (polled while it is open), the DM DAQ list (asked only while the form is open, never sooner than its
// answer is kept) and a session's runs cloned (the kit's prefill).
import { useMemo, useState } from "react"
import {
  ApiError,
  type LiveSession,
  type ProgramPrefill,
  useGetLiveSession,
  useListLiveDaqs,
  useListPrograms,
  usePrefillProgram,
} from "@/api"
import { isProgram, programSpec } from "@/features/program-form"
import { backoff, failureStreak, isGone } from "@/lib/polling"
import { cardSpec, daqListWait, type LiveSpecs, scanFields } from "./live-form"

/** How often an open session is read again (its counters, its arrival status). */
export const SESSION_POLL_MS = 5_000

/** Whether live processing is on: `GET /programs` lists the live programs (`[live] enabled`). */
export function useLiveEnabled(): boolean {
  const programs = useListPrograms({
    query: {
      select: (data) => data.programs.some(isProgram("wire-live")),
    },
  })
  return programs.data === true
}

/** The live programs read as the page's forms; `specs` undefined while loading or while live is off. */
export function useLiveSpecs() {
  const programs = useListPrograms()
  const specs = useMemo<LiveSpecs | undefined>(() => {
    const listed = programs.data?.programs ?? []
    const wire = listed.find(isProgram("wire-live"))
    const lauego = listed.find(isProgram("lauego-live"))
    if (!wire || !lauego) return undefined
    const wireSpec = programSpec(wire)
    return {
      scan: scanFields(wireSpec),
      reconstruction: cardSpec(wireSpec),
      indexing: cardSpec(programSpec(lauego)),
    }
  }, [programs.data])
  return { programs, specs }
}

/** The latest session, read again every 5 s while it is open (backed off after failures, stopped by a 404). */
export function useLiveSession() {
  const [streak] = useState(failureStreak)
  return useGetLiveSession({
    query: {
      refetchInterval: (query) => {
        const failures = streak(query.state)
        if (isGone(query.state.error)) return false
        if (query.state.data?.session?.open !== true) return false
        return backoff(SESSION_POLL_MS, failures)
      },
      refetchIntervalInBackground: false,
    },
  })
}

function apiError(error: unknown): ApiError | null {
  return error instanceof ApiError ? error : null
}

/**
 * The DM DAQ list, asked only while `open` (the form can take a DAQ) and never sooner than its last answer
 * is kept (`kept_seconds`, or a failure's `Retry-After`): the server shares one answer between browsers.
 */
export function useDaqList(open: boolean) {
  return useListLiveDaqs({
    query: {
      enabled: open,
      retry: false,
      staleTime: (query) =>
        daqListWait(
          query.state.data?.kept_seconds,
          apiError(query.state.error)
        ),
      refetchInterval: (query) =>
        daqListWait(
          query.state.data?.kept_seconds,
          apiError(query.state.error)
        ),
      refetchIntervalInBackground: false,
    },
  })
}

/** What a session's runs give the program cards: each run's clone (`GET /programs/{kind}/prefill`). */
export interface SessionClones {
  /** Every clone the session needs has answered (or failed). */
  settled: boolean
  reconstruction?: ProgramPrefill
  indexing?: ProgramPrefill
}

/**
 * The session's runs cloned as the kit clones a run: a wire run on the wire form, a LaueGo run on the
 * LaueGo form.
 */
export function useSessionClones(
  session: LiveSession | null | undefined
): SessionClones {
  const wire = session?.runs.find((run) => run.run?.kind === "reconstruction")
  const index = session?.runs.find((run) => run.run?.kind === "indexing")
  const options = { staleTime: Infinity, retry: false } as const
  const wireClone = usePrefillProgram(
    "wire",
    { reconstruction: String(wire?.run?.id ?? "") },
    { query: { ...options, enabled: wire !== undefined } }
  )
  const indexClone = usePrefillProgram(
    "lauego",
    { indexing: String(index?.run?.id ?? "") },
    { query: { ...options, enabled: index !== undefined } }
  )
  const pending =
    (wire !== undefined && wireClone.isPending) ||
    (index !== undefined && indexClone.isPending)
  return {
    settled: !pending,
    reconstruction: wire ? wireClone.data : undefined,
    indexing: index ? indexClone.data : undefined,
  }
}
