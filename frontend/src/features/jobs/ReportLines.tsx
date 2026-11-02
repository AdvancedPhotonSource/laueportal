import type { OutcomeLine } from "./run-monitor"

/** A toast's lines about the jobs an action applied to: one paragraph, or a list for several. */
export function ReportLines({ lines }: { lines: readonly OutcomeLine[] }) {
  if (lines.length === 1) {
    const [line] = lines
    return line ? (
      <p data-job={line.jobId} data-tone={line.tone}>
        {line.text}
      </p>
    ) : null
  }
  return (
    <ul className="flex list-disc flex-col gap-1 pl-4">
      {lines.map((line) => (
        <li key={line.jobId} data-job={line.jobId} data-tone={line.tone}>
          {line.text}
        </li>
      ))}
    </ul>
  )
}
