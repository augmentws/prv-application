# Question-Answering Batch Performance Improvement Plan

## Purpose

Reduce the wall-clock time, provider-request amplification, and operator uncertainty of Question Answering (Q&A)
Batch Set runs without weakening decision quality, source coverage, evidence traceability, durability, or immutable
run provenance.

This plan covers review-batch, Batch Set, Review Program, whole-matter, and playground execution because all of those
surfaces must retain the same per-document decision semantics.

## Implementation status (2026-11-18)

The shared Q&A evaluator now combines typed decision questions and evidence companions in one request when the
contract fits. Existing decision-result readers are unchanged; inline results record method
`decision-inline-evidence-v1`, the final answer fingerprint, request/window telemetry, and no separate evidence skill
run. JEV filters paragraph choices to the exact source window and binds evidence aggregation to the window that
supplied the final answer. Unsupported or oversized contracts retain the existing conditional second pass.

JEV and OpenAI now have separate DBOS-global batch and document queues, routed from the immutable workflow engine
snapshot. Aggregate provider-specific document capacity is validated against `QA_DOCUMENT_CONCURRENCY`.

Still open before declaring the entire plan complete: frozen-cohort replay and quality comparison, durable per-window
checkpoints, provider/model capability profiles and larger safe windows, request/throttle/queue timing in the job UI,
and the controlled rollout gate. The current implementation is therefore the first performance slice, not evidence
that the acceptance targets below have already been met.

## Measured baseline

The 2026-11-17 Privilege discovery run exposed two separate performance problems that the current UI presents as one
slow 250-document batch.

For Batch Set `7d0e1289-a202-4608-852d-a791635f5e4f`, the first 250-document population produced:

| Profile | Completed | Failed | Classification requests | Evidence requests | Total provider requests | Wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| JEV | 249 | 1 | 1,299 | 276 | 1,575 | 52m 14s |
| OpenAI Decisions | 244 | 6 | 244 | 207 | 451 | 46m 09s |

The two profiles therefore turned one 250-document population into 2,026 provider requests.

The local JEV policy was 30 requests per minute. Its 1,575 requests have a 52.5-minute throttle floor, which closely
matches the observed 52m 14s runtime. The first JEV classification pass averaged 5.2 provider windows per completed
document before evidence localization.

OpenAI's median recorded provider latency was approximately 0.27 seconds for the same population, but its batch still
took 46 minutes. JEV and OpenAI document workflows share the `analysis-task-batch-documents` DBOS queue, so slow,
multi-window JEV documents occupy or wait ahead of otherwise fast OpenAI work. This is cross-provider head-of-line
blocking rather than OpenAI inference latency.

The UI currently reports the population size but not the planned provider requests, source windows, evidence passes,
queue wait, throttle floor, or profile contention. An operator therefore sees "250 documents" without seeing the
actual work represented by that number.

## Goals

1. Capture decision evidence during the classification request whenever the provider can return a typed answer.
2. Execute one provider request per planned source window, excluding bounded transport retries and explicit repair.
3. Preserve paragraph-level, answer-bound evidence through deterministic long-document aggregation.
4. Prevent one decision provider from occupying another provider's document execution capacity.
5. Minimize source-window count without exceeding the resolved provider/model input contract.
6. Make planned requests, completed requests, queue wait, provider time, throttle floor, and estimated completion
   visible to operators.
7. Keep existing results readable and reproducible while the new contract is validated in shadow mode.

## Non-goals

- Do not omit document regions merely to make a negative decision cheaper.
- Do not silently truncate source text or treat incomplete coverage as a complete negative result.
- Do not increase token or character limits without checking a versioned provider/model capability profile.
- Do not weaken paragraph-reference validation or accept model-invented evidence identifiers.
- Do not make retries the normal path for deterministic schema, size, or evidence failures.
- Do not change human coding or publish Q&A output to the matter as part of this performance work.

## Target architecture

### One-pass decision and evidence contract

