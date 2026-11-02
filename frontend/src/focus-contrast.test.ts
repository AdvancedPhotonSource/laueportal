import { readFileSync } from "node:fs"

import { describe, expect, it } from "vitest"

// Read as a file: Vitest empties CSS imports (`?raw` ones too). The base is a variable because Vite rewrites
// `new URL("…", import.meta.url)` into a URL of its dev server.
const here = import.meta.url
const indexCss = readFileSync(new URL("index.css", here), "utf8")

// Keyboard focus must be visible (WCAG 2.2 AA: 2.4.7, and 1.4.11's 3:1 for what shows it). Every focus indicator
// is drawn in --ring (rings, outlines, the highlighted menu item's inset ring, the grid's focus shadow), opaque,
// so the token itself must reach 3:1 against every surface it is drawn on, in both themes.

type Rgb = readonly [number, number, number]

/** The custom properties of one block of index.css (`:root` or `.dark`). */
function tokens(selector: string): Map<string, string> {
  const start = indexCss.indexOf(`${selector} {`)
  expect(start, selector).toBeGreaterThanOrEqual(0)
  const block = indexCss.slice(start, indexCss.indexOf("}", start))
  return new Map(
    [...block.matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [
      name ?? "",
      (value ?? "").trim(),
    ])
  )
}

