# Large-Document Query-Directed Assessment Implementation Plan

Status: Proposed. This plan changes only Matter Definition assessment processing for documents that do not fit the
small-document direct request. It does not change authoritative document review, coding, or other workflows that need
complete-document evidence coverage.

## 1. Decision and outcome

Matter Definition assessment evaluates whether review guidance is clear, complete, and consistently applicable. It
does not create an authoritative coding or prove that every paragraph of every selected document was reviewed.

Keep the existing direct path for small documents. When a selected document is too large for the complete direct
assessment request, use the assessment's already-frozen retrieval plan and candidate provenance to analyze only the
passages likely to help evaluate the guidance:

~~~text
selected assessment document
        |
        +-- complete direct request fits --> existing small-document analysis
        |
        +-- request does not fit ----------> persisted query provenance
                                                |
                                                v
                                      passage selection and dedupe
                                                |
                                                v
                                        bounded context expansion
                                                |
                                                v
                                      assessment-signal extraction
                                                |
                                                v
                                    query/criterion-level synthesis
~~~

Large-document processing must not scan every source window by default, produce a document-level
NON_RESPONSIVE result from absent retrieved signals, or invoke the authoritative evidence/adjudication/narrative
graph solely to support assessment synthesis.

## 2. Existing inputs to reuse

No new query-planning model call is required. Assessment planning already persists:

- MatterDefinitionAssessmentQuery, including query ordinal, criterion key and label, rationale, search request,
  quota, and measured result count;
- MatterDefinitionAssessmentCandidate.retrieval_provenance, including every contributing query ordinal, criterion
  key, rank, score, and best passage;
- best-passage chunk identity, ordinal, character range, text, and score;
- candidate selection reason, including CRITERION_QUOTA, RANK_FUSION, and CONTROL_SAMPLE;
- the frozen search-index generation and Matter Definition revision used to produce the batch.

These records become the routing plan for large documents. The implementation must verify that passage coordinates
still resolve against the pinned source artifact and content hash before using them. It must not silently rerun a
different query plan or current search-index generation.

## 3. Scope boundaries

### In scope

- Matter Definition assessment documents whose complete direct request exceeds the admitted provider/model request;
- query-attributed passage selection, deterministic deduplication, and bounded neighboring context;
- focused assessment-signal extraction for the relevant criterion or criteria;
- adaptive expansion when the retrieved passage lacks necessary local context;
- control-sample probing and query-plan recall diagnostics;
- mixed synthesis inputs from small direct documents and large query-directed documents;
- durable passage plans, results, retry, accounting, progress, and operator diagnostics;
- execution-mode-aware request, token, cache, and cost estimates.

### Out of scope

- changing the small-document direct path;
- replacing authoritative evidence-first analysis used for actual review or coding;
- making query-directed results authoritative matter codings;
- claiming complete source coverage for a large assessment document;
- generating a new query plan after the assessment batch is frozen;
- silently converting an absence of retrieved signal into a negative document decision;
- scanning the entire large document as an automatic fallback.

## 4. Semantics: assessment signals, not document evidence

Introduce an assessment-specific result contract. A signal is a cited example that helps assess the guidance; it is
not an exhaustive evidence record and does not carry an authoritative document determination.

~~~text
assessment_id
matter_document_id
query_id and query_ordinal
criterion_key
signal_type:
  GUIDANCE_APPLIES_CLEANLY
  BOUNDARY_EXAMPLE
  AMBIGUOUS_UNDER_GUIDANCE
  POSSIBLE_GUIDANCE_GAP
  COUNTEREXAMPLE
  NO_USEFUL_ASSESSMENT_SIGNAL
fact
source_range_ids
context_status: SUFFICIENT | EXPANDED | INSUFFICIENT
selection_provenance
~~~

NO_USEFUL_ASSESSMENT_SIGNAL means only that the analyzed passages did not contribute to guidance assessment. It
must not be projected as NON_RESPONSIVE, a negative coding, or proof that the rest of the document lacks a signal.

Small direct results are projected into the same assessment-signal vocabulary for synthesis. The original direct
result remains available for compatibility and audit, but synthesis consumes a unified signal manifest rather than
requiring every large document to imitate the direct document-analysis schema.

## 5. Large-document routing

Routing is based on the complete serialized small-document assessment request and its output reserve, using the
resolved direct model's capability profile. Character count may be a fast precheck but is not the final boundary.

Persist one of these reasons for every selected document:

~~~text
DIRECT_REQUEST_FITS
QUERY_DIRECTED_REQUEST_TOO_LARGE
QUERY_DIRECTED_CONTROL_SAMPLE
SOURCE_UNAVAILABLE
~~~

