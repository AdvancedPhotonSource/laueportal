import { act, useEffect, useState } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { CreatedRun, ProblemItem } from "@/api"
import { bodyOf, specOf, urlOf } from "@/test/programs"
import { cleanup, render } from "@/test/render"
import type { RequestValues } from "./form-values"
import { type RunSubmission, useRunSubmission } from "./use-run-submission"
import { newKey } from "./use-submit-run"
import { VALIDATE_DELAY_MS } from "./use-live-validation"

const spec = specOf("lauego")

const CREATED: CreatedRun = {
  run: { id: 7, kind: "indexing", label: "I7" },
  job_id: 31,
  status: "queued",
  target: "local",
  queue_id: "run_31",
  failure: null,
}

const issue = (
  field: string,
  severity: "error" | "warning",
  message = `${field} ${severity}`
): ProblemItem => ({ loc: [field], severity, code: "x", message })

/** A create's 503 detail while 16 folder reads wait (`core.api.turn`). */
const FOLDER_READS_BUSY =
  "16 folder reads are already waiting for their turn (4 at a time): try again in a moment."

function problem(status: number, detail: string, errors?: ProblemItem[]) {
  return Response.json(
    { type: "about:blank", title: "Refused", status, detail, errors },
    { status, headers: { "content-type": "application/problem+json" } }
  )
}

interface Server {
  /** The issues each validate answers. */
  issues: ProblemItem[]
  /** Validate answers once this is released (a slow check), or fails when its request is aborted. */
  hold?: Promise<void>
  aborted: RequestValues[]
  /** What each create answers. */
  create: () => Response
  validations: RequestValues[]
  creates: { body: RequestValues; key: string | null }[]
}

let server: Server

function stubServer() {
  server = {
    issues: [],
    create: () => Response.json(CREATED, { status: 201 }),
    validations: [],
    creates: [],
    aborted: [],
  }
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = urlOf(input)
    const body = bodyOf(init) as RequestValues
    if (url.pathname === "/api/v1/indexing-runs/validate") {
      server.validations.push(body)
      const { hold, issues } = server
      if (!hold) return Promise.resolve(Response.json({ issues }))
      return new Promise<Response>((resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          server.aborted.push(body)
          reject(new DOMException("Aborted", "AbortError"))
        })
        void hold.then(() => {
          resolve(Response.json({ issues }))
        })
      })
    }
    if (url.pathname === "/api/v1/indexing-runs") {
      const key = new Headers(init?.headers).get("Idempotency-Key")
      server.creates.push({ body, key })
      return Promise.resolve(server.create())
    }
    return Promise.resolve(new Response(null, { status: 404 }))
  })
}

/** What the probe rendered last: the hook's answer and the setter of the values it checks. */
const probe: {
  latest?: RunSubmission
  setBody?: (body: RequestValues) => void
} = {}

function Probe() {
  const [body, changeBody] = useState<RequestValues>({ box_size: 18 })
  const submission = useRunSubmission("lauego", spec, body)
  useEffect(() => {
    probe.latest = submission
    probe.setBody = changeBody
  }, [submission])
  return null
}

/** The hook's last answer. */
function latest(): RunSubmission {
  if (!probe.latest) throw new Error("not rendered")
  return probe.latest
}

