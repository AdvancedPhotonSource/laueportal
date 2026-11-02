import { act } from "react"
import { afterEach, beforeEach, describe, expect, it } from "vitest"

import { THEME_STORAGE_KEY, type Theme } from "@/lib/theme"
import { byRole, cleanup, click, render, stubMatchMedia } from "@/test/render"
import { useTheme } from "./theme-context"
import { ThemeProvider } from "./ThemeProvider"

// The provider keeps the preference in localStorage (src/lib/theme.ts has the storage rules) and
// applies it to <html>.

function ThemeButtons() {
  const { theme, resolvedTheme, setTheme } = useTheme()
  return (
    <>
      <output>{`${theme} ${resolvedTheme}`}</output>
      {(["light", "dark", "system"] as Theme[]).map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => {
            setTheme(option)
          }}
        >
          {option}
        </button>
      ))}
    </>
  )
}

const html = document.documentElement
const shown = () => document.querySelector("output")?.textContent

async function renderProvider() {
  await render(
    <ThemeProvider>
      <ThemeButtons />
    </ThemeProvider>
  )
}

beforeEach(() => {
  localStorage.clear()
  html.className = ""
})

afterEach(() => {
  cleanup()
  localStorage.clear()
})

describe("ThemeProvider", () => {
  it("starts from the stored preference", async () => {
    stubMatchMedia(false)
    localStorage.setItem(THEME_STORAGE_KEY, "dark")
    await renderProvider()
    expect(shown()).toBe("dark dark")
    expect(html.classList.contains("dark")).toBe(true)
  })

  it("stores the chosen theme, and removes it for system", async () => {
    stubMatchMedia(false)
    await renderProvider()
    expect(shown()).toBe("system light")

    await click(byRole("button", "dark")[0])
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark")
    expect(html.classList.contains("dark")).toBe(true)

    await click(byRole("button", "light")[0])
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light")
    expect(html.classList.contains("dark")).toBe(false)

    await click(byRole("button", "system")[0])
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
    expect(shown()).toBe("system light")
  })

  it("follows the OS while the preference is system", async () => {
    const media = stubMatchMedia(false)
    await renderProvider()
    act(() => {
      media.setDark(true)
    })
    expect(shown()).toBe("system dark")
    expect(html.classList.contains("dark")).toBe(true)
  })

  it("picks up a change made in another tab", async () => {
    stubMatchMedia(false)
    await renderProvider()
    act(() => {
      localStorage.setItem(THEME_STORAGE_KEY, "dark")
      window.dispatchEvent(
        new StorageEvent("storage", { key: THEME_STORAGE_KEY })
      )
    })
    expect(shown()).toBe("dark dark")
  })
})
