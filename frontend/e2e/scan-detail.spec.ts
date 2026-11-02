import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test"

import type {
  FileRoots,
  IndexingRunPage,
  ReconstructionRunPage,
  ReconstructionRunSummary,
  ScanCatalogUpdate,
  ScanDetail,
} from "@/api"
import { makeScanDetail } from "../src/test/scans"
import { prefillAsked } from "./prefill"
import { indexingRun, mockRunList, reconstructionRun } from "./runs-api"

// The scan page. On the seeded stack (`make e2e`, nothing mocked): scan 990001's catalog edited and
// saved with the path picker, a folder outside the roots, a note, the tab in the URL, New Recon and
// New Index, an unknown scan. With the scan answered by the test (and its run lists from
// e2e/runs-api.ts): a depth scan with reconstructions, the unsaved-note prompt, a scan without a
// catalog, 900 px.
// Both projects run against the same database, so the stack's tests read its state first.

const DEMO = 990001

async function getScan(request: APIRequestContext, scan: number) {
  const response = await request.get(`/api/v1/scans/${String(scan)}`)
  expect(response.ok()).toBe(true)
  return (await response.json()) as ScanDetail
}

/** Matches a URL ending in `pathAndSearch` exactly. */
function endsWith(pathAndSearch: string) {
  return new RegExp(`${pathAndSearch.replace(/[?.]/g, "\\$&")}$`)
}

const tabPanel = (page: Page, name: string) =>
  page.getByRole("tabpanel", { name })

/** A definition list's value for a term. */
function definition(root: Locator, term: string) {
  return root.locator(`div:has(> dt:text-is("${term}")) > dd`).first()
}

function runCard(page: Page, title: string) {
  return page.getByRole("region", { name: title })
}

async function chooseAperture(page: Page, catalog: Locator, label: string) {
  await catalog.getByLabel("Aperture").click()
  await page.getByRole("option", { name: label, exact: true }).click()
  await expect(catalog.locator('[data-slot="select-value"]')).toHaveText(label)
}

