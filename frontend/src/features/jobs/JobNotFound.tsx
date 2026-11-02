import { Link } from "@tanstack/react-router"
import { FileQuestionIcon } from "lucide-react"

import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * The job page for a job the database does not have (the route's loader threw notFound()): which
 * job, and the way to the run monitor.
 */
export function JobNotFound({ jobId }: { jobId: string }) {
  return (
    <section className="mx-auto flex max-w-lg flex-col items-center gap-3 py-16 text-center">
      <FileQuestionIcon className="size-10 text-muted-foreground" aria-hidden />
      <h1 className="text-xl font-semibold">Job {jobId} not found</h1>
      <p className="text-muted-foreground">
        There is no job{" "}
        <code className="font-mono text-foreground">{jobId}</code> in the
        database.
      </p>
      <Link to="/runs" className={cn(buttonVariants({ variant: "outline" }))}>
        Go to the run monitor
      </Link>
    </section>
  )
}
