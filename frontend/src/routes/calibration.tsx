import { createFileRoute } from "@tanstack/react-router"

import { CalibrationPage } from "@/features/calibration/CalibrationPage"
import { titleHead } from "@/lib/title"

export const Route = createFileRoute("/calibration")({
  head: titleHead("Calibration"),
  component: CalibrationPage,
})
