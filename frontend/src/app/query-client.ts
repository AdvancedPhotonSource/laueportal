import { QueryClient } from "@tanstack/react-query"

import { ApiError } from "@/api"

const MAX_RETRIES = 2

/**
 * Client errors (4xx) will not succeed on retry, except timeouts and rate limits; network and
 * 5xx errors may.
 */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_RETRIES) return false
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return error.status === 408 || error.status === 429
  }
  return true
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetry },
    },
  })
}
