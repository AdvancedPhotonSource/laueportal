// The path picker's logic (no React): where its dialog opens, the breadcrumbs of a folder inside its
// root, the folder above it, and how the keys move through the list. Paths are the server's absolute
// POSIX paths (GET /api/v1/files/browse resolves them inside the allowed roots).

/** What the picker picks: a folder (the current one), or a file in it. */
export type PickerMode = "directory" | "file"

/** A folder inside a root: where the breadcrumbs start and up stops. */
export interface Place {
  root: string
  /** The root's folder, resolved. */
  rootPath: string
  /** The folder, resolved, inside rootPath. */
  path: string
}

/**
 * What the dialog shows: the allowed roots, a root's folder, or a folder by its path. A location
 * reached from a listing knows its root (`root`, `rootPath`), so its breadcrumbs and "up" work while
 * it loads and after it fails.
 */
export type PickerLocation =
  | { kind: "roots" }
  | { kind: "root"; root: string; rootPath?: string }
  | {
      kind: "path"
      path: string
      root?: string
      rootPath?: string
      /** The entry to highlight first (the file of the field, the folder left by going up). */
      highlight?: string
      /** The location comes from the field's value: when it fails, the roots show instead. */
      fromValue?: boolean
    }

export interface Crumb {
  label: string
  path: string
}

function trimSlashes(path: string): string {
  return path.length > 1 ? path.replace(/\/+$/, "") : path
}

/** The folder part of an absolute path: `/a/b/c.xml` is in `/a/b`; `/a` is in `/`. */
export function folderOf(path: string): string {
  const trimmed = trimSlashes(path.trim())
  const slash = trimmed.lastIndexOf("/")
  return slash <= 0 ? "/" : trimmed.slice(0, slash)
}

/** The last part of a path (`c.xml` of `/a/b/c.xml`). */
export function nameOf(path: string): string {
  const trimmed = trimSlashes(path.trim())
  return trimmed.slice(trimmed.lastIndexOf("/") + 1)
}

/**
 * Where the dialog opens: at the field's folder (in file mode, the folder of the field's file, which
 * is highlighted), else at `startPath` (e.g. the configured scan-log folder), else at the roots. Only
 * an absolute path is tried; the server checks it against the roots.
 */
export function startLocation(
  value: string,
  mode: PickerMode,
  startPath?: string
): PickerLocation {
  const typed = value.trim()
  if (typed.startsWith("/")) {
    return mode === "file"
      ? {
          kind: "path",
          path: folderOf(typed),
          highlight: nameOf(typed),
          fromValue: true,
        }
      : { kind: "path", path: trimSlashes(typed), fromValue: true }
  }
  const start = startPath?.trim()
  if (start?.startsWith("/")) {
    return { kind: "path", path: trimSlashes(start), fromValue: true }
  }
  return { kind: "roots" }
}

/**
 * The breadcrumbs of a folder inside its root: the root (by its name, at its folder), then each
 * folder below it down to `path`. A path outside `rootPath` (never from the API) is the root alone.
 */
export function breadcrumbs(
  root: string,
  rootPath: string,
  path: string
): Crumb[] {
  const base = trimSlashes(rootPath)
  const crumbs: Crumb[] = [{ label: root, path: base }]
  const prefix = base === "/" ? "/" : `${base}/`
  const current = trimSlashes(path)
  if (current === base || !current.startsWith(prefix)) return crumbs
  let folder = base === "/" ? "" : base
  for (const part of current.slice(prefix.length).split("/")) {
    if (!part) continue
    folder = `${folder}/${part}`
    crumbs.push({ label: part, path: folder })
  }
  return crumbs
}

function isWithin(rootPath: string, path: string): boolean {
  const base = trimSlashes(rootPath)
  const current = trimSlashes(path)
  return current === base || current.startsWith(base === "/" ? "/" : `${base}/`)
}

/**
 * Where "up" goes from a folder: the folder above it inside the same root (the folder left
 * highlighted), or the roots from the root's own folder.
 */
export function upFrom(place: Place): PickerLocation {
  const base = trimSlashes(place.rootPath)
  const current = trimSlashes(place.path)
  if (current === base || !isWithin(base, current)) return { kind: "roots" }
  return {
    kind: "path",
    path: folderOf(current),
    root: place.root,
    rootPath: place.rootPath,
    highlight: nameOf(current),
  }
}

/** A folder reached from `place` (an entry, a breadcrumb), with place's root when it is inside it. */
export function into(place: Place | undefined, path: string): PickerLocation {
  return place && isWithin(place.rootPath, path)
    ? { kind: "path", path, root: place.root, rootPath: place.rootPath }
    : { kind: "path", path }
}

/** Where a location is, when it knows its root: for breadcrumbs and "up" before its listing comes. */
export function placeOf(location: PickerLocation): Place | undefined {
  if (location.kind === "roots" || location.rootPath === undefined)
    return undefined
  if (location.kind === "root") {
    return {
      root: location.root,
      rootPath: location.rootPath,
      path: location.rootPath,
    }
  }
  return location.root === undefined
    ? undefined
    : { root: location.root, rootPath: location.rootPath, path: location.path }
}

const PAGE = 10

/**
 * The list's active entry after a key (ArrowDown, ArrowUp, Home, End, PageDown, PageUp), clamped to
 * the list; undefined for any other key or an empty list. With nothing active yet (-1), ArrowDown
 * and ArrowUp start at the first entry.
 */
export function moveActive(
  key: string,
  active: number,
  count: number
): number | undefined {
  if (count === 0) return undefined
  const last = count - 1
  const clamp = (index: number) => Math.min(last, Math.max(0, index))
  switch (key) {
    case "ArrowDown":
      return active < 0 ? 0 : clamp(active + 1)
    case "ArrowUp":
      return active < 0 ? 0 : clamp(active - 1)
    case "Home":
      return 0
    case "End":
      return last
    case "PageDown":
      return clamp(active + PAGE)
    case "PageUp":
      return clamp(active - PAGE)
    default:
      return undefined
  }
}

const UNITS = ["KiB", "MiB", "GiB", "TiB"]

/** A file size in bytes as `512 B`, `1.5 KiB`, `20 MiB` (one decimal below 10). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 1024) {
    return `${String(Math.max(0, Math.round(bytes)))} B`
  }
  let size = bytes
  let unit = "B"
  for (const next of UNITS) {
    if (size < 1024) break
    size /= 1024
    unit = next
  }
  const text =
    size < 10 ? size.toFixed(1).replace(/\.0$/, "") : Math.round(size)
  return `${String(text)} ${unit}`
}

/** The extensions as the API's filter (`.xml,.txt`), or undefined for every file. */
export function extensionFilter(
  extensions: readonly string[] | undefined
): string | undefined {
  const list = (extensions ?? [])
    .map((extension) => extension.trim().toLowerCase())
    .filter(Boolean)
  return list.length > 0 ? list.join(",") : undefined
}
