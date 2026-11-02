import { Link } from "@tanstack/react-router"
import { FileQuestionIcon } from "lucide-react"

import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * The indexing viewer for a run the database does not have (the route's loader threw notFound()):
 * which run, and the way to the indexations.
 */
export function IndexingNotFound({ indexingId }: { indexingId: string }) {
  return (
    <section className="mx-auto flex max-w-lg flex-col items-center gap-3 py-16 text-center">
      <FileQuestionIcon className="size-10 text-muted-foreground" aria-hidden />
      <h1 className="text-xl font-semibold">
        Indexing I{indexingId} not found
      </h1>
      <p className="text-muted-foreground">
        There is no indexing run{" "}
        <code className="font-mono text-foreground">{indexingId}</code> in the
        database.
      </p>
      <Link
        to="/indexing"
        className={cn(buttonVariants({ variant: "outline" }))}
      >
        Go to the indexations
      </Link>
    </section>
  )
}
