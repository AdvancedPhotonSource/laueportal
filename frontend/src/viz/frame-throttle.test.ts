import { describe, expect, it } from "vitest"

import { frameThrottle } from "./frame-throttle"

describe("frameThrottle", () => {
  it("runs once per frame with the latest argument, and cancel drops the pending run", () => {
    const frames = new Map<number, () => void>()
    let nextId = 1
    const request = (callback: () => void) => {
      frames.set(nextId, callback)
      return nextId++
    }
    const flush = () => {
      const due = [...frames.values()]
      frames.clear()
      for (const callback of due) callback()
    }
    const runs: number[] = []
    const throttle = frameThrottle<number>(
      (arg) => runs.push(arg),
      request,
      (id) => frames.delete(id)
    )

    throttle.schedule(1)
    throttle.schedule(2)
    throttle.schedule(3)
    expect(frames.size).toBe(1)
    flush()
    expect(runs).toEqual([3])

    throttle.schedule(4)
    flush()
    expect(runs).toEqual([3, 4])

    throttle.schedule(5)
    throttle.cancel()
    flush()
    expect(runs).toEqual([3, 4])
    throttle.schedule(6)
    flush()
    expect(runs).toEqual([3, 4, 6])
  })
})