Extend the provider-neutral decision envelope with evidence returned alongside each typed answer:

```text
DecisionEnvelope
├── answers[question_key]
│   ├── typed answer, distribution, and confidence
│   └── answer fingerprint
└── evidence[question_key]
    ├── exists probability
    ├── candidate paragraph IDs and support probabilities
    ├── source window ID
    └── answer fingerprint being supported
```

Evidence remains logically distinct from the decision even though it is obtained in the same provider request. The
answer fingerprint prevents a citation produced for one window-level answer from being attached to a different final
answer after aggregation.

The provider request supplies stable paragraph IDs with the source content. The provider must return only those IDs.
Core validates every returned ID against the exact request window before persisting the result.

For short documents, one request returns both typed answers and evidence candidates. Core applies the decision policy,
retains evidence only for matched recommendations and routes that require it, and discards unused candidates.

### Long-document aggregation

Each deterministic source window returns its typed answer and bounded evidence candidates in the same invocation.
The code-owned aggregation operator selects the final answer and then admits only evidence that:

- came from a successfully processed source window;
- references a paragraph present in that window;
- declares the same answer fingerprint as the final answer; and
- satisfies the question's reviewed evidence threshold.

Operator-specific behavior must be explicit:

- `ANY_WINDOW`: use evidence from the qualifying window that establishes the final positive outcome.
- `MAX_PROBABILITY`: use evidence from the window that supplied the selected maximum.
- `HIGHEST_CONFIDENCE_CHOICE`: use evidence from the winning compatible choice window.
- `ALL_WINDOWS`: retain representative supporting evidence and require complete window coverage.
- `INSUFFICIENT_IF_PARTIAL`: never produce a complete decision when any required window is missing.

If aggregation produces an actionable answer but no valid matching evidence, persist the decision with partial evidence
coverage. A narrowly scoped evidence repair may be offered for that document and question. Repair is an observable,
bounded exception; it is not an automatic second pass for every actionable document.

### Provider-isolated scheduling

Replace the shared document queue with provider-specific DBOS queues, for example:

- `analysis-task-batch-documents-jev`;
- `analysis-task-batch-documents-openai-decisions`.

Route documents from the frozen execution profile, not from mutable current task settings. Give each queue an explicit
DBOS-global concurrency that fits within the database, worker, and provider limits. The sum of configured provider
queue capacities must be validated against the supported worker/database capacity.

Use provider-specific batch coordinator queues or a fair coordinator admission policy so that one active JEV profile
does not consume every Q&A batch slot while an OpenAI profile is ready. Provider RPM and TPM policies remain separate
from DBOS queue capacity.

### Request shaping and window planning

Before dispatch, build a versioned complete-request plan using the resolved provider/model capability profile:

- exact static decision-context tokens;
- exact question/schema tokens;
- expected output allowance;
- document tokens;
- provider safety margin;
- stable paragraph boundaries; and
- maximum supported request size.

Do not serialize the same document content as both raw text and paragraph text when one canonical paragraph
representation satisfies the frozen state contract. Static guidance and question definitions should be represented
once per provider request and use supported prompt caching where that produces a measurable benefit.

Persist the planned window count and plan version. Provider-triggered refinement may split an individual planned
window, but it must record why the original plan was insufficient and must not restart already completed windows.

## Implementation phases

### Phase 0: lock the baseline and add measurements

1. Add a replay cohort containing short messages, ordinary email threads, large email threads, OCR-heavy documents,
   and the documents responsible for the measured JEV window amplification.
2. Record per run and profile:
   - document count and source-character/token distribution;
   - planned and executed window count;
   - classification, evidence, retry, and repair request counts;
   - queue-wait, throttle-wait, provider, validation, and persistence time;
   - input, cached-input, cache-write, and output tokens;
   - complete, partial, failed, and skipped coverage.
3. Add a deterministic report command that compares a candidate implementation with the frozen baseline.
4. Preserve the 2026-11-17 measured run as the first regression reference.

