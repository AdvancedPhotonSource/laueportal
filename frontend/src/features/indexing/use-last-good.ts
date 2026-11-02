import { useState } from "react"

/**
 * The last value that was not undefined: the plot stays while another is fetched or when one fails.
 */
export function useLastGood<T>(value: T | undefined): T | undefined {
  const [last, setLast] = useState(value)
  if (value !== undefined && value !== last) setLast(value)
  return value ?? last
}
