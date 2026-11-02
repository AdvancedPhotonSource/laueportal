import type { Page, Route } from "@playwright/test"

import type { ScanDetail, ScanPage, ScanSummary } from "@/api"
import { makeCatalog, makeMetadata, makeScanDetail } from "../src/test/scans"
import { databaseUnavailable, problem } from "./problems"

// A stand-in for GET /api/v1/scans with many scans (the e2e stack seeds only a few), answering the
// grid query protocol the way the backend does for the scans list: sort on any field, text, number
// and date filters (dates by the day that starts at the given instant), one or two conditions, and
// paging with `total`. It records each request so tests can check the queries.

const SAMPLES = [
  "Ni (111) film",
  "Si wafer",
  "Cu polycrystal",
  "GaN nanowires",
  "Ti-6Al-4V",
  "Fe-Cr alloy",
  "Al bicrystal",
  "W foil",
]
const APERTURES = ["wire", "none", "mask"]
const USERS = ["adoe", "jdoe", "mdoe", "rdoe"]
const TECHNIQUES = ["area + depth", "area", "line", "depth", "energy"]

/** `count` scans, numbered from 1000, a day apart from 2026-01-01 (their times at 06:30 UTC). */
export function makeScans(count: number): ScanSummary[] {
  const start = Date.UTC(2026, 0, 1, 6, 30)
  return Array.from({ length: count }, (_, index) => {
    const technique = TECHNIQUES[index % TECHNIQUES.length] ?? "area"
    return {
      scan_number: 1000 + index,
      sample_name: SAMPLES[index % SAMPLES.length] ?? null,
      aperture: APERTURES[index % APERTURES.length] ?? null,
      user_name: USERS[index % USERS.length] ?? null,
      n_dimensions: technique.split(" + ").length + (index % 2),
      technique,
      time: new Date(start + index * 86_400_000).toISOString(),
      notes: index % 4 === 0 ? null : `Scan ${String(1000 + index)} notes`,
    }
  })
}

type Condition = Record<string, unknown>
type Value = ScanSummary[keyof ScanSummary]

const DAY_MS = 86_400_000

function dayStart(value: unknown): number {
  return Date.parse(String(value))
}

function textMatches(value: Value, condition: Condition): boolean {
  const text = value == null ? "" : String(value).toLowerCase()
  const blank = text.trim() === ""
  const wanted =
    typeof condition.filter === "string" ? condition.filter.toLowerCase() : ""
  switch (condition.type) {
    case "contains":
      return !blank && text.includes(wanted)
    case "notContains":
      return blank || !text.includes(wanted)
    case "equals":
      return !blank && text === wanted
    case "notEqual":
      return blank || text !== wanted
    case "startsWith":
      return !blank && text.startsWith(wanted)
    case "endsWith":
      return !blank && text.endsWith(wanted)
    case "blank":
      return blank
    case "notBlank":
      return !blank
  }
  throw new Error(`unsupported text condition ${String(condition.type)}`)
}

function numberMatches(value: Value, condition: Condition): boolean {
  if (condition.type === "blank") return value == null
  if (condition.type === "notBlank") return value != null
  if (typeof value !== "number") return condition.type === "notEqual"
  const a = Number(condition.filter)
  const b = Number(condition.filterTo)
  switch (condition.type) {
    case "equals":
      return value === a
    case "notEqual":
      return value !== a
    case "lessThan":
      return value < a
    case "lessThanOrEqual":
      return value <= a
    case "greaterThan":
      return value > a
    case "greaterThanOrEqual":
      return value >= a
    case "inRange":
      return value >= Math.min(a, b) && value <= Math.max(a, b)
  }
  throw new Error(`unsupported number condition ${String(condition.type)}`)
}

function dateMatches(value: Value, condition: Condition): boolean {
  if (condition.type === "blank") return value == null
  if (condition.type === "notBlank") return value != null
  if (typeof value !== "string") return false
  const time = Date.parse(value)
  const from = dayStart(condition.dateFrom)
  switch (condition.type) {
    case "equals":
      return time >= from && time < from + DAY_MS
    case "lessThan":
      return time < from
    case "greaterThan":
      return time >= from + DAY_MS
    case "inRange": {
      const to = dayStart(condition.dateTo)
      return time >= Math.min(from, to) && time < Math.max(from, to) + DAY_MS
    }
  }
  throw new Error(`unsupported date condition ${String(condition.type)}`)
}

