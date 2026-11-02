// Test helpers for components (jsdom): no testing library is installed, so this mounts with
// react-dom and act, as React's own docs do. Call cleanup() in afterEach.
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
  type RouterHistory,
} from "@tanstack/react-router"
import { act, type ReactNode } from "react"
import { createRoot, type Root } from "react-dom/client"
import { vi } from "vitest"

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined
}

const roots: Root[] = []

/** Mount `ui` in a fresh container in document.body and flush its effects. */
export async function render(ui: ReactNode): Promise<HTMLElement> {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true
  const container = document.createElement("div")
  document.body.append(container)
  const root = createRoot(container)
  roots.push(root)
  await act(async () => {
    root.render(ui)
    await Promise.resolve()
  })
  return container
}

/**
 * Mount `ui` as the only page of a minimal router at "/", for components with links: a link
 * resolves its href from its route and params, whether or not this router has that route.
 * Pass `history` to navigate in the test (e.g. away from the page, to its unsaved-changes guard).
 */
export async function renderWithRouter(
  ui: ReactNode,
  history: RouterHistory = createMemoryHistory({ initialEntries: ["/"] })
): Promise<HTMLElement> {
  const router = createRouter({
    routeTree: createRootRoute({ component: () => ui }),
    history,
  })
  const container = await render(<RouterProvider router={router} />)
  await act(() => router.load())
  return container
}

export function cleanup(): void {
  for (const root of roots.splice(0)) {
    act(() => {
      root.unmount()
    })
  }
  document.body.replaceChildren()
  document.head.replaceChildren()
  globalThis.IS_REACT_ACT_ENVIRONMENT = undefined
}

/** Click an element inside act, so React flushes the updates it causes. */
export async function click(element: Element | null | undefined) {
  if (!(element instanceof HTMLElement)) throw new Error("nothing to click")
  await act(async () => {
    element.click()
    await Promise.resolve()
  })
}

/**
 * Enter text in a React input or textarea as a user does: the value goes through the prototype's
 * setter (React's own tracker would hide the change) and an input event follows, inside act.
 */
export async function enterText(
  element: Element | null | undefined,
  value: string
) {
  if (!(
    element instanceof HTMLInputElement ||
    element instanceof HTMLTextAreaElement
  )) {
    throw new Error("nothing to type into")
  }
  const prototype = Object.getPrototypeOf(element) as object
  await act(async () => {
    // The prototype's setter, called on the element (`this` is the receiver).
    Reflect.set(prototype, "value", value, element)
    element.dispatchEvent(new Event("input", { bubbles: true }))
    await Promise.resolve()
  })
}

/** Elements by role and accessible text (aria-label, else text content), like getByRole. */
export function byRole(
  role: string,
  name?: string | RegExp,
  root: ParentNode = document.body
): HTMLElement[] {
  const implicit: Record<string, string> = {
    button: "button",
    link: "a[href]",
    heading: "h1,h2,h3,h4,h5,h6",
    alert: "",
    status: "",
    alertdialog: "",
  }
  const selector = [`[role="${role}"]`, implicit[role]]
    .filter(Boolean)
    .join(",")
  const elements = Array.from(root.querySelectorAll<HTMLElement>(selector))
  if (name === undefined) return elements
  return elements.filter((element) => {
    const text = (
      element.getAttribute("aria-label") ?? element.textContent
    ).trim()
    return typeof name === "string" ? text === name : name.test(text)
  })
}

/**
 * A test's own timeout, for a test with a wait that may last longer than vitest's default 5 s test
 * timeout (AG Grid's first rows under a parallel run, busy retries): otherwise its wait's own ceiling
 * never takes effect and a slow start ends it as a test timeout.
 */
export const SLOW_TEST = { timeout: 20_000 }

/** jsdom has no matchMedia: answer `prefers-color-scheme: dark` with `dark`, everything else false. */
export function stubMatchMedia(dark = false) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const state = { dark }
  vi.stubGlobal("matchMedia", (query: string) => ({
    get matches() {
      return query.includes("prefers-color-scheme: dark") ? state.dark : false
    },
    media: query,
    onchange: null,
    addEventListener: (
      _type: string,
      listener: (event: MediaQueryListEvent) => void
    ) => listeners.add(listener),
    removeEventListener: (
      _type: string,
      listener: (event: MediaQueryListEvent) => void
    ) => listeners.delete(listener),
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }))
  return {
    /** Change the OS preference and notify the listeners. */
    setDark(next: boolean) {
      state.dark = next
      for (const listener of listeners) {
        listener({ matches: next } as MediaQueryListEvent)
      }
    },
  }
}
