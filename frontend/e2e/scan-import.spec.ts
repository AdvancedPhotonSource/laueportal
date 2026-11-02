import { expect, test } from "@playwright/test"

import type { FileRoots } from "@/api"

// The scan import page on the seeded stack (`make e2e`, nothing mocked): a sample scan log chosen
// through the server browser (which opens at the configured scan-log folder), indexed, quick-selected
// and imported; the statuses follow the import, and the new scans are in /scans. The demo database has
// none of the log's scans.

test("imports scans from a sample scan log through the server browser", async ({
  page,
  request,
}) => {
  const roots = (await (
    await request.get("/api/v1/files/roots")
  ).json()) as FileRoots
  const samples = roots.roots.find((root) => root.name === "samples")
  if (!samples) throw new Error("the e2e stack has no samples root")
  const log = `${samples.path}/scan-logs/test_log_2.xml`

  await page.goto("/scans/import")
  await expect(page).toHaveTitle("Import Scans · 3DMN Portal")
  await page.getByRole("button", { name: "Browse Server Logs" }).click()
  const dialog = page.getByRole("dialog", { name: "Browse Server Logs" })
  // It opens at the published files.scan_log_dir (the e2e stack's: the demo's staged scan logs).
  await expect(dialog.getByRole("listbox")).toHaveAccessibleName(
    `Contents of ${samples.path}/scan-logs`
  )
  await dialog.getByRole("option", { name: /^test_log_2\.xml/ }).click()
  await dialog.getByRole("button", { name: "Select file" }).click()

  const source = page.getByRole("region", { name: "Scan Log" })
  await expect(source.getByRole("definition").first()).toHaveText(log)
  await expect(source).toContainText("6 · New 6")
  const scans = page.getByRole("region", { name: "Scans" })
  // The data rows (the header rows have no scan ID).
  const rows = scans
    .getByRole("row")
    .filter({ has: page.getByRole("gridcell", { name: /^\d{6}$/ }) })
  // Newest first.
  await expect(rows.first()).toContainText("276802")
  await expect(rows.first()).toContainText("New")

  const importButton = scans.getByRole("button", { name: /^Import Selected/ })
  await expect(importButton).toHaveText("Import Selected (0)")
  await page.getByLabel("Scan ID greater than").fill("274037")
  await page.getByRole("button", { name: "Select by scan ID" }).click()
  await expect(importButton).toHaveText("Import Selected (4)")
  await page.getByLabel("Sample Name").fill("Imported in e2e")

  await importButton.click()
  await expect(
    scans.getByRole("status", { name: "Import summary" })
  ).toHaveText("Imported 4 · Exists 0 · Failed 0")
  const row = (scan: string) => rows.filter({ hasText: scan })
  for (const scan of ["274038", "274039", "276007", "276802"]) {
    await expect(row(scan)).toContainText("Imported")
  }
  await expect(row("274036")).toContainText("New")
  await expect(importButton).toHaveText("Import Selected (0)")

  await page.goto("/scans")
  await expect(page.getByRole("row", { name: /276802/ })).toContainText(
    "Imported in e2e"
  )
  await expect(page.getByRole("row", { name: /274038/ })).toBeVisible()
  await expect(page.getByRole("row", { name: /274036/ })).toHaveCount(0)
})
