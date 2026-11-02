import { LoaderCircleIcon } from "lucide-react"
import { useState, type ReactElement, type ReactNode } from "react"

import { ErrorState } from "@/components/ErrorState"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"

/**
 * Asks before an action that changes something, such as the run monitor's Stop and Move to Front.
 * Confirm runs `onConfirm`; until its promise settles the buttons are disabled (keeping keyboard
 * focus) and the dialog stays open. It closes when the promise resolves, and shows the error (a
 * problem's title and detail) when it rejects, so the user can try again or cancel. Open it with
 * `trigger` (e.g. a Button) or control it with `open` and `onOpenChange`.
 */
export function ConfirmDialog({
  open: openProp,
  onOpenChange,
  trigger,
  title,
  description,
  children,
  confirmLabel,
  cancelLabel = "Cancel",
  destructive = false,
  onConfirm,
}: {
  open?: boolean
  onOpenChange?: (open: boolean) => void
  trigger?: ReactElement
  title: ReactNode
  description?: ReactNode
  /** Content between the description and the buttons, e.g. the job IDs the action applies to. */
  children?: ReactNode
  confirmLabel: string
  cancelLabel?: string
  destructive?: boolean
  onConfirm: () => unknown
}) {
  const [ownOpen, setOwnOpen] = useState(false)
  const [pending, setPending] = useState(false)
  // Wrapped, so that a rejection with any value (even undefined) counts as an error.
  const [failure, setFailure] = useState<{ error: unknown } | null>(null)
  const open = openProp ?? ownOpen

  const changeOpen = (next: boolean) => {
    if (openProp === undefined) setOwnOpen(next)
    onOpenChange?.(next)
  }

  const confirm = async () => {
    if (pending) return
    setPending(true)
    setFailure(null)
    try {
      await onConfirm()
      changeOpen(false)
    } catch (error) {
      setFailure({ error })
    } finally {
      setPending(false)
    }
  }

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!pending) changeOpen(next)
      }}
      // However it closed (also by the parent), the next opening starts without the old error.
      onOpenChangeComplete={(isOpen) => {
        if (!isOpen) setFailure(null)
      }}
    >
      {trigger && <AlertDialogTrigger render={trigger} />}
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description && (
            <AlertDialogDescription>{description}</AlertDialogDescription>
          )}
        </AlertDialogHeader>
        {children}
        {failure && <ErrorState error={failure.error} />}
        {/* While the action runs, both buttons are aria-disabled but keep keyboard focus: a
            disabled button would drop it to the page (focusableWhenDisabled). Cancel closes
            through the dialog's own state, as Base UI's Close button cannot stay focusable. */}
        <AlertDialogFooter>
          <Button
            variant="outline"
            disabled={pending}
            focusableWhenDisabled
            className="aria-disabled:opacity-50"
            onClick={() => {
              if (!pending) changeOpen(false)
            }}
          >
            {cancelLabel}
          </Button>
          <AlertDialogAction
            variant={destructive ? "destructive" : "default"}
            disabled={pending}
            focusableWhenDisabled
            className="aria-disabled:opacity-50"
            onClick={() => void confirm()}
          >
            {pending && (
              <LoaderCircleIcon
                data-icon="inline-start"
                className="animate-spin"
                aria-hidden
              />
            )}
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
