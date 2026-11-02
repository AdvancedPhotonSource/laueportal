import { RotateCcwIcon } from "lucide-react"
import { useId, useState } from "react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { formatCount } from "@/lib/format"
import {
  DEFAULT_SCOPE,
  isSkipPeaks,
  MAX_SKIP_PEAKS,
  useIndexingViewerStore,
} from "./viewer-store"

/**
 * The scope bar: which records every tab reads. "Load only pattern 0" (on by default) and "Skip
 * steps with peaks ≤ N" (3 by default; 0 keeps every step, the field's tooltip; the range shows
 * only while the typed N is invalid), kept in the viewer's store; Reset restores both. A typed N
 * is saved at once and applies after a pause, or on Enter or leaving the field (a debounced
 * input); the products read it through useScopeParams. `stepsInScope` is the summary's count for
 * the applied scope.
 */
export function ScopeBar({ stepsInScope }: { stepsInScope?: number }) {
  const id = useId()
  const scope = useIndexingViewerStore((state) => state.scope)
  const setScope = useIndexingViewerStore((state) => state.setScope)
  const draftScope = useIndexingViewerStore((state) => state.draftScope)
  const applyScope = useIndexingViewerStore((state) => state.applyScope)
  const resetScope = useIndexingViewerStore((state) => state.resetScope)

  // The field's text: a valid N is saved at once (and applies after the pause); an invalid one is
  // shown until it is fixed or the field is left (which restores the saved N).
  const [draft, setDraft] = useState(String(scope.skipPeaksAtMost))
  // Another N applied (Reset, a link): the field shows it.
  const [shown, setShown] = useState(scope.skipPeaksAtMost)
  if (shown !== scope.skipPeaksAtMost) {
    setShown(scope.skipPeaksAtMost)
    setDraft(String(scope.skipPeaksAtMost))
  }
  const parsed = /^\d+$/.test(draft.trim()) ? Number(draft.trim()) : undefined
  const valid = isSkipPeaks(parsed)

  const isDefault =
    scope.pattern0Only === DEFAULT_SCOPE.pattern0Only &&
    scope.skipPeaksAtMost === DEFAULT_SCOPE.skipPeaksAtMost

  return (
    <div
      role="group"
      aria-label="Data scope"
      className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t bg-muted/50 px-4 py-1.5 text-sm"
    >
      <span className="font-medium">Data scope</span>
      <label className="flex items-center gap-2">
        <Checkbox
          checked={scope.pattern0Only}
          onCheckedChange={(checked) => {
            setScope({ pattern0Only: checked })
          }}
        />
        Load only pattern 0
      </label>
      <span className="flex items-center gap-2">
        <label htmlFor={`${id}-peaks`}>Skip steps with peaks ≤</label>
        <Input
          id={`${id}-peaks`}
          type="number"
          inputMode="numeric"
          min={0}
          max={MAX_SKIP_PEAKS}
          step={1}
          value={draft}
          aria-invalid={!valid || undefined}
          aria-describedby={valid ? undefined : `${id}-peaks-help`}
          title="0 keeps every step"
          className="h-7 w-20"
          onChange={(event) => {
            const text = event.target.value
            setDraft(text)
            const value = /^\d+$/.test(text.trim())
              ? Number(text.trim())
              : undefined
            if (isSkipPeaks(value)) {
              setShown(value)
              draftScope({ skipPeaksAtMost: value })
            }
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") applyScope()
          }}
          onBlur={() => {
            if (!valid) setDraft(String(scope.skipPeaksAtMost))
            applyScope()
          }}
        />
        {!valid && (
          <span id={`${id}-peaks-help`} className="text-destructive">
            {`a whole number, 0 to ${String(MAX_SKIP_PEAKS)}`}
          </span>
        )}
      </span>
      {stepsInScope !== undefined && (
        <span className="text-muted-foreground" role="status">
          {formatCount(stepsInScope)} {stepsInScope === 1 ? "step" : "steps"} in
          scope
        </span>
      )}
      <Button
        variant="ghost"
        size="sm"
        className="ml-auto"
        disabled={isDefault}
        onClick={resetScope}
      >
        <RotateCcwIcon aria-hidden />
        Reset
      </Button>
    </div>
  )
}
