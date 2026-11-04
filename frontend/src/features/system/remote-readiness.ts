import type { RemoteCheck, RemoteReadiness } from "@/api"
import type { Tone } from "./system-status"

export const REMOTE_POLL_MS = 300_000

export const CHECK_LABELS: Record<RemoteCheck["name"], string> = {
  dm: "DM workflows",
  flows: "Globus Flows",
  compute: "Globus Compute",
  aps: "APS storage",
  eagle: "Eagle storage",
  crux: "Crux endpoint",
}

export const CHECK_STATES: Record<RemoteCheck["state"], string> = {
  ok: "Available",
  login_required: "Login needed",
  denied: "Access denied",
  unavailable: "Unavailable",
  unknown: "Unknown",
  disabled: "Disabled",
}

export function checkTone(state: RemoteCheck["state"]): Tone {
  switch (state) {
    case "ok":
      return "ok"
    case "unavailable":
      return "error"
    case "login_required":
    case "denied":
      return "warning"
    default:
      return "unknown"
  }
}

export function readinessView(
  data: RemoteReadiness | undefined,
  failed: boolean
): { tone: Tone; label: string } {
  if (failed || data?.stale)
    return {
      tone: "warning",
      label: data?.checked_at ? "Status stale" : "Status unknown",
    }
  if (!data || (!data.checked_at && data.checking))
    return { tone: "unknown", label: "Checking" }
  if (!data.enabled) return { tone: "unknown", label: "Disabled" }
  if (data.state === "error") return { tone: "error", label: "Unavailable" }
  if (data.checks.some((c) => c.state === "login_required"))
    return { tone: "warning", label: "Login needed" }
  if (data.state === "warning")
    return { tone: "warning", label: "Needs attention" }
  if (data.state === "ok") return { tone: "ok", label: "Available" }
  return { tone: "unknown", label: "Unknown" }
}
