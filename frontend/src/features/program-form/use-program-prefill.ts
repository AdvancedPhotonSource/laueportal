// The create page's prefill (GET /programs/{kind}/prefill): one source context's values for the form.
// A prefill is read once per source (it is a starting point, not live data), and the last one that
// loaded stays while another loads or when one fails, so the form keeps its values.
import { useState } from "react"

import { type ProgramKind, usePrefillProgram } from "@/api"
import { compactPrefill, type CreatePrefill } from "@/lib/prefill"

export function useProgramPrefill(kind: ProgramKind, source: CreatePrefill) {
  const params = compactPrefill(source)
  const enabled = Object.keys(params).length > 0
  const query = usePrefillProgram(kind, params, {
    query: { enabled, staleTime: Infinity, refetchOnWindowFocus: false },
  })
  const data = enabled ? query.data : undefined
  const [last, setLast] = useState(data)
  if (data !== undefined && data !== last) setLast(data)
  return {
    /** The prefill of the source, or the last one that loaded. */
    prefill: data ?? last,
    /** The prefill of the source, once loaded. */
    current: data,
    /** The source's prefill is loading. */
    loading: enabled && query.isPending,
    error: enabled ? query.error : null,
    retry: () => void query.refetch(),
    retrying: query.isFetching,
  }
}
