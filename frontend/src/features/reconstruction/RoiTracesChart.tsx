import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { formatSignificant } from "@/lib/format"
import type { TraceChart } from "./roi-chart"

const tick = (value: unknown) =>
  typeof value === "number" ? formatSignificant(value) : String(value)

/**
 * The ROI traces (Recharts through shadcn's chart): one line per ROI in its colour; the axes,
 * ticks and tooltip text in the theme's text colours.
 */
export function RoiTracesChart({ chart }: { chart: TraceChart }) {
  const config: ChartConfig = Object.fromEntries(
    chart.lines.map((line) => [
      line.key,
      { label: line.label, color: line.color },
    ])
  )
  const log = chart.y.scale === "log"
  return (
    <ChartContainer
      config={config}
      className="aspect-auto h-72 w-full"
      aria-label="ROI traces"
      role="img"
    >
      <LineChart
        data={chart.rows}
        margin={{ top: 8, right: 16, bottom: 24, left: 16 }}
      >
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey={chart.x.key}
          type="number"
          domain={["dataMin", "dataMax"]}
          tickFormatter={tick}
          tickLine={false}
          label={{
            value: chart.x.label,
            position: "insideBottom",
            offset: -16,
            className: "fill-muted-foreground",
          }}
        />
        <YAxis
          key={chart.y.scale}
          scale={log ? "log" : "linear"}
          domain={log ? ["dataMin", "dataMax"] : [0, "auto"]}
          allowDataOverflow={log}
          tickFormatter={tick}
          tickLine={false}
          width={64}
          label={{
            value: chart.y.label,
            angle: -90,
            position: "insideLeft",
            offset: -8,
            style: { textAnchor: "middle" },
            className: "fill-muted-foreground",
          }}
        />
        <ChartTooltip
          content={
            <ChartTooltipContent
              labelFormatter={(_, payload) => {
                const row = payload[0]?.payload as
                  Record<string, number> | undefined
                return row ? `${chart.x.label}: ${tick(row[chart.x.key])}` : ""
              }}
            />
          }
        />
        {chart.lines.map((line) => (
          <Line
            key={line.key}
            dataKey={line.key}
            name={line.label}
            stroke={`var(--color-${line.key})`}
            strokeWidth={2}
            dot={false}
            connectNulls={false}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ChartContainer>
  )
}
