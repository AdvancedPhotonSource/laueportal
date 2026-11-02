// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import initScript from "../../public/theme-init.js?raw"
import {
  applyTheme,
  readStoredTheme,
  resolveTheme,
  storeTheme,
  THEME_STORAGE_KEY,
  type Theme,
} from "./theme"

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial))
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value)
    },
    removeItem: (key: string) => {
      data.delete(key)
    },
    data,
  }
}

const brokenStorage = {
  getItem: () => {
    throw new Error("SecurityError")
  },
  setItem: () => {
    throw new Error("QuotaExceededError")
  },
  removeItem: () => {
    throw new Error("SecurityError")
  },
}

describe("readStoredTheme", () => {
  it("defaults to system when nothing is stored", () => {
    expect(readStoredTheme(memoryStorage())).toBe("system")
    expect(readStoredTheme(undefined)).toBe("system")
  })

  it.each<Theme>(["light", "dark", "system"])(
    "returns a stored %s",
    (theme) => {
      expect(
        readStoredTheme(memoryStorage({ [THEME_STORAGE_KEY]: theme }))
      ).toBe(theme)
    }
  )

  it("ignores invalid values and unreadable storage", () => {
    expect(
      readStoredTheme(memoryStorage({ [THEME_STORAGE_KEY]: "blue" }))
    ).toBe("system")
    expect(readStoredTheme(brokenStorage)).toBe("system")
  })
})

describe("storeTheme", () => {
  it("stores light and dark, and removes the key for system", () => {
    const storage = memoryStorage()
    storeTheme(storage, "dark")
    expect(storage.data.get(THEME_STORAGE_KEY)).toBe("dark")
    storeTheme(storage, "light")
    expect(storage.data.get(THEME_STORAGE_KEY)).toBe("light")
    storeTheme(storage, "system")
    expect(storage.data.has(THEME_STORAGE_KEY)).toBe(false)
  })

  it("does not throw when storage fails", () => {
    expect(() => {
      storeTheme(brokenStorage, "dark")
    }).not.toThrow()
  })

  it("round-trips through readStoredTheme", () => {
    const storage = memoryStorage()
    for (const theme of ["dark", "system", "light"] as const) {
      storeTheme(storage, theme)
      expect(readStoredTheme(storage)).toBe(theme)
    }
  })
})

describe("resolveTheme", () => {
  it("follows the OS only for system", () => {
    expect(resolveTheme("system", true)).toBe("dark")
    expect(resolveTheme("system", false)).toBe("light")
    expect(resolveTheme("light", true)).toBe("light")
    expect(resolveTheme("dark", false)).toBe("dark")
  })
})

describe("applyTheme", () => {
  it("toggles the .dark class and the colour scheme", () => {
    const root = document.createElement("html")
    applyTheme(root, "dark")
    expect(root.classList.contains("dark")).toBe(true)
    expect(root.style.colorScheme).toBe("dark")
    applyTheme(root, "light")
    expect(root.classList.contains("dark")).toBe(false)
    expect(root.style.colorScheme).toBe("light")
  })

  it("keeps AG Grid's theme mode in step with the .dark class", () => {
    const root = document.createElement("html")
    applyTheme(root, "dark")
    expect(root.getAttribute("data-ag-theme-mode")).toBe("dark")
    applyTheme(root, "light")
    expect(root.getAttribute("data-ag-theme-mode")).toBe("light")
  })
})

// public/theme-init.js runs before the app to avoid a flash of the wrong theme. It must agree with
// the logic above for every stored value and OS preference.
describe("public/theme-init.js", () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.className = ""
  })
  afterEach(() => {
    localStorage.clear()
  })

  const stored = [null, "light", "dark", "system", "invalid"] as const
  const cases = stored.flatMap((value) =>
    [true, false].map((osDark) => [value, osDark] as const)
  )

  it.each(cases)("stored %s, OS dark %s", (value, osDark) => {
    if (value !== null) localStorage.setItem(THEME_STORAGE_KEY, value)
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: osDark,
      media: query,
    }))

    // Run our own script as the browser would (a classic script in the page's global scope).
    // eslint-disable-next-line @typescript-eslint/no-implied-eval
    const runInitScript = new Function(initScript) as () => void
    runInitScript()

    const expected = resolveTheme(readStoredTheme(localStorage), osDark)
    expect(document.documentElement.classList.contains("dark")).toBe(
      expected === "dark"
    )
    expect(document.documentElement.style.colorScheme).toBe(expected)
    expect(document.documentElement.getAttribute("data-ag-theme-mode")).toBe(
      expected
    )
  })
})
