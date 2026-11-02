// Theme preference logic (no React). The ThemeProvider in src/app uses it, and public/theme-init.js
// repeats the first-paint part of it before the app loads: keep the two in sync.

export type Theme = "light" | "dark" | "system"
export type ResolvedTheme = "light" | "dark"

export const THEMES: readonly Theme[] = ["light", "dark", "system"]
export const DEFAULT_THEME: Theme = "system"
export const THEME_STORAGE_KEY = "laueportal:theme"
export const DARK_MEDIA_QUERY = "(prefers-color-scheme: dark)"

export function isTheme(value: unknown): value is Theme {
  return (
    typeof value === "string" && (THEMES as readonly string[]).includes(value)
  )
}

/** The stored preference, or the default when it is missing, invalid or storage is unavailable. */
export function readStoredTheme(
  storage: Pick<Storage, "getItem"> | undefined
): Theme {
  try {
    const value = storage?.getItem(THEME_STORAGE_KEY)
    return isTheme(value) ? value : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME // storage disabled (privacy settings) or blocked
  }
}

/** Persist the preference; "system" (the default) is stored as the absence of a value. */
export function storeTheme(
  storage: Pick<Storage, "setItem" | "removeItem"> | undefined,
  theme: Theme
): void {
  try {
    if (theme === DEFAULT_THEME) storage?.removeItem(THEME_STORAGE_KEY)
    else storage?.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // Not persisted; the choice still applies to this page.
  }
}

export function resolveTheme(
  theme: Theme,
  systemPrefersDark: boolean
): ResolvedTheme {
  if (theme === "system") return systemPrefersDark ? "dark" : "light"
  return theme
}

/**
 * Apply the resolved theme to the document root: the .dark class drives the CSS variables, and AG
 * Grid's `data-ag-theme-mode` follows it (the DataGrid's theme).
 */
export function applyTheme(root: HTMLElement, resolved: ResolvedTheme): void {
  root.classList.toggle("dark", resolved === "dark")
  root.style.colorScheme = resolved
  root.dataset.agThemeMode = resolved
}
