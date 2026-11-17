# Assessment Synthesis Durability Plan

## Implementation status

Implemented in workflow code version 12:

- deterministic manifest compilation and conservative privilege-key canonicalization;
- deterministic topic assignments;
- bounded topic, signal, dimension, and narrative model calls with per-unit schemas and token budgets;
- durable attempt/unit records with resumable failed-unit retries;
- `READY`, `READY_WITH_WARNINGS`, and `ACTION_REQUIRED` assessment dispositions;
- synthesis-only recovery for completed and failed assessments without document reanalysis;
- Review Program gating while required units remain unresolved;
- synthesis-unit diagnostics in the API and assessment UI; and
- an offline manifest replay command at `scripts/replay_assessment_synthesis.py`.

The first implementation deliberately reuses the pinned `assessment_synthesis` managed skill with stage-specific
schemas and instruction layers. This preserves tenant skill overrides and avoids introducing four new binding roles
in the same migration. Unit records are the durable recovery boundary; a DBOS retry resumes them by manifest hash.
If operational evidence later supports parallel execution, the same unit records can be dispatched as child
workflows without changing the public result or recovery contract.

## Purpose

Make Matter Definition assessment synthesis reliable, explainable, and recoverable without weakening Review Guidance or rerunning completed document analyses.

The immediate target is the privilege assessment that successfully analyzed 239 of 250 documents but ended as `FAILED` after the synthesis step exhausted three DBOS attempts. The same design must continue to support previously successful 500- and 1,000-document assessments.

## Evidence and root cause

The failed privilege assessment and a prior successful 1,000-document assessment used the same:

- assessment synthesis skill version;
- output schema;
- `google:gemini-3.5-flash-lite` model;
- 24,000 cumulative output-token budget; and
- DBOS synthesis retry policy.

The successful 1,000-document assessment supplied approximately 3.43 million serialized input characters. The failed privilege assessment supplied approximately 878,000. Document count and raw synthesis input size are therefore not the primary cause.

The privilege analyses produced semantically overlapping criterion keys:

- `ATTORNEY_CLIENT_PRIVILEGE`;
- `LEGAL_ADVICE_OR_LEGAL_SERVICES`;
- `ATTORNEY_WORK_PRODUCT`;
- `WORK_PRODUCT`; and
- `WORK_PRODUCT_PROTECTION`.

The current `_synthesis_context` treated those values as five independent recurring signals. `_validate_synthesis_refinement` then required an evidence-backed clarification question for every signal. The model omitted several questions, full-output validation retries followed, and one attempt accumulated 25,449 output tokens against the application limit of 24,000.

The three outer DBOS attempts ended with:

1. structurally invalid evidence and missing signal questions;
2. cumulative output-budget exhaustion during validation repair; and
3. missing questions for three recurring signals.

The operator saw only the DBOS retry-exhaustion wrapper. The underlying attempt errors remained in DBOS storage and were not represented in the assessment API.

## Problems to solve

1. Document analysis can emit aliases instead of canonical Review Guidance criterion keys.
2. Recurring near misses are mechanically converted into required questions even when the guidance may already be sufficient.
3. One model call must generate topics, assignments, six refinement dimensions, questions, evidence, and narrative.
4. Validation retries regenerate the full result and consume one cumulative output budget.
5. Successful intermediate work is not durably addressable or independently retryable.
6. A synthesis-only failure marks the entire assessment `FAILED`, obscuring the useful document analyses.
7. `COMPLETED_WITH_ERRORS` currently has no separate readiness meaning and can automatically advance a Review Program.
8. The API and UI expose a generic error instead of the failing unit, cause, impact, and recommended action.
9. A failed synthesis cannot use the existing reuse-analyses regeneration path because that endpoint accepts only completed assessments.

## Goals

- Keep the published Review Guidance unchanged during recovery and replay.
- Reuse immutable successful document-analysis artifacts.
- Canonicalize all synthesis inputs against the pinned retrieval plan and guidance revision.
- Build the final synthesis from small, independently validated units.
- Persist every unit and its error before an outer workflow retry.
- Retry or repair only the invalid unit.
- Preserve valid partial results when another unit cannot complete.
- Distinguish execution outcome from whether the operator may safely continue.
- Prevent Review Program auto-advancement when operator action is required.
- Give the operator concise diagnostics and audited recovery actions.
- Prove the design against the exact failed privilege inputs and prior successful large assessments.

## Non-goals

