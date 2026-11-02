import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router"
import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api"
import { byRole, cleanup, click, render } from "@/test/render"
import { RouteError } from "./RouteError"

afterEach(cleanup)

// Each route match has its own boundary (the router's defaultErrorComponent), so a page that fails
// leaves the shell around it in place, and Retry renders the page again.

// The page fails while this is set (React renders a failing page more than once before giving up).
const failing = { current: true }

async function renderFailingPage() {
  vi.spyOn(console, "error").mockImplementation(() => undefined)
  vi.spyOn(console, "warn").mockImplementation(() => undefined)
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const rootRoute = createRootRoute({
    component: () => (
      <>
        <nav aria-label="Main">navbar</nav>
        <Outlet />
      </>
    ),
  })
  const page = createRoute({
    getParentRoute: () => rootRoute,
    path: "/scans",
    component: function ScansPage() {
      if (failing.current) {
        throw new ApiError(503, "Service Unavailable", {
          title: "Service Unavailable",
          status: 503,
          detail: "database is locked",
        })
      }
      return <p>the scans</p>
    },
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([page]),
    history: createMemoryHistory({ initialEntries: ["/scans"] }),
    defaultErrorComponent: RouteError,
  })
  await render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
  await act(() => router.load())
}

describe("RouteError", () => {
  it("shows the problem in the page and keeps the shell", async () => {
    failing.current = true
    await renderFailingPage()
    await vi.waitFor(() => {
      expect(byRole("alert")).toHaveLength(1)
    })
    const [alert] = byRole("alert")
    expect(byRole("heading", "Service Unavailable", alert)).toHaveLength(1)
    expect(alert?.textContent).toContain("database is locked")
    expect(document.querySelector('nav[aria-label="Main"]')).not.toBeNull()
  })

  it("renders the page again on Retry", async () => {
    failing.current = true
    await renderFailingPage()
    await vi.waitFor(() => {
      expect(byRole("button", "Retry")).toHaveLength(1)
    })
    failing.current = false
    await click(byRole("button", "Retry")[0])
    await vi.waitFor(() => {
      expect(document.body.textContent).toContain("the scans")
    })
    expect(byRole("alert")).toHaveLength(0)
  })
})