function setBody(body: RequestValues) {
  probe.setBody?.(body)
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** Render, and let the first check answer with these issues. */
async function checked(issues: ProblemItem[] = []) {
  server.issues = issues
  await render(<Probe />)
  await advance(VALIDATE_DELAY_MS)
}

beforeEach(() => {
  vi.useFakeTimers()
  stubServer()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("useRunSubmission", () => {
  it("validates 500 ms after the last change, and only the last values", async () => {
    await render(<Probe />)
    await advance(VALIDATE_DELAY_MS - 1)
    expect(server.validations).toHaveLength(0)
    await advance(1)
    expect(server.validations).toEqual([{ box_size: 18 }])

    act(() => {
      setBody({ box_size: 1 })
    })
    await advance(300)
    act(() => {
      setBody({ box_size: 2 })
    })
    await advance(VALIDATE_DELAY_MS - 1)
    expect(server.validations).toHaveLength(1)
    await advance(1)
    expect(server.validations).toEqual([{ box_size: 18 }, { box_size: 2 }])
    expect(latest().checking).toBe(false)
  })

  it("shows a touched field's issues, every issue after Submit, which stops on errors and asks to confirm warnings", async () => {
    await checked([issue("box_size", "error"), issue("scan_number", "warning")])
    expect([latest().issues.errors, latest().issues.warnings]).toEqual([1, 1])
    expect(latest().shown.all).toEqual([])
    expect(latest().canSubmit).toBe(true)

    await act(() => latest().requestSubmit())
    expect(server.creates).toHaveLength(0)
    expect(latest().shown.all.map((shown) => shown.field)).toEqual([
      "box_size",
      "scan_number",
    ])

    server.issues = [issue("scan_number", "warning")]
    act(() => {
      setBody({ box_size: 20 })
    })
    await advance(VALIDATE_DELAY_MS)
    await act(() => latest().requestSubmit())
    expect(latest().confirming).toBe(true)
    expect(server.creates).toHaveLength(0)

    await act(() => latest().confirm())
    expect(server.creates.map((create) => create.body)).toEqual([
      { box_size: 20 },
    ])
    expect(latest().submitState.status).toBe("created")
  })

  it("checks values not yet validated when Submit is clicked, and stops on their errors", async () => {
    await checked()
    server.issues = [issue("box_size", "error")]
    act(() => {
      setBody({ box_size: 0 })
    })
    await act(() => latest().requestSubmit())
    expect(server.validations.at(-1)).toEqual({ box_size: 0 })
    expect(server.creates).toHaveLength(0)
    expect(latest().issues.errors).toBe(1)
  })

  it("sends one Idempotency-Key per session: a double click creates once, a refused create's retry reuses it", async () => {
    await checked()
    server.create = () =>
      problem(422, "1 error", [issue("input_path", "error")])
    await act(async () => {
      const first = latest().requestSubmit()
      const second = latest().requestSubmit()
      await Promise.all([first, second])
    })
    expect(server.creates).toHaveLength(1)

    server.issues = []
    server.create = () => Response.json(CREATED, { status: 201 })
    act(() => {
      setBody({ box_size: 19 })
    })
    await advance(VALIDATE_DELAY_MS)
    await act(() => latest().requestSubmit())
    await act(() => latest().requestSubmit())
    expect(server.creates).toHaveLength(2)
    const [refused, created] = server.creates
    expect(created?.key).toBe(refused?.key)
    expect(created?.key).toMatch(/^[0-9a-f-]{36}$/)

    act(() => {
      latest().newSession()
    })
    await act(() => latest().requestSubmit())
    expect(server.creates).toHaveLength(3)
    expect(server.creates[2]?.key).not.toBe(created?.key)
  })

  it("shows the answer: the created run, a refusal's issues on their fields, a conflict's or an outage's message", async () => {
    await checked()
    await act(() => latest().requestSubmit())
    expect(latest().submitState).toEqual({
      status: "created",
      created: CREATED,
    })
    expect(latest().canSubmit).toBe(false)

    act(() => {
      latest().newSession()
    })
    server.create = () =>
      problem(422, "Validation failed", [
        issue("input_path", "error", "Folder not found"),
      ])
    await act(() => latest().requestSubmit())
    expect(latest().submitState.status).toBe("refused")
    expect(latest().issues.byField.input_path?.[0]?.message).toBe(
      "Folder not found"
    )

    for (const [status, detail] of [
      [409, "The request is still being processed"],
      [503, FOLDER_READS_BUSY],
    ] as const) {
      server.issues = []
      act(() => {
        setBody({ box_size: status })
      })
      await advance(VALIDATE_DELAY_MS)
      server.create = () => problem(status, detail)
      await act(() => latest().requestSubmit())
      expect([latest().message, latest().conflict]).toEqual([
        `Refused: ${detail}`,
        status === 409,
      ])
      act(() => {
        latest().newSession()
      })
    }
  })

  it("shows a refusal's or a failure's message until the values change; a conflict's stays, with a new submission", async () => {
    await checked()
    server.create = () => problem(503, FOLDER_READS_BUSY)
    await act(() => latest().requestSubmit())
    expect(latest().message).toBe(`Refused: ${FOLDER_READS_BUSY}`)
    act(() => {
      setBody({ box_size: 19 })
    })
    expect(latest().message).toBeNull()

    await advance(VALIDATE_DELAY_MS)
    server.create = () =>
      problem(409, "This Idempotency-Key was used with another body")
    await act(() => latest().requestSubmit())
    act(() => {
      setBody({ box_size: 20 })
    })
    expect([latest().message, latest().conflict]).toEqual([
      "Refused: This Idempotency-Key was used with another body",
      true,
    ])
  })

  it("after a conflict (409), New Submission sends the form's values with a new key", async () => {
    await checked()
    server.create = () =>
      problem(
        409,
        "An earlier request with this Idempotency-Key did not finish"
      )
    await act(() => latest().requestSubmit())
    expect(latest().conflict).toBe(true)
    act(() => {
      latest().newSession()
    })
    expect([latest().message, latest().conflict]).toEqual([null, false])
    server.create = () => Response.json(CREATED, { status: 201 })
    await act(() => latest().requestSubmit())
    const [conflicted, created] = server.creates
    expect(created?.body).toEqual(conflicted?.body)
    expect(created?.key).not.toBe(conflicted?.key)
    expect(latest().submitState.status).toBe("created")
  })

  it("a 409 for other values names the run the key created, until New Submission", async () => {
    await checked()
    server.create = () =>
      Response.json(
        {
          type: "urn:laueportal:problem:key-created",
          title: "Idempotency-Key conflict",
          status: 409,
          detail:
            "This Idempotency-Key already created I7 (job 31) with other values",
          created: CREATED,
        },
        { status: 409, headers: { "content-type": "application/problem+json" } }
      )
    await act(() => latest().requestSubmit())
    expect([latest().conflict, latest().existing]).toEqual([true, CREATED])
    act(() => {
      latest().newSession()
    })
    expect(latest().existing).toBeNull()
  })

  it("aborts a check in flight when the values change, and checks the new ones 500 ms later", async () => {
    let release = () => {}
    server.hold = new Promise((resolve) => {
      release = resolve
    })
    await checked()
    expect(server.validations).toEqual([{ box_size: 18 }])
    act(() => {
      setBody({ box_size: 19 })
    })
    expect(server.aborted).toEqual([{ box_size: 18 }])
    expect(latest().checking).toBe(true)
    await advance(VALIDATE_DELAY_MS)
    expect(server.validations).toEqual([{ box_size: 18 }, { box_size: 19 }])
    await act(async () => {
      release()
      await Promise.resolve()
    })
    await advance(0)
    expect(latest().checking).toBe(false)
  })

  it("shares a check in flight with Submit, and never lets an older answer replace a newer one", async () => {
    let release = () => {}
    server.hold = new Promise((resolve) => {
      release = resolve
    })
    await checked()
    let submitted: Promise<void> = Promise.resolve()
    act(() => {
      submitted = latest().requestSubmit() // waits for the check of { box_size: 18 } in flight
    })
    expect(server.validations).toHaveLength(1)

    act(() => {
      setBody({ box_size: 19 })
    })
    expect(server.aborted).toEqual([]) // Submit waits for it
    server.hold = undefined
    await advance(VALIDATE_DELAY_MS) // { box_size: 19 } is checked and answers first
    expect(latest().checking).toBe(false)
    await act(async () => {
      release()
      await submitted
    })
    expect(server.creates.map((create) => create.body)).toEqual([
      { box_size: 18 },
    ])
    expect(latest().checking).toBe(false) // the older answer did not replace the newer
  })

  it("draws a v4 UUID key without crypto.randomUUID (plain HTTP on a host name)", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: crypto.getRandomValues.bind(crypto),
    })
    const keys = [newKey(), newKey()]
    expect(keys[0]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
    )
    expect(keys[1]).not.toBe(keys[0])
  })
})