- Do not simplify legally meaningful Review Guidance to accommodate model behavior.
- Do not increase the 24,000-token limit as the primary fix.
- Do not reanalyze documents merely because synthesis failed.
- Do not add a conversational Coach before deterministic diagnostics and actions exist.
- Do not silently fuzzy-map an ambiguous criterion to a legal issue.
- Do not make PgBouncer or worker concurrency changes part of this work.

## Proposed architecture

### 1. Compile a deterministic synthesis manifest

Replace direct use of `_synthesis_context` with a compiler that produces a versioned `assessment_synthesis_manifest_v1`.

The manifest contains:

- assessment, guidance revision, and binding identifiers;
- coverage and corpus statistics;
- the canonical criteria from the pinned retrieval plan;
- canonical document determinations and criterion matches;
- deterministic document-to-topic assignments;
- recurring near-miss signal groups;
- document clarification candidates grouped by canonical criterion;
- bounded representative evidence for each topic and signal;
- unmapped or ambiguous criterion keys as diagnostics; and
- the required synthesis-unit inventory.

The manifest is immutable for a synthesis attempt and stored as a workflow artifact. Its content hash becomes part of every unit cache identity.

### 2. Canonicalize criterion keys conservatively

Build an alias index from each pinned retrieval-plan criterion:

- exact key;
- case-folded and punctuation-normalized key;
- normalized criterion label; and
- explicit aliases recorded by the compiler version.

Apply mappings only when they resolve to exactly one canonical criterion. Record ambiguous or unknown values as `UNMAPPED` diagnostics instead of silently guessing.

For new document analyses, pass the allowed canonical criteria in the dynamic input and validate returned keys before accepting the document result. A targeted document-output repair may replace an alias with one allowed key. The synthesis compiler remains responsible for legacy artifacts.

For the privilege replay, the initial explicit aliases should collapse:

- `LEGAL_ADVICE_OR_LEGAL_SERVICES` into `ATTORNEY_CLIENT_PRIVILEGE`; and
- `WORK_PRODUCT` and `WORK_PRODUCT_PROTECTION` into `ATTORNEY_WORK_PRODUCT`.

Aliases belong to a versioned canonicalization policy, not to the legal guidance text.

### 3. Derive assignments in application code

Do not ask the synthesis model to recreate every document-to-topic assignment. The document analyses already contain criterion matches, confidence, evidence, and determination.

Build assignments deterministically from validated `PRIMARY` and `SECONDARY` matches. Keep the synthesis model responsible for concise topic findings, limitations, and cross-document interpretation.

This removes a large and error-prone portion of the current output schema and prevents malformed document or paragraph identifiers from invalidating the entire synthesis.

### 4. Replace one-shot synthesis with durable units

Create the following unit types:

| Unit | Cardinality | Responsibility |
| --- | ---: | --- |
| `TOPIC_FINDING` | One per canonical criterion | Cross-document finding, boundary observations, limitations, and selected evidence |
| `SIGNAL_DISPOSITION` | One per canonical recurring signal or clarification cluster | Decide whether a question is warranted and, when needed, generate one question with evidence and suggested answers |
| `REFINEMENT_DIMENSION` | Six | Evaluate one required refinement dimension with a conclusion, rationale, and evidence |
| `FINAL_NARRATIVE` | One | Produce a concise narrative from validated units and deterministic statistics |

The application assembles the existing public synthesis shape from validated unit results plus deterministic assignments.

No LLM planning call is required. The deterministic manifest is the plan.

### 5. Require a disposition, not a question, for every signal

Each recurring signal must be accounted for with one of:

- `QUESTION_PROPOSED`;
- `GUIDANCE_SUFFICIENT`; or
- `INSUFFICIENT_EVIDENCE`.

Every disposition requires a rationale and valid evidence. Only `QUESTION_PROPOSED` produces a clarification question.

This preserves strict coverage while avoiding the current false assumption that every recurring near miss proves a defect in Review Guidance.

### 6. Use focused managed skills

Add pinned managed-skill roles with small output schemas:

- `assessment_topic_synthesis`;
- `assessment_signal_resolution`;
- `assessment_dimension_evaluation`; and
- `assessment_narrative_synthesis`.

Each role receives only the relevant manifest slice. Stable guidance and schema prefixes remain cacheable. Each role has an explicit request and output-token budget sized to its bounded result.

Retain the current `assessment_synthesis` skill for legacy workflow history and rollback during rollout. New assessments pin the v2 role set in their binding snapshot.

### 7. Persist attempts and units

Add two persistence models.

`matter_definition_assessment_synthesis_attempt`:

- `id`;
- `assessment_run_id`;
- `manifest_artifact_id` and content hash;
- compiler and workflow versions;
- status;
- completion disposition;
- total, completed, and failed unit counts;
- assembled result and diagnostics;
- request/token totals; and
- start/completion timestamps.

`matter_definition_assessment_synthesis_unit`:

- `id` and `synthesis_attempt_id`;
- unit type, key, and ordinal;
- status and attempt count;
- input hash;
- output artifact and `skill_run` references;
- typed error code and operator-safe error message;
- technical error details;
- request/token totals; and
- start/completion timestamps.

Use a uniqueness constraint on `(synthesis_attempt_id, unit_type, unit_key)` so DBOS recovery remains idempotent.

Store model outputs as immutable workflow artifacts. Store only orchestration state, summaries, and references in PostgreSQL.

### 8. Make DBOS retries unit-scoped

Use stable child workflow IDs:

`definition-assessment:{assessment_id}:synthesis:{attempt_id}:unit:{unit_type}:{unit_key}`

Topic, signal, and dimension units can run concurrently with a bounded queue concurrency. Final narrative runs only after required upstream units reach a terminal state.

Each unit:

1. loads its manifest slice;
2. records `RUNNING` in a short transaction;
3. performs the model request without holding a database transaction;
4. validates the result;
5. stores its immutable artifact and terminal record in a new transaction; and
6. returns a typed result to the parent workflow.

If model execution fails, persist the underlying error in a separate transaction before re-raising for DBOS retry. Do not depend on the rolled-back `skill_run` transaction or the DBOS operation table for operator diagnostics.

### 9. Repair only the invalid section

For schema-valid but semantically invalid output, give the same unit agent:

- its current output;
- a structured list of validation failures;
- the allowed evidence and identifiers; and
- an instruction to return one complete replacement for that unit.

Do not resend unrelated topics or request a complete assessment result. Bound repair attempts separately from provider-transient retries.

Typed error codes should include:

- `OUTPUT_VALIDATION_FAILED`;
- `OUTPUT_BUDGET_EXCEEDED`;
- `EVIDENCE_REFERENCE_INVALID`;
- `PROVIDER_TRANSIENT`;
- `PROVIDER_FATAL`;
- `MANIFEST_INVALID`; and
- `INTERNAL_ERROR`.

### 10. Assemble and validate deterministically

Final assembly must:

- include every canonical topic exactly once;
- attach deterministic assignments;
- include every signal disposition exactly once;
- include questions only for `QUESTION_PROPOSED` signals;
- include all six refinement dimensions exactly once;
- verify document and paragraph identifiers against the source analyses;
- compute the overall refinement outcome from validated units;
- include coverage and corpus statistics; and
- record omitted or failed optional units in diagnostics.

The final narrative summarizes this already validated aggregate. A narrative failure must not discard topic, signal, dimension, or assignment results.

## Terminal states and readiness

Keep workflow execution status and operator readiness separate.

Add `completion_disposition` to the assessment API:

- `READY`: synthesis is complete and no material warnings remain;
- `READY_WITH_WARNINGS`: synthesis is usable, but document failures, partial coverage, or non-blocking synthesis warnings exist; or
- `ACTION_REQUIRED`: useful work exists, but one or more required synthesis units are unresolved.

Recommended status mapping:

| Condition | Assessment status | Disposition |
| --- | --- | --- |
| All required work complete, no warnings | `COMPLETED` | `READY` |
| Complete synthesis with document or non-blocking warnings | `COMPLETED_WITH_ERRORS` | `READY_WITH_WARNINGS` |
| Document analyses retained but required synthesis units unresolved | `COMPLETED_WITH_ERRORS` | `ACTION_REQUIRED` |
| No usable assessment state due to planning, retrieval, manifest, or persistence failure | `FAILED` | `ACTION_REQUIRED` |
| User cancellation | `CANCELED` | `ACTION_REQUIRED` |

Do not infer readiness solely from `COMPLETED_WITH_ERRORS`.

## Review Program behavior

Update `project_review_program_assessment_terminal`:

- `READY` may complete and advance the stage;
- `READY_WITH_WARNINGS` may complete and advance while preserving visible warnings; and
- `ACTION_REQUIRED` must place the Review Program in `WAITING_FOR_USER` instead of publishing `STAGE_COMPLETED`.

For `ACTION_REQUIRED`, create an existing Review Program action with audited choices:

- retry failed synthesis units;
- continue with partial results when the minimum safe result is present; or
- cancel the run.