/** OKLCH (CSS Color 4) to linear-light sRGB, clipped to the gamut. */
function oklchToLinear(lightness: number, chroma: number, hue: number): Rgb {
  const radians = (hue * Math.PI) / 180
  const a = chroma * Math.cos(radians)
  const b = chroma * Math.sin(radians)
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
  const clip = (value: number) => Math.min(1, Math.max(0, value))
  return [
    clip(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    clip(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    clip(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ]
}

const encode = (value: number) =>
  value <= 0.0031308 ? 12.92 * value : 1.055 * value ** (1 / 2.4) - 0.055
const decode = (value: number) =>
  value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4

/** A token's colour (linear sRGB), a translucent one composited over `under` as browsers do (in sRGB). */
function color(value: string, under?: Rgb): Rgb {
  const match =
    /^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*(?:\/\s*([\d.]+)%)?\s*\)$/.exec(
      value
    )
  if (!match) throw new Error(`not an oklch() colour: ${value}`)
  const [, lightness, chroma, hue, percent] = match
  const rgb = oklchToLinear(Number(lightness), Number(chroma), Number(hue))
  if (percent === undefined) return rgb
  if (!under)
    throw new Error(`${value} is translucent: give what it is drawn over`)
  const alpha = Number(percent) / 100
  return rgb.map((channel, index) =>
    decode(alpha * encode(channel) + (1 - alpha) * encode(under[index] ?? 0))
  ) as unknown as Rgb
}

const luminance = ([r, g, b]: Rgb) => 0.2126 * r + 0.7152 * g + 0.0722 * b

/** WCAG 2 contrast ratio. */
function contrast(first: Rgb, second: Rgb): number {
  const [light, dark] = [luminance(first), luminance(second)].sort(
    (x, y) => y - x
  )
  return ((light ?? 0) + 0.05) / ((dark ?? 0) + 0.05)
}

// Where focus indicators are drawn: the page's canvas, the fields and grids on it (--background), cards and
// panels, popovers (menus, selects, dialogs), muted and secondary fills (tab lists, grid headers), and the
// highlighted menu item's --accent (its ring is inset).
const SURFACES = [
  "canvas",
  "background",
  "card",
  "popover",
  "muted",
  "accent",
  "secondary",
] as const

/** A token's colour with its alpha scaled (Tailwind's `bg-input/30`) or set (`ring-destructive/20`). */
function withAlpha(value: string, percent: number, scale = false): string {
  const alpha = /\/\s*([\d.]+)%/.exec(value)
  const base = value.replace(/\s*\/\s*[\d.]+%/, "").replace(/\)$/, "")
  const result = scale && alpha ? (Number(alpha[1]) * percent) / 100 : percent
  return `${base} / ${String(result)}%)`
}

describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
])("the %s theme's focus ring", (name, selector) => {
  const dark = name === "dark"
  const theme = new Map([...tokens(":root"), ...tokens(selector)])
  const value = (name: string) => {
    const found = theme.get(name)
    if (found === undefined) throw new Error(`no --${name} in ${selector}`)
    return found
  }
  const background = color(value("background"))

  it("is opaque", () => {
    expect(value("ring")).not.toMatch(/\//)
  })

  it.each(SURFACES)("reaches 3:1 against --%s", (surface) => {
    const ring = color(value("ring"))
    const behind = color(value(surface), background)
    expect(contrast(ring, behind)).toBeGreaterThanOrEqual(3)
  })

  it("shows on a field marked invalid: the invalid styles stand aside while it has focus", () => {
    // A text field is filled with --background (white on the light canvas), with --input at 30% in the dark theme
    // (input.tsx), so the ring meets its fill.
    const ring = color(value("ring"))
    const fill = dark
      ? color(withAlpha(value("input"), 30, true), background)
      : background
    expect(contrast(ring, fill)).toBeGreaterThanOrEqual(3)
    // shadcn's invalid ring (--destructive at 20%, 40% in dark) replaced the focus ring of an invalid field and is
    // no indicator (1.44:1 and 1.95:1); it applies only without focus (the source test below).
    const invalidRing = color(
      withAlpha(value("destructive"), dark ? 40 : 20),
      background
    )
    expect(contrast(invalidRing, background)).toBeLessThan(3)
  })
})

describe("contrast()", () => {
  it("computes WCAG ratios from OKLCH tokens", () => {
    const white = color("oklch(1 0 0)")
    const black = color("oklch(0 0 0)")
    expect(contrast(white, black)).toBeCloseTo(21, 1)
    expect(contrast(white, white)).toBe(1)
    // shadcn's grey ring, measured at 2.58:1 as a solid border and 1.55:1 at 50%.
    const grey = color("oklch(0.708 0 0)")
    expect(contrast(grey, white)).toBeCloseTo(2.58, 1)
    expect(contrast(color("oklch(0.708 0 0 / 50%)", white), white)).toBeCloseTo(
      1.55,
      1
    )
  })
})

describe("focus styles in the source", () => {
  // Every focus ring and outline is drawn in the opaque --ring, and nothing that can hold while an element has
  // focus draws over it: a translucent ring (shadcn's ring-ring/50) does not reach 3:1 even when the token does,
  // and shadcn's invalid styles (aria-invalid:ring-destructive/20 and its border) replaced the ring of a focused
  // invalid field. So an invalid field's ring and border apply only without focus (not-focus-visible).
  const sources = {
    ...import.meta.glob<string>(
      ["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}", "!./api/generated/**"],
      { query: "?raw", import: "default", eager: true }
    ),
    "./index.css": indexCss,
  }

  /** Why a class (a word of a class string: its variants, then the utility) would hide or fade a focus ring. */
  function hidesFocus(name: string): string | undefined {
    const variants = name.split(":")
    const utility = variants.pop() ?? ""
    const colour = /^(?:ring|border|outline)-(?!ring$|inset$)[\w-]+\/\d+$/.test(
      utility
    )
    if (/^(?:ring|outline)-ring\/\d+$/.test(utility))
      return "a translucent focus ring"
    if (
      colour &&
      variants.some((variant) => /^focus(?:-visible|-within)?$/.test(variant))
    )
      return "a translucent colour while focused"
    if (
      variants.includes("aria-invalid") &&
      /^(?:ring|border|outline)(?:-|$)/.test(utility) &&
      !variants.includes("not-focus-visible")
    )
      return "an invalid style that can hold while focused"
    return undefined
  }

  it("knows shadcn's originals from the fixed classes", () => {
    for (const name of [
      "focus-visible:ring-ring/50",
      "outline-ring/50",
      "focus-visible:ring-destructive/20",
      "dark:focus-visible:border-destructive/40",
      "aria-invalid:ring-destructive/20",
      "aria-invalid:border-destructive",
      "dark:aria-invalid:ring-destructive/40",
      "aria-invalid:ring-3",
    ])
      expect(hidesFocus(name), name).toBeDefined()
    for (const name of [
      "focus-visible:ring-ring",
      "focus-visible:ring-inset",
      "aria-invalid:not-focus-visible:ring-destructive/20",
      "dark:aria-invalid:not-focus-visible:border-destructive/50",
      "ring-foreground/10",
      "hover:bg-destructive/20",
    ])
      expect(hidesFocus(name), name).toBeUndefined()
  })

  it("reads the components", () => {
    expect(Object.keys(sources)).toContain("./components/ui/button.tsx")
    expect(Object.keys(sources)).toContain("./index.css")
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })

  it("draws every focus ring and outline in the opaque --ring, and lets nothing replace it", () => {
    const found = Object.entries(sources).flatMap(([path, text]) => [
      ...text.split(/[\s"'`{}(),]+/).flatMap((name) => {
        const why = hidesFocus(name)
        return why ? [`${path}: ${name} (${why})`] : []
      }),
      // The grid's focus shadow was a color-mix() of --ring at 50%.
      ...(/color-mix\([^)]*var\(--ring\)\s+\d+%/.test(text)
        ? [`${path}: a color-mix() of --ring`]
        : []),
    ])
    expect(found).toEqual([])
  })
})
