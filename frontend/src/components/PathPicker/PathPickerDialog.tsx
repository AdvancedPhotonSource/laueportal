import { keepPreviousData } from "@tanstack/react-query"
import {
  CircleAlertIcon,
  FileIcon,
  FolderIcon,
  HardDriveIcon,
  Link2OffIcon,
  LoaderCircleIcon,
} from "lucide-react"
import {
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from "react"

import {
  EntryKind,
  useBrowseFiles,
  useListFileRoots,
  type BrowseFilesParams,
  type DirectoryListing,
  type FileRoots,
} from "@/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { describeError } from "@/lib/errors"
import { formatDateTime } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  breadcrumbs,
  extensionFilter,
  formatBytes,
  into,
  moveActive,
  nameOf,
  placeOf,
  startLocation,
  upFrom,
  type PickerLocation,
  type PickerMode,
  type Place,
} from "./path-picker"

export interface PathPickerDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** A folder (the one shown when Select is pressed) or a file in it. */
  mode: PickerMode
  /** File mode: the files listed, e.g. `[".xml", ".txt"]` (folders always are). */
  extensions?: readonly string[]
  /** File mode: a file larger than this (bytes) is listed but cannot be chosen. */
  maxFileSize?: number
  /** The field's path: the dialog opens there (at its folder in file mode). */
  value: string
  /** Where the dialog opens when the field is empty (else at the roots). */
  startPath?: string
  title: string
  /** The folder or file chosen (absolute, resolved by the server). */
  onSelect: (path: string) => void
}

/**
 * The roots-restricted path picker's dialog (the form kit reuses it): it walks the allowed roots
 * through GET /api/v1/files/browse, with breadcrumbs, folders before files, and the keyboard (arrows,
 * Home/End, Page Up/Down, Enter opens a folder or picks a file, Backspace goes up). In folder mode,
 * Select takes the folder shown; in file mode, the file highlighted.
 */
export function PathPickerDialog({
  open,
  onOpenChange,
  ...props
}: PathPickerDialogProps) {
  const listRef = useRef<HTMLUListElement>(null)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex max-h-[min(42rem,calc(100svh-2rem))] flex-col sm:max-w-2xl"
        initialFocus={listRef}
      >
        {/* Mounted while open: each opening starts at the field's path again. */}
        {open && <PickerBody {...props} listRef={listRef} />}
      </DialogContent>
    </Dialog>
  )
}

/** One choice of the list: a root, a folder, a file, or an entry whose file system does not answer. */
interface Choice {
  key: string
  kind: "root" | "directory" | "file" | "unavailable"
  name: string
  path: string
  /** Shown, but not chosen (a file in folder mode, a root missing on this host). */
  disabled: boolean
  detail?: string
  size?: string
}

function rootChoices(roots: FileRoots | undefined): Choice[] {
  return (roots?.roots ?? []).map((root) => ({
    key: `root:${root.name}`,
    kind: "root",
    name: root.name,
    path: root.path,
    disabled: !root.available,
    detail: root.available ? root.path : `${root.path} (not on this host)`,
  }))
}

function entryChoices(
  listing: DirectoryListing,
  mode: PickerMode,
  maxFileSize: number | undefined
): Choice[] {
  return listing.entries.map((entry): Choice => {
    if (entry.available === false || entry.modified === null) {
      return {
        key: entry.path,
        kind: "unavailable",
        name: entry.name,
        path: entry.path,
        disabled: true,
        detail: "Not responding",
      }
    }
    const isFile = entry.kind === EntryKind.file
    const tooLarge =
      isFile &&
      maxFileSize !== undefined &&
      entry.size !== null &&
      entry.size > maxFileSize
    const modified = formatDateTime(entry.modified)
    return {
      key: entry.path,
      kind: isFile ? "file" : "directory",
      name: entry.name,
      path: entry.path,
      disabled: (isFile && mode === "directory") || tooLarge,
      detail: tooLarge
        ? `${modified} · over the ${formatBytes(maxFileSize)} limit`
        : modified,
      size: entry.size === null ? undefined : formatBytes(entry.size),
    }
  })
}

