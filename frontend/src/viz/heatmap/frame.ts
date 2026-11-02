// A detector or depth frame as the image endpoints send it: row-major little-endian `uint16`, or
// `float32` (a float source, or a reconstructed frame's signed values), with `X-Frame-Shape: H,W`
// and `X-Frame-Dtype`.

export type FrameData = Uint16Array | Float32Array

export interface Frame {
  /** Row-major: the pixel (x, y) is data[y × width + x]. */
  data: FrameData
  width: number
  height: number
}

const BYTES = { uint16: 2, float32: 4 } as const

/** The frame in a response body, read with its headers; throws when they disagree with the body. */
export function decodeFrame(
  body: ArrayBuffer,
  shape: string | null,
  dtype: string | null
): Frame {
  const [height, width, extra] = (shape ?? "")
    .split(",")
    .map((s) => Number(s.trim()))
  if (
    extra !== undefined ||
    !Number.isInteger(height) ||
    !Number.isInteger(width) ||
    !(height && height > 0 && width && width > 0)
  ) {
    throw new Error(`Unexpected frame shape "${shape ?? ""}"`)
  }
  if (dtype !== "uint16" && dtype !== "float32") {
    throw new Error(`Unexpected frame type "${dtype ?? ""}"`)
  }
  const expected = height * width * BYTES[dtype]
  if (body.byteLength !== expected) {
    throw new Error(
      `The frame has ${String(body.byteLength)} bytes; ${String(height)} × ${String(width)} ${dtype} needs ${String(expected)}`
    )
  }
  const data =
    dtype === "uint16" ? new Uint16Array(body) : new Float32Array(body)
  return { data, width, height }
}
