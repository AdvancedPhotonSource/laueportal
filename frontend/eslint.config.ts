import { defineConfig, globalIgnores } from "eslint/config"
import reactHooks from "eslint-plugin-react-hooks"
import tseslint from "typescript-eslint"

const rootDir = decodeURIComponent(new URL(".", import.meta.url).pathname)

const reactHooksRecommended = reactHooks.configs.flat["recommended-latest"]
// `eslint .` (the make check gate) exits 0 on warnings, so the react-hooks rules that ship as
// warnings (exhaustive-deps, incompatible-library, unsupported-syntax) are raised to errors.
const reactHooksAsErrors = Object.fromEntries(
  Object.keys(reactHooksRecommended.rules).map((rule) => [rule, "error"])
)

export default defineConfig(
  // Generated code is not linted (it still typechecks).
  globalIgnores([
    "dist/",
    "src/api/generated/",
    "src/routeTree.gen.ts",
    "public/",
    "test-results/",
    "playwright-report/",
    "blob-report/",
  ]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [tseslint.configs.strictTypeChecked, reactHooksRecommended],
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: rootDir },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      ...reactHooksAsErrors,
      // A leading underscore marks an intentionally unused name.
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
      // Loaders and beforeLoad throw TanStack Router's notFound() and redirect() objects by design.
      "@typescript-eslint/only-throw-error": [
        "error",
        {
          allow: [
            {
              from: "package",
              package: "@tanstack/router-core",
              name: ["NotFoundError", "Redirect"],
            },
          ],
        },
      ],
      // Server data comes only through src/api/index.ts (docs/contributing/frontend.md): import "@/api" (or the
      // folder by a relative path, "../api"), never a module inside it. The regex catches relative
      // paths made of ./ and ../ segments, such as "../../api/fetcher" or "../src/api/generated/...";
      // it would also flag another folder named "api" reached that way, so don't name one so.
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["@/api/*"],
              message: 'Import the API client from "@/api" only.',
            },
            {
              regex: String.raw`^(?:\.{1,2}/)+(?:src/)?api/(?!index(?:\.tsx?)?$).+`,
              message: 'Import the API client from "@/api" only.',
            },
          ],
        },
      ],
    },
  },
  {
    files: ["src/api/**"],
    rules: { "no-restricted-imports": "off" },
  }
)
