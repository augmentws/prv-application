# Large-Document Evidence-First Analysis Implementation Plan

Status: Controlled Phase 6 rollout implemented behind an explicit tenant-admin route and global kill switch; the v10 objective gate, qualitative review, v2 binding publication, and model-role pins are complete, while default routing and broader migration remain pending

This plan replaces the earlier design in which a large document was split into verbose map reports and one generative reducer was asked to reconstruct the same result produced by direct document analysis. The durable execution, coverage, provider-batching, provenance, and targeted-retry requirements remain. The target processing model changes to bounded evidence extraction, deterministic aggregation, criterion-level adjudication, document decision, and optional narrative generation.

**Matter Definition assessment scope correction:** Assessment is guidance evaluation, not authoritative document
review. For assessments, complete-source evidence extraction and document adjudication are superseded by the
[Large-Document Query-Directed Assessment Implementation Plan](large-document-assessment-query-directed-plan.md).
Small assessment documents keep direct analysis; only documents that do not fit the complete direct request use the
existing frozen query plan for selective passage analysis. This evidence-first plan remains applicable to workflows
that actually require complete-document evidence and a defensible document decision.

## 1. Outcome

Small documents continue to use direct analysis when the complete serialized request fits the selected provider and model. Large documents use a separate execution graph because evidence extraction and cross-window adjudication are different tasks:

~~~text
complete request fits?
    |
    +-- yes --> direct document analysis ----------------------+
    |                                                          |
    +-- no  --> immutable window plan                           |
                    |                                           |
                    v                                           |
             evidence extraction                                |
                    |                                           |
                    v                                           |
        validate, deduplicate, and group                        |
                    |                                           |
                    v                                           |
            criterion adjudication                              |
                    |                                           |
                    +-------------------------------------------+
                                        |
                                        v
                               document decision
                                        |
                                        v
                          optional narrative generation
                                        |
                                        v
                    result + coverage + provenance + diagnostics
~~~

Extraction, criterion adjudication, document decision, and narrative generation have independent schemas, model bindings, limits, and retry policies. They may initially use the same model, but only through explicit role bindings.

## 2. Why the processing model must change

The direct path asks a model to read one document and return one cited result. The current large-document path asks one model configuration to:

1. extract several kinds of signals from every window;
2. combine a variable number of verbose model-generated reports; and
3. recreate the complete direct-analysis result while deduplicating signals, reconciling conflicts, and preserving citations.

The reducer does not see the source document. It receives every map result, including narrative findings, criterion matches, scope analysis, countervailing considerations, clarification requests, and limitations. Application code later restores near misses and clarification candidates omitted by the reducer. That confirms that the reducer is not a safe lossless aggregation boundary.

Assessment 4a1622f2-7859-474c-a9db-0b9dbe548f47 provides the baseline:

- The pinned document-analysis model was google:gemini-3.5-flash-lite.
- 250 documents took approximately 4 hours 19 minutes.
- 168 direct documents completed through provider batches near the beginning of the run.
- 82 large documents expanded into 351 map windows and 82 generative reductions.
- The minimum planned work was 601 document-analysis calls, but whole-document replay increased the recorded structured calls to 614.
- Those calls produced 795 provider requests because 176 structured calls, or 28.7%, required output repair; five required two repairs.
- Two documents exhausted the cumulative 20,000-token output usage guard and replayed successful work.
- Successful invocation rows recorded approximately 24.1 million input tokens. Failed invocation usage and exact validation categories were not completely persisted.

The read-only Phase 0 exporter reproduced 614 baseline invocation rows, 181 additional repair rows, 253 SkillRuns
for 250 documents, and two documents with replayed SkillRuns. It also identified 27 transformations between the full
application schema and the Google realtime admission schema and 34 transformations in the Google batch schema. The
historical invocation rows do not contain validation categories; new execution telemetry records them per response.

The Phase 0 replay harness now freezes a deterministic, content-free corpus manifest. Failed and replayed documents are
mandatory, including historical failed attempts later recovered by replay; bounded strata add repair-heavy,
multi-request, slow, and stable control documents. The manifest retains
source and baseline-artifact hashes plus compact decision signatures, but never copies source text or complete model
output. An explicit replay command verifies every frozen hash, loads protected content at execution time, runs the
current direct/map-reduce contract without database writes, and reports validation, usage, latency, determination
agreement, and criterion-match agreement for a selected candidate model.

The first frozen corpus for assessment `4a1622f2-7859-474c-a9db-0b9dbe548f47` contains 24 cases: all two documents
with historical failed attempts and whole-document replay, 18 multi-window cases, and six clean direct controls. The
large cases range from four to 11 current map windows; every case has a verified baseline artifact and source hash.
The verification pass resolved all 24 protected sources and baseline artifacts with zero model requests and zero
failures. Its corpus hash is `7e77f7c3eb5a7a0df37eab82c3065315749b2eb7118ec06f8e09816119743269`.

The bounded live smoke tests show why the current contract should not be replayed across the full corpus. A clean
single-window control completed with the same `NON_RESPONSIVE` determination, but required two provider requests
because its first response failed application-schema validation. The repaired response also introduced a
`not_privileged` criterion key where the baseline had no criterion match, demonstrating that free-form criterion
naming is not a stable comparison boundary.

The smaller historical-failure case had five map windows. It failed after eight provider requests, approximately
575,000 input tokens, and 28,000 output tokens. Five recorded validation/usage failures showed the model repeatedly
returning 60 to more than 200 paragraph identifiers per finding, criterion match, or scope item. The application
contract permits at most 30 citation IDs, but the Google-admitted schema removes `maxItems`; output repair shortened
some arrays without making them valid and eventually exhausted the 20,000-output-token request limit. No full-corpus
baseline replay will be run against this unchanged verbose-map contract. Phase 1 must first freeze canonical criterion
IDs and a bounded observation contract whose invalid optional items can be isolated without replaying the unit.

As an immediate durability measure, the managed document-analysis cumulative output budget is raised from 20,000 to
40,000 tokens. A replay of the same five-window document with that explicit override completed in eight provider
requests and agreed with the baseline `NON_RESPONSIVE` determination. Two map responses still violated the citation
array limit and required repair, and criterion-match agreement was only 0.4 because criterion keys remain free-form.
The higher ceiling therefore prevents a near-boundary usage failure; it is not the evidence-first contract fix and
does not justify replaying the unchanged path across the full corpus.

The repair rate is not attributable to one cause because INVALID_OUTPUT currently combines provider parsing, JSON Schema validation, constraints removed from the provider-admitted schema, citation validation, semantic validation, and output-usage exhaustion. Phase 0 separates those categories before rollout decisions.

## 3. Scope

The shared implementation supports:

- Matter Definition assessments as the first production integration;
- Question Answering Analysis Tasks, including oversized Jev requests;
- other document-evaluating task adapters;
- provider-aware direct execution when the complete request fits;
- deterministic window planning when it does not;
- provider Batch APIs for high-volume extraction;
- independently bound extraction, adjudication, decision, and narrative models;
- exact source coverage and citation provenance;
- durable unit results, targeted retry, cancellation, and restart recovery;
- complete accounting for successful, repaired, and failed requests;
- partial, insufficient, failed, and completed-with-errors outcomes;
- existing isolated-result and explicit coding-promotion boundaries.

The first release does not:

- combine attachments or family members into one logical document;
- silently truncate source, observations, or criteria;
- use a model when a code-owned rule can produce the same result safely;
- treat generated narrative as the authoritative decision or evidence record;
- make an analysis result authoritative matter coding;
- force feature-based statistical classifiers through text windowing;
- redesign assessment-level synthesis. Existing synthesis consumes the compatible document result and can be migrated separately.

## 4. Architectural decisions

