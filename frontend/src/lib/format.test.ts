import { afterEach, describe, expect, it, vi } from "vitest"

import {
  formatAge,
  formatCount,
  formatDateTime,
  formatDuration,
  formatSignificant,
} from "./format"

afterEach(() => {
  vi.unstubAllEnvs()
})

describe("formatDateTime", () => {
  it("shows a UTC timestamp in local time as YYYY-MM-DD HH:MM:SS", () => {
    vi.stubEnv("TZ", "America/Chicago")
    // 06:30 UTC is 00:30 in Chicago in January (UTC-6) and 01:30 in July (UTC-5).
    expect(formatDateTime("2026-01-15T06:30:05Z")).toBe("2026-01-15 00:30:05")
    expect(formatDateTime("2026-07-15T06:30:05Z")).toBe("2026-07-15 01:30:05")
    // Before 06:00 UTC it is still the previous day there.
    expect(formatDateTime("2026-03-01T03:04:05.678Z")).toBe(
      "2026-02-28 21:04:05"
    )
  })

  it("uses the browser's time zone", () => {
    vi.stubEnv("TZ", "Asia/Kolkata")
    expect(formatDateTime("2026-01-15T20:00:00Z")).toBe("2026-01-16 01:30:00")
    vi.stubEnv("TZ", "UTC")
    expect(formatDateTime("2026-01-15T20:00:00Z")).toBe("2026-01-15 20:00:00")
  })

  it("is 24-hour at midnight", () => {
    vi.stubEnv("TZ", "UTC")
    expect(formatDateTime("2026-05-01T00:00:00Z")).toBe("2026-05-01 00:00:00")
  })

  it("shows nothing for no value and a non-date as it is", () => {
    expect(formatDateTime(null)).toBe("")
    expect(formatDateTime(undefined)).toBe("")
    expect(formatDateTime("")).toBe("")
    expect(formatDateTime("not a date")).toBe("not a date")
  })
})

describe("formatDuration", () => {
  it("formats seconds as HH:MM:SS", () => {
    expect(formatDuration(0)).toBe("00:00:00")
    expect(formatDuration(59)).toBe("00:00:59")
    expect(formatDuration(3661)).toBe("01:01:01")
    expect(formatDuration(86_399)).toBe("23:59:59")
  })

  it("keeps counting hours past 99 and drops fractions of a second", () => {
    expect(formatDuration(360_000)).toBe("100:00:00")
    expect(formatDuration(61.999)).toBe("00:01:01")
  })

  it('adds "(running)" while the run is active', () => {
    expect(formatDuration(3600, true)).toBe("01:00:00 (running)")
    expect(formatDuration(3600, false)).toBe("01:00:00")
  })

  it("shows nothing before the run starts, and never a negative time", () => {
    expect(formatDuration(null)).toBe("")
    expect(formatDuration(undefined, true)).toBe("")
    expect(formatDuration(Number.NaN)).toBe("")
    expect(formatDuration(-5)).toBe("00:00:00")
  })
})

describe("formatAge", () => {
  it("shows an age in its two largest units", () => {
    expect(formatAge(0)).toBe("0 s")
    expect(formatAge(42.9)).toBe("42 s")
    expect(formatAge(60)).toBe("1 min")
    expect(formatAge(200)).toBe("3 min 20 s")
    expect(formatAge(7200)).toBe("2 h")
    expect(formatAge(7500)).toBe("2 h 5 min")
    expect(formatAge(3 * 86_400 + 4 * 3600 + 59)).toBe("3 d 4 h")
  })

  it("shows nothing without an age, and no negative age", () => {
    expect(formatAge(null)).toBe("")
    expect(formatAge(undefined)).toBe("")
    expect(formatAge(Number.NaN)).toBe("")
    expect(formatAge(-3)).toBe("0 s")
  })
})

describe("formatSignificant", () => {
  it("keeps five significant digits and an integer's zeros, and drops only trailing decimal zeros", () => {
    expect(
      [12000, 10000, -10230, 123456, 12350.2, 0.000012, 1.5, 0, Number.NaN].map(
        formatSignificant
      )
    ).toEqual([
      "12000",
      "10000",
      "-10230",
      "123460",
      "12350",
      "0.000012",
      "1.5",
      "0",
      "—",
    ])
  })
})

describe("formatCount", () => {
  it("groups thousands with commas", () => {
    expect([0, 999, 35743, 550773, 1234567].map(formatCount)).toEqual([
      "0",
      "999",
      "35,743",
      "550,773",
      "1,234,567",
    ])
  })
})