const FILTER_TYPES: Record<keyof ScanSummary, "text" | "number" | "date"> = {
  scan_number: "number",
  n_dimensions: "number",
  sample_name: "text",
  aperture: "text",
  user_name: "text",
  technique: "text",
  notes: "text",
  time: "date",
}

function conditionMatches(
  field: keyof ScanSummary,
  value: Value,
  condition: Condition
): boolean {
  switch (FILTER_TYPES[field]) {
    case "text":
      return textMatches(value, condition)
    case "number":
      return numberMatches(value, condition)
    case "date":
      return dateMatches(value, condition)
  }
}

function matches(scan: ScanSummary, filter: Record<string, Condition>) {
  return Object.entries(filter).every(([name, model]) => {
    const field = name as keyof ScanSummary
    const conditions = model.conditions as Condition[] | undefined
    if (!conditions) return conditionMatches(field, scan[field], model)
    const results = conditions.map((condition) =>
      conditionMatches(field, scan[field], condition)
    )
    return model.operator === "OR"
      ? results.some(Boolean)
      : results.every(Boolean)
  })
}

function compare(a: Value, b: Value): number {
  if (a == null || b == null) return a == null ? (b == null ? 0 : -1) : 1
  if (typeof a === "number" && typeof b === "number") return a - b
  return String(a).toLowerCase().localeCompare(String(b).toLowerCase())
}

/** The page the scans list answers for `url` (its offset, limit, sort and filter). */
export function answer(scans: readonly ScanSummary[], url: URL): ScanPage {
  const params = url.searchParams
  const filterText = params.get("filter")
  const filter = filterText
    ? (JSON.parse(filterText) as Record<string, Condition>)
    : {}
  const sort = (params.get("sort") ?? "-scan_number").split(",")
  const rows = scans
    .filter((scan) => matches(scan, filter))
    .sort((a, b) => {
      for (const key of [...sort, "scan_number"]) {
        const field = key.replace(/^-/, "") as keyof ScanSummary
        const order = compare(a[field], b[field])
        if (order !== 0) return key.startsWith("-") ? -order : order
      }
      return 0
    })
  const offset = Number(params.get("offset") ?? 0)
  const limit = Number(params.get("limit") ?? 100)
  return {
    rows: rows.slice(offset, offset + limit),
    total: rows.length,
    as_of: new Date().toISOString(),
  }
}

export interface ScansMock {
  /** Every request's URL, oldest first. */
  requests: URL[]
  /** The decoded `filter` of the latest request (undefined when none). */
  lastFilter: () => Record<string, Condition> | undefined
  /** Answer the next requests as the API without its database does (false: answer normally again). */
  failWith: (fail: boolean) => void
}

/** A scan's detail (GET /api/v1/scans/{id}) for a scan of the list: its catalog, if it has one. */
export function scanDetail(scan: ScanSummary): ScanDetail {
  return makeScanDetail({
    scan_number: scan.scan_number,
    technique: scan.technique,
    metadata: makeMetadata(scan.scan_number, {
      user_name: scan.user_name,
      time: scan.time,
    }),
    catalog:
      scan.aperture === null
        ? null
        : makeCatalog({
            aperture: scan.aperture,
            sample_name: scan.sample_name,
            notes: scan.notes,
          }),
  })
}

/** Answer the page's GET /api/v1/scans from `scans`, and GET /api/v1/scans/{id} for each of them. */
export async function mockScans(
  page: Page,
  scans: readonly ScanSummary[]
): Promise<ScansMock> {
  const requests: URL[] = []
  let failing = false
  await page.route("**/api/v1/scans?*", (route: Route) => {
    const url = new URL(route.request().url())
    requests.push(url)
    if (failing) return route.fulfill(databaseUnavailable())
    return route.fulfill({ json: answer(scans, url) })
  })
  await page.route("**/api/v1/scans/*", (route: Route) => {
    const id = Number(new URL(route.request().url()).pathname.split("/").at(-1))
    const scan = scans.find((candidate) => candidate.scan_number === id)
    return scan
      ? route.fulfill({ json: scanDetail(scan) })
      : route.fulfill(problem(404, "Scan not found", `No scan ${String(id)}`))
  })
  return {
    requests,
    lastFilter: () => {
      const text = requests.at(-1)?.searchParams.get("filter")
      return text ? (JSON.parse(text) as Record<string, Condition>) : undefined
    },
    failWith: (fail) => {
      failing = fail
    },
  }
}
