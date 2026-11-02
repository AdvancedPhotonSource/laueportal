// The Live page's cards: Scan (the source, the identity and the paths each run gets) and the two program
// cards (a switch; the live program's fields but those the Scan card sets), drawn with the form kit's
// fields. While a session is open the page's fieldset disables them all.
import { useMemo } from "react"
import { type Control, useWatch, type UseFormSetValue } from "react-hook-form"
import type { LiveDaqEntry } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Switch } from "@/components/ui/switch"
import {
  blocksOf,
  type FieldBlock,
  type FieldSpec,
  type FormIssue,
  type FormValues,
  HklField,
  NumberField,
  OptionalNumberField,
  PathPickerField,
  type ProgramSpec,
  SectionIcon,
  sectionIdentity,
  SelectField,
  TextField,
  ToggleField,
} from "@/features/program-form"
import { describeError } from "@/lib/errors"
import { subheading } from "@/lib/styles"
import { cn } from "@/lib/utils"
import {
  chosenDaq,
  daqFolder,
  type Part,
  PART_TITLES,
  shownSections,
  sourceOptions,
} from "./live-form"

type IssuesOf = (name: string) => readonly FormIssue[] | undefined

/** The widgets drawn across a card's whole width. */
const WIDE = new Set(["folder", "file", "textarea"])

function blockKey(block: FieldBlock): string {
  return block.kind === "field"
    ? block.field.name
    : (block.fields[0]?.name ?? block.kind)
}

function wide(block: FieldBlock): boolean {
  return block.kind === "field" && WIDE.has(block.field.widget)
}

function KitField({
  field,
  control,
  issues,
  readOnly,
}: {
  field: FieldSpec
  control: Control<FormValues>
  issues: readonly FormIssue[] | undefined
  /** The page's fieldset disables the native controls; the kit's checkbox is told. */
  readOnly: boolean
}) {
  const props = { field, control, issues }
  switch (field.widget) {
    case "folder":
      return <PathPickerField {...props} mode="directory" />
    case "file":
      return <PathPickerField {...props} mode="file" />
    case "select":
      return <SelectField {...props} />
    case "checkbox":
      return <ToggleField {...props} disabled={readOnly} />
    case "textarea":
      return <TextField {...props} multiline />
    case "integer":
    case "number":
      return field.nullable ? (
        <OptionalNumberField {...props} />
      ) : (
        <NumberField {...props} />
      )
    default:
      return <TextField {...props} />
  }
}

function Block({
  block,
  control,
  issuesOf,
  readOnly,
}: {
  block: FieldBlock
  control: Control<FormValues>
  issuesOf: IssuesOf
  readOnly: boolean
}) {
  if (block.kind === "hkl") {
    return (
      <HklField fields={block.fields} control={control} issuesOf={issuesOf} />
    )
  }
  // A live LaueGo run's depth indices are typed: its catalog does not exist before the session.
  const fields = block.kind === "field" ? [block.field] : block.fields
  return (
    <>
      {fields.map((field) => (
        <KitField
          key={field.name}
          field={field}
          control={control}
          issues={issuesOf(field.name)}
          readOnly={readOnly}
        />
      ))}
    </>
  )
}

