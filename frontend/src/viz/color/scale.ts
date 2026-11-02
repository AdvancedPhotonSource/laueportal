// The colour scales: how a value between the range's minimum and maximum becomes a position t in
// [0, 1] along the colormap. Square root and gamma are powers of the linear position within the
// range (matplotlib's PowerNorm), so they reshape the contrast inside the chosen window whatever the
// data's offset; log is the logarithm between the range's ends (LogNorm).

export const SCALE_KINDS = ["linear", "log", "sqrt", "gamma"] as const
export type ScaleKind = (typeof SCALE_KINDS)[number]

export const SCALE_LABELS: Record<ScaleKind, string> = {
  linear: "Linear",
  log: "Log",
  sqrt: "Square root",
  gamma: "Gamma",
}

export interface ColorScale {
  kind: ScaleKind
  /** The exponent of the gamma scale (t = linear^gamma); the other scales ignore it. */
  gamma: number
}

export const DEFAULT_GAMMA = 2
export const GAMMA_LIMITS = [0.05, 20] as const

/**
 * The value → t map for the range [lo, hi], clamped to [0, 1]; NaN stays NaN. The log scale needs
 * a positive minimum (the colorbar clamps it to the smallest positive value); without one it is
 * linear.
 */
export function normalizer(
  lo: number,
  hi: number,
  scale: ColorScale
): (value: number) => number {
  const clamp = (t: number) => (t <= 0 ? 0 : t >= 1 ? 1 : t)
  if (scale.kind === "log" && lo > 0 && hi > lo) {
    const a = Math.log(lo)
    const span = Math.log(hi) - a
    return (v) => (v > 0 ? clamp((Math.log(v) - a) / span) : v <= 0 ? 0 : NaN)
  }
  // Divided rather than multiplied by a reciprocal, so the maximum maps to exactly 1.
  const span = hi > lo ? hi - lo : Infinity
  const power = powerOf(scale)
  if (power === 1) return (v) => clamp((v - lo) / span)
  return (v) => clamp((v - lo) / span) ** power
}

/** The power a scale applies to the linear position (1 for linear and log). */
export function powerOf(scale: ColorScale): number {
  if (scale.kind === "sqrt") return 0.5
  if (scale.kind === "gamma") {
    const [min, max] = GAMMA_LIMITS
    return Number.isFinite(scale.gamma)
      ? Math.min(max, Math.max(min, scale.gamma))
      : DEFAULT_GAMMA
  }
  return 1
}

/** A colorbar's axis runs in log space for the log scale and linearly otherwise. */
export function toAxis(value: number, kind: ScaleKind): number {
  return kind === "log" ? Math.log10(value) : value
}

export function fromAxis(position: number, kind: ScaleKind): number {
  return kind === "log" ? 10 ** position : position
}