1. **Direct and large-document execution are separate admitted plans.** They produce compatible results but do not pretend to be the same prompt with different-sized input.
2. **Large-document maps extract evidence.** They do not write miniature final reports or assign an overall determination.
3. **Aggregation is code owned.** Coverage, overlap removal, identifier validation, deduplication, grouping, ordering, and provenance are deterministic operations.
4. **Adjudication is criterion scoped.** A model reasons over one criterion and validated evidence rather than all verbose map reports.
5. **Later stages reference stable IDs.** Criterion and document decisions select observation or prior-decision IDs; they do not regenerate citations.
6. **Narrative is a projection.** Narrative failure cannot invalidate a completed evidence record or decision.
7. **Model bindings are role specific.** Each stage has its own skill version, capability requirements, settings, limits, and conformance results.
8. **Provider and application contracts are explicit.** Both schemas are versioned and tested. Every constraint unavailable at the provider boundary has a local enforcement policy.
9. **Validation is item aware.** A malformed optional observation may be dropped with diagnostics. Missing required decisions fail only their unit.
10. **Retries use the smallest durable boundary.** Transport retry, output repair, unit retry, and operator retry remain separate.
11. **Provider generation limits and cumulative budgets are different controls.** Both are enforced intentionally.
12. **Coverage controls negative conclusions.** A complete negative requires complete coverage unless the published policy explicitly admits otherwise.
13. **High-volume unit results use immutable artifacts.** Core stores job, document, rollup, selected result, and verified artifact references rather than one domain row per window.
14. **Publication boundaries do not change.** Results remain isolated until explicitly promoted by an authorized user.

## 5. Shared contracts

### 5.1 Task evaluation adapter

Each code-owned task adapter declares:

~~~text
adapter key and version
direct request builder
evidence-extraction request builder
observation validator and deduplicator
criterion-packet builder
criterion-decision combiner
optional document-decision request builder
optional narrative request builder
result assembler and renderer
~~~

It also freezes direct-result, observation, criterion-decision, document-decision, narrative, and coverage schema versions; deterministic versus model-backed stages; partial-result policy; adjustment and retry policies; and reuse compatibility.

### 5.2 Provider and model capability profile

Use a code-owned versioned registry keyed by provider and resolved model:

~~~text
context window
maximum provider output
supported structured-output keywords
dynamic-enum support
Batch API and prompt-cache support
token estimator and safety margin
~~~

Planning evaluates the complete serialized request, including instructions, frozen task context, schema, source or observations, and output reserve. Character count may be an early routing optimization but is not the authoritative provider-fit decision.

### 5.3 Criterion catalog

Matter Definition runs freeze a criterion catalog before document work begins:

~~~text
criterion key
display label
instruction text
aliases
decision semantics
catalog version and hash
~~~

Extraction selects a frozen criterion key. UNMAPPED is an admitted evidence bucket for a material signal that cannot
be mapped safely. It is retained for diagnostics and guidance review but is non-dispositive: it cannot independently
make a document responsive or unclear. Other task types may freeze question IDs or equivalent output-member IDs.

### 5.4 Immutable evaluation plan

Persist the plan before provider work:

~~~text
tenant, matter, document, source artifact, and source hash
task version, guidance revision, and criterion catalog hash
execution mode: DIRECT or EVIDENCE_FIRST
provider capability profiles and role bindings
paragraph-map and splitter versions
ordered extraction units and exact source ranges
expected adjudication strategy
stage schema and prompt hashes
generation, request, token, time, and cost budgets
plan hash
~~~

The plan is immutable. A recognized provider-size rejection may create a child refinement for only the offending unit.

### 5.5 Evidence observation

The initial Matter Definition extraction output is intentionally small:

~~~text
criterion_key
signal_type: SUPPORT, COUNTERVAILING, NEAR_MISS, SCOPE,
             BOUNDARY_AMBIGUITY, or LIMITATION
fact
citation_ids
~~~

The application assigns observation_id and source_unit_id after validation. When supported, the provider schema dynamically enumerates allowed criterion and paragraph IDs. The application always validates both against the frozen unit.

Observation IDs are deterministic hashes of source, unit, criterion, signal type, normalized fact, and citations. Overlap may create duplicates; deterministic aggregation retains one canonical observation and all contributing unit IDs.

Extraction does not request an overall determination, confidence, summary, or drafted clarification question. BOUNDARY_AMBIGUITY records the evidence and boundary; later synthesis may formulate the operator question.

### 5.6 Criterion adjudication

Validated observations are grouped into bounded criterion packets. A model-backed adjudicator returns:

~~~text
criterion_key
disposition: MATCH, NO_MATCH, NEAR_MISS, UNCLEAR, or INSUFFICIENT
confidence
supporting_observation_ids
countervailing_observation_ids
near_miss_observation_ids
optional unresolved_boundary
~~~

Referenced IDs are admitted from the packet. The application obtains citations by following observation IDs. The model cannot invent evidence.

If a criterion packet is too large, create stable source-ordered shards. Shard adjudication uses the same narrow contract; a final criterion adjudication receives only shard decisions and selected observation IDs. No observations are silently discarded.

When complete coverage contains no observations for a criterion, the adapter may produce NO_MATCH without a model call if the published semantics permit it. With incomplete coverage it produces INSUFFICIENT.

### 5.7 Document decision

Apply code-owned combination rules first. If the Matter Definition cannot be combined safely in code, a bounded document decider receives only criterion decisions, coverage, and the frozen decision policy:

~~~text
determination: RESPONSIVE, NON_RESPONSIVE, or UNCLEAR
confidence
contributing_criterion_keys
unresolved_criterion_keys
~~~

The application constructs criterion matches, near misses, scope results, and citations from selected criterion decisions and observations.

### 5.8 Narrative projection

Narrative generation receives the completed decision and selected observations. It returns a concise summary and explanation referencing observation IDs. The application resolves paragraph citations from those IDs.

Narrative status is independent:

~~~text
COMPLETED
COMPLETED_WITH_ADJUSTMENTS
FAILED
NOT_REQUESTED
~~~

Narrative failure produces a completed document decision with a warning. It never reopens extraction or adjudication.

### 5.9 Coverage envelope

Application-owned coverage records:

~~~text
COMPLETE, PARTIAL, or INSUFFICIENT
source length and paragraph-map version
planned, completed, failed, and refined units
analyzed, partial, and omitted source ranges
observation counts by criterion and signal type
criterion adjudication status
decision and narrative status
~~~

The model never generates coverage.

## 6. Planning and splitting

### 6.1 Direct admission

Build the exact direct request first. If it fits the selected capability profile with output reserve and budgets, use direct analysis. Persist measured size and routing reason for comparison.

### 6.2 Evidence-first planning

When the direct request does not fit:

1. Segment the immutable source with the versioned paragraph map.
2. Preserve paragraph IDs, character ranges, source order, and source hash.
3. Create the largest extraction units satisfying the capability profile and output reserve.
4. Repeat a bounded number of trailing semantic segments as overlap.
5. Count overlap in provider budgeting but once in coverage.
6. Assign stable unit IDs from plan identity, ranges, and splitter version.
7. Estimate extraction, adjudication, decision, and narrative requests plus bounded repair reserve.
8. Refuse or require an authorized configuration change when the frozen budget cannot admit the plan; never truncate to fit.

### 6.3 Provider-size refinement

A recognized input-size rejection supersedes only the offending unit. Split it into deterministic child units with reduced budgets, retain successful siblings, and stop at the configured depth or minimum size. Persist the refinement lineage.

## 7. Durable execution

### 7.1 Stage graph

~~~text
PREPARING_INPUT
PLANNING
EXTRACTING_EVIDENCE
VALIDATING_EVIDENCE
ADJUDICATING_CRITERIA
DECIDING_DOCUMENT
GENERATING_NARRATIVE
PERSISTING_RESULT
COMPLETED | COMPLETED_WITH_ERRORS | PARTIAL |
INSUFFICIENT | FAILED | CANCELED
~~~

Each stage is durable. Successful extraction units, criterion decisions, and document decisions survive restart and operator retry.

### 7.2 Provider batching

