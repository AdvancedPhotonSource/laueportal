import { FolderOpenIcon } from "lucide-react"
import { type ReactNode, useState, type Ref } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import type { PickerMode } from "./path-picker"
import { PathPickerDialog } from "./PathPickerDialog"

export interface PathFieldProps {
  /** The input's id, for its label. */
  id: string
  name?: string
  value: string
  onChange: (value: string) => void
  onBlur?: () => void
  /** The input (React Hook Form's `field.ref`, so an error can focus it). */
  inputRef?: Ref<HTMLInputElement>
  /** A folder (default) or a file. */
  mode?: PickerMode
  /** File mode: the files the dialog lists, e.g. `[".xml", ".txt"]`. */
  extensions?: readonly string[]
  /** Where the dialog opens while the field is empty (else at the allowed roots). */
  startPath?: string
  /** The path the dialog opens at and highlights, when not the field's value (an output template's folder). */
  browsePath?: string
  /** The field's value for the path chosen in the dialog, when not that path (the template's tail added). */
  fromBrowse?: (path: string) => string
  /** The dialog's title, e.g. "Choose the data folder". */
  dialogTitle: string
  invalid?: boolean
  /** The ids of the field's description and error. */
  describedBy?: string
  placeholder?: string
  disabled?: boolean
  className?: string
  /** More controls after Browse, in its row (the Live page's From data folder). */
  actions?: ReactNode
}

/**
 * A path on the server: typed, or chosen with Browse in the roots-restricted picker dialog. The
 * server checks a typed path against the roots when the form is saved.
 */
export function PathField({
  id,
  name,
  value,
  onChange,
  onBlur,
  inputRef,
  mode = "directory",
  extensions,
  startPath,
  browsePath,
  fromBrowse,
  dialogTitle,
  invalid = false,
  describedBy,
  placeholder,
  disabled = false,
  className,
  actions,
}: PathFieldProps) {
  const [open, setOpen] = useState(false)
  return (
    <div className={cn("flex gap-2", className)}>
      <Input
        id={id}
        name={name}
        ref={inputRef}
        value={value}
        onChange={(event) => {
          onChange(event.target.value)
        }}
        onBlur={onBlur}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        placeholder={placeholder}
        disabled={disabled}
        spellCheck={false}
        autoComplete="off"
        className="font-mono"
      />
      <Button
        type="button"
        variant="outline"
        disabled={disabled}
        // Not red with an invalid field (shadcn's Field colours its text): the field is, not Browse.
        className="text-foreground"
        onClick={() => {
          setOpen(true)
        }}
      >
        <FolderOpenIcon data-icon="inline-start" aria-hidden />
        Browse
      </Button>
      {actions}
      <PathPickerDialog
        open={open}
        onOpenChange={setOpen}
        mode={mode}
        extensions={extensions}
        value={browsePath ?? value}
        startPath={startPath}
        title={dialogTitle}
        onSelect={(path) => {
          onChange(fromBrowse ? fromBrowse(path) : path)
          setOpen(false)
        }}
      />
    </div>
  )
}
