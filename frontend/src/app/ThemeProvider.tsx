import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react"

import {
  applyTheme,
  DARK_MEDIA_QUERY,
  readStoredTheme,
  resolveTheme,
  storeTheme,
  THEME_STORAGE_KEY,
  type Theme,
} from "@/lib/theme"
import { ThemeContext, type ThemeContextValue } from "./theme-context"

function subscribeToSystemTheme(onChange: () => void): () => void {
  const media = window.matchMedia(DARK_MEDIA_QUERY)
  media.addEventListener("change", onChange)
  return () => {
    media.removeEventListener("change", onChange)
  }
}

function systemPrefersDark(): boolean {
  return window.matchMedia(DARK_MEDIA_QUERY).matches
}

function storage(): Storage | undefined {
  try {
    return window.localStorage
  } catch {
    return undefined
  }
}

/** Light, dark or system theme; the preference is kept in localStorage and synced across tabs. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() =>
    readStoredTheme(storage())
  )
  const prefersDark = useSyncExternalStore(
    subscribeToSystemTheme,
    systemPrefersDark
  )
  const resolvedTheme = resolveTheme(theme, prefersDark)

  useEffect(() => {
    applyTheme(document.documentElement, resolvedTheme)
  }, [resolvedTheme])

  // Another tab changed the preference.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === THEME_STORAGE_KEY || event.key === null) {
        setThemeState(readStoredTheme(storage()))
      }
    }
    window.addEventListener("storage", onStorage)
    return () => {
      window.removeEventListener("storage", onStorage)
    }
  }, [])

  const setTheme = useCallback((next: Theme) => {
    storeTheme(storage(), next)
    setThemeState(next)
  }, [])

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, resolvedTheme, setTheme }),
    [theme, resolvedTheme, setTheme]
  )
  return <ThemeContext value={value}>{children}</ThemeContext>
}
