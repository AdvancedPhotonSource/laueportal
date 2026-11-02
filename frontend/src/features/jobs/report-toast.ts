// The toast after a Stop or a Move to Front (the run monitor, the job page): the report's title, its
// tone, and a line per job.
import { createElement } from "react"
import { toast } from "sonner"

import { ERROR_TOAST_MS } from "@/lib/toast"
import { ReportLines } from "./ReportLines"
import type { ActionReport } from "./run-monitor"

/** Long enough to read a few lines; a report with an error stays as long as an error toast. */
export function reportDuration(report: ActionReport): number {
  const base =
    report.tone === "error" || report.tone === "warning"
      ? ERROR_TOAST_MS
      : 8_000
  return base + 2_000 * Math.max(report.lines.length - 1, 0)
}

export function toastReport(report: ActionReport): string | number {
  const show = {
    success: toast.success,
    info: toast.info,
    warning: toast.warning,
    error: toast.error,
  }[report.tone]
  return show(report.title, {
    description: createElement(ReportLines, { lines: report.lines }),
    duration: reportDuration(report),
  })
}
