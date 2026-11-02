import { createFileRoute } from "@tanstack/react-router"

import { LoadingState } from "@/components/LoadingState"
import { loadJob, parseJobId } from "@/features/jobs/job-page"
import { JobNotFound } from "@/features/jobs/JobNotFound"
import { JobPage } from "@/features/jobs/JobPage"
import { titleHeadFor } from "@/lib/title"

// A job. An unknown job is the route's not-found page.
export const Route = createFileRoute("/jobs/$jobId")({
  loader: ({ context, params }) => loadJob(context.queryClient, params.jobId),
  head: ({ match, params }) => titleHeadFor(match, `Job ${params.jobId}`),
  component: JobRoute,
  pendingComponent: () => <LoadingState label="Loading the job" />,
  notFoundComponent: JobRouteNotFound,
})

function JobRouteNotFound() {
  const { jobId } = Route.useParams()
  return <JobNotFound jobId={jobId} />
}

function JobRoute() {
  const { jobId } = Route.useParams()
  // The loader let only a job ID through.
  const job = parseJobId(jobId) ?? 0
  // Another job is another page (its failures page, its dialog).
  return <JobPage key={job} jobId={job} />
}
