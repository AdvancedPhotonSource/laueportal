// The one Node built-in the unit tests use: src/focus-contrast.test.ts reads src/index.css (Vitest empties every
// CSS import, `?raw` ones too), and src/features/grid-contract.test.ts reads openapi.json and lists src. The
// frontend's TypeScript has the browser's types only, and @types/node is not a dependency, so only what is used is
// declared (as e2e/node.d.ts does for the e2e helpers).
declare module "node:fs" {
  export function readFileSync(path: URL, encoding: "utf8"): string
  export function readdirSync(path: URL, options: { recursive: true }): string[]
}
