# Writing style

## Purpose

This guide defines the writing style of the Laue Portal repository. Use it when you write or review the docs (the
README, `AGENTS.md`, the architecture, the contributing conventions, the runbooks and the user docs), the text of the
UI, code comments, log and error messages, and commit messages. It adapts the writing guide of
[lauelab](https://github.com/advancedphotonsource/lauelab) to a web portal.

The goals are accuracy, direct instruction and consistent terminology. Clarity takes priority over brevity, but say
each thing once and link to it from elsewhere. Do not shorten a statement if the shorter form changes its meaning or
removes a required condition.

## Audience

The repository has three groups of readers:

- Scientists and beamline users read the user docs and the UI. They know X-ray science, Laue diffraction and the
  34-ID-E measurements, and they may be new to the portal. They need enough context to choose inputs, interpret a
  result and recognize a failure.
- Operators read the runbooks. They administer Linux hosts and services, and they may be new to the portal, DM,
  Globus and PBS. They need the exact commands, the expected result of each step and the way back.
- Contributors, human or agent, read `AGENTS.md`, the architecture and the contributing conventions. They know Python,
  TypeScript and web development, and they may be new to this code and to the beamline.

Do not teach crystallography, Python or React on the main path of a page. Define the portal's terms even when the
general concept is familiar. Link to deeper material when an explanation would interrupt the task at hand.

## Voice and tone

Address the reader as "you" when doing so makes an instruction clearer.

Use the imperative for procedures:

> After an API change, run `make gen` and commit the regenerated client.

Use direct descriptive prose for behaviour:

> `jobs.dispatch.claim_run` marks a queued run Running in one write transaction.

Use first-person plural only when it identifies a deliberate project decision; prefer a direct statement in most
cases. Do not use first-person singular.

Keep the tone technical and approachable. Do not use promotional claims, conversational filler or artificial
enthusiasm. A recommendation includes its operational or scientific reason:

> Leave `dm.cleanup_workflow` unset until the clean-up stage is deployed and tested on the remote side: when it is set,
> the clean-up also removes the remote files of the earlier portal's published `crux` runs that were migrated into the
> database.

Prefer active voice, but use passive voice when the actor is unknown or irrelevant:

> A missing file is reported as a state in the answer, never as a 500.

Vary sentence length naturally. Split a sentence when a reader must go back to parse it. Use sentence case for
headings.

### State facts plainly

Write what is stored where and what the code does. Do not give files, records, pages or results agency through
figurative verbs, and do not compress an explanation into a phrase that the reader must decode.

Only software that performs an operation can be the subject of an action verb, and the verb must be literal: a
function returns, reads, writes or raises; the worker claims a run; the API answers 503. Data does not act. For data,
use "contains", "is stored in", "is in", or the passive voice.

| Avoid | Write |
| --- | --- |
| A run's files are a contract. | The file names and keys of a run directory are a fixed format. |
| Job state has one door. | A job's status and phase change only through `jobs.state`. |
| The server trusts no path. | Every path from a request goes through `files.roots.resolve_inside_roots`. |
| The URL holds only coarse values. | The URL contains only coarse, non-default values. |
| The rule lives in `useRunSubmission`. | `useRunSubmission` implements the rule. |
| The observation rides in DM's record. | The observation is in DM's record of the call. |
| Links that last | Each viewer can copy a link to its current view. |
| Verification is evidence, not vibes. | A fix is done when a test of its failure case passes. |

Avoid these related patterns:

- Headings phrased as a teaser ("What it does"). Use a plain description of the content ("Features").
- A bold lead-in followed by a dash and a gloss ("**Scans** — browse and import"). Write a complete sentence; in a
  glossary, write "A **scan** is ...".
- Aphorisms and contrast for effect ("evidence, not vibes"). State the rule or the limitation directly.
- Noun stacks used as shorthand for a quantity. Name the quantity and its reduction: what is summed, over which
  pixels, at which depths.

This applies to code comments, log and error messages and UI text as well as to the docs.

## Terminology

Use one term for one concept. Do not rotate synonyms for variety. The [glossary](../../AGENTS.md#glossary) defines the
portal's terms; use them in these forms:

- Laue Portal (the project); `laueportal` (the repository, the Python package and the command); 3DMN Portal (only the
  title that the pages show); the earlier portal (the portal whose database the deploy runbook migrates)
- lauelab; LaueGo; X-ray; Laue diffraction
- scan, frame, point, step, pattern
- reconstruction (a wire run in the code), indexing run (a LaueGo run in the code), conversion
- job, status (Queued, Running, Finished, Failed, Cancelled), phase, run directory, execution target
- live run, live session, follower
- artifact, product, scope
- the API, the worker, the job process, turn, roots, the probe
- migrated run
- DM, DAQ, APS, ALCF, Crux, PBS, Globus
- a maintainer (the person who decides a change of a fixed format, a schema or a dependency)

Quote the names that the UI shows exactly as it shows them (Run Monitor, Move to Front, "Copy link to this view").
Use exact Python and TypeScript names in code formatting. Keep a precise software term ("idempotency key",
"operationId") when it carries its technical meaning.

## Units, numbers and notation

- Put a space between a number and its unit: 5 s, 1 MiB, 25 µm.
- State the unit of every value that has one, as the code defines it: a setting named `*_seconds` is in seconds, one
  named `*_mib` in MiB.
- Use a comma as the thousands separator: 4,096 bytes, 200,000 files.
- Put names of modules, functions, settings (`[files] probe_threads`), environment variables, files, paths and
  commands in code formatting. Write an HTTP status as a number (a 404, a 503) and a problem type in code formatting
  (`urn:laueportal:problem:not-responding`).
- Use tables for comparable entries (commands, settings, files). Use prose when entries need qualifications that a
  table would hide.

## Notes and warnings

Use warnings rarely. Write a warning when an apparently valid action can lose data, select the wrong data, show a
wrong result or cause a costly operation, such as two portals on one runs root writing the same run folders. Write it
as a block quote whose first sentence, in bold, states what to do or not to do; the next sentences give the reason and
the consequence. An invalid input that the portal refuses with a clear message needs no warning.

Write a note (context that helps interpretation but is not needed for the task) as ordinary prose in its section.
State a known limitation where the reader meets it, with what it affects and how to recover. A tip names a concrete
action and its benefit; do not use tips for encouragement.

In the user docs and the runbooks, include an implementation detail only when it explains observable behaviour, a
limit, performance or file compatibility. The architecture and the contributing conventions name modules, functions
and tests exactly, because their readers change the code.

## Guidance by text type

### README

State what the portal does, who it is for and where a reader begins. Keep details out of it and link to them. Do not
claim speed, robustness or quality without a measurement.

### User docs

Start from a task (import scans, run a live session). Use the names that the UI shows. Explain the consequences of a
choice, not only the accepted values, and include the mistakes that are plausible and consequential.

### Runbooks

Write numbered steps in the imperative: the prerequisites first, then one action per step with its exact command,
what the reader sees when the step worked, and how to undo it. Use strict structure for procedures, warnings, settings
requirements and error recovery: state a condition before its action, keep terms consistent, and use no idioms. These
are selected principles of Simplified Technical English (ASD-STE100); do not claim compliance with it.

### Architecture and contributing conventions

Keep descriptions exact and compact: module and function names, settings, limits with their units, and the test that
checks a rule. State each rule once and cite it from elsewhere. Code comments cite these pages by file and section name
(`docs/contributing/backend.md, "Long work"`), so rename a section only together with its citations.

### Troubleshooting and error documentation

State what failed, why it can fail and what the reader can check. Keep the uncertainty when several causes are
possible. Do not say that retrying is safe unless the behaviour supports it.

## UI text

- The UI shows labels, values, statuses and errors, not sentences about how it works.
- A control's hint is its tooltip (the `title`, or a form field's `x-hint`); a placeholder is at most a short value
  pattern (`Si_%d.h5`, `Automatic`).
- An empty state is one short line (a title, no description); a blocked state shows what is missing in one line.
- A confirm dialog states what its action does, in one sentence.
- A validation message is plain words without the field's label ("Required", "Must be a whole number"); the form's
  summary adds the label.
- One thing has one name on every page, and the docs use that name.

The components that implement these rules are in [the frontend's conventions](frontend.md#ui).

## Code comments and messages

- A comment explains why the code is as it is: the constraint, the failure it prevents, the format it must keep. It
  does not restate the code or tell the history of a change; the commit message records the change.
- A comment cites a rule by file and section: `(docs/contributing/backend.md, "Long work")`.
- A log line or an error message states what failed and what it concerns (the run, the setting's key, the file the
  user gave). It never contains a secret or a setting's value, and a problem never repeats a request's value whole
  (`core.text.quoted`).
- A message that users read uses the UI's names; a log line may use the code's names.
- A commit message has an imperative subject ("Show held runs in the Run Monitor") and one change per commit; its body
  states why. [`AGENTS.md`](../../AGENTS.md#docs) lists what a pull request contains.

## Examples

- A command in the docs runs as shown. Write a placeholder in angle brackets (`<run id>`) or as `/path/to/...`.
- Examples and screenshots use the synthetic demo data (scan 990001, the reconstructions R1-R3, the indexing runs I1
  and I2). Label measured data as measured.
- A home path in an example is `/home/user/...`: the path check that `make check` runs fails on any other.
- Examples contain no host names, accounts or credentials of a real deployment.

## Review checklist

Before a page, a UI text or a comment is complete, verify that:

- The text has a clear reader and outcome.
- The assumed background matches the audience above.
- The terms match the glossary, one term per concept.
- Every value has its unit, and every name in code formatting is exact.
- Claims follow from tests, measurements or the code's behaviour; a performance claim has a number.
- Recommendations include their reason and scope.
- Warnings identify a consequential and plausible mistake.
- Procedures are numbered steps in the imperative, with their checks and the way back.
- Links replace repetition, and every relative link resolves (`make check`).
- The text contains no promotional filler, chatbot phrasing or generic conclusion.
- Data, files, pages and records are not given figurative actions, and no heading or phrase needs decoding.
- No heading is a teaser, and no list item is a bold lead-in followed by a dash and a gloss.
- The UI text contains no sentences that explain how the UI works; hints are tooltips.
- Shortening has not removed a condition, a limit or an uncertainty.
