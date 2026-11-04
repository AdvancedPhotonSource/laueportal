import { describe, expect, it } from "vitest"

import type { RemoteReadiness } from "@/api"
import { readinessView } from "./remote-readiness"

const GOOD: RemoteReadiness = {
  enabled: true,
  state: "ok",
  stale: false,
  checking: false,
  checked_at: "2026-01-01T12:00:00Z",
  retry_after_seconds: 60,
  checks: [{ name: "crux", state: "ok", detail: "Online" }],
}

describe("ALCF readiness", () => {
  it("never leaves an old green indicator after an API failure or a stale backend result", () => {
    expect(readinessView(undefined, true)).toEqual({
      tone: "warning",
      label: "Status unknown",
    })
    expect(readinessView(GOOD, true)).toEqual({
      tone: "warning",
      label: "Status stale",
    })
    expect(readinessView({ ...GOOD, stale: true }, false)).toEqual({
      tone: "warning",
      label: "Status stale",
    })
    expect(readinessView(GOOD, false)).toEqual({
      tone: "ok",
      label: "Available",
    })
  })

  it("distinguishes a login challenge from an offline endpoint", () => {
    expect(
      readinessView(
        {
          ...GOOD,
          state: "warning",
          checks: [
            {
              name: "eagle",
              state: "login_required",
              detail: "Sign in to Globus",
            },
          ],
        },
        false
      )
    ).toEqual({ tone: "warning", label: "Login needed" })
    expect(readinessView({ ...GOOD, state: "error" }, false)).toEqual({
      tone: "error",
      label: "Unavailable",
    })
  })

  it("shows checking before any completed check, and disabled when remote execution is off", () => {
    expect(readinessView(undefined, false)).toEqual({
      tone: "unknown",
      label: "Checking",
    })
    expect(
      readinessView(
        { ...GOOD, enabled: false, state: "disabled", checks: [] },
        false
      )
    ).toEqual({ tone: "unknown", label: "Disabled" })
  })
})