/** The Scan card: Source (Notifications or a DAQ, which fills the folder), scan, author and paths. */
export function ScanCard({
  fields,
  control,
  setValue,
  issuesOf,
  daqs,
  daqError,
  readOnly,
}: {
  fields: Record<string, FieldSpec>
  control: Control<FormValues>
  setValue: UseFormSetValue<FormValues>
  issuesOf: IssuesOf
  daqs: readonly LiveDaqEntry[]
  /** Why the DAQ list is not there (DM unavailable), if it is not. */
  daqError: unknown
  readOnly: boolean
}) {
  const source = String(useWatch({ control, name: "source" }))
  const folder = String(useWatch({ control, name: "folder" }))
  const sourceField = fields.source
  const sourceSpec = useMemo(
    () =>
      sourceField && { ...sourceField, options: sourceOptions(daqs, source) },
    [sourceField, daqs, source]
  )
  // Picking a DAQ fills the folder with where it writes, unless the folder is already inside: the user's
  // pick only, never a session's values applied (its folder is as recorded).
  const pick = (choice: string) => {
    const daq = chosenDaq(daqs, choice)
    const filled = daq && daqFolder(daq, folder)
    if (filled !== undefined) {
      setValue("folder", filled, { shouldDirty: true })
    }
  }
  const daq = chosenDaq(daqs, source)
  const field = (name: string) => {
    const spec = fields[name]
    return (
      spec && (
        <KitField
          field={spec}
          control={control}
          issues={issuesOf(name)}
          readOnly={readOnly}
        />
      )
    )
  }
  return (
    <Card
      id="live-scan"
      role="region"
      aria-label={PART_TITLES.scan}
      className={sectionIdentity(PART_TITLES.scan).stripe}
    >
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <SectionIcon title={PART_TITLES.scan} className="size-4.5" />
          {PART_TITLES.scan}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid items-start gap-4 md:grid-cols-3">
        <div className="flex flex-col gap-2">
          {sourceSpec && (
            <SelectField
              field={sourceSpec}
              control={control}
              issues={issuesOf("source")}
              onPick={pick}
            />
          )}
          {daqError !== null && daqError !== undefined && (
            <p role="status" className="text-sm text-warning">
              DAQ list unavailable:{" "}
              {describeError(daqError).detail ?? describeError(daqError).title}
            </p>
          )}
        </div>
        <div className="md:col-span-2">
          {daq && (
            <DefinitionList
              aria-label="DAQ"
              rows={[
                { label: "From", value: daq.data_directory ?? "–", mono: true },
                { label: "To", value: daq.destination ?? "–", mono: true },
              ]}
            />
          )}
        </div>
        {field("scan_number")}
        {field("author")}
        <div aria-hidden />
        <div className="md:col-span-3">{field("folder")}</div>
        {field("filename_template")}
        <div className="md:col-span-2">{field("output_folder")}</div>
      </CardContent>
    </Card>
  )
}

/** A program card: its switch, and while it is on, the live program's sections (but the Scan card's fields). */
export function ProgramCard({
  part,
  spec,
  on,
  onChange,
  follower,
  control,
  issuesOf,
  readOnly,
}: {
  part: Exclude<Part, "scan">
  spec: ProgramSpec
  on: boolean
  onChange: (on: boolean) => void
  /** The LaueGo card follows the reconstruction (its depth indices). */
  follower: boolean
  control: Control<FormValues>
  issuesOf: IssuesOf
  /** A session is open: the switch too (Base UI's switch is not a native control). */
  readOnly: boolean
}) {
  const title = PART_TITLES[part]
  const sections = shownSections(spec, follower)
  return (
    <Card
      id={`live-${part}`}
      role="region"
      aria-label={title}
      className={cn(!on && "pb-0", sectionIdentity(title).stripe)}
    >
      {/* The switch first, left of the title (at the far right it is easy to miss). */}
      <CardHeader
        className={cn(
          "grid-cols-[auto_1fr] items-center gap-3",
          !on && "border-b-0"
        )}
      >
        <Switch
          checked={on}
          disabled={readOnly}
          onCheckedChange={onChange}
          aria-label={title}
          title={
            part === "reconstruction"
              ? "Reconstruct each wire-scan file as it arrives"
              : follower
                ? "Index each reconstructed point as it completes"
                : "Index each frame as it arrives"
          }
        />
        <CardTitle className="flex items-center gap-2">
          <SectionIcon title={title} className="size-4.5" />
          {title}
        </CardTitle>
      </CardHeader>
      {on && (
        <CardContent className="flex flex-col gap-6">
          {sections.map((section) => (
            <div key={section.group} className="flex flex-col gap-3">
              <h3 className={cn(subheading, "flex items-center gap-2")}>
                {/* The shape only: the card's stripe and title icon carry its one colour. */}
                <SectionIcon
                  title={section.group}
                  className="size-3.5 text-muted-foreground"
                />
                {section.group}
              </h3>
              <div className="grid items-start gap-4 md:grid-cols-3">
                {blocksOf(section.fields).map((block) => (
                  <div
                    key={blockKey(block)}
                    className={cn(
                      wide(block) && "md:col-span-3",
                      block.kind === "field" &&
                        block.field.widget === "checkbox" &&
                        "md:self-end"
                    )}
                  >
                    <Block
                      block={block}
                      control={control}
                      issuesOf={issuesOf}
                      readOnly={readOnly}
                    />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </CardContent>
      )}
    </Card>
  )
}
