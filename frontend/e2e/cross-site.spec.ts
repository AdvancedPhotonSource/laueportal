import { createServer, type Server } from "node:http"

import { expect, test, type Response } from "@playwright/test"

import type { JobDetail } from "@/api"
import {
  checkStackDatabase,
  lastJobId,
  queueSlowRuns,
  stopJobsAfter,
} from "./stack-tools"

// A page on another origin cannot stop or reorder runs. Without the header check, an auto-submitted form could
// stop a running job and a `no-cors` fetch move a queued one to the front: neither needs a CORS preflight. Every
// request that changes data needs X-Laue-Portal: 1, which such a page cannot send (a custom header needs a
// preflight, which the API never grants). On the seeded stack (`make e2e`): two long demo runs, one running and
// one queued, so that a request that got through would show; the foreign pages come from a server of the test on
// 127.0.0.1, on a free experiments port (2103-2109), a same-site origin of the API's that Chrome lets reach it.

interface Targets {
  api: string
  running: string
  queued: string
}

const PAGES = new Map<string, (to: Targets) => string>([
  [
    "/form.html",
    ({ api, running }) =>
      `<form id="f" method="POST" action="${api}/jobs/${running}/cancel"></form><script>document.getElementById("f").submit()</script>`,
  ],
  [
    // A no-cors fetch: the browser drops the custom header the page tries to add.
    "/fetch-cancel.html",
    ({ api, queued }) =>
      `<script>fetch("${api}/jobs/${queued}/cancel", {method: "POST", mode: "no-cors", headers: {"X-Laue-Portal": "1"}}).then(() => document.title = "sent", (e) => document.title = "error " + e)</script>`,
  ],
  [
    "/fetch-move.html",
    ({ api, queued }) =>
      `<script>fetch("${api}/jobs/${queued}/move-to-front", {method: "POST", mode: "no-cors"}).then(() => document.title = "sent", (e) => document.title = "error " + e)</script>`,
  ],
  [
    // A CORS fetch with the header needs a preflight, which the API refuses: the browser never sends the POST.
    "/cors.html",
    ({ api, queued }) =>
      `<script>fetch("${api}/jobs/${queued}/cancel", {method: "POST", headers: {"X-Laue-Portal": "1"}}).then(() => document.title = "sent", () => document.title = "blocked")</script>`,
  ],
])

let server: Server | undefined
let foreign = ""
let targets: Targets = { api: "", running: "", queued: "" }

function listen(port: number): Promise<Server> {
  return new Promise((resolve, reject) => {
    const candidate = createServer((request, response) => {
      const page = PAGES.get(request.url ?? "")
      response.writeHead(page ? 200 : 404, {
        "content-type": "text/html",
        connection: "close",
      })
      response.end(
        page ? `<!doctype html><title>innocent</title>${page(targets)}` : ""
      )
    })
    candidate.once("error", reject)
    candidate.listen(port, "127.0.0.1", () => {
      resolve(candidate)
    })
  })
}

test.beforeAll(async () => {
  for (let port = 2103; port <= 2109 && !server; port++) {
    try {
      server = await listen(port)
      foreign = `http://127.0.0.1:${String(port)}`
    } catch {
      // taken: the next experiments port
    }
  }
  if (!server) throw new Error("no free port in 2103-2109 for the foreign page")
})

test.afterAll(async () => {
  const stopping = server
  if (!stopping) return
  stopping.closeAllConnections()
  await new Promise<void>((resolve) => {
    stopping.close(() => {
      resolve()
    })
  })
  server = undefined
})

// The newest job before the test queued its runs: those after it are stopped, also after a failure.
let queuedAfter: number | undefined

test.afterEach(async ({ request }) => {
  if (queuedAfter === undefined) return
  await stopJobsAfter(request, queuedAfter)
  queuedAfter = undefined
})

test("a page on another origin can neither stop a run nor move one to the front", async ({
  page,
  request,
  baseURL,
}) => {
  test.setTimeout(120_000)
  await checkStackDatabase(request)
  queuedAfter = await lastJobId(request)
  // The worker starts the first run at once; the second waits in the queue.
  const [first, second] = queueSlowRuns(2, 2000)
  if (!first || !second) throw new Error("two runs were queued")
  const api = `${baseURL ?? ""}/api/v1`
  targets = {
    api,
    running: String(first.job_id),
    queued: String(second.job_id),
  }
  const state = async (jobId: string) => {
    const detail = (await (
      await request.get(`/api/v1/jobs/${jobId}`)
    ).json()) as JobDetail
    return `${detail.status}${detail.progress.stop_requested ? ", stop requested" : ""}`
  }
  await expect
    .poll(() => state(targets.running), { timeout: 30_000 })
    .toBe("running")
  expect(await state(targets.queued)).toBe("queued")

  const answers: string[] = []
  page.on("response", (response: Response) => {
    if (response.url().startsWith(api)) {
      const method = response.request().method()
      const path = response.url().slice(api.length)
      answers.push(`${method} ${path} ${String(response.status())}`)
    }
  })

  // The browser navigates to the API's answer of the form.
  const formAnswer = page.waitForResponse(
    `${api}/jobs/${targets.running}/cancel`
  )
  await page.goto(`${foreign}/form.html`)
  expect((await formAnswer).status()).toBe(403)
  await expect(page.locator("body")).toContainText("Cross-site request refused")

  for (const [name, action] of [
    ["fetch-cancel", "cancel"],
    ["fetch-move", "move-to-front"],
  ] as const) {
    const answer = page.waitForResponse(
      `${api}/jobs/${targets.queued}/${action}`
    )
    await page.goto(`${foreign}/${name}.html`)
    expect((await answer).status()).toBe(403)
    await expect(page).toHaveTitle("sent") // an opaque answer: the page cannot even read the refusal
  }

  await page.goto(`${foreign}/cors.html`)
  await expect(page).toHaveTitle("blocked")

  // The CORS POST never left the browser: the API refused its preflight (the browser's own request, which
  // Playwright does not report; tests/test_api_sweep.py checks the API's answer to it).
  expect(answers).toEqual([
    `POST /jobs/${targets.running}/cancel 403`,
    `POST /jobs/${targets.queued}/cancel 403`,
    `POST /jobs/${targets.queued}/move-to-front 403`,
  ])
  // Either run would have changed had a request got through.
  expect(await state(targets.running)).toBe("running")
  expect(await state(targets.queued)).toBe("queued")
})
