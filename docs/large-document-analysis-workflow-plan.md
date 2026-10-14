# Large-Document Analysis Workflow Implementation Plan

Status: proposed

## 1. Purpose

Analysis Tasks must be able to evaluate a document even when its text and task context do not fit in one model
request. This is a platform concern rather than a Jev-only workaround: every Analysis Task should use the same
planning, splitting, execution, coverage, retry, and combination framework, while its code-owned task adapter
defines what an evaluation unit means and how unit results may be combined.

The immediate failure is Jev's `max_tokens_exceeded` response. Jev has a practical limit of approximately 32,000
tokens for state plus the longest question and a separate 64,000-token budget for the complete request. The exact
limits are provider and model capabilities, not application-wide document limits. The application currently checks
a configurable serialized-character ceiling, but raising that ceiling can merely move the failure to the provider.
The [independent Jev error reference](https://jevaiguide.com/errors/max-tokens-exceeded) documents the observed
failure and links to the underlying TypeSafe limit documentation.

The intended result is:

```text
immutable document source
        |
        v
provider-aware request planning
        |
        +--> one bounded evaluation unit ----------------+
        |                                                |
        +--> deterministic document windows --> evaluate |
                                                         v
                                               typed combination
                                                         |
                                                         v
                                      optional targeted evidence pass
                                                         |
                                                         v
                              one document result + complete coverage
```

This plan specializes the long-document direction already described in the
[System One Decision Engine Plan](system-one-decision-engine-plan.md). It reuses the execution control plane in the
[Unified Job Execution Platform Plan](unified-job-execution-platform-plan.md) and the high-volume diagnostic pattern
in the [Reusable Job Outcome Manifest Platform Plan](job-outcome-manifest-platform-plan.md).

## 2. Scope

The shared workflow must support:

- any code-owned Analysis Task workflow, including current `QUESTION_ANSWERING` and `CLASSIFICATION` tasks and
  future task types;
- any registered model/provider adapter, including Jev and OpenAI Decisions;
- a single-document playground, a review batch, or a whole-matter run without changing result semantics;
- one-request execution when the complete request fits;
- deterministic multi-window execution when splittable task input does not fit;
- task-specific deterministic aggregation and, where explicitly allowed, a bounded structured reducer;
- complete, partial, insufficient, failed, and canceled coverage states;
- targeted retry without repeating successful evaluation units;
- immutable provenance for the source, plan, model calls, unit results, combined result, and evidence;
- existing isolated-result and explicit-promotion boundaries.

This plan does not:

- combine separate documents, attachments, or family members into one logical document result;
- silently truncate text to fit a provider;
- infer a safe aggregation rule from a model response;
- make analysis results authoritative matter coding;
- replace the task-specific result contract, statistical-classification scoring artifacts, or the common job
  execution framework;
- require text splitting for task types whose evaluator already consumes bounded features rather than source text.

## 3. Architectural decisions

1. **Large-document handling is a shared execution layer, not a new Analysis Task type.** The published task version
   continues to select a code-owned workflow. That workflow registers an evaluation adapter with the common layer.
2. **Provider limits are modeled capabilities.** Do not use one global character limit as a proxy for every provider
   and model. Plan against the fully serialized request, including repeated global state, questions, wrapper
   structure, and any required output reserve.
3. **The preferred immutable source remains authoritative.** Windows identify ranges in the source artifact through
   the versioned paragraph map. They do not create altered document text or a second content authority.
4. **Splitting and combination are independently versioned.** A splitter determines evaluation units. A combiner
   converts a complete or explicitly partial ordered set of unit results into one task result. Their keys, versions,
   settings, and hashes are frozen in the run.
5. **Combination is output-contract aware.** Typed decisions, classifications, and open-ended structured outputs
   cannot share an accidental "average everything" policy. Every output member must have an admitted combination
   rule before the task version can be published.
6. **Coverage is part of the result.** A negative answer over half a document is not equivalent to a negative answer
   over the whole document. Missing windows and omitted source ranges must remain visible to policies, users, and
   downstream promotion.
7. **Do not create one domain-result row per window.** Core retains the logical run, document status, combined result,
   counters, verified Artifact references, and the existing per-provider-call invocation telemetry. Complete window
   plans and unit results use immutable, bounded Parquet artifacts rather than a new Core window-result table.
8. **A provider size rejection refines the plan; it does not retry the same request.** A bounded deterministic split
   of only the offending unit may continue the document. The refinement lineage is retained.
9. **Evidence follows the combined decision.** Evidence localization runs only for outputs selected by policy and
   only over contributing windows. It must not resend the entire oversized document.
10. **Existing publication boundaries remain unchanged.** Windowing affects evaluation and provenance only. Results
    remain isolated until an authorized user explicitly promotes an eligible run.

## 4. Existing components to reuse

The first implementation should extend, not duplicate, these components:

- `get_preferred_text_source` for immutable source selection and source hashes;
- `segment_paragraphs`, `ParagraphMap`, and stable paragraph identifiers in `app/document_evidence.py`;
- the paragraph-aware splitting concepts in `build_document_map_plan`;
- the existing assessment map/reduce implementation as a migration reference, not a second permanent framework;
- `SystemOneDecisionEngine`, its provider registry, normalized typed answers, and `ModelInvocation` telemetry;
- `WorkflowRun`, `WorkflowStepRun`, `SkillRun`, `ReviewBatchRunDocument`, and the common cancellation/retry lifecycle;
- `ReviewDecisionResult` for the combined question-answering result;
- classification scoring's immutable Artifact-backed Parquet result pattern;
- Artifact Service lineage, content hashes, derivation keys, scoped authorization, and bounded Parquet reads;
- the outcome-manifest platform for item status and failure diagnostics, without storing task answers in the
  diagnostic manifest.

Embedding `CHUNK_SET` artifacts are not automatically Analysis Task windows. Their current size and overlap are
tuned for embedding retrieval, while analysis requests must reserve space for task context and questions and retain
exact evidence coverage. A later splitter may reuse a compatible chunk set only when its complete configuration and
coverage contract are pinned and validated.

## 5. Shared contracts

### 5.1 Analysis evaluation adapter

Register adapters by the task version's code-owned `workflow_key`. Conceptually:

```python
class AnalysisDocumentEvaluator(Protocol):
    def build_input(self, context: AnalysisDocumentContext) -> PreparedAnalysisInput: ...
    def plan(self, prepared: PreparedAnalysisInput, capabilities: ModelCapabilities) -> EvaluationPlan: ...
    async def evaluate_unit(self, unit: EvaluationUnit) -> EvaluationUnitResult: ...
    def combine(self, plan: EvaluationPlan, results: Sequence[EvaluationUnitResult]) -> CombinedResult: ...
    async def collect_evidence(self, combined: CombinedResult) -> EvidenceResult: ...
```

The protocol is behavioral; it does not require every task type to use a decision model. For example:

| Task workflow | Split behavior | Unit evaluator | Combiner |
| --- | --- | --- | --- |
| Question answering | Paragraph windows when needed | Registered decision engine | Per-question typed operators |
| Statistical classification | `NONE` for current pooled document features | Classification runtime | Existing prediction resolver |
| Future chunk classifier | Feature or text windows | Classification runtime | Versioned class resolver |
| Future structured summary | Paragraph windows | Generative structured model | Pinned structured reduce stage |

An adapter that declares `split_behavior=NONE` must either prove its input already fits its runtime contract or return
an explicit unsupported/insufficient result. It may not silently truncate.

### 5.2 Provider/model capability profile

Add a code-owned, versioned capability profile resolved from the registered provider and pinned model. It includes:

```text
provider_key
model_key or resolved model pattern
profile_version
token_estimator_key and version
maximum_request_tokens
maximum_state_plus_largest_question_tokens (nullable)
maximum_question_tokens (nullable)
maximum_question_count (nullable)
maximum_request_bytes (nullable)
reserved_output_tokens
safety_margin_tokens
usage_reporting_capability
```

Jev's profile represents both the complete-request budget and its state-plus-longest-question budget. Another
provider may expose only a context window or separate input/output ceilings. Provider credentials and tenant secrets
never enter the profile snapshot.

Use the provider tokenizer when it is available locally and versionable. Otherwise use a named conservative
estimator and retain its estimate. Successful provider usage supplies measured input tokens for monitoring but does
not retroactively change the frozen plan.

### 5.3 Published document-evaluation policy

Each executable task version freezes a validated policy, either as a new `document_evaluation_policy` snapshot or
inside its existing input/output contracts:

```json
{
  "schema_version": "analysis-document-evaluation-v1",
  "splitter": {
    "key": "paragraph-window",
    "version": "v1",
    "overlap_segments": 1
  },
  "maximum_windows": 200,
  "maximum_provider_requests": 250,
  "partial_coverage_policy": "INSUFFICIENT",
  "evidence_strategy": "CONTRIBUTING_WINDOWS",
  "outputs": {
    "responsiveness.issue_401": {"operator": "ANY_WINDOW"}
  }
}
```

The task-version validator must ensure that every output has a supported combination rule for its output type. A
task may prohibit windowing when no safe combination exists, but that limitation must be explicit before execution.

### 5.4 Immutable evaluation plan

Planning produces a canonical `EvaluationPlan` containing:

- task version and input/output contract hashes;
- source artifact ID and content hash;
- state-builder, paragraph-map, splitter, estimator, capability-profile, and combiner versions;
- provider, requested model, and resolved model when known;
- canonical global-state hash and question/output-contract hash;
- single-request or windowed mode;
- measured bytes, estimated tokens, applicable budgets, reserved capacity, and safety margin;
- ordered evaluation units with stable IDs, paragraph IDs, source character ranges, estimated tokens, and overlap;
- maximum request/window gates and the selected partial-coverage policy;
- a canonical plan hash.

An evaluation-unit ID is derived from the source content hash, exact source ranges, splitter version and settings,
global-state hash, question/output-contract hash, provider/model profile, and parent unit when refined. It is not based
only on its ordinal position.

## 6. Request planning and document splitting

### 6.1 Build the complete request envelope first

The planner separates the prepared input into:

- **global state**, which must be repeated for every unit: task context, allowlisted document metadata, schemas, and
  other immutable instructions;
- **splittable state**, normally numbered document paragraphs;
- **questions or requested output schema**;
- **provider wrapper overhead and output reserve**.

It serializes the actual provider-neutral request shape before estimating. The available text budget is the most
restrictive applicable provider budget minus global state, questions, wrappers, output reserve, and safety margin.
If fixed overhead alone cannot fit, splitting the document cannot repair the request; fail with a specific
configuration error.

### 6.2 Prefer one request when it fits

The single-request path remains the normal path for ordinary documents. It uses the same plan and combination
contract with one unit, ensuring that provenance and result semantics do not diverge between small and large
documents.

### 6.3 Split on stable semantic boundaries

The initial `paragraph-window-v1` splitter:

1. Uses the existing versioned paragraph map.
2. Adds complete paragraphs until the calculated unit budget would be exceeded.
3. Splits an oversized paragraph at sentence boundaries, then word boundaries, then hard character boundaries only
   as a final fallback.
4. Retains exact source offsets and the original paragraph ID for every segment.
5. Repeats a bounded number of trailing semantic segments as overlap.
6. Counts overlap for request budgeting but only once for coverage.
7. Produces at least one unit for a non-empty source and never emits an over-budget unit knowingly.

The initial implementation should preserve email headers and quoted-message boundaries only when they are already
represented in the source and paragraph map. A future splitter can understand headings, tables, email threads, or
page boundaries without changing evaluator or combiner contracts.

### 6.4 Apply cost and safety gates

Before dispatch, calculate expected unit and provider-request counts. If the plan exceeds the task's frozen maximum:

- do not truncate to the allowed count;
- mark the document `INSUFFICIENT` with full measured diagnostics;
- expose an authorized choice to use another compatible model/profile or publish a new task version with reviewed
  limits;
- never allow a per-document exception to mutate the already published task version.

### 6.5 Refine an unexpectedly oversized unit

If a provider returns a recognized input-size error despite preflight:

1. Classify the provider error, for example `PROVIDER_INPUT_TOO_LARGE`, rather than a generic HTTP 400.
2. Mark that unit as superseded, not failed.
3. Deterministically split only that unit into smaller child units using the same source ranges and a reduced budget.
4. Retain successful sibling results.
5. Stop after the code-owned refinement depth or minimum unit size is reached.

DBOS retries must never send the unchanged oversized request three times. A provider input-size failure is retryable
only through an admitted refinement or configuration change.

## 7. Unit evaluation and durable orchestration

The existing Analysis Task batch workflow remains the outer job. For each document, use durable stages:

```text
PREPARING_INPUT
PLANNING
EVALUATING_UNITS
COMBINING
LOCALIZING_EVIDENCE (when required)
PERSISTING_RESULT
COMPLETED | PARTIAL | INSUFFICIENT | FAILED | CANCELED
```

Requirements:

- Freeze and persist the plan before the first provider request.
- Use bounded document and unit concurrency governed by the provider/model rate limiter.
- Heartbeat and check cancellation at every unit boundary.
- Use the stable unit identity as part of request idempotency and result reuse.
- Persist provider request ID, usage, latency, and sanitized failure information per invocation.
- Commit successful unit results durably before advancing so a worker restart does not repeat them.
- Retry only failed retriable units. A manual "retry failed documents" operation reuses every compatible successful
  unit from the prior attempt and records retry lineage.
- Reauthorize the initiating user at workflow execution boundaries just as existing Analysis Task jobs do.

Progress should distinguish documents from provider calls:

```text
documents: 124 / 1,000
current document windows: 7 / 12
provider requests: 181 succeeded, 2 retried, 1 failed
```

Window counts are discovered during planning and therefore may increase the request-level total without changing the
frozen document population total.

## 8. Combination semantics

### 8.1 General rules

- Combine normalized provider results, never provider-specific raw payloads.
- Process units in stable source order.
- Deduplicate overlap by stable paragraph/range identity.
- Retain the contributing unit IDs and source ranges for every combined output.
- Do not average Choice distributions or ordinal scores unless a separately reviewed operator explicitly defines
  that behavior.
- Apply the task's decision policy only after unit outputs have been combined into document outputs.
- If missing unit results can change the answer, return `INSUFFICIENT` or route to review rather than manufacturing a
  complete result.

### 8.2 Initial typed operators

| Operator | Admitted use | Combination rule | Partial coverage |
| --- | --- | --- | --- |
| `ANY_WINDOW` | Noul/presence question | Use the maximum yes probability and retain all threshold-matching contributors | A positive may be actionable; a negative is insufficient unless coverage is complete |
| `ALL_WINDOWS` | Noul/universal condition | Use the minimum yes probability | Always insufficient when coverage is incomplete |
| `MAX_PROBABILITY` | Noul or bounded score use case | Select the unit with the strongest reviewed probability measure | Allowed only under the task's explicit partial policy |
| `HIGHEST_CONFIDENCE_CHOICE` | Choice with one document-level value | Select the unit with greatest admitted confidence; apply deterministic ties and conflict routing | Conflicting strong choices or missing material route to review |
| `SET_UNION` | Independently evaluated multi-value outputs | Union distinct policy-approved mapped values and retain per-value contributors | Each value may be positive on partial coverage; absence requires complete coverage |
| `INSUFFICIENT_IF_PARTIAL` | Any output requiring full-document coverage | Run its underlying operator only after complete coverage | Always insufficient when incomplete |
| `STRUCTURED_REDUCE` | Explicit future open-ended schema | Invoke a pinned reducer over bounded unit outputs, with recursive reduction if required | Defined by the published reducer policy |

For current multi-value enum Decision Specifications, independent Noul questions can continue to use `ANY_WINDOW`;
the document decision policy then maps each qualifying question to its static value. `SET_UNION` describes the
equivalent generic output behavior and should not create duplicate mappings.

### 8.3 Choice conflicts

`HIGHEST_CONFIDENCE_CHOICE` must not conceal a material cross-window conflict. The operator snapshot defines:

- the uncertainty measure used;
- minimum winning confidence/probability;
- minimum margin from the runner-up;
- deterministic tie behavior;
- the threshold at which two different choices become a review route.

The combined result retains both leading choices and their contributing ranges when it routes a conflict.

### 8.4 Structured reducers

A model-backed reducer is permitted only for an output that cannot be combined deterministically. It must:

- be selected by a code-owned reducer key;
- pin provider, model, prompt/schema version, limits, and aggregation tree;
- receive structured unit outputs and citations rather than the full document again;
- use hierarchical reduction when all unit outputs do not fit one request;
- validate its output against the published task schema;
- preserve the deterministic coverage envelope and never claim access to omitted source material.

Question-answering decisions should use deterministic typed operators for the first implementation.

## 9. Evidence and citations

The combined output identifies the windows that caused each recommendation or review route. Conditional evidence
localization then receives only:

- the frozen combined target answer;
- the contributing window's numbered paragraphs;
- the evidence-existence Noul and paragraph-location Choice;
- the minimum task context needed to interpret that target.

For `ANY_WINDOW`, localize evidence in the winning window and, when useful, other threshold-matching windows. For a
Choice conflict, localize both conflicting contributors. Negative full-document conclusions normally do not require
one localization request per window; their support is the complete coverage envelope unless the task explicitly
requires negative evidence.

Every citation is validated against the original document paragraph map. Overlap may produce the same paragraph in
multiple windows, but the stored evidence cites the original paragraph once and retains all contributing unit IDs as
provenance.

## 10. Persistence and artifact contracts

### 10.1 Core records

Core remains authoritative for:

- the Analysis Task and immutable published version;
- task-version document-evaluation policy;
- Analysis Task run, common `WorkflowRun` attempt, and document-level status;
- the combined task-specific result, such as `ReviewDecisionResult`;
- document-level coverage, usage totals, plan hash, and verified Artifact references;
- isolated projected coding values and explicit promotions.

Extend the combined result contract with:

```text
evaluation_mode                 SINGLE | WINDOWED
evaluation_plan_hash
splitter_key / splitter_version
combiner_key / combiner_version
capability_profile_key / version
planned_window_count
successful_window_count
failed_window_count
refined_window_count
coverage_status                COMPLETE | PARTIAL | INSUFFICIENT
window_result_artifact_id
window_result_artifact_hash
window_result_schema_version
```

Some values may remain in the existing `coverage` JSON initially, but identities and verified Artifact references
that are required for joins, constraints, or lifecycle operations should be first-class columns.

### 10.2 Artifact records

Add neutral Artifact types such as:

```text
ANALYSIS_EVALUATION_PLAN
ANALYSIS_UNIT_RESULT_FRAGMENT
ANALYSIS_UNIT_RESULTS
```

For high-volume runs, store fragments per durable work batch and compact/finalize them into a run-level result
artifact. Do not create one Artifact database record per window. A result row includes:

```text
document_id
plan_hash
unit_id
parent_unit_id
ordinal
paragraph_ids_json
source_ranges_json
estimated_input_tokens
status
reason_code
request_hash
provider_request_id
normalized_output_json
usage_json
started_at
completed_at
```

Do not duplicate document text in the unit-result artifact. Reconstruct it only through the authorized immutable
source artifact, source hash, paragraph map, and exact source ranges. Artifact finalization validates tenant/client/
matter scope, producer run, schema, hashes, row counts, document population, and plan identities before Core stores
the reference.

The job outcome manifest may point to failed documents and reasons such as `analysis.provider_input_too_large` or
`analysis.window_limit_exceeded`. The typed answers belong in Analysis Unit Results, not in the diagnostic manifest.

### 10.3 Reuse and idempotency

A reusable unit-result key includes:

- tenant, matter, document, immutable source artifact, and source hash;
- task version, definition/specification/output-contract hashes;
- global-state and question-set hashes;
- provider, resolved model, model settings, and capability-profile version;
- state-builder, paragraph-map, splitter, unit ranges, and combiner-compatible schema versions.

A policy-only change may reuse normalized raw unit outputs when the questions and evaluation request are unchanged,
then recombine and reapply policy under the new published task version. Any change to source text, question semantics,
unit ranges, provider/model semantics, or normalized output schema requires new evaluation.

## 11. API and user experience

### 11.1 Task version and launch

- Show the published large-document policy and provider/model profile in the task-version details.
- Validate unsupported output/operator combinations before publication.
- At launch, estimate documents, likely provider calls, and any configured cost/window ceiling without requiring the
  browser to orchestrate work.

### 11.2 Job progress

The Jobs and Analysis Run surfaces should show:

- documents completed over the frozen population;
- current stage;
- single-request versus windowed document counts;
- windows planned, completed, reused, failed, and refined;
- provider request and token totals;
- complete, partial, insufficient, and failed document counts;
- cancellation and retry actions through the common job framework.

### 11.3 Document result

The result view should show one combined document result by default, plus an expandable "Evaluation coverage" area
containing:

- why the document was split;
- provider/model budget and measured/estimated size;
- window count and source coverage;
- omitted or failed ranges;
- which windows contributed to each answer and evidence citation;
- conflicts and combination rules;
- provider request IDs for authorized support users.

Normal reviewers should not need to understand windows to read an ordinary complete result.

### 11.4 Failure and retry

Add stable failure codes, including:

```text
ANALYSIS_FIXED_CONTEXT_TOO_LARGE
ANALYSIS_WINDOW_LIMIT_EXCEEDED
ANALYSIS_PROVIDER_INPUT_TOO_LARGE
ANALYSIS_SPLITTING_FAILED
ANALYSIS_UNIT_EVALUATION_FAILED
ANALYSIS_COMBINATION_FAILED
ANALYSIS_PARTIAL_COVERAGE
ANALYSIS_EVIDENCE_FAILED
```

The failed-document dialog identifies the stage, unit/range when applicable, safe message, retriable state, and the
action that can make it retriable. Historical generic failures remain manually retryable where the source and task
version still resolve, but their missing unit diagnostics must be disclosed.

## 12. Delivery phases

### Phase 1: capability profiles and diagnostics

- Add the code-owned provider/model capability registry.
- Estimate the complete serialized request rather than document characters alone.
- Parse Jev `max_tokens_exceeded` into `ANALYSIS_PROVIDER_INPUT_TOO_LARGE` and retain the provider request ID.
- Record phase, bytes, estimated tokens, paragraph count, question count, and applicable budgets.
- Keep the current single-request behavior, but fail with accurate, actionable diagnostics.

### Phase 2: shared plan and splitter

- Generalize the existing paragraph-map/document-map primitives into the shared immutable `EvaluationPlan`.
- Implement `paragraph-window-v1`, stable unit IDs, overlap, exact coverage, cost gates, and plan validation.
- Add unit/property tests proving deterministic boundaries and complete range coverage.
- Migrate the Matter Definition assessment map/reduce planner to the shared splitter without changing its result
  contract.

### Phase 3: durable unit execution and artifacts

- Add unit-result fragment/final Artifact schemas and scoped APIs.
- Add DBOS stages, bounded concurrency, heartbeats, cancellation, request idempotency, and unit-level reuse.
- Persist plan and result artifacts and reconcile them after ambiguous failures.
- Integrate unit failures with the common failed-document and outcome-manifest experience.

### Phase 4: typed deterministic combiners

- Implement and validate `ANY_WINDOW`, `ALL_WINDOWS`, `MAX_PROBABILITY`,
  `HIGHEST_CONFIDENCE_CHOICE`, `SET_UNION`, and `INSUFFICIENT_IF_PARTIAL`.
- Reject task publication when an output has no safe admitted operator.
- Persist contributors, conflicts, combined uncertainty semantics, and coverage effects.

### Phase 5: Question Answering integration

- Replace the current oversized-state rejection with automatic single/windowed planning.
- Run conditional evidence localization only over contributing windows.
- Use the same path for the playground and durable batch execution.
- Preserve isolated `ReviewDecisionResult` and review-batch projection/promotion behavior.
- Migrate Jev first, then run the same conformance suite against OpenAI Decisions.

### Phase 6: remaining Analysis Task adapters

- Register the current classification workflow explicitly as a non-text-splitting evaluator using its existing
  immutable feature and prediction artifacts.
- Migrate other document-evaluating workflows from private map/reduce implementations.
- Add `STRUCTURED_REDUCE` only when a concrete task requires open-ended reduction.

### Phase 7: UI, operations, and rollout

- Add job progress and document coverage views.
- Add failed-unit diagnostics and targeted retry.
- Shadow-run windowed and prior single-request evaluation on a bounded calibration corpus.
- Compare outputs for documents that fit both paths and review boundary cases manually.
- Enable windowing per provider/task workflow, then make it the default after acceptance criteria pass.

## 13. Testing strategy

### Unit and property tests

- Same source/configuration produces the same plan and unit IDs.
- Every source range is covered; overlap never creates a false omitted range.
- Oversized paragraphs split without losing or reordering characters.
- Every planned request satisfies all modeled capability constraints.
- Provider-size refinement terminates and does not repeat an unchanged request.
- Aggregators are order-stable and preserve contributor provenance.
- Partial coverage cannot produce a false complete negative.
- Choice ties, conflicts, margins, and multi-value unions follow their frozen policies.

### Contract and integration tests

- Jev and OpenAI Decisions adapters pass the shared request-budget and normalized-result suite.
- Single-unit and equivalent multi-unit fixtures produce compatible combined decisions.
- Conditional evidence citations resolve to original paragraph IDs after overlap and paragraph splitting.
- Plan/result artifacts validate hashes, scope, schema, population, and producer identity.
- A worker restart resumes from successful units; cancellation stops at a unit boundary.
- Retrying failed documents reuses compatible successful units and creates correct attempt lineage.
- Playground, review-batch, and whole-matter execution use the same evaluator semantics.

### Load and failure tests

- Exercise large OCR text, long email threads, dense identifiers/logs, one enormous paragraph, and many short
  paragraphs.
- Exercise documents producing hundreds of windows and verify the configured gate.
- Inject 400 input-size errors, 429 throttles, timeouts, invalid provider responses, Artifact failures, cancellation,
  and failure after provider success but before Core commit.
- Verify bounded memory, bounded database growth, sanitized logs, and no source text in error records.

## 14. Acceptance criteria

The shared workflow is complete when:

- no Analysis Task rejects a document solely because it exceeds one provider request when that task has a valid
  splitter and combiner;
- providers receive only requests that pass a versioned capability preflight, aside from explicitly handled
  estimator drift;
- the same published task version yields reproducible plans and combined results for unchanged inputs;
- every combined result discloses complete, partial, or insufficient source coverage;
- a negative full-document decision cannot be produced from incomplete coverage unless an explicit reviewed policy
  allows that exact semantic;
- failed or canceled units can be inspected and eligible units retried without rerunning successful work;
- no new Core domain-result table grows once per window; existing provider-call telemetry continues to record one
  invocation per actual request;
- complete unit outputs and provenance are retained in verified immutable Artifact-owned Parquet;
- Jev and OpenAI Decisions pass the same long-document conformance tests;
- existing classification Analysis Tasks continue to use their native feature/scoring workflow through the shared
  adapter boundary;
- batch coding projection and matter coding promotion remain explicit, isolated operations.

## 15. Open decisions to resolve during Phase 1

- Which tokenizer implementations can be pinned locally for each registered provider/model?
- What safety margin and maximum-window defaults should be applied to Jev after measuring representative matter
  documents?
- Should `document_evaluation_policy` be a first-class task-version column or a versioned member of `input_contract`?
- What fragment size best balances resumability against Artifact record and object counts?
- Which result identities require first-class Core columns instead of the existing coverage/provenance JSON?
- Should a positive `ANY_WINDOW` result permit early stopping for tasks that do not require complete-document
  coverage, or should the first release always evaluate all windows for auditability and detection of conflicts?

The conservative MVP answers the last question by evaluating every planned window. Early stopping can be added later
as a separately versioned policy because it changes coverage, cost, evidence, and conflict-detection semantics.
