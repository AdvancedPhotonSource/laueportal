import { defineConfig } from "orval"

// `make gen` writes openapi.json from the backend, then runs Orval. The output in src/api/generated
// is committed; features import it only through src/api/index.ts.
const input = { target: "./openapi.json" }

export default defineConfig({
  api: {
    input,
    output: {
      target: "./src/api/generated/endpoints",
      schemas: "./src/api/generated/model",
      mode: "tags-split",
      client: "react-query",
      httpClient: "fetch",
      clean: true,
      indexFiles: true,
      override: {
        // Prefixes the base URL, sends JSON, and throws ApiError (problem+json) on non-2xx responses.
        mutator: { path: "./src/api/fetcher.ts", name: "apiFetch" },
        fetch: { includeHttpResponseReturnType: false },
        query: { signal: true },
      },
    },
  },
  zod: {
    input,
    output: {
      target: "./src/api/generated/zod",
      mode: "tags-split",
      client: "zod",
      fileExtension: ".zod.ts",
      clean: true,
      indexFiles: true,
    },
  },
})
