import { Link, useLocation } from "@tanstack/react-router"
import { FileQuestionIcon } from "lucide-react"

import { buttonVariants } from "@/components/ui/button"
import { NOT_FOUND_PAGE } from "@/lib/title"
import { cn } from "@/lib/utils"

export function NotFound() {
  const { pathname } = useLocation()
  return (
    <section className="mx-auto flex max-w-lg flex-col items-center gap-3 py-16 text-center">
      <FileQuestionIcon className="size-10 text-muted-foreground" aria-hidden />
      <h1 className="text-xl font-semibold">{NOT_FOUND_PAGE}</h1>
      <p className="text-muted-foreground">
        There is no page at{" "}
        <code className="font-mono text-foreground">{pathname}</code>.
      </p>
      <Link to="/" className={cn(buttonVariants({ variant: "outline" }))}>
        Go to the status page
      </Link>
    </section>
  )
}