Only QUERY_DIRECTED_REQUEST_TOO_LARGE and QUERY_DIRECTED_CONTROL_SAMPLE enter the new passage workflow. A document
must not be split merely to make all assessment documents use one implementation.

## 6. Passage planning

### 6.1 Query-attributed documents

For a large document selected by retrieval:

1. Load all persisted provenance entries for the document.
2. Group them by query ordinal and criterion key.
3. Validate each best-passage chunk and character range against the pinned source.
4. Deduplicate identical and overlapping passages while retaining every contributing query.
5. Expand each selected range to stable paragraph or chunk boundaries.
6. Add bounded neighboring context before and after the hit.
7. Allocate the document budget in round-robin query order so a high-scoring query cannot consume all space while
   another represented criterion receives no passage.
8. Combine compatible passages into as few model requests as possible without mixing unrelated criterion contracts
   or exceeding the complete serialized request limit.

The first implementation should use the already persisted best passage for each query-document relationship. A
later measured optimization may request additional top passages for a query, but it must retain the same frozen query
identity and search-index generation.

### 6.2 Metadata-only hits

Some search requests may select a document from metadata without producing a best text passage. Preserve the matched
metadata as citable assessment context. Add source text only when the criterion cannot be assessed from metadata and
a deterministic fallback range, such as the document header or first substantive paragraphs, is admitted by policy.
Do not substitute the complete document.

### 6.3 Control-sample documents

Control documents intentionally have no retrieval provenance. They test whether the query plan missed useful
assessment material. For a large control document:

1. Execute the frozen assessment queries scoped to that document and pinned search generation, without generating
   new queries.
2. Retain a bounded top passage per criterion when available.
3. Add a small deterministic source sample across the beginning, middle, and end only for recall diagnostics.
4. Analyze those passages under a distinct CONTROL_SAMPLE provenance.

A material signal found only through the deterministic control sample is a query-plan surprise. It must be surfaced
to synthesis and the operator as a reason to refine queries or expand the assessment. It must not cause every large
document to be scanned automatically.

### 6.4 Immutable passage plan

Persist a content-addressed ASSESSMENT_PASSAGE_PLAN containing:

~~~text
assessment, matter, document, and source identities
source artifact and content hash
Matter Definition revision and retrieval-plan hash
query rows and candidate-provenance hashes
search-index generation
routing reason
ordered passage ranges and contributing queries
context-expansion policy and version
direct-model capability profile
request, token, time, and cost budgets
plan hash
~~~

Do not duplicate the complete source text in the plan. Passage text is loaded from the pinned source by verified
range when the unit runs.

## 7. Adaptive context expansion

The first request receives the selected passage, local neighbors, available citable metadata, the relevant criterion,
and the review guidance needed to interpret it. It may return a typed context request rather than invent missing
facts:

~~~text
NO_EXPANSION
PRECEDING_CONTEXT
FOLLOWING_CONTEXT
THREAD_PARTICIPANTS
DEFINED_TERM
QUOTED_MESSAGE_BOUNDARY
ATTACHMENT_OR_PARENT_CONTEXT
OTHER_LOCAL_CONTEXT
~~~

Application code resolves admitted expansion requests deterministically from the same source or already-linked
metadata. Expansion is limited by a per-passage round count and document token budget. Exceeding either produces
INSUFFICIENT_CONTEXT; it does not trigger an unchanged retry or complete-document scan.

The operator may explicitly request broader analysis for a named document after seeing the limitation. That action
creates a child plan with a larger budget and clear cost estimate; it is not an automatic workflow retry.

## 8. Prompt, schema, and caching shape

Use a fixed provider-facing assessment-signal schema. Do not embed document-specific paragraph, chunk, or observation
IDs into the provider schema. Supply admitted IDs in the dynamic payload and validate all returned references locally.
An invalid optional signal is dropped with diagnostics; an invalid required reference fails only that passage unit.

The reusable prefix should contain:

~~~text
platform and assessment-signal instructions
review guidance or stable guidance excerpt
one criterion definition
fixed output schema
~~~

The dynamic suffix contains passage text, metadata, admitted range IDs, query provenance, and prior expansion state.
Cache identity is scoped to tenant, matter, guidance revision, criterion, skill version, model/settings, and schema. It
must not contain document ID, passage ID, unit ID, or attempt number.

Implement Google explicit cached-content lifecycle only after the fixed request shape is verified. Cache resources
must be tenant/matter/revision/criterion scoped, have bounded TTL, record their provider identity and cost, and be
invalidated by guidance, prompt, schema, model, or authorization changes. Correct selective processing is the primary
optimization; caching is secondary.