function browseParams(
  location: PickerLocation,
  extensions: string | undefined
): BrowseFilesParams | undefined {
  switch (location.kind) {
    case "roots":
      return undefined
    case "root":
      return { root: location.root, extensions }
    case "path":
      return { path: location.path, extensions }
  }
}

function PickerBody({
  mode,
  extensions,
  maxFileSize,
  value,
  startPath,
  title,
  onSelect,
  listRef,
}: Omit<PathPickerDialogProps, "open" | "onOpenChange"> & {
  listRef: RefObject<HTMLUListElement | null>
}) {
  const listId = useId()
  const [location, setLocation] = useState<PickerLocation>(() =>
    startLocation(value, mode, startPath)
  )
  const filter = mode === "file" ? extensionFilter(extensions) : undefined
  const params = browseParams(location, filter)
  const roots = useListFileRoots()
  const listing = useBrowseFiles(params ?? {}, {
    query: {
      enabled: params !== undefined,
      // From folder to folder, the last list stays on screen (dimmed, inert) until the next one is
      // read, so the keyboard focus stays in it; a root opened from the roots has no last list.
      placeholderData: location.kind === "path" ? keepPreviousData : undefined,
    },
  })

  // A folder from the field that cannot be opened (outside the roots, not on this host): the roots
  // show instead, with the reason.
  const startFailed =
    location.kind === "path" &&
    location.fromValue === true &&
    listing.isError &&
    !listing.isFetching
  const showRoots = location.kind === "roots" || startFailed
  // The list on screen: the folder asked for once read (`folder`), or the last one while the next
  // loads (`stale`: dimmed, and nothing in it is opened or selected).
  const shown = showRoots ? undefined : listing.data
  const stale = shown !== undefined && listing.isPlaceholderData
  const folder = stale ? undefined : shown
  const busy =
    !showRoots && params !== undefined && (stale || listing.isPending)
  // Where the folder asked for is: from its listing, else from how it was reached (so the
  // breadcrumbs and "up" work while it loads and after it fails).
  const place: Place | undefined = showRoots
    ? undefined
    : folder
      ? { root: folder.root, rootPath: folder.root_path, path: folder.path }
      : placeOf(location)
  const choices = showRoots
    ? rootChoices(roots.data)
    : shown
      ? entryChoices(shown, mode, maxFileSize)
      : []
  const shownKey = showRoots ? "roots" : (shown?.path ?? "")
  const highlight = location.kind === "path" ? location.highlight : undefined

  // The active choice, for the list shown (another list starts at its highlighted entry or the top).
  const [activeState, setActiveState] = useState<{
    key: string
    index: number
  }>()
  const initialActive = Math.max(
    0,
    choices.findIndex((choice) => choice.name === highlight)
  )
  const active =
    activeState?.key === shownKey
      ? Math.min(activeState.index, choices.length - 1)
      : choices.length > 0
        ? initialActive
        : -1
  const activeChoice = active >= 0 ? choices[active] : undefined

  const setActive = (index: number, scroll: boolean) => {
    setActiveState({ key: shownKey, index })
    if (scroll) {
      document
        .getElementById(`${listId}-${String(index)}`)
        ?.scrollIntoView({ block: "nearest" })
    }
  }

  const go = (next: PickerLocation) => {
    setLocation(next)
    listRef.current?.focus()
  }

  const goUp = () => {
    if (place) go(upFrom(place))
    else if (!showRoots) go({ kind: "roots" })
  }

  const open = (choice: Choice | undefined, confirm: boolean) => {
    if (!choice || choice.disabled || stale) return
    if (choice.kind === "root") {
      go({ kind: "root", root: choice.name, rootPath: choice.path })
    } else if (choice.kind === "directory") go(into(place, choice.path))
    else if (confirm) onSelect(choice.path)
  }

  const selected = stale
    ? undefined
    : mode === "directory"
      ? folder?.path
      : activeChoice?.kind === "file" && !activeChoice.disabled
        ? activeChoice.path
        : undefined

  const onKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    if (
      event.key === "Backspace" ||
      (event.key === "ArrowUp" && event.altKey)
    ) {
      event.preventDefault()
      goUp()
      return
    }
    const next = moveActive(event.key, active, choices.length)
    if (next !== undefined) {
      event.preventDefault()
      // The last list, while the next one loads, does not move.
      if (!stale) setActive(next, true)
      return
    }
    if (event.key === "Enter") {
      event.preventDefault()
      open(activeChoice, true)
    }
  }

  const listError = showRoots ? roots.error : listing.error
  const listName = showRoots
    ? "Allowed roots"
    : folder
      ? `Contents of ${folder.path}`
      : place
        ? `Contents of ${place.path}`
        : "Folder contents"
  const crumbs = place
    ? breadcrumbs(place.root, place.rootPath, place.path)
    : []
  const opening =
    place?.path ?? (location.kind === "path" ? location.path : undefined)
  const noun = mode === "directory" ? "folder" : "file"
  const invalid = folder?.invalid_names ?? 0

  return (
    <>
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
      </DialogHeader>

      <Breadcrumb>
        <BreadcrumbList>
          <BreadcrumbItem>
            {showRoots ? (
              <BreadcrumbPage>Roots</BreadcrumbPage>
            ) : (
              <BreadcrumbLink
                render={<button type="button" />}
                className={CRUMB_BUTTON}
                onClick={() => {
                  go({ kind: "roots" })
                }}
              >
                Roots
              </BreadcrumbLink>
            )}
          </BreadcrumbItem>
          {crumbs.map((crumb, index) => (
            <Crumb
              key={crumb.path}
              label={crumb.label}
              current={index === crumbs.length - 1}
              onOpen={() => {
                go(into(place, crumb.path))
              }}
            />
          ))}
        </BreadcrumbList>
      </Breadcrumb>

      {startFailed && (
        <StartNotice path={location.path} error={listing.error} />
      )}
      {listError !== null && !startFailed && (
        <Alert variant="destructive">
          <CircleAlertIcon aria-hidden />
          <AlertTitle>{describeError(listError).title}</AlertTitle>
          <AlertDescription>{describeError(listError).detail}</AlertDescription>
        </Alert>
      )}

      <div className="relative flex min-h-40 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border">
          <ul
            ref={listRef}
            id={listId}
            role="listbox"
            tabIndex={0}
            aria-label={listName}
            title={
              mode === "directory"
                ? "Open a folder: click or Enter. Up: Backspace"
                : "Open a folder: click or Enter. Pick a file: double-click or Enter. Up: Backspace"
            }
            aria-busy={busy || (showRoots && roots.isPending)}
            aria-activedescendant={
              active >= 0 && !stale ? `${listId}-${String(active)}` : undefined
            }
            onKeyDown={onKeyDown}
            className={cn(
              "group/list flex flex-col p-1 outline-none focus-visible:ring-3 focus-visible:ring-ring",
              stale && "opacity-50 transition-opacity delay-150"
            )}
          >
            {choices.map((choice, index) => (
              <ChoiceOption
                key={choice.key}
                id={`${listId}-${String(index)}`}
                choice={choice}
                active={index === active && !stale}
                onClick={() => {
                  listRef.current?.focus()
                  if (stale) return
                  setActive(index, false)
                  if (choice.kind !== "file") open(choice, false)
                }}
                onDoubleClick={() => {
                  open(choice, true)
                }}
              />
            ))}
          </ul>
          {showRoots && roots.isPending && (
            <p className="flex items-center gap-2 px-3 py-2 text-muted-foreground">
              <LoaderCircleIcon className="size-4 animate-spin" aria-hidden />
              Loading the roots
            </p>
          )}
          {!busy &&
            !(showRoots && roots.isPending) &&
            listError === null &&
            choices.length === 0 && (
              <p className="px-3 py-2 text-muted-foreground">
                {showRoots
                  ? "No roots are configured (files.roots in the settings)."
                  : filter
                    ? `No folders or ${filter.replaceAll(",", " or ")} files here.`
                    : "This folder is empty."}
              </p>
            )}
        </div>
        {busy && (
          <p
            role="status"
            className="pointer-events-none absolute top-2 left-1/2 flex max-w-[90%] -translate-x-1/2 items-center gap-2 rounded-md border bg-popover px-3 py-1 shadow-sm"
          >
            <LoaderCircleIcon
              className="size-4 shrink-0 animate-spin"
              aria-hidden
            />
            <span className="truncate">
              Opening {opening ? nameOf(opening) || opening : "the folder"}
            </span>
          </p>
        )}
      </div>
      {folder?.truncated && (
        <p className="text-muted-foreground">
          Showing {folder.entries.length} entries: the folder holds more.
        </p>
      )}
      {invalid > 0 && (
        <p className="text-muted-foreground">
          {invalid === 1
            ? "1 entry is not shown: its name is not valid UTF-8."
            : `${String(invalid)} entries are not shown: their names are not valid UTF-8.`}
        </p>
      )}

      <DialogFooter className="items-center sm:justify-between">
        <p className="min-w-0 truncate font-mono text-xs text-muted-foreground">
          {selected ?? (busy && opening ? opening : `No ${noun} selected`)}
        </p>
        <div className="flex shrink-0 gap-2">
          <DialogClose render={<Button variant="outline" />}>
            Cancel
          </DialogClose>
          <Button
            disabled={selected === undefined}
            onClick={() => {
              if (selected !== undefined) onSelect(selected)
            }}
          >
            Select {noun}
          </Button>
        </div>
      </DialogFooter>
    </>
  )
}

