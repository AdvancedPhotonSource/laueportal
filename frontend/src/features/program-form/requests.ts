// The kit's requests by program kind: validate and create. The body is checked against the API's own
// Zod schema (Orval) before it is sent; the create's Idempotency-Key goes through the request options,
// since the generated client has no header parameter.
import {
  createIndexingRun,
  CreateIndexingRunBodySchema,
  createReconstructionRun,
  CreateReconstructionRunBodySchema,
  type CreatedRun,
  type ProgramKind,
  validateIndexingRun,
  ValidateIndexingRunBodySchema,
  validateReconstructionRun,
  ValidateReconstructionRunBodySchema,
  type ValidationReport,
} from "@/api"
import type { RequestValues } from "./form-values"

export function validateProgram(
  kind: ProgramKind,
  body: RequestValues,
  signal?: AbortSignal
): Promise<ValidationReport> {
  return kind === "lauego"
    ? validateIndexingRun(ValidateIndexingRunBodySchema.parse(body), { signal })
    : validateReconstructionRun(
        ValidateReconstructionRunBodySchema.parse(body),
        {
          signal,
        }
      )
}

export function createProgramRun(
  kind: ProgramKind,
  body: RequestValues,
  idempotencyKey: string
): Promise<CreatedRun> {
  const options = { headers: { "Idempotency-Key": idempotencyKey } }
  return kind === "lauego"
    ? createIndexingRun(CreateIndexingRunBodySchema.parse(body), options)
    : createReconstructionRun(
        CreateReconstructionRunBodySchema.parse(body),
        options
      )
}
