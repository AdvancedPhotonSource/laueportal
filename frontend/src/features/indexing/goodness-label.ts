// What the results viewer calls a pattern's goodness. LaueMatching's value is its quality, NMatches ×
// √Intensity: another quantity on another scale than LaueGo's goodness, so it has another name.
import { createContext, useContext } from "react"

export const GoodnessLabel = createContext("Goodness")

/** The name of a run's goodness, by its method. */
export function goodnessLabel(method: string | undefined): string {
  return method === "laue_matching" ? "Quality" : "Goodness"
}

export function useGoodnessLabel(): string {
  return useContext(GoodnessLabel)
}
