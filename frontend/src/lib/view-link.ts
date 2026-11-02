// "Copy link to this view": a viewer's state packed into one URL parameter, `?link=`, as compressed
// base64url JSON. The browser's CompressionStream compresses (deflate-raw); no package needed.

/** What a link carries: the viewer's schema version, its URL parameters and its stored settings. */
export interface ViewLink {
  v: number
  url: Record<string, string>
  settings: unknown
}

const FORMAT = "deflate-raw"

async function pipe(
  bytes: Uint8Array<ArrayBuffer>,
  transform: CompressionStream | DecompressionStream
): Promise<Uint8Array> {
  const stream = new ReadableStream<Uint8Array<ArrayBuffer>>({
    start(controller) {
      controller.enqueue(bytes)
      controller.close()
    },
  }).pipeThrough(transform)
  const chunks: Uint8Array[] = []
  const reader = stream.getReader()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value)
  }
  const out = new Uint8Array(
    chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  )
  let offset = 0
  for (const chunk of chunks) {
    out.set(chunk, offset)
    offset += chunk.length
  }
  return out
}

function toBase64Url(bytes: Uint8Array): string {
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

function fromBase64Url(text: string): Uint8Array<ArrayBuffer> {
  if (!/^[A-Za-z0-9_-]+$/.test(text)) throw new Error("not base64url")
  const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/"))
  return Uint8Array.from(binary, (char) => char.charCodeAt(0))
}

/** The link's state as the `link` parameter's value. */
export async function packViewLink(link: ViewLink): Promise<string> {
  const json = new TextEncoder().encode(JSON.stringify(link))
  return toBase64Url(await pipe(json, new CompressionStream(FORMAT)))
}

/**
 * The state a `link` value packs, or undefined when it is not one (not base64url, not compressed
 * JSON, not shaped as a link). The caller checks the version.
 */
export async function unpackViewLink(
  text: string
): Promise<ViewLink | undefined> {
  try {
    const bytes = await pipe(
      fromBase64Url(text),
      new DecompressionStream(FORMAT)
    )
    const value: unknown = JSON.parse(new TextDecoder().decode(bytes))
    if (typeof value !== "object" || value === null) return undefined
    const { v, url, settings } = value as Record<string, unknown>
    if (typeof v !== "number" || typeof url !== "object" || url === null) {
      return undefined
    }
    const strings = Object.fromEntries(
      Object.entries(url).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string"
      )
    )
    return { v, url: strings, settings }
  } catch {
    return undefined
  }
}

/** A view's search values as URL strings (what a copied link carries: never another link). */
export function searchStrings(search: object): Record<string, string> {
  return Object.fromEntries(
    Object.entries(search)
      .filter(([key, value]) => key !== "link" && value !== undefined)
      .map(([key, value]) => [key, String(value)])
  )
}

/** The full URL of a page's view (`href` is the page's location, any search replaced). */
export function viewLinkUrl(href: string, packed: string): string {
  const url = new URL(href)
  url.search = new URLSearchParams({ link: packed }).toString()
  return url.toString()
}

/** What a `link` value asks for: a view to apply, or why it is ignored. */
export type OpenedLink<S, T> =
  | { kind: "apply"; search: S; settings: T }
  | { kind: "mismatch"; version: number }
  | { kind: "unreadable" }

/**
 * What a `link` value of a viewer of schema `version` asks for: its search and settings, each read
 * as the viewer's URL and store read theirs (`readSearch`, `readSettings`). A link never carries
 * another link, so applying one cannot loop.
 */
export async function openViewLink<S extends { link?: string }, T>(
  packed: string,
  version: number,
  readSearch: (url: Record<string, string>) => S,
  readSettings: (value: unknown) => T
): Promise<OpenedLink<S, T>> {
  const link = await unpackViewLink(packed)
  if (!link) return { kind: "unreadable" }
  if (link.v !== version) return { kind: "mismatch", version: link.v }
  return {
    kind: "apply",
    search: { ...readSearch(link.url), link: undefined },
    settings: readSettings(link.settings),
  }
}
