import { HeadContent, Outlet } from "@tanstack/react-router"

import { Navbar } from "./Navbar"

/** The layout every page shares: navbar on top, the page below. */
export function AppShell() {
  return (
    <>
      <HeadContent />
      <div className="flex min-h-svh flex-col">
        <Navbar />
        <main className="flex-1 px-4 py-4">
          <Outlet />
        </main>
      </div>
    </>
  )
}