Exit criterion: request amplification and wall time can be attributed without reading worker logs or inferring state
from document counters.

### Phase 1: define the inline-evidence contract

1. Add versioned `DecisionEvidenceCandidate` and per-question evidence result models.
2. Bind evidence to the canonical typed-answer fingerprint and source-window ID.
3. Extend each decision-engine adapter to request and normalize inline evidence.
4. Validate paragraph IDs, probability ranges, maximum candidates, answer binding, and window ownership in Core.
5. Keep the existing `DecisionAnswer` semantics stable so policy evaluation does not depend on evidence availability.
6. Version the request, response, and stored evidence method as `inline-decision-evidence-v1`.

Exit criterion: both configured providers can return a typed answer plus validated evidence for a short document in one
provider invocation. If a provider cannot support the contract, it remains on the legacy path and is reported as such;
the system must not pretend that provider received the optimization.

### Phase 2: use inline evidence for short documents

1. Update playground, review-batch, Batch Set, Review Program, and whole-matter execution through their shared
   document evaluator.
2. Apply the decision policy after receiving the combined response and retain evidence only for actionable required
   questions.
3. Stop creating `MATTER_DOCUMENT_EVIDENCE` skill runs for inline-capable executions.
4. Retain legacy second-pass evidence code only for legacy task versions and unsupported providers during rollout.
5. Store partial coverage rather than automatically launching a broad second request when inline evidence is absent.

Exit criterion: an inline-capable short document uses one successful provider invocation, with no automatic evidence
localization invocation.

### Phase 3: combine long-document answers and evidence

1. Emit answer-bound evidence candidates from every source window.
2. Extend each reviewed aggregation operator to select compatible evidence deterministically.
3. Persist window results durably so retry resumes only missing or retryable windows.
4. Add explicit incomplete-coverage outcomes for missing windows, incompatible evidence, and unsupported aggregation.
5. Implement an operator-requested, question-scoped repair action for actionable decisions lacking valid evidence.
6. Cap repair attempts and classify deterministic failures as non-retriable.

Exit criterion: long-document execution makes one normal provider call per planned window and never rereads the entire
document solely to locate evidence.

### Phase 4: isolate provider queues and remove head-of-line blocking

1. Add provider-specific document and, if required, batch coordinator queues.
2. Route by the immutable execution-profile snapshot.
3. Add configuration for each provider's DBOS-global document capacity and validate aggregate capacity at startup.
4. Preserve per-provider concurrency, RPM, and TPM enforcement inside the decision-engine adapter.
5. Add fairness tests in which deliberately slow JEV windows cannot delay ready OpenAI documents.
6. Ensure cancellation, targeted retry, and restart recovery operate across every provider queue.

Exit criterion: the fast-profile completion time is independent of a simultaneous slow-profile run, within its own
provider quota and normal database variance.

### Phase 5: reduce primary window amplification

1. Introduce versioned provider/model capability profiles with tokenizer, context size, output reserve, and safety
   margin.
2. Remove duplicate raw-text/paragraph serialization from the provider state.
3. Size windows from exact request tokens instead of only a global conservative target.
4. Keep complete paragraph coverage and stable paragraph IDs across larger windows.
5. Measure prompt-cache reuse for the static guidance/question prefix and retain it only where provider telemetry
   demonstrates a benefit.
6. Make a quota change an explicit operational decision after verifying the account-wide provider allowance; do not
   treat a higher RPM setting as the software fix.

Exit criterion: the replay cohort has fewer primary JEV windows with identical paragraph coverage and no increase in
provider size errors.

### Phase 6: operator progress and ETA

Display profile-specific work rather than only the document population:

- documents completed, partial, failed, skipped, queued, and active;
- source windows planned and completed;
- provider requests completed, retried, repaired, and remaining;
- current provider RPM/TPM policy and calculated throttle floor;
- queue wait versus provider processing time;
- separate JEV and OpenAI profile progress; and
- an ETA range with the assumptions used to calculate it.

