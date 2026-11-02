import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { CircleAlertIcon, PlusIcon, XIcon } from "lucide-react"
import {
  useEffect,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type Ref,
} from "react"
import { Controller, useFieldArray, useForm, useWatch } from "react-hook-form"
import { toast } from "sonner"
import type { z } from "zod"

import {
  getGetScanQueryKey,
  getGetScanQueryOptions,
  useUpdateScanCatalog,
  type ScanCatalog,
  type ScanDetail,
} from "@/api"
import { PathField } from "@/components/PathPicker"
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { subheading, surface } from "@/lib/styles"
import { toastError } from "@/lib/toast"
import { cn } from "@/lib/utils"
import {
  catalogFieldProblems,
  catalogFormSchema,
  catalogFormValues,
  catalogUpdate,
  isCatalogChanged,
  onlyNoteAppended,
  sameCatalogValues,
} from "./scan-catalog"
import { APERTURE_LABELS, APERTURES, apertureValue } from "./scan-detail"

/** The form's group headings, as a card's sub-headings (the legend's own size is overridden). */
const LEGEND = cn(subheading, "data-[variant=legend]:text-xs")

/** What the scan page asks of the catalog form. */
export interface CatalogFormHandle {
  /**
   * A note was appended to the stored notes (Add to DB on Scan Info; `catalog` is the answer).
   * Unedited, Notes shows it when the scan is read again; edited, it is appended to the edits, so
   * Save cannot drop it, and the edits then carry the appended catalog's version.
   */
  noteAdded: (text: string, catalog: ScanCatalog) => void
  /** Give the keyboard to the first field (Aperture). */
  focusFirstField: () => void
}

type FormInput = z.input<typeof catalogFormSchema>
type FormOutput = z.output<typeof catalogFormSchema>

/** The form path of a filename prefix's text. */
function prefixPath(index: number) {
  return `filename_prefix.${String(index)}.value` as `filename_prefix.${number}.value`
}

/**
 * The Catalog tab: the aperture (None, Wire, Coded Aperture), the sample name, the data folder with
 * the roots-restricted path picker, the filename prefixes and the notes. Save creates or replaces
 * the catalog (updateScanCatalog); a folder outside the roots shows the server's reason at the Files
 * Path. Edits survive a change of the stored catalog, and a note added on Scan Info while Notes has
 * edits is appended to them (CatalogFormHandle.noteAdded). Save sends the version of the stored
 * catalog the edits started from (`base`): one changed since (another tab or user) is a 409, which
 * the form explains, with Reload (the stored catalog; the edits are dropped).
 */
