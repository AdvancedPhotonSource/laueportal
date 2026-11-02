// Runtime WebGL detection for the 3D view. VTK.js renders with WebGL 2 only. Without it (no GPU
// and no software fallback: Chrome 139 and later drop SwiftShader) the view shows a message
// instead, and the 2D views keep working.

/** What the probe needs from a canvas (structural, so tests can pass a stand-in). */
export interface WebGLProbeCanvas {
  getContext(kind: "webgl2"): {
    getExtension(name: "WEBGL_lose_context"): { loseContext(): void } | null
  } | null
}

/** Whether the canvas gives a WebGL 2 context; the probe's context is released at once. */
export function canCreateWebGL2(canvas: WebGLProbeCanvas): boolean {
  try {
    const gl = canvas.getContext("webgl2")
    if (!gl) return false
    // Browsers cap live contexts (about 16 in Chrome): don't keep the probe's.
    gl.getExtension("WEBGL_lose_context")?.loseContext()
    return true
  } catch {
    return false
  }
}

let available: boolean | undefined

/** Whether this browser and machine provide WebGL 2 (probed once per page load). */
export function webGLAvailable(): boolean {
  available ??=
    typeof document !== "undefined" &&
    canCreateWebGL2(document.createElement("canvas"))
  return available
}
