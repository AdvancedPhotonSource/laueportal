import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  HeadContent,
  notFound,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, describe, expect, it, vi } from "vitest"

import { titleHead } from "./title"

// The document title comes only from route heads, rendered by HeadContent (as in the app shell),
// including when a loader sends the router to its not-found or error component.

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined
}

function makeRouter(path: string) {
  const rootRoute = createRootRoute({
    head: titleHead(),
    component: () => (
      <>
        <HeadContent />
        <Outlet />
      </>
    ),
  })
  const routes = [
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/scans",
      head: titleHead("Scans"),
      component: () => <p>scans</p>,
    }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/scans/$scanId",
      head: titleHead("Scan"),
      loader: () => {
        throw notFound()
      },
      component: () => <p>scan</p>,
    }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/broken",
      head: titleHead("Broken"),
      loader: () => {
        throw new Error("loader failed")
      },
      component: () => <p>broken</p>,
    }),
  ]
  return createRouter({
    routeTree: rootRoute.addChildren(routes),
    history: createMemoryHistory({ initialEntries: [path] }),
    defaultNotFoundComponent: () => <p>not found</p>,
    defaultErrorComponent: () => <p>error</p>,
  })
}

let root: Root | undefined

afterEach(() => {
  act(() => root?.unmount())
  root = undefined
  document.head.replaceChildren()
  document.body.replaceChildren()
  globalThis.IS_REACT_ACT_ENVIRONMENT = undefined
})

async function titleAt(path: string): Promise<string> {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined) // not in jsdom
  const container = document.createElement("div")
  document.body.append(container)
  const router = makeRouter(path)
  await act(async () => {
    root = createRoot(container)
    root.render(<RouterProvider router={router} />)
    await router.load()
  })
  await vi.waitFor(() => {
    expect(document.querySelector("title")).not.toBeNull()
  })
  return document.title
}

describe("route titles", () => {
  it("uses the page's head", async () => {
    expect(await titleAt("/scans")).toBe("Scans · 3DMN Portal")
  })

  it("titles a loader's notFound() as the 404 page", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined)
    expect(await titleAt("/scans/7")).toBe("Page not found · 3DMN Portal")
  })

  it("titles a loader error as an error page", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined)
    vi.spyOn(console, "warn").mockImplementation(() => undefined)
    expect(await titleAt("/broken")).toBe("Error · 3DMN Portal")
  })
})