Extraction units from multiple documents may share a provider batch when provider, resolved model, prompt/schema version, and settings are compatible. Every request retains assessment, document, plan, unit, and attempt identity.

Batch finalization validates and checkpoints each item independently. One invalid result does not fail valid siblings. A document advances when its own extraction is terminal; unrelated documents do not impose a reduce barrier.

Adjudication and narrative may also use provider batching when latency requirements permit. Batch and realtime execution enforce the same normalized contract.

### 7.3 Concurrency and budgets

Use separately bounded queues or runtime capabilities for extraction, adjudication, decision, and narrative. Provider rate limits, database capacity, and artifact finalization govern dispatch concurrency.

Reserve the next request's maximum admitted usage before dispatch. Pause or terminate the correct scope when a role, document, or assessment budget cannot admit more work.

### 7.4 Persistence

Core remains authoritative for the published task or guidance version, workflow attempts, document stage/status, selected decision, coverage and diagnostic rollups, artifact references, projected isolated values, and promotion history.

Use verified immutable artifacts for:

~~~text
ANALYSIS_EVALUATION_PLAN
ANALYSIS_OBSERVATION_FRAGMENT
ANALYSIS_OBSERVATIONS
ANALYSIS_CRITERION_DECISIONS
ANALYSIS_DOCUMENT_DECISION
ANALYSIS_NARRATIVE
~~~

Write fragments per durable work batch and compact them without creating one Core domain-result row per window. Artifacts contain identities, ranges, status, reasons, normalized outputs, usage references, and timestamps but not duplicated source text.

### 7.5 Idempotency and reuse

Reuse identity includes source artifact/hash, task or guidance version, criterion catalog, prompt/schema versions, provider/model/settings, capability profile, paragraph map, unit ranges, and adapter version.

A policy-only change may reuse observations when extraction semantics are unchanged. Source, criterion, extraction prompt, unit range, provider/model semantic, or observation-schema changes require new extraction.

## 8. Validation, adjustment, and retry

### 8.1 Canonical contract and enforcement policy

Generate provider-admitted and complete application schemas from the same versioned contract. Contract tests show every removed or transformed keyword. A task cannot publish until every application-only constraint has one policy:

~~~text
NORMALIZE
DROP_ITEM
FAIL_UNIT
FOCUSED_REPAIR
OPERATOR_REVIEW
~~~

Do not rely on prose instructions as the only enforcement of critical identifiers.

### 8.2 Failure taxonomy

Use stable codes:

~~~text
ANALYSIS_FIXED_CONTEXT_TOO_LARGE
ANALYSIS_WINDOW_LIMIT_EXCEEDED
ANALYSIS_PROVIDER_INPUT_TOO_LARGE
ANALYSIS_SPLITTING_FAILED
ANALYSIS_PROVIDER_OUTPUT_UNPARSEABLE
ANALYSIS_OUTPUT_SCHEMA_INVALID
ANALYSIS_OUTPUT_IDENTIFIER_INVALID
ANALYSIS_OUTPUT_SEMANTIC_INVALID
ANALYSIS_OUTPUT_REPAIR_EXHAUSTED
ANALYSIS_OUTPUT_USAGE_LIMIT_EXCEEDED
ANALYSIS_RUN_BUDGET_EXHAUSTED
ANALYSIS_UNIT_EVALUATION_FAILED
ANALYSIS_ADJUDICATION_FAILED
ANALYSIS_DECISION_FAILED
ANALYSIS_NARRATIVE_FAILED
ANALYSIS_PARTIAL_COVERAGE
~~~

Diagnostics record safe field paths, invalid IDs, policy applied, retryability, and operator action without copying privileged source text into generic logs.

### 8.3 Retry boundaries

- Retry transport errors only when provider outcome and idempotency make it safe.
- Refine provider-size errors instead of resending unchanged input.
- Normalize or drop optional invalid observations locally under published policy.
- Focused repair receives only invalid required fields, admitted IDs, and minimum necessary evidence.
- Output or usage exhaustion is terminal for that attempt and is not a generic workflow retry.
- Operator retry reuses compatible successful units and records lineage.
- Narrative retry never reopens the decision.

### 8.4 Usage accounting

Every actual provider request creates telemetry even when parsing, validation, usage enforcement, or persistence later fails. Record provider request ID, parent attempt, stage, unit, latency, provider usage, cache usage, validation result, and adjustment or repair action.

Separate controls include provider response-generation maximum, repair requests per unit, cumulative unit usage, document stage and total budgets, assessment stage and total budgets, and elapsed-time or cost ceilings.

The UI and outcome manifest reconcile planned baseline calls, repair allowance, baseline calls, repair calls, workflow retries, and failed calls.

## 9. Model selection and conformance

Create independent managed-skill roles:

~~~text
large_document_evidence_extraction
large_document_criterion_adjudication
large_document_decision
large_document_narrative
~~~

Each pins provider, model, settings, prompt, provider schema, application schema, and limits. Multiple roles may initially use the current model but remain independently replaceable.

The conformance corpus includes:

- small documents admitted by both paths;
- long email threads, OCR text, dense identifiers, and long paragraphs;
- documents from current failed and repaired calls;
- positive, negative, near-miss, scope-conflict, and insufficient-evidence cases;
- intentionally invalid identifiers, extra fields, excessive items, and malformed optional items.

Measure first-response provider and application validity, identifier validity, dropped-item rate, repair requests, decision agreement with direct execution, reviewer assessment of material differences, tokens, latency, cost, and batch throughput.

Do not select a stronger model merely because it compensates for an oversized contract. Compare models on each bounded stage. A fast extractor plus a stronger adjudicator or decider is likely, but conformance evidence determines the binding.

## 10. Matter Definition integration

### 10.1 Compatibility result

The assembled result remains compatible with existing rendering and synthesis:

~~~text
summary
determination
confidence
responsiveness_summary
criterion_matches
scope_analysis
countervailing_considerations
clarification_requests
limitations
coverage
~~~

Provenance identifies model-generated, derived, and deterministically assembled fields. If narrative fails, deterministic evidence digests may fill presentation fields and the document becomes COMPLETED_WITH_ERRORS; the decision and evidence remain usable under explicit synthesis policy.

### 10.2 Direct path

Keep direct analysis as the production baseline during migration. Bring schema translation, invocation telemetry, failure taxonomy, provider generation limits, and item-aware adjustment into the shared framework. Do not window small documents merely for implementation uniformity.

### 10.3 Large-document migration

Shadow evidence-first execution while the current map/reduce result remains authoritative. Compare decisions, criteria, citations, near misses, boundary signals, coverage, usage, and elapsed time.

Keep the verbose-map/generative-reducer path only as a bounded rollback. Remove it after evidence-first execution passes the production cohort and acceptance gates.

## 11. Other Analysis Task adapters

Question Answering maps frozen questions or output members to the criterion-equivalent catalog. Typed operators such as ANY_WINDOW, ALL_WINDOWS, and INSUFFICIENT_IF_PARTIAL may eliminate model adjudication. Open-ended answers use bounded adjudication over selected observations.

Feature-based classification registers a non-windowing adapter and retains its existing immutable feature and prediction artifacts while participating in common execution, accounting, progress, and promotion contracts.

No adapter may publish until every output member has an admitted combination or adjudication policy.

## 12. Operator experience

### 12.1 Launch

Show frozen document population, direct versus evidence-first estimate, expected extraction and adjudication calls, model roles, repair reserve, estimated tokens/cost, and gates. Estimates are diagnostic; runtime budgets are authoritative.

### 12.2 Progress

Show separate counts for:

~~~text
direct and evidence-first documents
extraction units planned, completed, adjusted, and failed
criteria pending, adjudicated, insufficient, and failed
document decisions completed and failed
narratives completed, warned, and failed
baseline requests, repair requests, and workflow retries
estimated and actual tokens and remaining budget
~~~

Stage display comes from durable work, not only parent workflow state.

