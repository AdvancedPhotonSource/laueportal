// Toasts (sonner): the Toaster sits in src/app/Providers.tsx. Use `toast` from "sonner" for success
// and info messages, and toastError for a failed action, so errors read the same everywhere.
import { toast } from "sonner"

import { describeError } from "./errors"

/**
 * How long an error stays on screen: an error toast, and a Stop or Move to Front report with an error; sonner's
 * 4 s default is too short to read a problem's detail.
 */
export const ERROR_TOAST_MS = 15_000

/**
 * Show a failed action as an error toast, for ERROR_TOAST_MS. With `context` (what failed, e.g. "Job 12 was not
 * stopped") as the title, the description is the problem's title and detail; without it, the
 * problem's title is the toast's title.
 */
export function toastError(error: unknown, context?: string): string | number {
  const { title, detail } = describeError(error)
  const description = (context ? [title, detail] : [detail])
    .filter(Boolean)
    .join(": ")
  return toast.error(context ?? title, {
    ...(description ? { description } : {}),
    duration: ERROR_TOAST_MS,
  })
}
