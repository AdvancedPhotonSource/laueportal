import { useState } from "react"

import { apiUrl, getGetIndexingLegendUrl, LegendColor } from "@/api"

/** The corner labels of the IPF triangle (the stereographic 001-101-111 region). */
const IPF_LABELS = [
  {
    text: "001",
    className: "left-0 bottom-0 -translate-x-1/4 translate-y-full",
  },
  {
    text: "101",
    className: "right-0 bottom-0 translate-x-1/4 translate-y-full",
  },
  { text: "111", className: "left-[88.4%] top-[11.6%] -translate-y-full" },
]

/** The axis labels around the HSV wheel (the diagonals just outside the circle). */
const HSV_LABELS = [
  { text: "+X", className: "left-full top-1/2 -translate-y-1/2 pl-0.5" },
  { text: "−X", className: "right-full top-1/2 -translate-y-1/2 pr-0.5" },
  { text: "+Y", className: "left-[4%] top-[4%] -translate-1/2" },
  { text: "−Y", className: "left-[96%] top-[96%] -translate-1/2" },
  { text: "+Z", className: "left-[4%] top-[96%] -translate-1/2" },
  { text: "−Z", className: "left-[96%] top-[4%] -translate-1/2" },
]

/**
 * The key of an orientation colouring: the IPF triangle or the HSV wheel from the legend endpoint
 * (the browser revalidates the image by its ETag), with the labels and caption drawn by the page.
 */
export function MapLegend({
  indexingId,
  legend,
  caption,
}: {
  indexingId: number
  legend: LegendColor
  caption: string
}) {
  const src = apiUrl(getGetIndexingLegendUrl(indexingId, { color: legend }))
  const [failed, setFailed] = useState<string | null>(null)
  const labels = legend === LegendColor.cubic_ipf ? IPF_LABELS : HSV_LABELS
  return (
    <figure className="flex flex-col items-center gap-5 py-1">
      {failed === src ? (
        <p role="alert" className="text-xs text-destructive">
          The colour key could not be loaded.
        </p>
      ) : (
        <div className="relative mx-6 size-32">
          <img
            src={src}
            alt={
              legend === LegendColor.cubic_ipf
                ? "Cubic IPF colour key"
                : "Stereographic HSV colour key"
            }
            className="size-full"
            onError={() => {
              setFailed(src)
            }}
          />
          {labels.map((label) => (
            <span
              key={label.text}
              aria-hidden
              className={`absolute font-mono text-[11px] text-muted-foreground ${label.className}`}
            >
              {label.text}
            </span>
          ))}
        </div>
      )}
      <figcaption className="text-center text-xs text-muted-foreground">
        {caption}
      </figcaption>
    </figure>
  )
}