### 12.3 Results and diagnostics

Reviewers see the combined decision, evidence, narrative, and coverage. An expandable coverage area explains splitting, omitted ranges, contributing criteria, and narrative adjustments.

Technical diagnostics remain closed by default and visible only to authorized tenant administrators. They include unit and invocation identities, schema differences, validation policies, dropped items, repair lineage, request IDs, and usage reconciliation.

### 12.4 Actions

Provide stage-aware actions to retry extraction units, criterion adjudications, document decision, or narrative only; continue with partial coverage when policy and authorization permit; expand the assessment batch when assessment evidence is insufficient; and export an outcome manifest.

Explain which completed work will be reused before retry.

## 13. Delivery phases

### Phase 0: baseline and diagnostics

- Persist provider parse, provider-schema, application-schema, identifier, citation, semantic, and usage failure categories.
- Persist failed-call usage and validation outcomes.
- Export frozen inputs and outputs for safe replay of assessment 4a1622f2-7859-474c-a9db-0b9dbe548f47.
- Produce direct, map, reduce, repair, replay, token, latency, and cost baselines.
- Capture provider-versus-application schema differences.
- Deliverable: baseline report and conformance corpus without routing changes.

### Phase 1: contracts and conformance harness

- Implement capability profiles and serialized-request planning.
- Add criterion catalog, observation, criterion-decision, document-decision, narrative, and coverage contracts.
- Generate both schemas from one contract and require enforcement policy for each difference.
- Add role-specific skill bindings and limits.
- Build replay tooling for selectable models without changing assessment state.
- Deliverable: versioned contracts and model comparison for the failed/repaired corpus.

Implemented contract slice:

- Frozen, hashed criterion catalogs are derived from the assessment retrieval plan and reserve `UNMAPPED` as a
  contextual evidence bucket that cannot independently support a positive document decision.
- Versioned observation, criterion-decision, document-decision, and narrative-projection Pydantic contracts use
  stable IDs and validate every downstream reference against its admitted packet.
- Evidence extraction dynamically enumerates criterion and paragraph IDs in the application schema, generates the
  provider-admitted schema from the same contract, and reports every removed keyword with an explicit local policy.
- Item-aware normalization preserves valid sibling observations, normalizes only unambiguous aliases and duplicate
  citations, drops malformed optional items with reason codes, and fails the unit only for an invalid outer envelope
  or observation-count overflow. Provider-facing observations use one required primary citation and four
  required-but-nullable additional scalar slots, so the provider can enforce both the admitted paragraph IDs and the
  five-citation ceiling without relying on unsupported array-length constraints.
- Criterion adjudication uses the same provider-enforceable pattern: one nullable primary observation reference and
  four nullable additional scalar references. References are disposition-neutral, limited to the admitted packet,
  deduplicated locally, and never require the model to populate competing support/counter/near-miss arrays.
- `scripts.run_evidence_extraction_conformance` verifies a frozen source and baseline artifact by hash and exercises
  exactly one selected source window with no output retry or assessment-state writes. Its default report excludes
  source text and observation facts while retaining usage, validation, adjustment, schema, and observation-signature
  diagnostics.

Production routing, durable extraction artifacts, role-specific bindings, and authoritative decision assembly remain
pending. The first live historical-window conformance request completed on the first and only
provider request: a 59,426-character, 118-paragraph window produced two valid observations with two citations each,
zero dropped items, zero adjustments, and zero validation failures. It consumed 16,909 input tokens and 145 output
tokens in 1.575 seconds. The old verbose-map contract had cited most or all of this same window in individual arrays
and entered output repair. This is strong evidence for the bounded extraction contract, but it covers one extraction
window only and does not yet validate aggregation, adjudication, or final document-decision agreement.

The subsequent five-window document shadow runs completed every extraction unit without output repair and used six or
seven provider requests, depending on whether one or two criteria required adjudication. Every code-owned decision
agreed with the historical `NON_RESPONSIVE` baseline. The first two document runs each produced one optional
observation with more than five citations; item-aware validation dropped it locally instead of entering the former
repair loop. The final corrected run accepted eight observations with no adjustment and used 144,202 input tokens,
822 output tokens, and 8.446 seconds of aggregate provider latency across seven requests.

Model output varied materially across identical runs: one to eight observations were retained and the contributing
windows changed. One final-run adjudication returned `NO_MATCH` with only 0.2 confidence while also populating an
unresolved-boundary explanation. The bounded contract therefore removes retry amplification and preserves safe
structure, but adjudication disposition/confidence/boundary semantics still need cohort calibration. A dropped
material observation makes evidence completeness false: the deterministic combiner returns `UNCLEAR` with all
positive criteria unresolved and labels the shadow run `COMPLETED_WITH_ADJUSTMENTS`; it cannot report a negative from
an incomplete validated evidence record. The adjudication boundary also normalizes provenance arrays incompatible
with a `NO_MATCH` disposition without changing the disposition or making a repair request.

A five-document cohort then repeated both historical failures, one responsive direct control, one four-window
negative/near-miss control, and one clean direct negative control three times each. All 15 runs reached a document
result with no harness failure. They used 77 provider requests, 1,178,119 input tokens (607,102 cached), 10,761 output
tokens, and 179.875 seconds of aggregate provider latency. There were no structured-output validation failures and no
repair requests. Determination was stable for only two of five cases, and nine of 15 runs agreed with the historical
baseline. The clean direct negative and four-window negative controls agreed in all six runs. Each historical failure
agreed in one of three runs; the responsive control agreed in one of three.

Every lost observation in the V1 cohort had the same cause: five items exceeded the five-citation application limit
because Google's admitted schema removes `maxItems`. The safety rule correctly changed affected documents to
`UNCLEAR`, including two responsive-control runs, rather than converting evidence loss into a false negative. Five
adjudications also populated references incompatible with `NO_MATCH`; those optional references were removed locally
without repair. This result led to the provider-enforceable scalar-slot contract rather than prompt-only tuning or
additional retries.

The V2 contract was tested against the exact same five documents and three repeats. All 15 runs completed with no
harness failure, extraction failure, adjudication failure, structured-output validation failure, normalization
adjustment, or dropped observation. All 15 determinations agreed with their historical baselines and all five cases
were determination-stable: both historical failures and both negative controls were `NON_RESPONSIVE` in every run,
and the responsive control was `RESPONSIVE` in every run. The cohort used 71 provider requests, 1,836,452 reported
input tokens (1,301,117 cached), 6,006 output tokens, and 99.550 seconds of aggregate provider latency. Compared with
V1, requests fell from 77 to 71, output tokens fell from 10,761 to 6,006, and aggregate provider latency fell from
179.875 to 99.550 seconds; reported input tokens rose and cached-input accounting changed materially, so no input-cost
improvement is inferred from this cohort. Observation counts still varied on several negative cases, but that
variation no longer changed the final determination or caused evidence loss. This satisfies the bounded V2 contract
gate for the five-case failure/control cohort; it does not yet authorize production routing. The next Phase 1 work is
to freeze role-specific bindings and acceptance gates, then extend shadow evaluation to a broader representative
corpus before implementing durable extraction artifacts.

The conformance harness now accepts independent extraction and adjudication model bindings while retaining a shared
model shortcut. It can select every frozen case and safely resume only child reports whose corpus hash, contract
version, case ID, and role bindings match. The deterministic combiner was also corrected for admitted `UNMAPPED`
evidence: an unmapped-only unresolved decision is now `UNCLEAR` at confidence zero instead of attempting to read a
nonexistent criterion-adjudication row. A regression test freezes that conservative behavior.

The first full-corpus V2 calibration then ran all 24 frozen documents once with Gemini 3.5 Flash Lite bound to both
roles. All 24 documents completed, all source windows and evidence records were complete, and detailed reports
contained no extraction failure, adjudication failure, structured-output validation failure, or dropped observation.
Twenty-three determinations agreed with baseline: both baseline-responsive documents remained `RESPONSIVE`; 21 of 22
baseline-negative documents remained `NON_RESPONSIVE`; and one baseline-negative document became `UNCLEAR`. The pass
used 172 provider requests, 3,743,842 reported input tokens (308,817 cached), 12,216 output tokens, and 213.082 seconds
of aggregate provider latency.

