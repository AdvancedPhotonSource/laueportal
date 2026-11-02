import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router"
import { act } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { cleanup, render } from "@/test/render"
import { parseSearch, stringifySearch } from "./search"
import { useViewSearch, VIEW_SEARCH_DELAY_MS } from "./view-search"

// useViewSearch on a route of its own: `tab` is a history key, `color` is not.

interface Search {
  tab?: string
  color?: string
}

let change: (patch: Partial<Search>) => void = () => undefined

/** The page at `url`; `path` is its route's (`/runs/$id`: one component for every run). */
async function openPage(url: string, path = "/") {
  const root = createRootRoute()
  const page = createRoute({
    getParentRoute: () => root,
    path,
    validateSearch: (search: Record<string, unknown>): Search => ({
      tab: typeof search.tab === "string" ? search.tab : undefined,
      color: typeof search.color === "string" ? search.color : undefined,
    }),
    component: function Page() {
      const navigate = page.useNavigate()
      const [search, setSearch] = useViewSearch<Search>({
        search: page.useSearch(),
        write: (next, replace) => {
          void navigate({ search: next, replace })
        },
        historyKeys: ["tab"],
      })
      change = setSearch
      return <output>{`${search.tab ?? "-"} ${search.color ?? "-"}`}</output>
    },
  })
  const history = createMemoryHistory({ initialEntries: [url] })
  const router = createRouter({
    routeTree: root.addChildren([page]),
    history,
    parseSearch,
    stringifySearch,
  })
  await render(<RouterProvider router={router} />)
  await act(() => router.load())
  return { router, history }
}

const shown = () => document.querySelector("output")?.textContent
const url = (router: { state: { location: { href: string } } }) =>
  router.state.location.href

beforeEach(() => {
  // The router restores the scroll position, which jsdom does not implement.
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

describe("the view's URL", () => {
  it("shows a change at once and writes it after a pause: a history key pushes, another value replaces", async () => {
    const { router, history } = await openPage("/")
    act(() => {
      change({ tab: "map" })
      change({ color: "goodness" })
    })
    expect(shown()).toBe("map goodness")
    expect(url(router)).toBe("/")
    await vi.waitFor(() => {
      expect(url(router)).toBe("/?tab=map&color=goodness")
    })
    expect(history.length).toBe(2)

    act(() => {
      change({ color: undefined })
    })
    await vi.waitFor(() => {
      expect(url(router)).toBe("/?tab=map")
    })
    expect(history.length).toBe(2)
  })

  it("changes only the history keys on back and forward", async () => {
    const { router } = await openPage("/")
    act(() => {
      change({ tab: "map" })
    })
    await vi.waitFor(() => {
      expect(url(router)).toBe("/?tab=map")
    })
    act(() => {
      change({ color: "goodness" })
    })
    await vi.waitFor(() => {
      expect(url(router)).toBe("/?tab=map&color=goodness")
    })

    act(() => {
      router.history.back()
    })
    await vi.waitFor(() => {
      expect(url(router)).toBe("/?color=goodness")
    })
    expect(shown()).toBe("- goodness")
    act(() => {
      router.history.forward()
    })
    await vi.waitFor(() => {
      expect(url(router)).toBe("/?tab=map&color=goodness")
    })
  })

  it("writes a change 300 ms after the last one", async () => {
    const { router } = await openPage("/")
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] })
    act(() => {
      change({ color: "goodness" })
    })
    act(() => {
      vi.advanceTimersByTime(VIEW_SEARCH_DELAY_MS - 100)
      change({ tab: "map" })
      vi.advanceTimersByTime(VIEW_SEARCH_DELAY_MS - 1)
    })
    await act(() => Promise.resolve())
    expect(url(router)).toBe("/")
    expect(VIEW_SEARCH_DELAY_MS).toBe(300)
    act(() => {
      vi.advanceTimersByTime(1)
    })
    await vi.waitFor(() => {
      expect(url(router)).toBe("/?tab=map&color=goodness")
    })
  })

  it("takes a navigation after a back that changed nothing as the page's own", async () => {
    const { router } = await openPage("/?tab=map")
    // Another entry with the same URL: going back to the first changes neither the path nor the search.
    act(() => {
      router.history.push("/?tab=map")
    })
    act(() => {
      router.history.back()
    })
    await act(() =>
      router.navigate({
        to: "/",
        search: { tab: "map", color: "goodness" },
        replace: true,
      })
    )
    await act(() => Promise.resolve())
    expect(url(router)).toBe("/?tab=map&color=goodness")
    expect(shown()).toBe("map goodness")
  })

  it("keeps each path's values to itself (another run on the same route)", async () => {
    const { router } = await openPage("/runs/1?color=goodness", "/runs/$id")
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] })
    // A change not yet written belongs to the run left: it is not written on the next one.
    act(() => {
      change({ color: "red" })
    })
    act(() => {
      router.history.push("/runs/70")
    })
    await vi.waitFor(() => {
      expect(url(router)).toBe("/runs/70")
    })
    act(() => {
      vi.advanceTimersByTime(VIEW_SEARCH_DELAY_MS)
    })
    await act(() => Promise.resolve())
    expect(url(router)).toBe("/runs/70")
    expect(shown()).toBe("- -")

    // Back to the first run: its own values, not the second run's.
    act(() => {
      router.history.back()
    })
    await vi.waitFor(() => {
      expect(shown()).toBe("- goodness")
    })
    await act(() => Promise.resolve())
    expect(url(router)).toBe("/runs/1?color=goodness")
  })
})