The Continue action must state exactly what is missing and must never be the default selection.

## Recovery of existing assessments

Allow reuse-analyses synthesis regeneration when:

- the assessment is `FAILED` or `COMPLETED_WITH_ERRORS`;
- at least one successful document analysis exists; and
- failure occurred in or after synthesis.

Starting v2 synthesis must create a new synthesis attempt without deleting prior DBOS, skill-run, or artifact history. On success, replace the assessment's current assembled synthesis result and retain prior attempt diagnostics.

The failed privilege assessment should therefore be recoverable without rerunning its 239 successful analyses or changing its Review Guidance revision.

## API changes

Extend `MatterDefinitionAssessmentRead` with:

- `completion_disposition`;
- `current_synthesis_attempt_id`;
- `synthesis_status`;
- `synthesis_diagnostics_summary`; and
- counts for total, completed, failed, and retryable synthesis units.

Add endpoints to:

- list synthesis attempts;
- list unit status and operator-safe diagnostics;
- retry failed or selected units;
- regenerate v2 synthesis from existing analyses; and
- retrieve the compiled manifest summary without exposing unnecessary document content.

Keep raw provider responses and sensitive full prompts out of normal API responses.

## Operator experience

Replace the generic failure banner with an Assessment Health panel showing:

- execution status and readiness disposition;
- completed document analyses and document failures;
- synthesis units completed, failed, and pending;
- the actual typed failure category;
- impact on the assessment result;
- whether continuing is safe; and
- the recommended next action.

Use an action menu rather than a row of buttons:

- Retry failed synthesis units;
- Reuse analyses and regenerate all synthesis units;
- Review failed documents;
- Continue with partial assessment, when permitted; and
- Reanalyze frozen documents.

The first Coach implementation should be deterministic. It translates structured diagnostics into concise guidance and links to available actions. A conversational Agent/Coach can be added later using the same diagnostics and audited action APIs; it must not invent recovery actions or execute consequential actions without confirmation.

## Testing strategy

### Replay harness

Add a read-only synthesis replay command that:

- loads an assessment's persisted successful analysis artifacts;
- compiles a v2 manifest;
- reports canonicalization and signal-group changes;
- optionally runs v2 synthesis as a shadow attempt; and
- never mutates the selected assessment unless an explicit apply option is used.

Do not place production document contents in the repository. CI uses synthetic fixtures that reproduce the alias and signal structure. The local replay uses authorized stored artifacts.

### Unit tests

- Canonical aliases collapse to one pinned criterion.
- Ambiguous aliases remain unmapped and produce diagnostics.
- Assignments are derived deterministically from document matches.
- Repeated near misses require a disposition, not automatically a question.
- Every signal is accounted for exactly once.
- Evidence IDs and paragraph IDs are verified.
- A failed repair does not alter previously completed units.
- Final assembly is stable regardless of unit completion order.
- Readiness is derived independently from execution status.

### Workflow and integration tests

- Topic, signal, and dimension units recover idempotently under DBOS replay.
- Provider-transient failures retry only the affected unit.
- Validation failures invoke targeted repair only for the affected unit.
- Output-budget exhaustion fails one unit and preserves other results.
- A final-narrative failure produces retained structured results and `ACTION_REQUIRED` or a non-blocking warning according to policy.
- Failure details survive transaction rollback and appear in the API.
- A failed synthesis can be regenerated from existing analyses.
- Review Programs do not auto-advance on `ACTION_REQUIRED`.
- An explicit Continue action is audited.
- Concurrent unit completion does not reintroduce workflow-step deadlocks.

### Regression tests

- Replay the failed 239-analysis privilege assessment with its Review Guidance unchanged.
- Replay at least one prior successful 500-document assessment.
- Replay the prior successful 994-analysis assessment.
- Compare canonical topic coverage, evidence validity, clarification questions, and refinement outcome with the legacy result.

### Failure injection

Inject:

- a missing signal disposition;
- an invalid document ID;
- an invalid paragraph ID;
- a malformed structured response;
- a unit output-token overrun;
- provider timeout and rate limit;
- process interruption after artifact storage but before unit completion; and
- process interruption after all units but before final assembly.

Each test must demonstrate idempotent recovery or a terminal operator-action state with preserved work.

## Acceptance criteria

The change is ready to enable when:

1. The failed privilege inputs complete five consecutive shadow replays without changing Review Guidance or rerunning documents.
2. The five legacy privilege signal aliases collapse into the intended canonical criteria, with ambiguous values reported rather than guessed.
3. No validation failure causes regeneration of already valid units.
4. Every final evidence reference resolves to an allowed document and paragraph.
5. A deliberately failed unit can be retried independently and the final result then assembled.
6. An unrecoverable required-unit failure ends as `COMPLETED_WITH_ERRORS` plus `ACTION_REQUIRED`, with the underlying error and recommended action visible.
7. Review Programs do not advance when disposition is `ACTION_REQUIRED`.
8. Existing successful large assessments retain complete topic and refinement coverage under replay.
9. Backend tests, generated API checks, frontend tests, and migration tests pass.
10. Token and request metrics are recorded per unit and for the complete attempt.

## Implementation sequence

### Pull request 1: replay, manifest, and canonicalization

- Add the read-only replay command.
- Add `assessment_synthesis_manifest_v1` and its compiler.
- Add conservative criterion canonicalization and diagnostics.
- Move document assignments into deterministic application logic.
- Add synthetic regression fixtures for the privilege aliases.
- Demonstrate manifest output for the failed privilege assessment and prior successful assessments.

This PR changes no production synthesis path.

### Pull request 2: persistence and unit skills

- Add synthesis attempt and unit migrations/models.
- Add the four focused managed skills and schemas.
- Store manifest and unit outputs as immutable artifacts.
- Add typed unit validation and error persistence.
- Implement topic, signal, and dimension child workflows.

Keep v2 behind a configuration flag.

### Pull request 3: assembly, repair, and recovery

- Add targeted repair.
- Add final narrative and deterministic assembly.
- Add per-unit retry APIs.
- Permit reuse-analyses recovery for synthesis-stage failures.
- Add shadow replay and comparison metrics.
- Run the required privilege and large-assessment replays.

### Pull request 4: readiness and Review Program integration

- Add completion disposition and diagnostics APIs.
- Change synthesis exhaustion to `COMPLETED_WITH_ERRORS` plus `ACTION_REQUIRED` when analyses are usable.
- Prevent Review Program auto-advance for `ACTION_REQUIRED`.
- Add audited Retry, Continue, and Cancel actions.
- Add reconciliation for existing synthesis-stage failures.

### Pull request 5: operator Health panel and deterministic Coach

- Add the Assessment Health panel.
- Add synthesis-unit drill-down and action menu.
- Explain impact and recommended next steps from structured diagnostics.
- Add frontend tests for ready, warning, action-required, retrying, and recovered states.

### Pull request 6: controlled enablement

- Run v1 and v2 in shadow mode for selected assessments.
- Compare result coverage, evidence validity, latency, requests, and tokens.
- Enable v2 for new assessments after acceptance criteria pass.
- Retain v1 rollback until the observation window completes.
- Remove temporary rollout code only after operational sign-off.

## Operational metrics

Track:

- synthesis attempts by workflow version and disposition;
- units by type, status, error code, and retry count;
- model requests and tokens by unit type;
- targeted repairs versus full regenerations;
- canonical, aliased, ambiguous, and unmapped criterion counts;
- time from document-analysis completion to usable synthesis;
- operator retries and explicit partial-result continuations; and
- Review Program waits caused by synthesis action requirements.

Alert on increasing unmapped-key rates, repeated unit output-budget failures, or action-required assessments without an available retry.

## Primary code areas

- `app/assessment_execution.py`: manifest compilation, validation, assembly, completion semantics.
- `app/workflows/matter_definition_assessments.py`: DBOS orchestration and unit workflows.
- `app/matter_definition_assessments.py`: regeneration and recovery.
- `app/standard_skills.py`: focused managed skills and schemas.
- `app/workflow_specs.py`: v2 role bindings.
- `app/models.py` and Alembic: attempt/unit persistence and disposition.
- `app/review_programs.py`: readiness-aware stage projection and actions.
- `app/routers/matter_definition_assessments.py` and `app/schemas.py`: diagnostics and retry APIs.
- `web/src/components/matter-definition-assessment-panel.tsx`: Health panel, unit diagnostics, and actions.
- `tests/test_matter_definition_assessments.py` and `tests/test_review_programs.py`: core regressions.

## First implementation checkpoint

Begin with Pull Request 1. Before modifying production synthesis behavior, produce a replay report showing:

- the failed privilege assessment's canonical criteria;
- the alias mappings applied;
- the number of signals before and after canonicalization;
- unmapped or ambiguous values;
- deterministic assignment counts;
- bounded evidence sizes; and
- the compiled unit inventory.

That report is the evidence gate for the remainder of the implementation.