const CRUMB_BUTTON =
  "rounded-sm underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring"

function Crumb({
  label,
  current,
  onOpen,
}: {
  label: string
  current: boolean
  onOpen: () => void
}) {
  return (
    <>
      <BreadcrumbSeparator />
      <BreadcrumbItem className="min-w-0">
        {current ? (
          <BreadcrumbPage className="truncate">{label}</BreadcrumbPage>
        ) : (
          <BreadcrumbLink
            render={<button type="button" />}
            className={cn(CRUMB_BUTTON, "truncate")}
            onClick={onOpen}
          >
            {label}
          </BreadcrumbLink>
        )}
      </BreadcrumbItem>
    </>
  )
}

function StartNotice({ path, error }: { path: string; error: unknown }) {
  const { title, detail } = describeError(error)
  return (
    <Alert>
      <CircleAlertIcon aria-hidden />
      <AlertTitle>
        {path} cannot be opened: {title}
      </AlertTitle>
      {detail && <AlertDescription>{detail}</AlertDescription>}
    </Alert>
  )
}

const CHOICE_ICONS = {
  root: HardDriveIcon,
  directory: FolderIcon,
  file: FileIcon,
  unavailable: Link2OffIcon,
} as const

function ChoiceOption({
  id,
  choice,
  active,
  onClick,
  onDoubleClick,
}: {
  id: string
  choice: Choice
  active: boolean
  onClick: () => void
  onDoubleClick: () => void
}) {
  const Icon = CHOICE_ICONS[choice.kind]
  // Muted text is too faint on the highlight: there, the highlight's own text colour.
  const detailColor = active
    ? "text-accent-foreground"
    : "text-muted-foreground"
  return (
    <li
      id={id}
      role="option"
      aria-selected={active}
      aria-disabled={choice.disabled || undefined}
      data-kind={choice.kind}
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      className={cn(
        "grid cursor-default grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2 rounded-md px-2 py-1 select-none",
        // The keyboard's place in the list: a tint, and an outline in the link colour (3:1 and more
        // against the dialog) while the list has the focus.
        active &&
          "bg-accent text-accent-foreground group-focus/list:outline-2 group-focus/list:-outline-offset-2 group-focus/list:outline-link",
        choice.disabled && "text-muted-foreground"
      )}
    >
      <Icon
        aria-hidden
        className={cn(
          "size-4",
          choice.kind === "directory" && !choice.disabled && "text-link"
        )}
      />
      <span className="truncate">
        <span
          data-slot="choice-name"
          className={cn(choice.kind === "root" && "font-medium")}
        >
          {choice.name}
        </span>
        {/* Read out apart from the name: "i70, 2026-10-13 19:11". */}
        <span className="sr-only">{", "}</span>
        {choice.kind === "root" && choice.detail && (
          <span className={cn("ml-2 font-mono text-xs", detailColor)}>
            {choice.detail}
          </span>
        )}
      </span>
      {choice.kind !== "root" && (
        <span className={cn("text-xs tabular-nums", detailColor)}>
          {[choice.size, choice.detail].filter(Boolean).join(" · ")}
        </span>
      )}
    </li>
  )
}