The sole disagreement, matter document `9aa300e9-5cb4-4371-92c4-0bd3b77db854`, was repeated twice more. It was
stably `UNCLEAR` in all three runs because extraction consistently returned admitted `UNMAPPED` observations; no
observation was dropped. One repeat removed a duplicated adjudication reference locally, without repair or evidence
loss. The cited source is a long commercial agreement containing definitions, payment terms, and bracketed drafting
questions. That evidence is related to the frozen draft-contract criterion but does not obviously establish the
criterion's required confidential legal advice, lawyer comments, or explicit request for legal advice. This is a
semantic acceptance case, not a provider-schema defect. Before production routing, an authorized reviewer must decide
whether this pattern is a criterion-level `NEAR_MISS`, a material unmapped signal that should keep the document
`UNCLEAR`, or safely non-responsive. Do not tune the prompt merely to reproduce the historical negative.

The operator accepted the criterion-level `NEAR_MISS` interpretation. A prompt-only V3 experiment eliminated
`UNMAPPED` and produced the baseline document determination in all three repeats, but it did not meet the semantic
gate: extraction emitted a near-miss signal in only one repeat and adjudication returned `NO_MATCH` in all three.
The adjudication contract was therefore separated from the domain decision. The model must now select one explicit
relationship—`MATCH`, `RELATED_MISSING_REQUIRED_ELEMENT`, `UNRELATED`, `CONFLICTING`, or `INSUFFICIENT`—and code maps
`RELATED_MISSING_REQUIRED_ELEMENT` deterministically to `NEAR_MISS` while retaining the boundary explanation.

With Flash Lite performing both roles, this structural contract preserved the agreed near miss in two of three
repeats, including the missing attorney-involvement/legal-advice boundary. The third adjudication still selected
`UNRELATED` for admitted draft-contract evidence. The structure now prevents semantic label collapse after the model
chooses the correct relationship, but Flash Lite does not choose that subtle relationship reliably enough. Do not add
a criterion-specific code override: that would overfit this privilege example and could misclassify other review
guidance.

The authorized role-specialization experiment retained Gemini 3.5 Flash Lite for extraction and used Gemini 3.7
Flash only for criterion adjudication. Across three focused repeats, the draft-contract adjudicator selected
`RELATED_MISSING_REQUIRED_ELEMENT` three of three times and the deterministic combiner preserved the operator-approved
`NEAR_MISS` three of three times. Each boundary explanation correctly identified the same missing requirement:
contract drafting or revisions were present, but the evidence did not establish counsel involvement, substantive
legal comments, or a request for legal advice. No observation was dropped or locally adjusted.

This passes the focused semantic gate for role specialization, but not the broader production-durability gate. One
repeat also extracted a stray `legal_invoices_and_fees` observation; its adjudicator returned `UNRELATED` without an
observation reference and failed semantic validation, so that document run was `COMPLETED_WITH_ERRORS` even though
its draft-contract near miss was correct. The three runs used 37 provider requests, 671,725 reported input tokens
(545,763 cached), 10,185 output tokens, and 89.386 seconds of aggregate provider latency.

The generic missing-reference edge is addressed in conformance contract V6. The contradictory prompt allowance for
an all-null negative relationship was removed: every relationship except `INSUFFICIENT` must cite admitted evidence.
When a non-insufficient adjudication nevertheless omits all references and its packet contains exactly one admitted
observation, application code attaches that sole unambiguous reference and records a normalization adjustment. It
does not guess when the packet contains multiple observations, and it does not replace a supplied invalid reference.
The remaining pre-production calibration step is a representative repeated cohort with these role bindings.

### Phase 2: shadow evidence extraction

- Generalize paragraph-map and splitter primitives into the immutable plan.
- Implement evidence-only extraction, admitted identifiers, item validation, stable observation IDs, overlap deduplication, and artifacts.
- Run extraction beside the current path without changing authoritative results.
- Compare evidence coverage, invalid observations, citations, requests, tokens, and latency.
- Deliverable: validated observations for a representative corpus.

The first durable Phase 2 slice is implemented without changing production routing. A versioned
`ANALYSIS_EVALUATION_PLAN` JSON artifact freezes source and guidance hashes, criterion catalog, role-specific skill and
model bindings, schema/prompt/settings/capability hashes, budgets, splitter configuration, ordered source ranges, and
stable extraction-unit identities. The plan excludes source text, requires complete source coverage, canonicalizes
role ordering, and verifies its own content hash when loaded. Extraction-semantic changes produce new unit identities.

`ANALYSIS_OBSERVATION_FRAGMENT` Parquet artifacts now provide the durable unit-result boundary. Each row retains plan,
source, unit, attempt, terminal status, normalized observations and adjustments, invocation IDs, errors, and
timestamps. Before storage, application validation proves plan ownership, unit/ordinal identity, unique attempts,
in-unit citations, and observation-content hashes. Plan artifacts derive from the immutable source artifact; fragment
artifacts derive from the plan artifact. The artifact platform allowlist, media/size policy, database constraint, and
migration admit these types.

Fragment compaction into `ANALYSIS_OBSERVATIONS` is also implemented. It selects the highest durable attempt for each
planned unit, ignores exact duplicate fragment rows idempotently, and rejects conflicting content for the same unit
attempt. Coverage is calculated by subtracting the union of successful source ranges from all planned ranges, so
overlap is handled exactly rather than treating every failed unit as an automatic gap. Material adjustments such as a
dropped observation make evidence incomplete even when source coverage is complete; normalization-only adjustments
do not. The compacted Parquet artifact carries a hash-checked manifest, aggregated observations, selected attempts,
missing units, exact omitted ranges, and lineage to its plan and fragment artifacts.

A restart-safe shadow execution adapter now connects those artifact boundaries without changing authoritative
results. It persists or reuses the immutable plan, discovers prior unit attempts by deterministic
`plan + unit + attempt` derivation keys, reuses terminal successful units after restart, and executes only units that
have no durable attempt. A failure classified as retryable is persisted with that classification and automatically
advances only its unit to the next bounded attempt, including after process restart. Non-retryable and canceled
attempts remain terminal unless an operator explicitly requests a targeted retry. Extractor exceptions and
contract-invalid responses are persisted as failed unit attempts rather than aborting or replaying the complete document. Every pass compacts all
discovered attempts into a comparison-ready observations artifact with complete lineage. The extractor is injected,
so the adapter is covered with deterministic local tests and does not itself select or call a model.

The adapter is now bound to a dedicated system-managed `evidence_extraction` skill through the isolated
`matter_definition_evidence_shadow_v1` workflow specification. This does not add a required role to the live
assessment workflow. The managed executor verifies the plan's pinned skill version, resolved provider/model, tenant,
client, matter, guidance hash, criterion-catalog hash, source hash, and source length before dispatch. It renders only
the planned source ranges, applies the dynamic criterion and paragraph identifier schema, records ordinary SkillRun
and ModelInvocation accounting, and translates item-level normalization or drop diagnostics into the durable unit
fragment. The entrypoint rejects execution unless `DEFINITION_ASSESSMENT_EVIDENCE_SHADOW_ENABLED=true`; it defaults
to false and no production route invokes it. Plan construction from a selected assessment/document and controlled
shadow dispatch are now available through the operator-only `scripts.run_evidence_shadow` launcher. Its default dry
run validates assessment-batch membership and produces content-free plan reports without writes or model calls.
The launcher requires an explicit active root or owning-tenant administrator and attributes source access, the
workflow, and audit records to that operator. Explicit execution creates a separate idempotent shadow workflow,
is capped at 25 selected documents, persists only
shadow artifacts and ordinary execution telemetry, and cannot update assessment results or start synthesis. Automatic
production routing remains pending the representative cohort gate.

