import type { FullConfig } from "@playwright/test"

import type { SystemStatus } from "@/api"

// Before any spec: the stack under test must serve the e2e database. The specs on the seeded stack
// write (catalogs, notes, runs), so against the dev stack they would change dev.db; `make e2e` starts
// the e2e stack (deploy/supervisor/e2e.conf, its database set by e2e.sh).

const E2E_DATABASE = "e2e.db"

export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]?.use.baseURL ?? "http://127.0.0.1:2100"
  const url = new URL("/api/v1/system/status", baseURL)
  let response: Response
  try {
    response = await fetch(url)
  } catch (error) {
    throw new Error(
      `e2e: nothing answers at ${baseURL}; make e2e starts the e2e stack`,
      { cause: error }
    )
  }
  if (!response.ok) {
    throw new Error(
      `e2e: ${url.href} answered ${String(response.status)}; make e2e starts the e2e stack`
    )
  }
  const status = (await response.json()) as SystemStatus
  const database = status.database.path
  if (database.split("/").at(-1) !== E2E_DATABASE) {
    throw new Error(
      `e2e: the stack at ${baseURL} serves ${database}, not ${E2E_DATABASE}; make e2e starts the e2e stack`
    )
  }
}
