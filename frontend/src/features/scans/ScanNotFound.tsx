import { Link } from "@tanstack/react-router"
import { FileQuestionIcon } from "lucide-react"

import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * The scan page for a scan the database does not have (the route's loader threw notFound()): which
 * scan, and the way to the scans and to importing them.
 */
export function ScanNotFound({ scanId }: { scanId: string }) {
  return (
    <section className="mx-auto flex max-w-lg flex-col items-center gap-3 py-16 text-center">
      <FileQuestionIcon className="size-10 text-muted-foreground" aria-hidden />
      <h1 className="text-xl font-semibold">Scan {scanId} not found</h1>
      <p className="text-muted-foreground">
        There is no scan{" "}
        <code className="font-mono text-foreground">{scanId}</code> in the
        database.
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        <Link
          to="/scans"
          className={cn(buttonVariants({ variant: "outline" }))}
        >
          Go to the scans
        </Link>
        <Link
          to="/scans/import"
          className={cn(buttonVariants({ variant: "outline" }))}
        >
          Import Scans
        </Link>
      </div>
    </section>
  )
}