### Phase 3: durable batching

- Submit extraction units across documents through compatible provider batches.
- Add stage checkpoints, artifact fragments, bounded concurrency, cancellation, refinement, and targeted retry.
- Advance each document when its own extraction is terminal.
- Reserve budgets before dispatch and account for every invocation.
- Deliverable: restart-safe extraction with no whole-document replay.

### Phase 4: adjudication and decision

- Implement criterion packets, deterministic empty-evidence handling, sharding, and bounded adjudication.
- Implement code-owned combination and bounded document-decider fallback.
- Assemble the compatibility result and coverage without narrative.
- Back-test documents that fit both paths and review material disagreements.
- Deliverable: evidence-first authoritative decision candidate.

Criterion adjudication is now available as a second, isolated non-authoritative shadow workflow. It derives an
immutable plan from the durable `ANALYSIS_OBSERVATIONS` artifact rather than changing the extraction plan, so testing
this role cannot replay extraction or alter prior artifact identities. Each frozen criterion receives a packet;
packets with no observations are resolved deterministically as `NO_MATCH` only when extraction is complete and as
`INSUFFICIENT` otherwise. Non-empty packets are capped at 32 observations and independently retryable. The managed
role validates the pinned skill, model, provider, schemas, guidance, catalog, observation hash, and packet-scoped
references before persisting a decision fragment. Invalid model results become durable failed fragments rather than
aborting the document. A code-owned compactor selects the latest attempt, combines shards with fixed precedence, and
emits a lineage-complete `ANALYSIS_CRITERION_DECISIONS` artifact. The operator launcher is dry-run by default,
administrator-only, feature-flagged for execution, and has no path to assessment or coding projection.

The code-owned document combination is now implemented as a durable extension of that shadow workflow. It consumes
only the compacted criterion-decisions artifact and frozen catalog. For positive inclusion guidance, any `MATCH`
produces `RESPONSIVE`; absent a match, unresolved or incomplete evidence produces `UNCLEAR`; a complete set of
resolved non-matches and near misses produces `NON_RESPONSIVE`. The result is stored as a hash-checked,
lineage-preserving `ANALYSIS_DOCUMENT_DECISION` artifact without a model request. Non-positive criterion semantics
are explicitly returned as `MODEL_FALLBACK_REQUIRED`, preserving the planned bounded fallback boundary without
silently applying the wrong policy. Existing adjudication workflows gain the deterministic decision step on resume,
so prior extraction and adjudication work is reused.

The deterministic compatibility projection is now implemented as the third resumable step of the same shadow
workflow. It writes a lineage-complete `ANALYSIS_COMPATIBILITY_RESULT` whose completed `output` validates against the
existing `document-analysis-pipeline-v4` result schema. It carries only adjudicated matches and near misses, uses
cited observation facts as a bounded evidence digest, translates coverage from the immutable extraction plan, and
records the provenance of every direct-output field. It does not invent narrative or clarification questions. A
decision that requires the bounded model fallback produces a durable `BLOCKED` compatibility artifact instead of a
fabricated direct result. Existing v2 adjudication workflow identity remains stable so completed criterion packets and
document decisions resume into this new step without replay.

The representative back-test harness and downstream synthesis compatibility check are now implemented. The v7
conformance runner assembles every candidate with the same production compatibility function, validates the direct
schema, and compiles it into the current synthesis manifest. The read-only comparator verifies frozen direct artifact
hashes, compares determinations and criterion signatures, and compiles both complete populations without publishing
either result.

A first offline pass reused the 24 existing v2 conformance reports: all 24 direct artifacts and candidate reports were
valid; both populations compiled into synthesis manifests; 23 determinations agreed; and one historical candidate was
`UNCLEAR` where the direct baseline was `NON_RESPONSIVE`. That disagreement came from four `UNMAPPED` observations
produced by the older extraction prompt. A later role-split report for the same document, using Flash Lite extraction
and 3.7 Flash adjudication, agreed with the baseline and compiled successfully. Criterion signatures differed on 17
documents, largely because the direct path emitted non-canonical negative labels and broad near misses that the
evidence-first adjudicator did not admit. These differences require cohort review but are not schema failures.

The authorized v7 role-split calibration is complete. It ran all 24 frozen documents three times (72 runs) with
`google:gemini-3.5-flash-lite` extraction and `google:gemini-3.7-flash` adjudication. Seventy runs agreed with the
frozen direct determination, 71 produced valid compatibility and synthesis inputs, 23 of 24 documents were stable
across all repeats, and no observations were dropped. The run made 505 provider requests and consumed 11,231,578
input tokens and 106,441 output tokens, with 1,182,214 milliseconds of aggregate provider latency.

The original objective v8 rollout gate is `BLOCKED`. Document `247c3be1-5d40-42d6-b868-934894f99248`, whose direct baseline is
`RESPONSIVE`, produced one correct `RESPONSIVE` decision, one false-negative `NON_RESPONSIVE` decision, and one
incomplete run after the attorney-client adjudication stage failed with `MODEL_FAILURE`. The two completed runs found
the same attorney-client and work-product evidence categories but adjudicated their boundary differently. This is an
adjudication reliability and semantic-stability problem, not an extraction coverage problem. The failed historical
report had an empty provider error message and no captured invocation. Future reports now preserve the wrapped
exception type even when its message is empty, but the old report cannot be reconstructed retroactively.

Criterion signatures agreed exactly in only 8 of 72 runs. Those 64 criterion-only differences remain a representative
review item, not an automatic safety-gate failure, because the evidence-first contract intentionally normalizes broad
or non-canonical direct-path labels. Production routing remains disabled until the responsive false negative is
resolved, the model-stage failure is durably handled, and the full frozen cohort passes the repeated objective gate.

Focused diagnosis showed that the document body supplied to evidence extraction omitted the imported email sender and
recipient even though Artifact metadata retained both values. The direct path's one agreeing run had inferred the
missing participants; the stricter adjudicator correctly refused to do so. The evidence contract now supplies
hash-pinned structured document metadata as citable `metadata:` evidence alongside the body text. Metadata values are
excluded from content-free plans and reports, while their hash and citation IDs participate in plan, unit, cache, and
observation identity. The persisted direct-output contract accepts typed metadata citations but coverage remains
strictly paragraph based.

With the metadata-aware v9 document contract and strengthened element-preservation prompt, the previously unstable
document passed three of three focused repeats as `RESPONSIVE`. All three agreed with the frozen baseline, completed
without failed stages or dropped observations, and produced valid compatibility and synthesis inputs. The focused gate
therefore passes.

The complete v9 calibration has now rerun all 24 frozen documents three times. All 72 planned reports completed;
70 determinations agreed with the frozen baseline, 71 reports produced valid compatibility and synthesis inputs,
and no observations were dropped. The run made 505 provider requests and consumed 11,439,962 input tokens and
94,420 output tokens, with 1,105,406 milliseconds of aggregate provider latency. The previously unstable privilege
document passed all three full-cohort repeats as `RESPONSIVE`, so the metadata-aware contract resolves the targeted
semantic false negative.

The objective rollout gate remains `BLOCKED` for two independent residual issues. Document
`05d4d705-2134-418c-a91d-45aba385d36d` had a transient extraction `ConnectError` on window 6 in repeat 1; repeats 2
and 3 completed and matched the `NON_RESPONSIVE` baseline. Document
`9aa300e9-5cb4-4371-92c4-0bd3b77db854` produced an `UNMAPPED` support observation in repeat 1, which conservatively
made the document `UNCLEAR`; repeats 2 and 3 matched the `NON_RESPONSIVE` baseline. The gate reports six blocking
reason categories because these two root causes propagate into incomplete-decision, failed-stage, material-difference,
stability, compatibility, and synthesis checks. Deterministic backtests confirm 24/24 determination agreement in
repeats 2 and 3; repeat 1 contains one invalid report and one material disagreement. The remaining work is therefore
targeted transport retry/resume durability plus deterministic treatment of an isolated `UNMAPPED` observation, not a
reopening of the metadata repair.

