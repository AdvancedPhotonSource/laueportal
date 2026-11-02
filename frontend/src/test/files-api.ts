// A stand-in for the files API (GET /api/v1/files/roots and /files/browse) over a small in-memory
// tree, answering as the backend does: folders first, the extension filter, 400 outside the roots or
// for an unknown root, 404 for a missing folder, 422 for a file, and a cap with `truncated`; a folder
// can fail or be slow (FakeFiles.failures, holds), and list entries that do not answer (notResponding).
import type { DirectoryListing, FileEntry, FileRoots } from "@/api"

/** A folder's entries: a folder's name ends with "/"; a file has its size. */
export type FakeTree = Record<string, Record<string, number | "/">>

export interface FakeFiles {
  roots: Record<string, string>
  tree: FakeTree
  /** Folders missing on this host (a root's folder elsewhere). */
  unavailable?: string[]
  cap?: number
  /** Entries a folder's listing leaves out because their names are not valid UTF-8. */
  invalidNames?: Record<string, number>
  /** A folder whose listing fails with this status (e.g. 500, as for a server error). */
  failures?: Record<string, number>
  /** Entries a folder lists last as not available: links or mount points whose file system does not answer. */
  notResponding?: Record<string, string[]>
  /** A folder whose listing waits for this promise (a slow folder): see answerFilesLater. */
  holds?: Record<string, Promise<unknown>>
  /** The scan-log import's settings (default: 25 MiB, .xml and .txt, opening at the first root). */
  scanLogs?: Partial<FileRoots["scan_logs"]>
}

function problem(status: number, title: string, detail: string): Response {
  return Response.json(
    { type: "about:blank", title, status, detail },
    { status, headers: { "content-type": "application/problem+json" } }
  )
}

function isInside(path: string, folder: string): boolean {
  return path === folder || path.startsWith(`${folder}/`)
}

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(
    input instanceof Request ? input.url : input.toString(),
    "http://localhost"
  )
}

/** answerFiles, once the requested folder's hold (FakeFiles.holds), if it has one, is released. */
export async function answerFilesLater(
  files: FakeFiles,
  input: RequestInfo | URL
): Promise<Response | undefined> {
  const path = requestUrl(input).searchParams.get("path")
  const hold = path === null ? undefined : files.holds?.[path]
  if (hold) await hold
  return answerFiles(files, input)
}

/** The answer to a files API request, or undefined for another URL. */
export function answerFiles(
  files: FakeFiles,
  input: RequestInfo | URL
): Response | undefined {
  const url = requestUrl(input)
  if (url.pathname === "/api/v1/files/roots") {
    const roots: FileRoots = {
      roots: Object.entries(files.roots).map(([name, path]) => ({
        name,
        path,
        available: !(files.unavailable ?? []).includes(path),
      })),
      scan_logs: {
        max_bytes: 25 * 1024 * 1024,
        extensions: [".xml", ".txt"],
        start_folder: Object.values(files.roots)[0] ?? null,
        ...files.scanLogs,
      },
    }
    return Response.json(roots)
  }
  if (url.pathname !== "/api/v1/files/browse") return undefined

  const rootName = url.searchParams.get("root")
  const path = url.searchParams.get("path")?.replace(/(?<=.)\/+$/, "")
  const filter = url.searchParams.get("extensions")?.split(",")
  if (rootName !== null && !(rootName in files.roots)) {
    return problem(
      400,
      "Path not allowed",
      `There is no root named '${rootName}'.`
    )
  }
  const folder = path ?? files.roots[rootName ?? ""] ?? ""
  const failure = files.failures?.[folder]
  if (failure !== undefined) {
    return problem(failure, "Internal Server Error", `${folder} failed.`)
  }
  const root = Object.entries(files.roots)
    .filter(([, rootPath]) => isInside(folder, rootPath))
    .sort(([, a], [, b]) => b.length - a.length)[0]
  if (!root) {
    return problem(
      400,
      "Path not allowed",
      `${folder} is outside the allowed roots.`
    )
  }
  const entries = files.tree[folder]
  if (!entries || (files.unavailable ?? []).includes(folder)) {
    const parent = files.tree[folder.slice(0, folder.lastIndexOf("/"))]
    const name = folder.slice(folder.lastIndexOf("/") + 1)
    if (parent && typeof parent[name] === "number") {
      return problem(422, "Invalid selection", `${folder} is not a directory.`)
    }
    return problem(
      404,
      "Path not found",
      `${folder} does not exist or is not available on this host.`
    )
  }
  const listed: FileEntry[] = Object.entries(entries)
    .filter(
      ([name, size]) =>
        size === "/" ||
        !filter ||
        filter.some((extension) => name.toLowerCase().endsWith(extension))
    )
    .sort(([a, aSize], [b, bSize]) =>
      (aSize === "/") !== (bSize === "/")
        ? aSize === "/"
          ? -1
          : 1
        : a.localeCompare(b, undefined, { numeric: true })
    )
    .map(([name, size]): FileEntry => ({
      name,
      path: `${folder === "/" ? "" : folder}/${name}`,
      kind: size === "/" ? "directory" : "file",
      size: size === "/" ? null : size,
      modified: "2026-10-01T12:00:00Z",
    }))
    .concat(
      (files.notResponding?.[folder] ?? []).map((name) => ({
        name,
        path: `${folder === "/" ? "" : folder}/${name}`,
        kind: null,
        size: null,
        modified: null,
        available: false,
      }))
    )
  const cap = files.cap ?? 1000
  const listing: DirectoryListing = {
    root: rootName ?? root[0],
    root_path: rootName ? (files.roots[rootName] ?? root[1]) : root[1],
    path: folder,
    entries: listed.slice(0, cap),
    truncated: listed.length > cap,
    invalid_names: files.invalidNames?.[folder] ?? 0,
  }
  return Response.json(listing)
}
