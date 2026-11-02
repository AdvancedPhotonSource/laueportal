// The router's search-string format (createAppRouter's parseSearch and stringifySearch). Search
// values are plain strings in the URL, and each route's validateSearch converts and checks them.
// TanStack Router's default instead JSON-parses values (`hkl=110` became the number 110, `hkl=1,-10,2`
// stayed a string) and JSON-quotes strings that look like JSON (`hkl=%22110%22`). Commas stay readable
// (the indexing viewer's `hkl=1,-10,2`); everything else is percent-encoded.

/** The search string as an object of strings (the first value of a repeated key). */
export function parseSearch(searchStr: string): Record<string, string> {
  const search: Record<string, string> = {}
  for (const [key, value] of new URLSearchParams(searchStr)) {
    if (!Object.hasOwn(search, key)) search[key] = value
  }
  return search
}

/**
 * The search object as a search string ("" or "?…"). Strings are written as they are, numbers and
 * booleans as text, other values as JSON; undefined and null are left out.
 */
export function stringifySearch(search: Record<string, unknown>): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(search)) {
    if (value === undefined || value === null) continue
    params.set(
      key,
      typeof value === "string"
        ? value
        : typeof value === "number" || typeof value === "boolean"
          ? String(value)
          : JSON.stringify(value)
    )
  }
  const query = params.toString().replace(/%2C/gi, ",")
  return query ? `?${query}` : ""
}