## 9. Durable execution and artifacts

Use independent durable boundaries:

~~~text
PLANNING_PASSAGES
ANALYZING_PASSAGES
EXPANDING_CONTEXT
COMPACTING_SIGNALS
READY_FOR_SYNTHESIS
COMPLETED | COMPLETED_WITH_ERRORS | INSUFFICIENT | FAILED | CANCELED
~~~

Add immutable artifacts:

~~~text
ASSESSMENT_PASSAGE_PLAN
ASSESSMENT_SIGNAL_FRAGMENT
ASSESSMENT_SIGNALS
ASSESSMENT_SIGNAL_MANIFEST
~~~

Each passage attempt records its plan/unit identity, queries and criteria, exact source ranges, terminal status,
normalized signals, context requests, adjustments, provider invocations, usage, and errors. Successful passages
survive worker restart. Transport retry, context expansion, validation failure, and operator-requested broader
analysis have distinct identities and limits.

Fragments contain source references and bounded cited facts, not a second copy of the complete document. Compaction
deduplicates overlapping signals while retaining every query and passage that contributed.

## 10. Synthesis integration

Create an assessment synthesis source mode such as ASSESSMENT_SIGNALS_V1. It consumes:

- projected signals from small direct documents;
- native signals from large query-directed documents;
- query result counts, quotas, selected-document counts, and control-sample outcomes;
- passage/context limitations and query-plan surprises.

Synthesis groups by the frozen query and criterion plan, not by a presumption that every document has a complete
coding. It reports:

~~~text
documents and passages examined per query and criterion
clean examples, boundary examples, ambiguities, gaps, and counterexamples
signal diversity and repetition/saturation
queries with insufficient material
control-sample surprises
documents requiring optional broader context
recommended guidance changes and follow-up questions
~~~

If a query has no useful signal, synthesis states that the assessment lacks material for that query. It must not infer
that the query's criterion is absent from the population or that its documents are non-responsive.

## 11. Estimation and budgets

The passage plan is available before model dispatch, so preflight can estimate the exact baseline request count and
serialized input for every large document. Report separately:

- small direct documents and requests;
- large query-directed documents, passages, and initial requests;
- bounded context-expansion reserve;
- control-sample probing;
- synthesis requests;
- resolved models and standard/batch/cache consumption assumptions;
- zero-cache and expected-cache token and cost scenarios.

Runtime reconciles actual requests and usage against the plan. Variance categories are additional context, transport
retry, invalid output, operator expansion, synthesis regeneration, and cache miss. The estimator must not reuse the
legacy exhaustive-window count for this mode.

## 12. Operator experience

Launch explains that small documents receive complete direct assessment while large documents use query-directed
passages for guidance evaluation. It shows query allocation, control-sample size, large-document count, passage and
expansion budgets, and estimated cost.

Progress shows:

~~~text
small documents complete
large passage plans complete
passages analyzed and expanded
signals by query/criterion
queries with insufficient material
control-sample surprises
estimated versus actual usage
~~~

Results distinguish No useful assessment signal in selected passages from a document coding. Available actions are
Analyze broader context, Refine or rerun query plan in a new assessment, and Expand assessment batch. Technical range,
query, cache, invocation, and retry diagnostics remain tenant-admin only and closed by default.

## 13. Delivery phases

### Phase 0: frozen baseline and passage-plan dry run

- Use assessment 219a549c-0a9e-40a8-a3a9-12ae70652632 as the initial performance baseline: 250 selected documents,
  49 multi-unit large documents, 3,596,200 large-document input tokens across extraction/adjudication/narrative, and
  approximately $1.51 attributable to those documents.
- Reconstruct passage plans for the 49 large documents from persisted queries and candidate provenance without model
  calls or state changes.
- Verify best-passage ranges against protected source artifacts and classify missing or metadata-only passages.
- Report planned passages, context size, query/criterion allocation, expected requests, zero-cache tokens, and cost.
- Deliverable: content-free baseline report and frozen passage-plan corpus.

### Phase 1: contracts and durable planning

- Add assessment-signal, context-request, passage-plan, fragment, compacted-signal, and manifest contracts.
- Add large-only serialized-request routing and immutable plan identities.
- Add fixed provider schema and local reference validation.
- Add dry-run commands and deterministic tests; do not change assessment results.
- Deliverable: restart-safe plans and local contract validation without provider calls.

### Phase 2: non-authoritative shadow execution

- Execute only the frozen large-document passage corpus.
- Compare query-directed signals with the prior exhaustive assessment outputs and cited source.
- Measure material-signal recall through reviewer evaluation, control-sample surprises, requests, tokens, cache, cost,
  latency, expansion frequency, invalid references, and failed units.
