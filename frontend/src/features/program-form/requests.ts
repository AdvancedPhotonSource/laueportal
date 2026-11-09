// The kit's requests by program kind: validate and create. The body is checked against the API's own
// Zod schema (Orval) before it is sent; the create's Idempotency-Key goes through the request options,
// since the generated client has no header parameter.
import {
  createIndexingRun,
  CreateIndexingRunBodySchema,
  createLaueMatchingRun,
  CreateLaueMatchingRunBodySchema,
  createReconstructionRun,
  CreateReconstructionRunBodySchema,
  type CreatedRun,
  type ProgramKind,
  validateIndexingRun,
  ValidateIndexingRunBodySchema,
  validateLaueMatchingRun,
  ValidateLaueMatchingRunBodySchema,
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
  const options = { signal }
  switch (kind) {
    case "lauego":
      return validateIndexingRun(
        ValidateIndexingRunBodySchema.parse(body),
        options
      )
    case "laue_matching":
      return validateLaueMatchingRun(
        ValidateLaueMatchingRunBodySchema.parse(body),
        options
      )
    case "wire":
      return validateReconstructionRun(
        ValidateReconstructionRunBodySchema.parse(body),
        options
      )
  }
}

export function createProgramRun(
  kind: ProgramKind,
  body: RequestValues,
  idempotencyKey: string
): Promise<CreatedRun> {
  const options = { headers: { "Idempotency-Key": idempotencyKey } }
  switch (kind) {
    case "lauego":
      return createIndexingRun(CreateIndexingRunBodySchema.parse(body), options)
    case "laue_matching":
      return createLaueMatchingRun(
        CreateLaueMatchingRunBodySchema.parse(body),
        options
      )
    case "wire":
      return createReconstructionRun(
        CreateReconstructionRunBodySchema.parse(body),
        options
      )
  }
}