test.describe("on the seeded stack", () => {
  test("edits and saves the catalog with the path picker; a folder outside the roots is refused", async ({
    page,
    request,
  }, testInfo) => {
    const roots = (await (
      await request.get("/api/v1/files/roots")
    ).json()) as FileRoots
    const samples = roots.roots.find((root) => root.name === "samples")
    if (!samples) throw new Error("the e2e stack has no samples root")

    await page.goto(`/scans/${String(DEMO)}?tab=catalog`)
    await expect(page).toHaveTitle(`Scan ${String(DEMO)} · 3DMN Portal`)
    const catalog = tabPanel(page, "Catalog")
    const folder = catalog.getByLabel("Files Path")
    // The demo's aperture ("synthetic") is none the form offers: one is chosen to save.
    await chooseAperture(page, catalog, "Coded Aperture")

    // The server checks the folder against the roots: its reason shows at the field.
    await folder.fill("/tmp")
    await catalog.getByRole("button", { name: "Save" }).click()
    const error = catalog.getByText(/^\/tmp is outside the allowed roots/)
    await expect(error).toBeVisible()
    await expect(folder).toHaveAttribute("aria-invalid", "true")
    await expect(folder).toBeFocused()

    // The picker: /tmp cannot be opened, so it starts at the roots.
    await catalog.getByRole("button", { name: "Browse" }).click()
    const dialog = page.getByRole("dialog", { name: "Choose the data folder" })
    await expect(dialog.getByText("/tmp cannot be opened")).toBeVisible()
    const list = dialog.getByRole("listbox")
    await expect(list).toBeFocused()
    await dialog.getByRole("option", { name: /^samples/ }).click()
    await expect(list).toHaveAccessibleName(`Contents of ${samples.path}`)
    // A click opens a folder: registered is a folder of the samples (the demo's inputs; not R1's input folder,
    // which the wire form's spec expects to differ from the catalog's).
    await dialog.getByRole("option", { name: /^registered/ }).click()
    await expect(list).toHaveAccessibleName(
      `Contents of ${samples.path}/registered`
    )
    await expect(
      dialog.getByRole("option", { name: /^fine_wire_1\.h5/ })
    ).toHaveAttribute("aria-disabled", "true")
    // Backspace goes up and highlights the folder it left.
    await page.keyboard.press("Backspace")
    await expect(list).toHaveAccessibleName(`Contents of ${samples.path}`)
    const highlighted = dialog.getByRole("option", {
      name: /^registered/,
      selected: true,
    })
    await expect(highlighted).toBeVisible()
    // The keyboard's row is outlined while the list has the focus.
    await expect(list).toBeFocused()
    expect(
      await highlighted.evaluate((element) => {
        const style = getComputedStyle(element)
        return `${style.outlineStyle} ${style.outlineWidth}`
      })
    ).toBe("solid 2px")
    // The keyboard opens a folder too: Enter opens the highlighted folder, and Backspace leaves it again.
    await page.keyboard.press("Enter")
    await expect(list).toHaveAccessibleName(
      `Contents of ${samples.path}/registered`
    )
    await expect(dialog).toBeVisible()
    await page.keyboard.press("Backspace")
    await expect(list).toHaveAccessibleName(`Contents of ${samples.path}`)
    await expect(highlighted).toBeVisible()

    // A slow folder (not read yet in this dialog): the list says what opens and stays inert until it
    // is read.
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => {
      release = () => {
        resolve()
      }
    })
    await page.route(
      (url) =>
        url.pathname === "/api/v1/files/browse" &&
        (url.searchParams.get("path") ?? "").endsWith("/synthetic-indexing"),
      async (route) => {
        await held
        await route.continue()
      }
    )
    await dialog.getByRole("option", { name: /^synthetic-indexing/ }).click()
    await expect(dialog.getByRole("status")).toHaveText(
      "Opening synthetic-indexing"
    )
    await expect(list).toHaveAttribute("aria-busy", "true")
    await expect(
      dialog.getByRole("button", { name: "Select folder" })
    ).toBeDisabled()
    release()
    await expect(dialog.getByRole("status")).toHaveCount(0)
    await expect(list).toHaveAttribute("aria-busy", "false")
    await page.unrouteAll()
    await page.keyboard.press("Backspace")
    await expect(list).toHaveAccessibleName(`Contents of ${samples.path}`)
    await dialog.getByRole("option", { name: /^registered/ }).click()
    await expect(list).toHaveAccessibleName(
      `Contents of ${samples.path}/registered`
    )
    await dialog.getByRole("button", { name: "Select folder" }).click()
    await expect(dialog).toHaveCount(0)
    await expect(folder).toHaveValue(`${samples.path}/registered`)
    await expect(error).toHaveCount(0)

    const sample = `Synthetic portal demo (${testInfo.project.name})`
    await catalog.getByLabel("Sample Name").fill(sample)
    await catalog.getByRole("button", { name: "Add prefix" }).click()
    await expect(
      catalog.getByLabel(/^Filename prefix \d+$/).last()
    ).toBeFocused()
    await page.keyboard.type("frame_e2e_%d.h5")
    await catalog.getByRole("button", { name: "Save" }).click()
    await expect(
      page.getByText(`Catalog saved for scan ${String(DEMO)}`)
    ).toBeVisible()

    const saved = await getScan(request, DEMO)
    expect(saved.catalog).toMatchObject({
      aperture: "mask",
      filefolder: `${samples.path}/registered`,
      sample_name: sample,
    })
    expect(saved.catalog?.filename_prefix).toContain("frame_e2e_%d.h5")

    // Scan Info shows the saved catalog; the demo scan is no depth scan, so no aperture.
    await page.getByRole("tab", { name: "Scan Info" }).click()
    const info = tabPanel(page, "Scan Info")
    await expect(definition(info, "Data Folder")).toHaveText(
      `${samples.path}/registered`
    )
    await expect(definition(info, "Sample")).toHaveText(sample)
    if (!saved.technique.includes("depth")) {
      await expect(info.locator('dt:text-is("Aperture")')).toHaveCount(0)
    }
  })

  test("appends a note to the catalog's notes", async ({
    page,
    request,
  }, testInfo) => {
    const note = `Checked by e2e (${testInfo.project.name}, ${String(Date.now())})`
    await page.goto(`/scans/${String(DEMO)}`)
    const info = tabPanel(page, "Scan Info")
    const box = info.getByLabel("Note")
    await expect(box).toHaveValue("")
    await expect(info.getByRole("button", { name: "Add to DB" })).toBeDisabled()
    await box.fill(note)
    await info.getByRole("button", { name: "Add to DB" }).click()
    await expect(
      page.getByText(`Note added to scan ${String(DEMO)}`)
    ).toBeVisible()
    await expect(box).toHaveValue("")
    await expect(box).toBeFocused()

    const notes = (await getScan(request, DEMO)).catalog?.notes ?? ""
    expect(notes.endsWith(`\n${note}`)).toBe(true)
    await page.getByRole("tab", { name: "Catalog" }).click()
    await expect(tabPanel(page, "Catalog").getByLabel("Notes")).toHaveValue(
      notes
    )
  })

  test("a note added while Notes has unsaved edits is appended to them, and Save keeps both", async ({
    page,
    request,
  }, testInfo) => {
    const stamp = `${testInfo.project.name}-${String(Date.now())}`
    await page.goto(`/scans/${String(DEMO)}?tab=catalog`)
    const catalog = tabPanel(page, "Catalog")
    const notes = catalog.getByLabel("Notes")
    await notes.click()
    await page.keyboard.press("Control+End")
    await page.keyboard.type(` EDITED-${stamp}`)
    const edited = await notes.inputValue()

    await page.getByRole("tab", { name: "Scan Info" }).click()
    const info = tabPanel(page, "Scan Info")
    await info.getByLabel("Note").fill(`APPENDED-${stamp}`)
    await info.getByRole("button", { name: "Add to DB" }).click()
    await expect(
      page.getByText(`Note added to scan ${String(DEMO)}`)
    ).toBeVisible()

    await page.getByRole("tab", { name: "Catalog" }).click()
    await expect(notes).toHaveValue(`${edited}\nAPPENDED-${stamp}`)
    await expect(
      catalog.getByText("The note added on Scan Info is appended here")
    ).toBeVisible()
    // The demo's aperture may still be "synthetic" (none the form offers).
    await chooseAperture(page, catalog, "Wire")
    await catalog.getByRole("button", { name: "Save" }).click()
    await expect(
      page.getByText(`Catalog saved for scan ${String(DEMO)}`)
    ).toBeVisible()
    const saved = (await getScan(request, DEMO)).catalog?.notes ?? ""
    expect(saved).toContain(`EDITED-${stamp}`)
    expect(saved.endsWith(`\nAPPENDED-${stamp}`)).toBe(true)
  })

  test("keeps the tab in the URL", async ({ page }) => {
    await page.goto("/scans")
    await page.goto(`/scans/${String(DEMO)}?tab=metadata`)
    await expect(page.getByRole("tab", { name: "Metadata" })).toHaveAttribute(
      "aria-selected",
      "true"
    )
    const metadata = tabPanel(page, "Metadata")
    await expect(
      definition(runCardIn(metadata, "Source"), "Energy")
    ).toHaveText("20 keV")
    await expect(
      definition(runCardIn(metadata, "Identifiers"), "Scan Number")
    ).toHaveText(String(DEMO))

    await page.getByRole("tab", { name: "Catalog" }).click()
    await expect(page).toHaveURL(endsWith(`/scans/${String(DEMO)}?tab=catalog`))
    await page.reload()
    await expect(page.getByRole("tab", { name: "Catalog" })).toHaveAttribute(
      "aria-selected",
      "true"
    )
    await page.getByRole("tab", { name: "Scan Info" }).click()
    await expect(page).toHaveURL(endsWith(`/scans/${String(DEMO)}`))
    // Tabs replace their history entry: Back leaves the scan.
    await page.goBack()
    await expect(page).toHaveURL(endsWith("/scans"))
  })

  test("New Recon and New Index open the create forms with this scan, or with it and the run selected", async ({
    page,
    request,
  }) => {
    const response = await request.get(
      `/api/v1/indexing-runs?scan_id=${String(DEMO)}`
    )
    const runs = (await response.json()) as IndexingRunPage
    const [run] = runs.rows
    if (!run) throw new Error("the demo scan has no indexing run")

    await page.goto(`/scans/${String(DEMO)}`)
    const card = runCard(page, "Peak Indexing")
    await expect(card.locator('.ag-row [col-id="id"]')).toHaveText(
      runs.rows.map((row) => `I${String(row.id)}`)
    )
    // The create forms take their prefill once and remove it from the URL.
    expect(
      await prefillAsked(page, "wire", () =>
        card.getByRole("link", { name: "New Recon" }).click()
      )
    ).toEqual({ scan: String(DEMO) })
    await expect(page).toHaveURL(endsWith("/reconstructions/new"))
    await page.goBack()
    expect(
      await prefillAsked(page, "lauego", () =>
        card.getByRole("link", { name: "New Index" }).click()
      )
    ).toEqual({ scan: String(DEMO) })
    await expect(page).toHaveURL(endsWith("/indexing/new"))
    await page.goBack()

    await card
      .locator(`.ag-row[row-id="${String(run.id)}"] input[type="checkbox"]`)
      .check()
    await expect(card.getByText("1 selected")).toBeVisible()
    const reconstruction =
      run.reconstruction_id === null
        ? {}
        : { reconstruction: String(run.reconstruction_id) }
    expect(
      await prefillAsked(page, "lauego", () =>
        card.getByRole("link", { name: "New Index" }).click()
      )
    ).toEqual({
      scan: String(DEMO),
      ...reconstruction,
      indexing: String(run.id),
    })
    await expect(page).toHaveURL(endsWith("/indexing/new"))
  })

  test("the demo scan, of type area, lists its reconstruction in a Reconstructions card", async ({
    page,
    request,
  }) => {
    const scan = await getScan(request, DEMO)
    expect(scan.technique).not.toContain("depth")
    const response = await request.get(
      `/api/v1/reconstruction-runs?scan_id=${String(DEMO)}`
    )
    const runs = (await response.json()) as ReconstructionRunPage
    expect(runs.rows.map((run) => run.id)).toContain(1)

    await page.goto(`/scans/${String(DEMO)}`)
    const card = runCard(page, "Reconstructions")
    await expect(card.locator('.ag-row [col-id="id"]')).toHaveText(
      runs.rows.map((run) => `R${String(run.id)}`)
    )
    await card.locator('.ag-row[row-id="1"] input[type="checkbox"]').check()
    expect(
      await prefillAsked(page, "wire", () =>
        card.getByRole("link", { name: "New Recon" }).click()
      )
    ).toEqual({ scan: String(DEMO), reconstruction: "1" })
  })

  test("an unknown scan is a not-found page", async ({ page }) => {
    await page.goto("/scans/123456789")
    await expect(
      page.getByRole("heading", { name: "Scan 123456789 not found" })
    ).toBeVisible()
    await expect(page).toHaveTitle("Page not found · 3DMN Portal")
    await page.getByRole("link", { name: "Go to the scans" }).click()
    await expect(page).toHaveURL(endsWith("/scans"))
  })
})