The completion metric must not count a document as active provider processing while it is waiting in a DBOS queue or
rate limiter.

Exit criterion: an operator can explain a slow run and its remaining time from the job UI without database access.

### Phase 7: shadow comparison and controlled rollout

1. Run legacy and inline-evidence execution against the frozen replay cohort.
2. Compare typed decisions, probability distributions, paragraph agreement, coverage, requests, tokens, and runtime.
3. Require tenant-admin rollout controls and retain the legacy reader for historical results.
4. Enable inline evidence first for the playground, then isolated review batches, then Batch Sets and Review Programs.
5. Remove the automatic second evidence pass only after both providers meet their quality and durability gates.
6. Retire legacy execution writes after the observation period; do not rewrite historical decision results.

## Compatibility and persistence

- Existing `review_decision_result.evidence` JSON remains readable with its recorded evidence method/version.
- The new method writes inline evidence into the existing immutable decision result rather than creating a second
  authoritative result.
- Reuse keys include the evidence-contract, request-planner, tokenizer, state-builder, and aggregation versions.
- A legacy result may be displayed and compared but cannot be silently relabeled as inline evidence.
- Existing `MATTER_DOCUMENT_EVIDENCE` SkillRuns and ModelInvocations remain part of historical usage accounting.
- No database migration is required if the versioned evidence contract fits the existing JSON coverage/evidence
  fields. Add a migration only if queryable first-class columns are justified by the UI/reporting implementation.

## Testing strategy

### Contract tests

- Every evidence paragraph ID must belong to the exact request window.
- Evidence must bind to the selected typed-answer fingerprint.
- Extra, malformed, cross-window, and invented IDs are rejected or dropped with an adjustment record.
- An invalid citation cannot invalidate a valid typed decision; it produces partial evidence coverage.

### Aggregation tests

- Each supported aggregation operator selects the expected answer and compatible evidence.
- Conflicting windows cannot lend evidence to the winning answer.
- Partial source coverage never becomes a complete negative result.
- Retry reuses completed windows and does not duplicate model usage accounting.

### Scheduling tests

- Slow JEV documents do not delay OpenAI documents in a separate queue.
- Queue capacities remain DBOS-global across multiple workers.
- Cancellation terminalizes queued and active children without orphaning documents.
- A restarted worker resumes durable work without restarting successful requests.

### Quality tests

- Decision agreement against the reviewed replay baseline.
- Paragraph agreement and evidence-presence precision/recall.
- Complete/partial coverage agreement.
- Human review of disagreements and new near-boundary evidence.

## Acceptance targets

For the frozen 250-document privilege cohort, using the same provider models and account quotas:

- eliminate the 483 normal second-pass evidence requests observed across the first JEV and OpenAI runs;
- reduce combined provider requests by at least 35% after inline evidence and request shaping;
- reduce the JEV run from 1,575 requests to no more than 900, making a 30-minute throttle floor possible at 30 RPM;
- complete the OpenAI profile within 10 minutes when its own provider latency and quota permit, regardless of
  simultaneous JEV execution;
- keep decision agreement at or above the approved replay threshold and produce no statistically or legally material
  evidence-quality regression;
- preserve 100% accounting for provider requests, retries, repairs, tokens, and coverage; and
- expose enough UI telemetry for the operator to distinguish queue wait, throttle wait, provider time, and failures.

The 900-request JEV target is a performance gate, not permission to omit source coverage. If exact capability-aware
planning cannot meet it safely, report the achievable throttle floor and require an explicit product/provider decision
before rollout.

## Delivery sequence

1. Baseline instrumentation and replay report.
2. Inline-evidence contract and provider adapter prototypes.
3. Short-document inline evidence in shadow mode.
4. Long-document answer/evidence aggregation.
5. Provider-isolated queues.
6. Capability-aware request shaping and window reduction.
7. UI progress and ETA.
8. Controlled rollout and legacy-write retirement.
