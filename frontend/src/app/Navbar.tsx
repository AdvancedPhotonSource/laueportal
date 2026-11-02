import { Link, useMatchRoute } from "@tanstack/react-router"
import { ChevronDownIcon } from "lucide-react"

import portalIcon from "@/assets/portal_icon.svg"
import { Button, buttonVariants } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLinkItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { useLiveEnabled } from "@/features/live/use-live"
import { cn } from "@/lib/utils"
import { HealthIndicators } from "./HealthIndicators"
import { ThemeToggle } from "./ThemeToggle"

// The main links. "Live" (after Run Monitor) shows only while live processing is on (GET /programs lists the
// live programs); below 1024 px it is the Menu's first item, so the Worker label still fits down to 768 px.
const NAV_LINKS = [
  { to: "/scans", label: "Scans" },
  { to: "/reconstructions", label: "Reconstructions" },
  { to: "/indexing", label: "Indexations" },
  { to: "/runs", label: "Run Monitor" },
] as const

const LIVE_LINK = { to: "/live", label: "Live" } as const

const MENU_LINKS = [
  { to: "/scans/import", label: "Import Scans" },
  { to: "/reconstructions/new", label: "New Wire Reconstruction" },
  { to: "/indexing/new", label: "New LaueGo Indexation" },
] as const

const navLinkClass = cn(
  buttonVariants({ variant: "ghost" }),
  "text-muted-foreground data-[section=active]:bg-muted data-[section=active]:text-foreground"
)

/**
 * A main link, highlighted on its page and on the pages below it (/scans/7, /scans/import). Screen readers
 * hear aria-current="page" on the page itself (the router sets it, matching exactly) and aria-current="true"
 * (the current section) below it.
 */
function SectionLink({
  to,
  label,
  className,
}: ((typeof NAV_LINKS)[number] | typeof LIVE_LINK) & { className?: string }) {
  const matchRoute = useMatchRoute()
  const inSection = Boolean(matchRoute({ to, fuzzy: true }))
  return (
    <Link
      to={to}
      activeOptions={{ exact: true }}
      aria-current={inSection ? "true" : undefined}
      data-section={inSection ? "active" : undefined}
      className={cn(navLinkClass, className)}
    >
      {label}
    </Link>
  )
}

export function Navbar() {
  const live = useLiveEnabled()
  return (
    <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-backdrop-filter:bg-background/80">
      <div className="flex h-12 items-center gap-2 px-4">
        {/* The brand, the links and the theme toggle keep their size; the indicator gives way. */}
        <Link
          to="/"
          className="flex shrink-0 items-center gap-2 text-base font-semibold whitespace-nowrap"
        >
          <img src={portalIcon} alt="" aria-hidden className="size-7" />
          3DMN Portal
        </Link>
        {/* The Worker indicator, live (polled). */}
        <HealthIndicators />
        <nav
          aria-label="Main"
          className="ml-auto flex shrink-0 items-center gap-1"
        >
          {NAV_LINKS.map((link) => (
            <SectionLink key={link.to} {...link} />
          ))}
          {live && (
            <SectionLink {...LIVE_LINK} className="hidden lg:inline-flex" />
          )}
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button variant="ghost" className="text-muted-foreground" />
              }
            >
              Menu
              <ChevronDownIcon data-icon="inline-end" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              {live && (
                <DropdownMenuLinkItem
                  className="lg:hidden"
                  render={<Link to={LIVE_LINK.to} />}
                >
                  {LIVE_LINK.label}
                </DropdownMenuLinkItem>
              )}
              {MENU_LINKS.map((link) => (
                <DropdownMenuLinkItem
                  key={link.to}
                  render={<Link to={link.to} />}
                >
                  {link.label}
                </DropdownMenuLinkItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </nav>
        <ThemeToggle />
      </div>
    </header>
  )
}
