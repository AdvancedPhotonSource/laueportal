// GET /api/v1/programs as the backend serves it while live processing is on (captured from
// `jobs.programs.list_programs` on the dev config, the live programs with the batch ones' defaults; paths made
// neutral): the form kit's and the Live page's tests read the forms from it. The backend's
// tests/jobs/test_programs.py checks it against the served schema (every field, the layout and the required list;
// not the defaults, the dev config's), and program-spec.test.ts against the API's Zod schema of the stored LaueGo
// parameters; refresh it from the running API (`curl /api/v1/programs`) when the models change.
import type { ListedProgramKind, ProgramList } from "@/api"
import {
  type FieldSpec,
  isProgram,
  type ProgramOf,
  programSpec,
  type ProgramSpec,
} from "@/features/program-form/program-spec"
import programs from "./programs.json"

export const PROGRAMS = programs as ProgramList

export function programOf<K extends ListedProgramKind>(kind: K): ProgramOf<K> {
  const program = PROGRAMS.programs.find(isProgram(kind))
  if (!program) throw new Error(`no ${kind} program`)
  return program
}

export function specOf<K extends ListedProgramKind>(kind: K): ProgramSpec<K> {
  return programSpec(programOf(kind))
}

/** A form field of the program (a test's precondition: it throws for an unknown name). */
export function fieldOf(kind: ListedProgramKind, name: string): FieldSpec {
  const field = specOf(kind).fields[name]
  if (!field) throw new Error(`no ${kind} field ${name}`)
  return field
}

/** A fetch call's URL (a stubbed fetch's first argument). */
export function urlOf(input: RequestInfo | URL): URL {
  return new URL(
    input instanceof Request ? input.url : input.toString(),
    "http://localhost"
  )
}

/** A fetch call's JSON body. */
export function bodyOf(init: RequestInit | undefined): unknown {
  return typeof init?.body === "string" ? JSON.parse(init.body) : undefined
}