function runCardIn(root: Locator, title: string) {
  return root.getByRole("region", { name: title })
}

/** Answer GET /api/v1/scans/{scan} (and PUT its catalog, which the next GET then shows). */
async function mockScan(page: Page, initial: ScanDetail) {
  let scan = initial
  const updates: ScanCatalogUpdate[] = []
  await page.route(
    `**/api/v1/scans/${String(initial.scan_number)}**`,
    (route: Route) => {
      const request = route.request()
      if (request.method() === "PUT") {
        const update = request.postDataJSON() as ScanCatalogUpdate
        updates.push(update)
        scan = { ...scan, catalog: { ...update, version: 'W/"saved"' } }
        return route.fulfill({ json: scan.catalog })
      }
      return route.fulfill({ json: scan })
    }
  )
  return { updates }
}

test.describe("with the scan answered by the test", () => {
  test("a depth scan lists its reconstructions; New Recon takes the one selected wire run", async ({
    page,
  }) => {
    const scan = makeScanDetail({ scan_number: 424242 })
    await mockScan(page, scan)
    const reconstructions = await mockRunList(
      page,
      "/api/v1/reconstruction-runs",
      [
        reconstructionRun(5, { scan_number: 424242, percent_brightest: 2.5 }),
        reconstructionRun(4, { scan_number: 424242, depth_start: -20 }),
      ]
    )
    await mockRunList(page, "/api/v1/indexing-runs", [
      indexingRun(9, { scan_number: 424242, reconstruction_id: 4 }),
    ])

    await page.goto("/scans/424242")
    const info = tabPanel(page, "Scan Info")
    await expect(definition(info, "Aperture")).toHaveText("Wire")
    await expect(definition(info, "Scan Type")).toHaveText("depth + area")
    const card = runCard(page, "Reconstructions")
    await expect(card.locator('.ag-row [col-id="id"]')).toHaveText(["R5", "R4"])
    const row = card
      .locator('.ag-row[row-id="4"]')
      .filter({ has: page.locator('[col-id="depth_start"]') })
    await expect(row.locator('[col-id="depth_start"]')).toHaveText("-20")
    await expect(row.locator('[col-id="depth_end"]')).toHaveText("50")
    expect(
      reconstructions.requests.every(
        (url) => url.searchParams.get("scan_id") === "424242"
      )
    ).toBe(true)

    // One checked row: checking R5 unchecks R4.
    const checkbox = (id: number) =>
      card.locator(`.ag-row[row-id="${String(id)}"] input[type="checkbox"]`)
    await checkbox(4).check()
    await checkbox(5).check()
    await expect(checkbox(4)).not.toBeChecked()
    await expect(card.getByText("1 selected")).toBeVisible()
    await expect(card.getByRole("link", { name: "New Index" })).toHaveAttribute(
      "href",
      "/indexing/new?scan=424242&reconstruction=5"
    )
    expect(
      await prefillAsked(page, "wire", () =>
        card.getByRole("link", { name: "New Recon" }).click()
      )
    ).toEqual({ scan: "424242", reconstruction: "5" })
  })

  test("a scan of another type shows Reconstructions only when it has some", async ({
    page,
  }) => {
    await mockScan(
      page,
      makeScanDetail({ scan_number: 424245, technique: "area" })
    )
    const reconstructions = await mockRunList<ReconstructionRunSummary>(
      page,
      "/api/v1/reconstruction-runs",
      []
    )
    await mockRunList(page, "/api/v1/indexing-runs", [])
    await page.goto("/scans/424245")
    await expect(runCard(page, "Peak Indexing")).toBeVisible()
    // The count (one run asked for) answered "none".
    await expect
      .poll(() =>
        reconstructions.requests.some(
          (url) =>
            url.searchParams.get("limit") === "1" &&
            url.searchParams.get("scan_id") === "424245"
        )
      )
      .toBe(true)
    await expect(runCard(page, "Reconstructions")).toHaveCount(0)

    reconstructions.update([reconstructionRun(7, { scan_number: 424245 })])
    await page.reload()
    await expect(
      runCard(page, "Reconstructions").locator('.ag-row [col-id="id"]')
    ).toHaveText(["R7"])
  })

  test("with a note not added, reloading the page asks first", async ({
    page,
  }) => {
    await mockScan(page, makeScanDetail({ scan_number: 424244 }))
    await mockRunList(page, "/api/v1/reconstruction-runs", [])
    await mockRunList(page, "/api/v1/indexing-runs", [])
    await page.goto("/scans/424244")
    const note = tabPanel(page, "Scan Info").getByLabel("Note")
    // A click first: Chromium asks only on a page the user has interacted with.
    await note.click()
    await note.fill("draft")
    const asked = new Promise<string>((resolve) => {
      page.once("dialog", (dialog) => {
        resolve(dialog.type())
        void dialog.accept()
      })
    })
    await page.reload()
    expect(await asked).toBe("beforeunload")
    await expect(tabPanel(page, "Scan Info").getByLabel("Note")).toHaveValue("")
  })

  test("a scan without a catalog prompts to fill it in; Save creates it", async ({
    page,
    request,
  }) => {
    const roots = (await (
      await request.get("/api/v1/files/roots")
    ).json()) as FileRoots
    const runsRoot = roots.roots.find((root) => root.name === "runs")
    if (!runsRoot) throw new Error("the e2e stack has no runs root")
    const { updates } = await mockScan(
      page,
      makeScanDetail({ scan_number: 424243, catalog: null })
    )
    await mockRunList(page, "/api/v1/reconstruction-runs", [])
    await mockRunList(page, "/api/v1/indexing-runs", [])

    await page.goto("/scans/424243")
    const info = tabPanel(page, "Scan Info")
    await expect(
      info.getByText("This scan has no catalog entry yet")
    ).toBeVisible()
    await info.getByRole("button", { name: "Fill in the catalog" }).click()
    await expect(page).toHaveURL(endsWith("/scans/424243?tab=catalog"))
    const catalog = tabPanel(page, "Catalog")
    // The button went with Scan Info: the first field has the keyboard.
    await expect(catalog.getByLabel("Aperture")).toBeFocused()
    await expect(catalog.getByText("No catalog entry yet.")).toBeVisible()

    // The form's own rules first.
    await catalog.getByRole("button", { name: "Save" }).click()
    await expect(catalog.getByText("Choose an aperture.")).toBeVisible()
    await expect(catalog.getByText("Enter the data folder.")).toBeVisible()
    expect(updates).toEqual([])

    await chooseAperture(page, catalog, "Wire")
    // An empty field opens the picker at the roots.
    await catalog.getByRole("button", { name: "Browse" }).click()
    const dialog = page.getByRole("dialog", { name: "Choose the data folder" })
    await expect(dialog.getByRole("listbox")).toHaveAccessibleName(
      "Allowed roots"
    )
    await dialog.getByRole("option", { name: /^runs/ }).click()
    await expect(dialog.getByRole("listbox")).toHaveAccessibleName(
      `Contents of ${runsRoot.path}`
    )
    await dialog.getByRole("button", { name: "Select folder" }).click()
    await catalog.getByLabel("Sample Name").fill("Si wafer")
    await catalog.getByRole("button", { name: "Save" }).click()
    await expect(
      page.getByText("Catalog created for scan 424243")
    ).toBeVisible()
    // A new catalog: no stored version to start from.
    expect(updates).toEqual([
      {
        version: null,
        aperture: "wire",
        sample_name: "Si wafer",
        filefolder: runsRoot.path,
        filename_prefix: [],
        notes: null,
      },
    ])

    await page.getByRole("tab", { name: "Scan Info" }).click()
    await expect(definition(info, "Data Folder")).toHaveText(runsRoot.path)
    await expect(info.getByLabel("Note")).toBeVisible()
  })

  test("at 900 px the page fits, and the run cards keep ID and Status in view", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 900 })
    await mockScan(page, makeScanDetail({ scan_number: 424244 }))
    await mockRunList(page, "/api/v1/reconstruction-runs", [
      reconstructionRun(5, { scan_number: 424244 }),
    ])
    await mockRunList(page, "/api/v1/indexing-runs", [
      indexingRun(9, { scan_number: 424244 }),
    ])
    await page.goto("/scans/424244")
    const card = runCard(page, "Reconstructions")
    await expect(card.locator('.ag-row [col-id="status"]')).toHaveText(
      "Finished"
    )
    // The columns overflow the cards: AG Grid pins the ID on the left and the status on the right.
    for (const name of ["Reconstructions", "Peak Indexing"]) {
      await expect
        .poll(() =>
          runCard(page, name).evaluate((card) => {
            const colId = (selector: string) =>
              card
                .querySelector(`.ag-header-row-column ${selector}`)
                ?.getAttribute("col-id") ?? null
            return {
              left: colId(".ag-header-cell-last-left-pinned"),
              right: colId(".ag-header-cell-first-right-pinned"),
            }
          })
        )
        .toEqual({ left: "id", right: "status" })
      // Every header shows whole.
      const truncated = await runCard(page, name).evaluate((card) =>
        Array.from(card.querySelectorAll<HTMLElement>(".ag-header-cell-text"))
          .filter((label) => label.scrollWidth > label.clientWidth)
          .map((label) => label.textContent)
      )
      expect(truncated).toEqual([])
    }
    for (const tab of ["Scan Info", "Metadata", "Catalog"]) {
      await page.getByRole("tab", { name: tab }).click()
      const overflow = await page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth
      )
      expect(overflow).toBe(0)
    }
  })
})
