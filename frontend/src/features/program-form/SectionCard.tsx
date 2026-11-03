import type { ComponentProps, ReactNode } from "react"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { sectionIdentity } from "./section-identity"
import { SectionIcon } from "./SectionIcon"

/**
 * A section's card: its accent stripe and its icon before the title (`sectionIdentity`), an optional control before
 * them (`lead`, the Live page's switch) and action after them (Restore Default), then its content. The create forms,
 * the Live page and the viewers' Parameters tabs draw their sections with it, so a section looks the same on each.
 * Without content it is its header alone, with no divider (a Live program card switched off).
 */
export function SectionCard({
  title,
  lead,
  action,
  contentClassName,
  className,
  children,
  ...props
}: Omit<ComponentProps<"div">, "title"> & {
  /** The section's title (`x-layout`'s group, a Live card's, a Parameters tab's group). */
  title: string
  lead?: ReactNode
  action?: ReactNode
  contentClassName?: string
}) {
  const open = drawn(children)
  return (
    <Card
      className={cn(sectionIdentity(title).stripe, !open && "pb-0", className)}
      {...props}
    >
      <CardHeader
        className={cn(
          drawn(lead) && "grid-cols-[auto_1fr] items-center gap-3",
          !open && "border-b-0"
        )}
      >
        {lead}
        <CardTitle className="flex items-center gap-2">
          <SectionIcon title={title} className="size-4.5" />
          <h2>{title}</h2>
        </CardTitle>
        {drawn(action) && <CardAction>{action}</CardAction>}
      </CardHeader>
      {open && (
        <CardContent className={contentClassName}>{children}</CardContent>
      )}
    </Card>
  )
}

/** Whether React draws anything for `node` (`cond && <X />` gives false when it does not). */
function drawn(node: ReactNode): boolean {
  return node !== undefined && node !== null && node !== false
}