- Do not generate document determinations or update synthesis.
- Deliverable: reviewed large-document signal cohort and calibrated passage/expansion defaults.

### Phase 3: mixed synthesis shadow

- Project existing small direct results into assessment signals.
- Build a complete mixed signal manifest for the same assessment.
- Run synthesis in shadow and compare findings, guidance recommendations, follow-up questions, topic coverage, and
  unsupported conclusions with the current synthesis.
- Require reviewer sign-off on material differences.
- Deliverable: synthesis-compatible mixed population without authoritative UI changes.

### Phase 4: controlled assessment rollout

- Add an explicit tenant-admin assessment mode such as QUERY_DIRECTED_LARGE_V1 behind a kill switch.
- Keep small-document execution unchanged and route only non-fitting documents to passage analysis.
- Expose passage/query progress, limitations, and broader-context actions.
- Reconcile final usage and estimator variance.
- Deliverable: operator-visible controlled rollout with the current assessment mode as rollback.

### Phase 5: default large-document assessment route

- Run representative 250- and 500-document cohorts.
- Approve measured quality, query coverage, control recall, durability, latency, token, and cost gates.
- Make query-directed processing the default only for large Matter Definition assessment documents.
- Retain exhaustive evidence-first execution for workflows that actually require authoritative document decisions.
- Deliverable: separated assessment and authoritative-review processing models.

## 14. Testing

### Unit and property tests

- Direct-fit documents never enter the passage workflow.
- Query/candidate provenance produces deterministic passage plans.
- Overlap deduplication preserves all contributing query identities.
- Round-robin allocation cannot starve a represented criterion.
- Returned references resolve only to admitted ranges.
- Context expansion cannot escape the pinned source, round limit, or document budget.
- No-signal output cannot become a negative document determination.
- Plan and result identities change when source, query plan, guidance, model, prompt, schema, or expansion policy changes.

### Integration and durability tests

- Search generation, source hash, and best-passage coordinates are verified before dispatch.
- Metadata-only and control-sample documents follow their explicit policies.
- Restart reuses successful passage units; retry does not replay siblings.
- Mixed signal manifests include every selected document with direct, query-directed, no-signal, insufficient, or
  source-unavailable status.
- Synthesis reports missing query material without inventing a population-level negative.
- Failed and expanded attempts reconcile to provider usage.

### Quality tests

- Reviewer comparison covers clean examples, subtle boundary cases, long threads, contracts, OCR, repeated boilerplate,
  metadata-dependent signals, and intentionally irrelevant large documents.
- Control samples measure material signals missed by query-attributed passages.
- Query-directed and prior exhaustive outputs are compared at the guidance-finding level, not document-coding
  agreement.
- Material guidance recommendations and follow-up questions must be supported by cited admitted passages.

## 15. Acceptance gates

Controlled rollout requires:

- zero changes to small-document routing and output;
- every large-document model input attributable to a frozen assessment query or explicit control-sample policy;
- no automatic exhaustive source scan and no document-level negative inferred from unselected text;
- exact source/query provenance and durable passage-level retry;
- synthesis coverage for every frozen query and criterion;
- explicit insufficient-material and control-surprise handling;
- reviewer acceptance of material guidance findings against the shadow baseline;
- actual usage reconciled to the invocation ledger, including failed and expanded calls;
- a material reduction in large-document input, requests, latency, and cost against the frozen 49-document baseline;
- estimator bounds that contain actual usage and explain material variance;
- tenant-admin-only technical diagnostics and a tested rollback switch.

Default routing additionally requires representative 250- and 500-document cohorts, stable control-sample recall,
no unsupported synthesis conclusions, and approval that the measured savings do not remove material guidance examples.

## 16. First implementation slice

Implement Phase 0 before changing prompts or workflow routing:

1. Add a read-only passage-plan builder that loads the persisted assessment queries and selected-candidate
   provenance for assessment 219a549c-0a9e-40a8-a3a9-12ae70652632.
2. Route only its 49 multi-unit large documents into the builder.
3. Verify each best-passage chunk/range against the pinned source and record missing, invalid, duplicate, overlapping,
   and metadata-only entries.
4. Apply a versioned bounded-neighbor expansion policy and round-robin query allocation.
5. Emit a content-free JSON report containing plan hashes, query/criterion allocation, passage/range counts, estimated
   serialized tokens, request counts, expansion reserve, and cost scenarios.
6. Review that report before authorizing any model-backed shadow execution.

This slice answers whether the existing query plan provides enough usable passage provenance and quantifies the
expected savings without spending additional model tokens or changing the current assessment.
