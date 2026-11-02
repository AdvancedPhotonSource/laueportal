import { createContext, useContext } from "react"

import type { ResolvedTheme, Theme } from "@/lib/theme"

export interface ThemeContextValue {
  /** The user's preference ("system" follows the OS). */
  theme: Theme
  /** What is applied now. */
  resolvedTheme: ResolvedTheme
  setTheme: (theme: Theme) => void
}

export const ThemeContext = createContext<ThemeContextValue | null>(null)

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext)
  if (!value) throw new Error("useTheme must be used inside <ThemeProvider>")
  return value
}
