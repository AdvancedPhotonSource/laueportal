// Orval mutator: every generated request goes through apiFetch (see orval.config.ts).
// The OpenAPI paths already start with /api/v1, so the base URL is only the app's mount point.

/** Base URL of the app (Vite's `base`, without a trailing slash); "" when served at the root. */
export function apiBaseUrl(base: string = import.meta.env.BASE_URL): string {
  return base.replace(/\/+$/, "")
}

/** Build the request URL for an API path such as "/api/v1/health". */
export function apiUrl(path: string, base?: string): string {
  if (!path.startsWith("/"))
    throw new Error(`API paths must start with "/": ${path}`)
  return `${apiBaseUrl(base)}${path}`
}

/** One per-field issue in a problem+json body. */
export interface ProblemIssue {
  loc: (string | number)[]
  message: string
  code: string
  severity: "error" | "warning"
}

/** RFC 9457 problem details, as returned by the backend for every error. */
export interface Problem {
  type?: string
  title?: string
  status?: number
  detail?: string
  instance?: string
  errors?: ProblemIssue[]
}

/**
 * Thrown for every non-2xx response. `problem` is set when the body is a JSON object with the
 * problem details shape (see asProblem); the message is its detail, else its title, else the status.
 */
export class ApiError extends Error {
  readonly status: number
  readonly problem: Problem | undefined
  /** A busy answer's `Retry-After` in seconds (a 503 of a capped route), null without one. */
  readonly retryAfter: number | null

  constructor(
    status: number,
    statusText: string,
    problem?: Problem,
    retryAfter: number | null = null
  ) {
    super(
      problem?.detail ||
        problem?.title ||
        `HTTP ${String(status)} ${statusText}`.trim()
    )
    this.name = "ApiError"
    this.status = status
    this.problem = problem
    this.retryAfter = retryAfter
  }
}

/** `Retry-After` in seconds (the delay form; an HTTP date is not used by the API), or null. */
function retryAfterOf(response: Response): number | null {
  const value = response.headers.get("retry-after")?.trim()
  if (!value || !/^\d+$/.test(value)) return null
  return Number(value)
}

async function errorOf(response: Response): Promise<ApiError> {
  return new ApiError(
    response.status,
    response.statusText,
    await readProblem(response),
    retryAfterOf(response)
  )
}

/**
 * Thrown when a request got no response at all: the API is stopped or restarting, or the network is
 * down. The browser's own error (a TypeError whose message differs by browser) is the `cause`.
 */
export class NetworkError extends Error {
  constructor(cause: unknown) {
    super("The API could not be reached", { cause })
    this.name = "NetworkError"
  }
}

function mediaType(response: Response): string {
  return (
    (response.headers.get("content-type") ?? "")
      .split(";")[0]
      ?.trim()
      .toLowerCase() ?? ""
  )
}

/** application/json and the +json types (application/problem+json, ...). */
function isJson(type: string): boolean {
  return (
    type === "application/json" || /^application\/[\w.-]+\+json$/.test(type)
  )
}

function isOptional(value: unknown, type: "string" | "number"): boolean {
  return value === undefined || typeof value === type
}

/**
 * The body as a Problem when its members have the problem details types (an object whose
 * type/title/detail/instance are strings, status a number and errors an array, each when present);
 * undefined otherwise. Other JSON error bodies, such as FastAPI's default `{"detail": [...]}`, from a
 * path without the problem handlers or from a proxy, yield an ApiError with the status only.
 */
function asProblem(body: unknown): Problem | undefined {
  if (typeof body !== "object" || body === null || Array.isArray(body))
    return undefined
  const { type, title, status, detail, instance, errors } = body as Record<
    string,
    unknown
  >
  const typed =
    isOptional(type, "string") &&
    isOptional(title, "string") &&
    isOptional(detail, "string") &&
    isOptional(instance, "string") &&
    isOptional(status, "number") &&
    (errors === undefined || Array.isArray(errors))
  return typed ? body : undefined
}

async function readProblem(response: Response): Promise<Problem | undefined> {
  if (!isJson(mediaType(response))) return undefined
  try {
    return asProblem(await response.json())
  } catch {
    return undefined // a malformed error body still yields an ApiError with the status
  }
}

/**
 * Body by media type: JSON parsed, text/* as a string, anything else (Arrow, raw frames) as a
 * Blob; nothing for 204/205.
 */
async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.status === 205) return undefined
  const type = mediaType(response)
  if (isJson(type)) return response.json()
  if (type.startsWith("text/")) return response.text()
  return response.blob()
}

/**
 * Sent with every request. The API refuses any request that can change data (every method but GET) without it,
 * with a 403 (backend `core.api.refuse_cross_site_writes`): a page on another origin can make the browser send a
 * form or a `no-cors` fetch, but never a custom header (that needs a CORS preflight, which the API never grants),
 * so it cannot stop or reorder runs.
 */
export const PORTAL_HEADER = ["X-Laue-Portal", "1"] as const

async function send(url: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers)
  if (!headers.has("Accept")) headers.set("Accept", "application/json")
  headers.set(...PORTAL_HEADER)
  try {
    return await fetch(apiUrl(url), { ...init, headers })
  } catch (error) {
    // A cancelled request (TanStack Query aborts it) keeps its AbortError.
    if (init?.signal?.aborted) throw error
    throw new NetworkError(error)
  }
}

export async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await send(url, init)
  if (!response.ok) throw await errorOf(response)
  return (await readBody(response)) as T
}

/**
 * A GET revalidated by its ETag (the viewer's products, `Cache-Control: private, no-cache`): with the
 * `etag` of the copy the caller holds it sends `If-None-Match` and resolves to the 304 Not Modified
 * (the copy is current), else to the 200 with the body for the caller to decode. Errors as apiFetch.
 */
export async function apiRevalidate(
  url: string,
  etag: string | null,
  init?: RequestInit
): Promise<Response> {
  const headers = new Headers(init?.headers)
  // A request with If-None-Match bypasses the browser's cache (fetch spec), so the 304 reaches us.
  if (etag !== null) headers.set("If-None-Match", etag)
  const response = await send(url, { ...init, method: "GET", headers })
  if (response.ok || (etag !== null && response.status === 304)) {
    return response
  }
  throw await errorOf(response)
}

/**
 * Orval uses these to type errors and request bodies in the generated hooks. A hook's error is
 * what apiFetch throws: an ApiError for an HTTP error, a NetworkError when no response arrived.
 */
export type ErrorType<_Error> = ApiError | NetworkError
export type BodyType<Body> = Body
