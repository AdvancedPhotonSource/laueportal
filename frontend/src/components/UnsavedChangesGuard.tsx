import { useBlocker, type ShouldBlockFn } from "@tanstack/react-router"

import { ConfirmDialog } from "@/components/ConfirmDialog"

/** Only another page is leaving: a change of the page's own search (a tab, a prefill) is not. */
const leavesPage: ShouldBlockFn = ({ current, next }) =>
  current.pathname !== next.pathname

/**
 * Asks before leaving a page with unsaved edits (`when`): a link or Back to another page opens a
 * dialog (Stay, Leave), and closing or reloading the tab gets the browser's own prompt
 * (beforeunload). One per page: a page with several edited regions passes whether any has edits.
 */
export function UnsavedChangesGuard({ when }: { when: boolean }) {
  const blocker = useBlocker({
    shouldBlockFn: leavesPage,
    enableBeforeUnload: when,
    disabled: !when,
    withResolver: true,
  })
  return (
    <ConfirmDialog
      open={blocker.status === "blocked"}
      onOpenChange={(open) => {
        if (!open) blocker.reset?.()
      }}
      title="Leave without saving?"
      description="Your unsaved changes on this page are discarded."
      cancelLabel="Stay"
      confirmLabel="Leave"
      destructive
      onConfirm={() => blocker.proceed?.()}
    />
  )
}