Criterion signatures agreed exactly in 3 of 72 v9 runs. The 69 criterion-only differences remain a representative
review item rather than an objective rollout blocker because the candidate intentionally normalizes legacy broad and
non-canonical labels. They still require qualitative review before production routing is approved.

The two residual blockers are implemented and focused v10 calibration now passes. Extraction and adjudication
fragments persist a `retryable` classification; retryable failures automatically advance at the unit boundary up to
the existing three-attempt ceiling, and restart resumes that next attempt without an operator flag. Successful units
remain reusable, while non-retryable and exhausted failures remain explicit partial outcomes. The conformance runner
uses the same bounded retry classification and records every failed attempt in `retry_failures`. The document-decision
policy is now `positive-any-match-v2`: `UNMAPPED` observations remain counted in criterion, compatibility, and operator
diagnostics but are non-dispositive and cannot independently force `UNCLEAR`.

A focused model-backed run repeated both formerly blocking documents three times using Flash Lite extraction and 3.7
Flash adjudication. All 6 reports completed, all 6 matched the frozen `NON_RESPONSIVE` baseline, all 6 produced valid
compatibility and synthesis inputs, both documents were stable, and the focused rollout gate passed with zero blocking
reasons.

The complete v10 calibration then reran all 24 frozen documents three times. All 72 reports completed, all 72 matched
the frozen baseline determination, all 72 produced valid compatibility and synthesis inputs, all 24 documents were
stable across repeats, and there were no failed stages, incomplete decisions, harness failures, dropped observations,
or material decision differences. The run made 513 provider requests and consumed 11,497,408 input tokens and 107,898
output tokens, with 1,176,000 milliseconds of aggregate provider latency. One extraction window encountered a
retryable `ConnectError`; the bounded unit retry recovered automatically and its document completed normally, directly
exercising the transport-durability fix. Deterministic back-comparisons for each repeat independently confirmed 24/24
determination agreement, zero invalid or missing reports, and zero material decision differences. The objective v10
rollout gate therefore passes with no blocking reasons.

Criterion signatures differ from the legacy baseline in 71 of 72 v10 reports. This remains a qualitative review item,
not an objective gate blocker, because the candidate deliberately replaces legacy broad and non-canonical labels with
the frozen canonical criterion catalog. Production enablement still requires that review and publication of the v2
managed extraction binding described below.

The qualitative review is complete for the controlled-rollout decision. The 66 non-responsive runs remain
non-responsive: 62 consistently report only `draft_contracts_and_documents:NEAR_MISS`, two report no criterion match,
and two also report `third_party_disclosures:NEAR_MISS`. The legacy baseline used twelve different signatures for the
same non-responsive population, including broad, duplicated, case-varied, and non-canonical labels such as
`not_privileged`, `commercial_contract`, and `privilege`. The candidate therefore reduces label variance without
introducing a positive match. All six responsive runs consistently retain
`attorney_client_privilege:PRIMARY`. Work-product treatment varies between absent, `NEAR_MISS`, and `PRIMARY`, but it
never changes the document decision because attorney-client privilege independently supports responsiveness. This is
acceptable for a controlled rollout, while per-criterion stability remains a monitoring requirement before making the
route the default.

The durable shadow launcher and managed extractor now use the same metadata-aware contract. New plans use task and
launcher v2, pin a context hash and citation IDs without persisting metadata values, and require managed input schema
`matter_definition_evidence_extraction_input_v2`. A read-only live dry run correctly rejected the earlier v1 binding
rather than silently dropping metadata. The standard-skill bootstrap has now published and bound v2, and a subsequent
live dry run verified launcher v2 with the frozen metadata context.

### Phase 5: narrative and operations

- Add optional narrative over selected observation IDs.
- Add evidence-digest fallback and completed-with-errors behavior.
- Add stage progress, budgets, outcome manifests, tenant-admin diagnostics, and stage-specific retry.
- Deliverable: complete operator workflow without map/reduce internals.

Implementation status: the optional narrative role is now isolated in
`matter_definition_evidence_narrative_shadow_v1`. Its managed schema admits only observation IDs selected from material
criterion dispositions, permits an empty result, and explicitly prohibits re-adjudication or invented policy. The
durable `ANALYSIS_NARRATIVE_PROJECTION` artifact contains both the observation-referenced prose and a validated direct
schema `presented_output`; only `summary` and `responsiveness_summary` may differ from the deterministic compatibility
result. All decision fields are checked for exact preservation. The controlled `scripts.run_evidence_narrative_shadow`
launcher is dry-run by default, makes at most one model request, reuses a completed artifact, and supports explicit
stage-only retry. Narrative failure produces `COMPLETED_WITH_ERRORS` and identifies the unchanged deterministic
compatibility artifact as the fallback. Migration `0020_narrative_projection` admits the new artifact type.

The collection-level outcome foundation is also implemented. Extraction and adjudication workflows retain their
per-document artifact references, and `scripts.build_evidence_outcome_manifest` deterministically reconciles the
selected assessment population into `ANALYSIS_OUTCOME_MANIFEST`. The manifest records each stage, bounded errors,
retryable stages, the selected synthesis artifact, readiness/fallback state, and deduplicated workflow token/request
usage. A narrative failure selects the deterministic compatibility result as `READY_WITH_FALLBACK`; it never makes the
document unavailable. Migration `0021_analysis_outcome_manifest` admits the new artifact type.

The Phase 5 operator read surface is now implemented. The assessment API exposes the reconciled outcome summary and
per-document readiness rows. Matter administrators see operational readiness, fallback, and retry eligibility without
workflow or artifact identifiers. Authorized tenant administrators additionally receive workflow/artifact usage and
stage distributions in a technical-diagnostics section that is at the bottom of the assessment and closed by default.
The checked-in OpenAPI document and generated web client include the endpoint. An assessment with no evidence-first
workflows returns an empty outcome without querying the artifact store.

Durable stage-specific retry is now implemented for extraction, adjudication, and narrative. The tenant-admin-only
API validates that the selected stage is retryable and that its current model binding still reproduces the frozen
plan, locks the target workflow, records the requesting administrator, and transactionally enqueues a distinct DBOS
operation. The API never performs model work. The worker reconstructs the frozen input and resumes the existing
workflow with `retry_failed=True`: successful extraction units and criterion packets are reused, adjudication rebuilds
only its deterministic downstream decision/projection, and narrative retry never reopens adjudication. A failed retry
is persisted back to the target workflow with a bounded operator-safe error. The assessment UI exposes these actions
through one per-document action menu and explains what will be reused. Matter administrators can see retry eligibility
but must ask a tenant administrator to queue the currently non-authoritative shadow operation.

Upstream retry propagation is also implemented. Before an extraction or adjudication retry is queued, completed
downstream workflows for that assessment document are marked `CANCELED` with an
`UPSTREAM_RETRY_SUPERSEDED` lineage marker; their immutable artifacts remain available for audit but are excluded from
the current outcome manifest. A retry is refused while any affected descendant is still queued, waiting, or running.
After the selected stage completes, the worker rebuilds only the necessary descendants: extraction continues through
adjudication and optional narrative, adjudication continues through optional narrative, and narrative remains isolated.
If the assessment is explicitly routed to `EVIDENCE_FIRST_V1`, the repaired document outcome is reconciled into its
review-batch row and synthesis is regenerated. Manual shadow runs rebuild only the shadow chain and do not mutate the
assessment result. A downstream propagation failure does not overwrite a successfully completed upstream retry.

