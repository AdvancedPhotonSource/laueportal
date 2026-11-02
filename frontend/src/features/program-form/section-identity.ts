// Section identity: each form section's accent (a 4 px stripe on its card's left edge, its icon's colour)
// and lucide icon, one mapping for the create forms' sections and sidebar and the Live page's cards and
// their sub-sections. The science sections carry the colours, the bookkeeping ones (Identity, Notes) the
// neutral accent; no red or amber, which mean errors and warnings. The accents are theme tokens
// (`--section-*` in index.css); the class names are whole strings, so Tailwind finds them. An unknown title
// gets the neutral accent and a generic icon.
import {
  CrosshairIcon,
  FolderOpenIcon,
  IdCardIcon,
  LayersIcon,
  LayoutListIcon,
  type LucideIcon,
  NetworkIcon,
  NotebookPenIcon,
} from "lucide-react"

export interface SectionIdentity {
  icon: LucideIcon
  /**
   * The card's left stripe: a 4 px bar over the card's left edge, taking no layout space (a striped card's content
   * starts where a neutral card's does); the Card's own `overflow-hidden rounded-xl` clips it to its left corners.
   */
  stripe: string
  /** The icon's colour. */
  accent: string
}

const NEUTRAL = {
  stripe:
    "relative before:absolute before:inset-y-0 before:left-0 before:w-1 before:bg-section-neutral",
  accent: "text-section-neutral",
}
const TEAL = {
  stripe:
    "relative before:absolute before:inset-y-0 before:left-0 before:w-1 before:bg-section-teal",
  accent: "text-section-teal",
}
const VIOLET = {
  stripe:
    "relative before:absolute before:inset-y-0 before:left-0 before:w-1 before:bg-section-violet",
  accent: "text-section-violet",
}
const FUCHSIA = {
  stripe:
    "relative before:absolute before:inset-y-0 before:left-0 before:w-1 before:bg-section-fuchsia",
  accent: "text-section-fuchsia",
}

/** By the section's title: the backend's `x-layout` groups and the Live page's cards (`PART_TITLES`). */
export const SECTION_IDENTITIES: ReadonlyMap<string, SectionIdentity> = new Map(
  [
    ["Identity", { ...NEUTRAL, icon: IdCardIcon }],
    ["File Paths", { ...TEAL, icon: FolderOpenIcon }],
    ["Scan", { ...TEAL, icon: FolderOpenIcon }],
    ["Wire Reconstruction Parameters", { ...VIOLET, icon: LayersIcon }],
    ["Reconstruction", { ...VIOLET, icon: LayersIcon }],
    ["Peak Search Parameters", { ...VIOLET, icon: CrosshairIcon }],
    ["Indexing Parameters", { ...FUCHSIA, icon: NetworkIcon }],
    ["Indexing", { ...FUCHSIA, icon: NetworkIcon }],
    ["Notes", { ...NEUTRAL, icon: NotebookPenIcon }],
  ]
)

const UNKNOWN: SectionIdentity = { ...NEUTRAL, icon: LayoutListIcon }

export function sectionIdentity(title: string): SectionIdentity {
  return SECTION_IDENTITIES.get(title) ?? UNKNOWN
}
