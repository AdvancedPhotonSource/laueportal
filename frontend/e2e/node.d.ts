// The Node built-ins the e2e helpers use (e2e/stack-tools.ts, e2e/cross-site.spec.ts, e2e/live.spec.ts). The
// frontend's TypeScript has the browser's types only, and @types/node is not a dependency, so only what is used is
// declared.
declare module "node:child_process" {
  export function execFileSync(
    file: string,
    args: readonly string[],
    options: {
      encoding: "utf8"
      timeout?: number
      stdio?: readonly ("ignore" | "pipe" | "inherit")[]
    }
  ): string
}

// e2e/cross-site.spec.ts serves a page of another origin (a page the test answers through page.route() is
// not on the loopback address, so Chrome would not let it reach the API at all).
declare module "node:http" {
  export interface IncomingMessage {
    url?: string
  }
  export interface ServerResponse {
    writeHead(status: number, headers: Record<string, string>): this
    end(body?: string): this
  }
  export interface Server {
    listen(port: number, host: string, listener: () => void): this
    once(event: "error", listener: (error: Error) => void): this
    close(callback?: (error?: Error) => void): this
    closeAllConnections(): void
  }
  export function createServer(
    listener: (request: IncomingMessage, response: ServerResponse) => void
  ): Server
}

// e2e/live.spec.ts saves the Live page's screenshots only when LIVE_SHOTS names a folder.
declare const process: { env: Record<string, string | undefined> }

// e2e/live.spec.ts stages a live wire run's files, and holds its follower with a point it appends to the wire run's
// completed.jsonl; its DAQ session writes the stand-in DM's state and reads its logs (e2e/stack-tools.ts).
declare module "node:fs" {
  export function mkdirSync(
    path: string,
    options?: { recursive?: boolean }
  ): string | undefined
  export function copyFileSync(source: string, destination: string): void
  export function appendFileSync(path: string, data: string): void
  export function readFileSync(path: string, encoding: "utf8"): string
  export function writeFileSync(path: string, data: string): void
  export function renameSync(source: string, destination: string): void
  export function existsSync(path: string): boolean
  export function statSync(path: string): { size: number }
}

declare module "node:path" {
  export function dirname(path: string): string
}