The Phase 5 synthesis consumer is now implemented behind an explicit frozen assessment setting. Only assessments with
`configuration_snapshot.synthesis_document_source = EVIDENCE_FIRST_V1` consume the evidence-first outcome manifest;
all existing and unconfigured assessments continue to load direct document-analysis results. The consumer prefers a
completed narrative `presented_output`, automatically falls back to the deterministic compatibility output, verifies
artifact hashes and matter/document lineage, refuses to synthesize while any evidence outcome is pending, and records
fallback, action-required, and artifact-error counts in synthesis coverage diagnostics. It never silently mixes direct
and evidence-first results. Production routing remains gated by the Phase 4 calibration decision.

The Phase 5 parent orchestration is also implemented as
`matter_definition_evidence_assessment_v2`. An explicitly dispatched
run freezes the extraction, adjudication, and narrative bindings and models plus the evidence-first synthesis source
before work begins. It retrieves the assessment population, runs one durable extraction → adjudication → optional
narrative coordinator per document, waits for every child, reconciles terminal
readiness into the review-batch run, and only then invokes synthesis and assessment completion. A narrative failure
keeps the deterministic compatibility result; a document-stage failure is isolated and reported without stopping
other documents. Missing-source failures that never create an extraction workflow are persisted as terminal batch
failures so synthesis does not wait indefinitely. The dispatcher is intentionally separate from the default assessment
dispatcher, preserving the production-routing gate.

The first performance implementation of the v2 parent removes serial extraction within a document. It checkpoints the
immutable plan, dispatches every extraction unit as an independently identified DBOS child, and compacts only after that
document's unit handles are terminal. Document coordinators, extraction units, adjudication, and narrative now have
separately configurable queues. Unit workers checkpoint their `SkillRun` before provider admission, release the database
connection during rate-limit/provider waits, and defer aggregate workflow accounting to the document compaction
checkpoint to avoid hot-row contention. Evidence extraction also has its own configurable admission budget (default
60 RPM / 1,000,000 estimated input TPM) while retaining provider 429 backoff. Cross-document provider Batch API
submission and the direct-path/evidence-path hybrid remain later throughput gates; the v2 change improves real-time
controlled-rollout execution without changing evidence, retry, or synthesis semantics.

### Phase 6: rollout and migration

- Shadow-run both paths on a production calibration cohort.
- Review differences and pin approved model roles.
- Enable evidence-first routing per tenant/task behind rollback control.
- Back-test the 250-document baseline and compare requests, repair, replay, usage, time, cost, and decisions.
- Make evidence-first the default for large Matter Definition documents after gates pass.
- Migrate Question Answering and other adapters, then retire the old reducer.

Current objective gate status: `PASSED` for the complete v10 cohort. The cohort report emits machine-readable blocking reasons for missing runs,
harness failures, incomplete decisions, failed model stages, material decision differences, unstable repeated
determinations, invalid compatibility outputs, incompatible synthesis inputs, and dropped observations. Criterion-only
signature drift is emitted separately as a manual-review item; 71 of 72 v10 reports carry that non-blocking review
item.

The first controlled rollout slice is now implemented. The standard-skill bootstrap publishes the v2 extraction
contract and assigns stable system model keys to the three evidence roles. Runtime configuration resolves extraction
to `google:gemini-3.5-flash-lite` and adjudication and optional narrative to `google:gemini-3.7-flash`, reproducing the
validated cohort split rather than inheriting one shared default model. Read-only live planners verified all three
active bindings. Legacy evaluation plans that predate metadata context and legacy v1 document decisions remain
readable, so publishing the new bindings does not strand durable completed work.

Assessment creation now accepts an explicit `execution_mode`. `DIRECT_V1` remains the default and rollback path.
`EVIDENCE_FIRST_V1` is accepted only for a tenant administrator and only while
`DEFINITION_ASSESSMENT_EVIDENCE_SHADOW_ENABLED=true`; the same flag is the global kill switch. The selected mode is
frozen in the assessment configuration and audit event. Evidence-first retries re-enter the durable parent route and
reuse compatible artifacts; the direct frozen-batch reanalysis action is rejected for evidence-first runs because it
would silently cross processing models. The launch dialog labels evidence-first as a controlled rollout and explains
the authorization and rollback behavior. Making this route the default remains deferred until controlled tenant runs
confirm per-criterion stability and operating cost.

## 14. Testing

### Unit and property tests

- Identical inputs produce identical plan, unit, observation, packet, and result identities.
- Every source range is covered and overlap does not create false omissions.
- Oversized paragraphs split without loss or reordering.
- Every request satisfies the frozen capability profile.
- Every schema transformation has a tested enforcement policy.
- Invalid optional observations do not affect valid siblings.
- Required invalid output fails only its unit.
- Later stages cannot reference IDs outside admitted input.
- Incomplete coverage cannot produce an inadmissible complete negative.
- Hard budgets prevent dispatch beyond their ceiling.

### Contract and integration tests

- Batch and realtime paths enforce the same normalized contract.
- Direct and evidence-first results share the compatibility schema.
- Citation provenance resolves from narrative to decision to observation to source.
- Restart resumes completed extraction and adjudication units.
- Retry reuses successful work and records lineage.
- Ambiguous persistence failure reconciles without untracked duplicate requests.
- Failed and repaired calls appear in usage totals.
- Narrative failure preserves decision and evidence.
- Existing synthesis consumes the new result without semantic change.

### Model conformance tests

- Compare first response before automatic repair.
- Exercise malformed JSON, extra fields, invalid enums, unknown paragraph or observation IDs, excessive arrays, empty required fields, and inconsistent decisions.
- Compare candidate models independently for each role.
- Manually review material direct-versus-evidence-first disagreements.
- Retain frozen prompts, schemas, settings, model identities, and results.

### Load and failure tests

- Exercise OCR, email threads, dense identifiers, enormous paragraphs, and many short paragraphs.
- Exercise many extraction units and criterion sharding.
- Inject size errors, throttles, timeouts, malformed responses, usage and budget exhaustion, cancellation, artifact failure, and worker termination at every boundary.
- Verify bounded memory, database growth, concurrency, and artifact counts.
- Verify source text is absent from generic logs and manifests.

## 15. Acceptance gates

Evidence-first execution may become authoritative when:

- no whole document is replayed because one unit or narrative failed;
- every provider request, including failures and repairs, reconciles to invocation and usage telemetry;
- provider-versus-application schema differences are versioned and tested;
- extraction cannot cite outside its unit and later stages cannot invent evidence;
- optional invalid observations can be isolated without discarding valid siblings;
- coverage and partial or insufficient reasons are exact and visible;
- negative decisions obey the published coverage policy;
- direct and evidence-first results pass compatibility and synthesis tests;
- material decision differences have been reviewed and accepted or resolved;
- back-testing reports request, repair, replay, usage, latency, and cost changes with no unexplained amplification;
- restart, cancellation, targeted retry, budget, and narrative-failure tests pass;
- technical diagnostics are tenant-admin only, closed by default, and actionable;
- coding projection and matter promotion remain explicit and isolated.

Phase 0 establishes model-specific quality and performance thresholds from the frozen corpus. Those measured gates must be approved before rollout rather than inventing arbitrary repair-rate or latency targets here.

## 16. Open decisions for Phase 0 and Phase 1

- Which candidate models should be tested for extraction, adjudication, decision, and narrative roles against the
  pinned gemini-3.5-flash-lite baseline?
- Can the provider dynamically enumerate all paragraph IDs for the largest extraction unit?
- Which guidance structures can provide a stable criterion catalog automatically, and when must an operator confirm keys?
- Which criterion and document decisions are safe to combine without a model?
- What packet and refinement defaults follow from measured serialized request size?
- Which identities need first-class Core columns rather than artifact or coverage JSON?
- May a partial positive proceed to synthesis, and how must it be labeled?
- Which narrative fields are mandatory downstream and which can use an evidence digest?
- When is Batch API latency preferable to realtime adjudication or narrative?

The conservative first release evaluates every planned source unit, requires complete coverage for a negative decision, uses UNMAPPED only as a non-dispositive evidence bucket, and retains direct analysis as the rollback baseline.
