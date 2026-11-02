import vtkRenderer from "@kitware/vtk.js/Rendering/Core/Renderer"
import { describe, expect, it } from "vitest"

import { pickPoint, projectPoints } from "./pick"

// World to clip, row-major: x and y pass through, z = -z_world / 10 (so larger z_world is nearer),
// and w = 1 + z_world / 10 for a perspective divide.
const PERSPECTIVE = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, -0.1, 0, 0, 0, 0.1, 1]
// Orthographic, the identity: NDC = world.
const IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]

const all = (n: number) => Int32Array.from({ length: n }, (_, i) => i)

describe("projectPoints", () => {
  it("projects through the camera matrix to CSS pixels from the top left, dropping clipped points", () => {
    const xyz = [
      [0, 0, 0], // the centre
      [0.5, 0.5, 0], // upper right quarter
      [0.5, 0.5, 5], // the same, nearer: w = 1.5 shrinks it towards the centre
      [0, 0, -20], // behind the camera (w < 0)
      [0, 0, -9.5], // outside the depth range (NDC z = 0.95 / 0.05)
      [9, 0, 0], // far off-screen
    ].flat()
    const screen = projectPoints(xyz, all(6), PERSPECTIVE, 200, 100)
    expect(Array.from(screen.ids)).toEqual([0, 1, 2])
    expect(screen.sx[0]).toBeCloseTo(100)
    expect(screen.sy[0]).toBeCloseTo(50)
    expect(screen.sx[1]).toBeCloseTo(150)
    expect(screen.sy[1]).toBeCloseTo(25)
    expect(screen.sx[2]).toBeCloseTo(100 + 50 / 1.5)
    expect(screen.sy[2]).toBeCloseTo(50 - 25 / 1.5)
    expect(screen.sz[2]).toBeLessThan(screen.sz[1] as number)
  })

  // The scene passes VTK.js's camera matrix as `m`, so this pins VTK.js's convention (a VTK.js update
  // that changed it would make the hover pick the wrong point): getCompositeProjectionMatrix(aspect,
  // -1, 1) is world to clip, row-major; VTK.js's display y runs up, ours down; nearer is smaller z.
  it("matches VTK.js's own world-to-display transform, in perspective and parallel projection", () => {
    const width = 900
    const height = 400
    const points = [
      [0, 0, 0],
      [1, -1, 0.3],
      [-2, 0.5, 1],
      [0.3, 0.2, -0.7],
      [0.5, 0.5, 0.5], // towards the camera from the origin
    ]
    for (const parallel of [false, true]) {
      const renderer = vtkRenderer.newInstance()
      const camera = renderer.getActiveCamera()
      camera.setParallelProjection(parallel)
      camera.setFocalPoint(0, 0, 0)
      camera.setPosition(1, 1, 1)
      camera.setViewUp(0, 0, 1)
      renderer.resetCamera([-2, 2, -2, 2, -2, 2])
      const screen = projectPoints(
        points.flat(),
        all(points.length),
        camera.getCompositeProjectionMatrix(width / height, -1, 1),
        width,
        height
      )
      expect(Array.from(screen.ids)).toEqual([0, 1, 2, 3, 4])
      points.forEach(([x = 0, y = 0, z = 0], k) => {
        const [nx = NaN, ny = NaN] = renderer.worldToNormalizedDisplay(
          x,
          y,
          z,
          width / height
        )
        expect(screen.sx[k]).toBeCloseTo(nx * width, 2)
        expect(screen.sy[k]).toBeCloseTo((1 - ny) * height, 2)
      })
      expect(screen.sz[4]).toBeLessThan(screen.sz[0] as number)
      renderer.delete()
    }
  })
})

describe("pickPoint", () => {
  it("picks the front-most marker under the cursor (the selection first), else the nearest centre within the radius", () => {
    // Screen 100 × 100: NDC (u, v) lands at ((u + 1) * 50, (1 - v) * 50).
    const xyz = [
      [0, 0, 0.5], // A at (50, 50), behind B
      [0.08, 0, -0.5], // B at (54, 50), in front
      [-0.5, 0, 0], // C at (25, 50), alone
      [0.5, 0.5, 0], // D at (75, 25): not drawn
    ].flat()
    const screen = projectPoints(xyz, all(3), IDENTITY, 100, 100)
    const half = 4
    const radius = 6
    // Both markers cover (51, 50); B is nearer the viewer although A's centre is closer.
    expect(pickPoint(screen, 51, 50, half, radius)).toBe(1)
    // Only A covers (47, 50).
    expect(pickPoint(screen, 47, 50, half, radius)).toBe(0)
    // No marker covers (25, 55.5); C's centre is 5.5 px away.
    expect(pickPoint(screen, 25, 55.5, half, radius)).toBe(2)
    expect(pickPoint(screen, 25, 57, half, radius)).toBe(-1)
    // A point drawn on top (the selection) wins under the cursor, even behind.
    const top = Uint8Array.of(1, 0, 0, 0)
    expect(pickPoint(screen, 51, 50, half, radius, top)).toBe(0)
    // A point that is not drawn is never picked.
    expect(pickPoint(screen, 75, 25, half, radius)).toBe(-1)
  })

  it("finds what a scan of every point finds, across the grid's cells and edges", () => {
    let seed = 7
    const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647
    const n = 3000
    const xyz = Array.from({ length: 3 * n }, () => rand() * 2.2 - 1.1)
    const width = 640
    const height = 360
    const screen = projectPoints(xyz, all(n), IDENTITY, width, height, 24)
    const brute = (x: number, y: number, half: number, radius: number) => {
      let best = -1
      let bestZ = Infinity
      for (let s = 0; s < screen.count; s++) {
        const dx = (screen.sx[s] as number) - x
        const dy = (screen.sy[s] as number) - y
        if (
          Math.abs(dx) <= half &&
          Math.abs(dy) <= half &&
          (screen.sz[s] as number) < bestZ
        ) {
          best = s
          bestZ = screen.sz[s] as number
        }
      }
      if (best >= 0) return screen.ids[best] as number
      let nearest = -1
      let nearestD = radius * radius
      let nearestZ = Infinity
      for (let s = 0; s < screen.count; s++) {
        const dx = (screen.sx[s] as number) - x
        const dy = (screen.sy[s] as number) - y
        const d = dx * dx + dy * dy
        const z = screen.sz[s] as number
        if (d < nearestD || (d === nearestD && z < nearestZ)) {
          nearest = s
          nearestD = d
          nearestZ = z
        }
      }
      return nearest >= 0 ? (screen.ids[nearest] as number) : -1
    }
    for (let q = 0; q < 400; q++) {
      const x = rand() * width
      const y = rand() * height
      const half = q % 2 ? 2 : 20
      expect(pickPoint(screen, x, y, half, 30)).toBe(brute(x, y, half, 30))
    }
  })
})
