// The LaueMatching form's Forward Cache: the one that fits is chosen, none makes it a new one with the
// suggested name, and a problem with the data folder is shown as one line.
import { useEffect } from "react"
import { useForm } from "react-hook-form"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { cleanup, render } from "@/test/render"
import { ForwardCacheField } from "./ForwardCacheField"
import type { FieldSpec } from "./program-spec"

const answer = vi.hoisted(() => ({
  data: undefined as
    | {
        caches: string[]
        suggested_name: string | null
        problem: string | null
      }
    | undefined,
}))

vi.mock("@/api", async (original) => ({
  ...(await original<typeof import("@/api")>()),
  useListForwardCaches: () => ({ data: answer.data }),
}))

const field = {
  name: "forward_cache",
  title: "Forward Cache",
  widget: "forward-cache",
} as unknown as FieldSpec

let values: () => Record<string, unknown>

function Form({ start = {} }: { start?: Record<string, unknown> }) {
  const form = useForm({
    defaultValues: {
      forward_cache: "",
      create_forward_cache: false,
      geometry_file: "/g.xml",
      crystal_file: "/c.xml",
      elo: "5",
      ehi: "30",
      max_nr_laue_spots: "30",
      ...start,
    },
  })
  useEffect(() => {
    values = () => form.getValues()
  }, [form])
  return <ForwardCacheField field={field} control={form.control as never} />
}

const newName = () =>
  document.querySelector<HTMLInputElement>('[aria-label="New cache name"]')

describe("ForwardCacheField", () => {
  beforeEach(() => {
    answer.data = undefined
  })
  afterEach(cleanup)

  it("chooses the one cache that fits", async () => {
    answer.data = { caches: ["Ni"], suggested_name: "x", problem: null }
    await render(<Form />)
    await vi.waitFor(() => {
      expect(values()).toMatchObject({
        forward_cache: "Ni",
        create_forward_cache: false,
      })
    })
    expect(newName()).toBeNull()
  })

  it("makes a new cache with the suggested name when none fits", async () => {
    answer.data = { caches: [], suggested_name: "Ni_5-30", problem: null }
    await render(<Form />)
    await vi.waitFor(() => {
      expect(values()).toMatchObject({
        forward_cache: "Ni_5-30",
        create_forward_cache: true,
      })
    })
    await vi.waitFor(() => {
      expect(newName()?.value).toBe("Ni_5-30")
    })
  })

  it("leaves several caches for the user to choose", async () => {
    answer.data = { caches: ["a", "b"], suggested_name: "x", problem: null }
    await render(<Form />)
    expect(values()).toMatchObject({
      forward_cache: "",
      create_forward_cache: false,
    })
  })

  it("keeps a cloned run's cache and shows a problem as one line", async () => {
    answer.data = { caches: [], suggested_name: null, problem: "not set here" }
    await render(<Form start={{ forward_cache: "cloned" }} />)
    await vi.waitFor(() => {
      expect(document.body.textContent).toContain("not set here")
    })
    expect(values()).toMatchObject({
      forward_cache: "cloned",
      create_forward_cache: false,
    })
  })
})
