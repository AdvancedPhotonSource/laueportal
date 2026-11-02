import { Link } from "@tanstack/react-router"
import { FileQuestionIcon } from "lucide-react"

import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * The wire viewer for a run the database does not have (the route's loader threw notFound()): which
 * run, and the way to the reconstructions.
 */
export function ReconstructionNotFound({
  reconstructionId,
}: {
  reconstructionId: string
}) {
  return (
    <section className="mx-auto flex max-w-lg flex-col items-center gap-3 py-16 text-center">
      <FileQuestionIcon className="size-10 text-muted-foreground" aria-hidden />
      <h1 className="text-xl font-semibold">
        Reconstruction R{reconstructionId} not found
      </h1>
      <Link
        to="/reconstructions"
        className={cn(buttonVariants({ variant: "outline" }))}
      >
        Go to the reconstructions
      </Link>
    </section>
  )
}