export function ScanCatalogForm({
  scanId,
  catalog,
  ref,
  onDirtyChange,
}: {
  scanId: number
  catalog: ScanCatalog | null
  ref?: Ref<CatalogFormHandle>
  /** Whether the form has unsaved edits (the page's unsaved-changes guard); keep it stable. */
  onDirtyChange?: (dirty: boolean) => void
}) {
  const id = useId()
  const queryClient = useQueryClient()
  const stored = useMemo(() => catalogFormValues(catalog), [catalog])
  const form = useForm<FormInput, unknown, FormOutput>({
    resolver: zodResolver(catalogFormSchema),
    values: stored,
    resetOptions: { keepDirtyValues: true },
  })
  // A note added on Scan Info while Notes has edits: appended to them here too (see noteAdded).
  const [noteMerged, setNoteMerged] = useState(false)
  // Edits: the form differs from the stored catalog. Not formState.isDirty, which a new stored
  // catalog (the `values` reset) clears while it keeps the edited values.
  const watched = useWatch({ control: form.control })
  const dirty = !sameCatalogValues(watched, stored)
  // The stored catalog the edits started from: the current one while the form has no edits.
  const [base, setBase] = useState(catalog)
  if (!dirty && base !== catalog) setBase(catalog)
  // A save refused because the stored catalog changed since `base`.
  const [changed, setChanged] = useState(false)
  const [reloading, setReloading] = useState(false)
  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])

  useImperativeHandle(
    ref,
    () => ({
      noteAdded: (text, appended) => {
        // The edits keep the note (below, or Notes takes it unedited), so they may start from it.
        if (base && onlyNoteAppended(base, text, appended)) setBase(appended)
        // Unedited, Notes takes the stored notes, the note with them, when the scan is read again.
        if (!form.getFieldState("notes").isDirty) return
        const notes = form.getValues("notes")
        form.setValue("notes", notes ? `${notes}\n${text}` : text, {
          shouldDirty: true,
        })
        setNoteMerged(true)
      },
      focusFirstField: () => {
        form.setFocus("aperture")
      },
    }),
    [form, base]
  )
  const prefixes = useFieldArray({
    control: form.control,
    name: "filename_prefix",
  })
  const save = useUpdateScanCatalog()

  // After a prefix is removed, the keyboard goes to the prefix in its place, else to Add prefix.
  const addPrefixRef = useRef<HTMLButtonElement>(null)
  const focusAfterRemove = useRef<number | null>(null)
  const prefixCount = prefixes.fields.length
  useEffect(() => {
    const index = focusAfterRemove.current
    if (index === null) return
    focusAfterRemove.current = null
    if (prefixCount === 0) addPrefixRef.current?.focus()
    else form.setFocus(prefixPath(Math.min(index, prefixCount - 1)))
  }, [prefixCount, form])

  const onValid = (values: FormOutput) => {
    // A second click while saving is ignored.
    if (save.isPending) return
    save.mutate(
      { scanId, data: catalogUpdate(values, base?.version ?? null) },
      {
        onSuccess: (saved) => {
          queryClient.setQueryData<ScanDetail>(
            getGetScanQueryKey(scanId),
            (old) => old && { ...old, catalog: saved }
          )
          void queryClient.invalidateQueries({
            queryKey: getGetScanQueryKey(scanId),
          })
          // Not the form's keepDirtyValues: what was saved is the form now.
          form.reset(catalogFormValues(saved), { keepDirtyValues: false })
          setBase(saved)
          setNoteMerged(false)
          setChanged(false)
          toast.success(
            catalog
              ? `Catalog saved for scan ${String(scanId)}`
              : `Catalog created for scan ${String(scanId)}`
          )
        },
        onError: (error) => {
          if (isCatalogChanged(error)) {
            setChanged(true)
            return
          }
          const problems = catalogFieldProblems(error)
          problems.forEach(({ field, message }, index) => {
            form.setError(
              field,
              { type: "server", message },
              { shouldFocus: index === 0 }
            )
          })
          if (problems.length === 0) {
            toastError(error, "The catalog was not saved")
          }
        },
      }
    )
  }

  // Reload after a refused save: the stored catalog as it is now, the edits dropped. A failed read
  // keeps the edits and the alert.
  const reload = async () => {
    setReloading(true)
    let scan: ScanDetail
    try {
      scan = await queryClient.query({
        ...getGetScanQueryOptions(scanId),
        staleTime: 0,
      })
    } catch (error) {
      toastError(error, "The catalog was not reloaded")
      return
    } finally {
      setReloading(false)
    }
    const fresh = scan.catalog
    form.reset(catalogFormValues(fresh), { keepDirtyValues: false })
    setBase(fresh)
    setNoteMerged(false)
    setChanged(false)
  }

  const unknownAperture =
    catalog && apertureValue(catalog.aperture) === null
      ? catalog.aperture
      : undefined

  return (
    <form
      onSubmit={(event) => void form.handleSubmit(onValid)(event)}
      noValidate
      className={cn(surface, "flex max-w-3xl flex-col gap-6 p-4")}
    >
      {!catalog && (
        <p className="text-muted-foreground">No catalog entry yet.</p>
      )}
      <FieldSet>
        <FieldLegend className={LEGEND}>Scan Info</FieldLegend>
        <FieldGroup className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
          <Controller
            control={form.control}
            name="aperture"
            render={({ field, fieldState }) => (
              <Field data-invalid={fieldState.invalid || undefined}>
                <FieldLabel htmlFor={`${id}-aperture`}>Aperture</FieldLabel>
                <Select
                  items={APERTURE_LABELS}
                  value={field.value}
                  onValueChange={(value) => {
                    field.onChange(value)
                  }}
                >
                  <SelectTrigger
                    id={`${id}-aperture`}
                    ref={field.ref}
                    onBlur={field.onBlur}
                    aria-invalid={fieldState.invalid || undefined}
                    aria-describedby={`${id}-aperture-help ${id}-aperture-error`}
                    className="w-full"
                  >
                    <SelectValue placeholder="Choose an aperture" />
                  </SelectTrigger>
                  <SelectContent>
                    {APERTURES.map((aperture) => (
                      <SelectItem key={aperture} value={aperture}>
                        {APERTURE_LABELS[aperture]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {unknownAperture !== undefined && (
                  <FieldDescription id={`${id}-aperture-help`}>
                    Stored as &ldquo;{unknownAperture}&rdquo;, which is none of
                    these: choose one to save.
                  </FieldDescription>
                )}
                <FieldError
                  id={`${id}-aperture-error`}
                  errors={[fieldState.error]}
                />
              </Field>
            )}
          />
          <Field>
            <FieldLabel htmlFor={`${id}-sample`}>Sample Name</FieldLabel>
            <Input
              id={`${id}-sample`}
              autoComplete="off"
              {...form.register("sample_name")}
            />
          </Field>
        </FieldGroup>
      </FieldSet>

      <FieldSet>
        <FieldLegend className={LEGEND}>File Parameters</FieldLegend>
        <FieldGroup className="gap-4">
          <Controller
            control={form.control}
            name="filefolder"
            render={({ field, fieldState }) => (
              <Field data-invalid={fieldState.invalid || undefined}>
                <FieldLabel htmlFor={`${id}-folder`}>Files Path</FieldLabel>
                <PathField
                  id={`${id}-folder`}
                  name={field.name}
                  value={field.value}
                  onChange={field.onChange}
                  onBlur={field.onBlur}
                  inputRef={field.ref}
                  invalid={fieldState.invalid}
                  describedBy={`${id}-folder-error`}
                  dialogTitle="Choose the data folder"
                />
                <FieldError
                  id={`${id}-folder-error`}
                  errors={[fieldState.error]}
                />
              </Field>
            )}
          />
          <FieldSet className="gap-2">
            <FieldLegend variant="label">Filename Prefix</FieldLegend>
            {prefixCount > 0 ? (
              <ul className="flex flex-col gap-2">
                {prefixes.fields.map((item, index) => (
                  <li key={item.id} className="flex gap-2">
                    <Input
                      aria-label={`Filename prefix ${String(index + 1)}`}
                      placeholder="Ni_%d.h5"
                      autoComplete="off"
                      spellCheck={false}
                      className="font-mono"
                      {...form.register(prefixPath(index))}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      aria-label={`Remove filename prefix ${String(index + 1)}`}
                      onClick={() => {
                        focusAfterRemove.current = index
                        prefixes.remove(index)
                      }}
                    >
                      <XIcon aria-hidden />
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground">No prefixes.</p>
            )}
            <div>
              <Button
                ref={addPrefixRef}
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  prefixes.append({ value: "" })
                }}
              >
                <PlusIcon data-icon="inline-start" aria-hidden />
                Add prefix
              </Button>
            </div>
          </FieldSet>
        </FieldGroup>
      </FieldSet>

      <FieldSet>
        <FieldLegend className={LEGEND}>Notes</FieldLegend>
        <Field>
          <FieldLabel htmlFor={`${id}-notes`} className="sr-only">
            Notes
          </FieldLabel>
          <Textarea
            id={`${id}-notes`}
            className="min-h-24"
            {...form.register("notes")}
          />
          {noteMerged && (
            <p role="status" className="text-sm">
              The note added on Scan Info is appended here, after your edits:
              Save keeps it.
            </p>
          )}
        </Field>
      </FieldSet>

      {changed && (
        <Alert variant="destructive">
          <CircleAlertIcon aria-hidden />
          <AlertTitle>Catalog changed since it was loaded</AlertTitle>
          <AlertDescription>
            Reload it, then apply your changes again.
          </AlertDescription>
          <AlertAction>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={reloading}
              title="Load the stored catalog; your edits here are dropped"
              onClick={() => void reload()}
            >
              Reload
            </Button>
          </AlertAction>
        </Alert>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit">{save.isPending ? "Saving…" : "Save"}</Button>
        {dirty && (
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              form.reset(stored, { keepDirtyValues: false })
              setNoteMerged(false)
              setChanged(false)
            }}
          >
            Discard changes
          </Button>
        )}
      </div>
    </form>
  )
}
