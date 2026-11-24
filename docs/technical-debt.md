# Technical debt register

This document records known architectural and product-quality gaps that are intentionally deferred. Each item
describes the current compromise, the intended end state, and a concrete completion condition.

## Collections and ingestion

### Classify and surface item-level ingestion errors at the collection boundary

**Current state:** Collection items can be marked `READY` even when the imported source cannot support review or
downstream analysis. The EDRM Enron v2 adapter, for example, imports every corpus `.txt` entry as a document. Some
attachment entries are zero-byte text placeholders for binary attachments whose native files are not present in the
source package. Those items receive a finalized zero-byte `NATIVE` artifact and a `READY` processing status. The
absence of usable text is discovered only when a later embedding, analysis, or assessment workflow requests a
preferred text source. That downstream workflow then reports an extraction failure even though the underlying
condition is a non-retryable source-data limitation. The collection has no durable error classification, aggregate
health summary, or drill-down showing which items are affected and why.

The concrete failure observed for TREC document
`3.1134087.HQ4A1KG433D0JCQHXEM5IPVLKYZ5XZPSB.1` illustrates the gap. Its source attachment text file, imported native
artifact, and content blob are all zero bytes. The parent email identifies the attachment as `pic09372.pcx`, but the
EDRM text package contains no corresponding native PCX file to extract or OCR. Retrying document analysis cannot
repair this condition.

**Risk:** Collection readiness overstates the usable population. Operators learn about source defects only after
launching expensive downstream jobs, where the same condition may be inconsistently labeled as extraction,
embedding, indexing, or model failure. Blind retries waste resources and obscure whether corpus coverage is complete.
Empty attachment placeholders may also be counted as failed review documents rather than retained, auditable
unreviewable items within their families.

**Desired state:** Add a versioned collection-item error classifier that runs during import and every subsequent
content-processing stage. Persist a stable reason code, lifecycle stage, retryability, operator-safe explanation,
technical detail, detection timestamp, and resolution/supersession state without discarding the source item or its
family relationships. Distinguish source limitations from processing and infrastructure failures. The initial
taxonomy should cover at least empty source content, missing source/native content, unsupported format, extraction
failure, OCR required or failed, missing/corrupt artifact content, metadata incompleteness, and transient provider or
storage failures. A zero-byte placeholder with no available native must be classified as a non-retryable source-data
limitation, not as a generic extraction failure.

Expose collection health in the collection UI with totals by classification and retryability, affected-item
drill-down, source package/custodian/family context, and CSV export. Downstream consumers must use this classification
to skip or explicitly mark unreviewable items, preserve the limitation in coverage reporting, and retry only
conditions classified as retryable. Raw exceptions and sensitive technical details remain tenant-administrator
diagnostics; all authorized collection operators receive an actionable explanation.

**Completion criteria:**

- Import and processing stages emit stable, versioned item-level error codes rather than relying only on free-text job
  errors or a broad `processing_status`.
- Each classified condition records its stage, retryability, operator explanation, technical detail, timestamps,
  source artifact/package, and the event that resolved or superseded it.
- Zero-byte content is detected before an item is declared review-ready. Empty EDRM attachment placeholders remain
  linked to their parent family but are classified as non-retryable source limitations.
- Collection summaries reconcile total, usable, limited, retryable-failed, and non-retryable-unreviewable items, with
  drill-down and export from the collection UI.
- Import jobs report classification counts and representative reasons without failing an otherwise valid bulk load.
- Embedding, indexing, publishing, review-batch, and assessment workflows consume the same classification and do not
  rediscover or relabel the condition independently.
- Retry actions are offered only for retryable classifications; non-retryable conditions explain the required source
  remediation or the decision to accept the item as an explicit coverage limitation.
- Tests cover empty files, absent natives, unsupported binary attachments, extraction/OCR failures, corrupt blobs,
  transient storage failures, successful remediation, family preservation, aggregate reconciliation, and role-based
  diagnostic visibility.

## Matter Definition assessments

### Eliminate the synchronous multi-window tail from provider-batched assessments

**Current state:** Matter Definition assessment batching only sends documents that fit in one paragraph-map window
to the provider Batch API. Eligible documents are grouped into batches of up to 50 or the configured byte ceiling,
and up to eight provider batches can execute concurrently. Documents that exceed the 60,000-character map-window
limit bypass provider batching and enter the real-time document queue, where each document runs the existing
multi-window map/reduce pipeline. The parent assessment enqueues both paths up front, but it waits for every child
workflow to reach a terminal state before starting synthesis. Provider-batch results are also finalized only after
the provider marks the complete batch terminal; each finalizer validates and persists its documents sequentially in
checkpoints of 10, although multiple batch finalizers may run concurrently.

The 500-document assessment `b72ca17d-e307-49cb-9729-318802abebd1` demonstrated the resulting long tail:

- 421 single-window documents were divided into nine provider batches (eight batches of 50 and one batch of 21).
- The first eight batches were submitted concurrently at approximately 16:10 UTC. They completed around 16:12 UTC;
  the ninth completed around 16:14 UTC. The provider-batch path therefore processed its entire share in about four
  minutes.
- The provider-batch outcomes were 400 completed documents and 21 invalid model outputs.
- The remaining 79 multi-window documents bypassed provider batching. More than an hour after the provider batches
  had completed, this real-time map/reduce tail was still running even though its DBOS queue had the configured
  concurrency of eight.

The 250-document assessment `4a1622f2-7859-474c-a9db-0b9dbe548f47` exposed a second source of amplification:

- The assessment completed in approximately 4 hours 19 minutes. Its 168 single-window documents completed through
  four provider batches near the beginning of the run, leaving 82 multi-window documents on the synchronous path.
- Those 82 documents expanded into 351 map windows and 82 document reductions. Including the direct documents, the
  plan therefore required at least 601 structured model calls before any output repair.
- The run recorded 614 document-analysis calls because a failed unit caused successful sibling work to be replayed
  as part of a whole-document retry. It then made 795 provider requests because 176 of the 614 calls required at
  least one structured-output repair; five required two repairs.
- Two documents exhausted the 20,000-token per-agent output usage guard during repair. DBOS treated those failures as
  generic retriable step failures and replayed the complete document, including successful map work.
- Successful model-invocation rows recorded approximately 24.1 million input tokens and 625,699 output tokens,
  compared with the assessment's preflight estimate of approximately 6.3 million input tokens and 721,200 output
  tokens. The reported output from three failed calls adds another 105,895 tokens that are not represented in the
  successful-invocation ledger, so the true provider usage is higher than the persisted total.
- Validation retry reasons were not persisted when repair eventually succeeded, and failed invocation usage was not
  recorded when model execution raised before returning its normal envelope. The run can therefore demonstrate the
  amplification but cannot reconstruct the exact schema, citation, or semantic validation failures responsible for
  every repair.

**Why this is slow:** The batch size and parent handle-wait order are not the primary bottleneck. All batch children
are enqueued before the parent waits, and the queue concurrency allows multiple provider jobs to run together. The
dominant cost is the routing boundary: every large document becomes several synchronous model requests followed by
a reduce request. Output validation and repair can multiply those requests again, and the current DBOS step boundary
can replay the entire document after one unit exhausts its repair or usage allowance. Eight concurrently active
documents can therefore consume many sequential provider round trips while the much faster Batch API path has already
drained. Synthesis has a whole-assessment completion barrier, so the last multi-window documents determine total
elapsed time. Aggregate progress does not distinguish the completed provider-batch path from the large-document tail,
validation repair, or document replay, which makes the assessment appear as though batching itself has stalled.

**Phase 0 progress:** Model execution now classifies schema, citation, semantic, normalization, provider-parse, and
usage-limit failures and retains failed-call invocation telemetry. A read-only baseline exporter and frozen replay
harness have been added. The first corpus for assessment `4a1622f2-7859-474c-a9db-0b9dbe548f47` contains 18
multi-window cases and six clean direct controls, includes both documents with historical failed attempts and replay,
and verifies every protected source and baseline artifact by hash. The manifest contains no source text or complete
model output. This establishes a repeatable model/prompt/schema comparison boundary; it does not yet replace the
production map/reduce execution path.

Two bounded live replays establish an actionable failure mechanism. A clean direct control required one schema-repair
request even though its final determination agreed with the baseline. A five-window historical-failure document made
eight provider requests, consumed approximately 575,000 input and 28,000 output tokens, and still failed on output
usage. Its map responses attached 60 to more than 200 paragraph IDs to individual items even though the application
limit is 30. Google's provider-admitted schema removes the unsupported `maxItems` constraint, so application
validation and model repair—not provider enforcement—became the expensive control loop. The full corpus should not be
replayed through the unchanged contract; the next comparison must use canonical criterion IDs, compact evidence
observations, and item-aware validation that preserves valid siblings.

The managed document-analysis budget has been raised to 40,000 cumulative output tokens as an interim safeguard. The
same five-window case then completed with the baseline determination after eight provider requests, including two
schema-repair requests. This confirms that a larger budget can prevent premature usage exhaustion, while the retained
repair count and free-form criterion differences confirm that it does not resolve the underlying contract problem.

The first Phase 1 contract slice is now implemented without changing production routing. It freezes and hashes the
criterion catalog, defines bounded evidence observations and downstream reference-only decision contracts, generates
application and provider schemas from one source, and applies explicit item-aware normalization policies. A read-only
single-window conformance command verifies frozen hashes, makes at most one provider request with no output repair,
and reports accepted and dropped observations plus usage and schema diagnostics. This creates the measurement boundary
needed before durable extraction artifacts or routing changes are introduced. Its first historical-window run
completed with one provider request, two accepted observations, no dropped items, no adjustments, and no validation
failures. Each observation cited two paragraphs rather than the old contract's exhaustive arrays. This is a successful
extraction-contract check, not yet an end-to-end decision-agreement result.

A five-window shadow runner now performs extraction, exact deterministic overlap deduplication, criterion-scoped
adjudication, and code-owned combination without assessment writes. The historical failure document used five
extraction requests plus one or two adjudication requests and agreed with its `NON_RESPONSIVE` baseline in three
document runs. The first two runs each exposed one oversized optional observation, which was dropped locally without
repair; the final run accepted all eight returned observations. Because a dropped material signal makes validated
evidence incomplete even when source-window coverage is complete, the combiner now returns `UNCLEAR` rather than
permitting a negative. This preserves progress and avoids both whole-document replay and an unsupported conclusion.

The repeated runs also exposed model variability that the bounded workflow cannot solve by structure alone:
identical inputs produced one to eight accepted observations, and one adjudication returned `NO_MATCH` at 0.2
confidence while populating an unresolved-boundary explanation. Before routing production documents through this
path, a small repeated cohort must calibrate extraction stability and define the admitted relationship among
disposition, confidence, and unresolved boundaries without adding arbitrary thresholds.

The first V1 repeated cohort is now complete: five documents times three runs, covering both historical failures, a
responsive control, a multi-window negative/near-miss control, and a clean direct negative. Only two cases had stable
determinations and baseline agreement was nine of 15. No run entered structured-output repair; all five dropped
observations violated only the unsupported citation-array `maxItems` constraint. Five adjudications separately used
disposition-incompatible reference arrays that were normalized locally. This changes the next action: do not tune the
same prompt or add more retries. Replace bounded citation arrays with provider-enforceable scalar citation slots and
simplify adjudication references to a disposition-neutral admitted-ID shape, then replay the same cohort before
durable artifact or production-routing work.

That V2 contract and replay are now complete. Extraction emits one required primary citation plus four
required-but-nullable additional citation slots; adjudication emits one nullable primary admitted-observation
reference plus four nullable additional slots. The exact same five-document, three-repeat cohort completed 15 of 15
runs with 15 baseline agreements and stable determinations for all five cases. Detailed reports contained no
extraction or adjudication failure, schema-validation failure, normalization adjustment, or dropped observation. V2
used 71 provider requests, 1,836,452 reported input tokens (1,301,117 cached), 6,006 output tokens, and 99.550 seconds
of aggregate provider latency, compared with 77 requests, 1,178,119 reported input tokens (607,102 cached), 10,761
output tokens, and 179.875 seconds for V1. The input-token and cache-accounting change prevents a cost conclusion,
but the structural defect and its retry/adjustment symptoms are removed in this cohort. Observation counts still vary
on negative documents, so broader shadow quality calibration and explicit role/model acceptance gates remain before
production routing or durable artifact implementation.

The harness now supports separate extraction and adjudication model bindings, full-corpus selection, and verified
resume of compatible child reports. The first one-pass V2 run across all 24 frozen cases completed every document with
complete evidence, no model-output validation failure, and no dropped observation. It agreed with 23 baselines: both
responsive controls remained responsive, 21 baseline negatives remained negative, and one baseline negative became
`UNCLEAR`. The pass used 172 provider requests, 3,743,842 reported input tokens (308,817 cached), 12,216 output tokens,
and 213.082 seconds of aggregate provider latency.

The disagreement was stable across three runs. Document `9aa300e9-5cb4-4371-92c4-0bd3b77db854` repeatedly produced
material `UNMAPPED` observations from a long commercial agreement with bracketed drafting questions, so the
conservative combiner returned `UNCLEAR` at confidence zero. One repeat normalized a duplicate adjudication reference;
none lost evidence. The broader run also exposed and fixed a combiner defect that previously attempted to derive
confidence from a nonexistent `UNMAPPED` adjudication row. Remaining debt is semantic rather than structural: review
this cited pattern and freeze whether it is `NEAR_MISS`, material unmapped ambiguity, or non-responsive before tuning
the extraction prompt or approving production routing.

The operator selected `NEAR_MISS`. Prompt clarification alone was insufficient: three focused runs removed
`UNMAPPED`, but only one extraction emitted a near-miss signal and all three adjudications returned `NO_MATCH`. The
provider-facing adjudication schema now uses an explicit relationship taxonomy, and application code deterministically
maps `RELATED_MISSING_REQUIRED_ELEMENT` to `NEAR_MISS` with the stated missing boundary. Flash Lite selected that
relationship in two of three focused repeats and selected `UNRELATED` once. Remaining debt is therefore adjudication
model calibration, not another label-normalization rule.

The authorized role-specific comparison retained Flash Lite extraction and used Gemini 3.7 Flash for adjudication.
The draft-contract criterion produced `RELATED_MISSING_REQUIRED_ELEMENT` and the code-owned `NEAR_MISS` result in all
three repeats, with the correct missing counsel-involvement/legal-advice boundary. No observation was dropped or
adjusted. One repeat nevertheless completed with errors because extraction introduced a stray legal-invoice signal
and adjudication returned `UNRELATED` without the required admitted-observation reference. This validates the stronger
adjudicator for the focused semantic case, but it does not yet establish production durability.

The task-neutral missing-reference edge is fixed in conformance contract V6. The prompt now requires every
relationship except `INSUFFICIENT` to cite admitted evidence. If all reference slots are null and the packet contains
exactly one observation, code attaches that sole reference and records the normalization; multiple-observation packets
remain validation failures rather than inviting a guess, and supplied invalid references are not silently replaced.
Run a representative repeated cohort with the role-specific binding; do not encode a privilege-specific override
into the combiner.

**Risk:** Assessments with a meaningful proportion of long email threads, OCR text, or other multi-window documents
have unpredictable completion time and poor operator observability. Increasing only document concurrency may raise
provider pressure and database contention without removing the extra round trips. Increasing the single-window
character ceiling can exceed provider context budgets or degrade analysis quality. Starting final synthesis before
all document results are terminal could make the synthesis incomplete or nondeterministic.

**Desired state:** Keep direct analysis for documents whose complete request fits, and route large documents through
the evidence-first workflow described in the
[Large-Document Evidence-First Analysis Implementation Plan](large-document-analysis-workflow-plan.md). Submit each
source window as an independently addressable evidence-extraction request, validate and aggregate observations in
code, adjudicate bounded criterion packets, and produce the document decision from those criterion outcomes. Generate
the narrative separately so a presentation failure cannot invalidate the evidence or decision. Make normalization and
repair bounded unit-level operations: preserve valid siblings, classify every adjustment or failure, and never
translate a deterministic output-usage failure into an unchanged whole-document replay. Persist usage and request
lineage for successful and failed calls, and distinguish provider response-generation limits from cumulative usage
guards. Preserve the final completeness barrier for assessment synthesis while preparing its inputs incrementally.

**Completion criteria:**

- Multi-window documents no longer fall back wholesale to the synchronous real-time analysis path.
- Every extraction-window request has a stable identity, versioned plan, durable status, bounded retry policy, and
  exact source/paragraph coverage.
- Provider batches may contain extraction windows from multiple documents without losing document ownership,
  ordering, or provenance.
- Criterion adjudication begins when that document's required extraction units are terminal; unrelated documents do
  not impose a reduction barrier.
- Successfully completed windows survive worker restart and are not resubmitted during targeted retry.
- Invalid observations or adjudications are isolated and reported without discarding valid sibling results.
- Recoverable optional-field violations are normalized or dropped under an explicit policy before another provider
  request is made; material schema, citation, and semantic violations receive distinct stable reason codes.
- Each validation failure and repair is persisted, including repairs that ultimately succeed, without exposing
  privileged source text in diagnostics.
- A failed extraction, adjudication, decision, or narrative request retries only that unit. Output-usage exhaustion
  and other deterministic
  non-retriable failures do not cause an unchanged request or the complete document to be replayed.
- Provider response-generation limits, per-call limits, cumulative per-agent usage limits, and assessment-level
  budgets are explicit, independently configured, and enforced before dispatch where possible.
- Every provider invocation records request lineage and usage on success or failure. Assessment totals reconcile to
  the invocation ledger, including failed and repaired calls, rather than counting only successful envelopes.
- The frozen execution plan estimates direct, extraction, adjudication, decision, and narrative calls and reserves a
  bounded repair allowance. The operator can see estimated versus actual calls and tokens as the run progresses.
- Synthesis starts only from a frozen set of terminal document analyses, while synthesis preparation may proceed
  incrementally without changing final semantics.
- Operator progress distinguishes provider submission/waiting, evidence extraction, criterion adjudication, document
  decision, narrative generation, focused repair, and synthesis.
- A representative 500-document performance test records document-size distribution, request/window counts,
  validation and repair counts, retry amplification, estimated and actual token usage, concurrency, provider latency,
  finalization time, long-tail duration, failures, and total time; its multi-window tail and replay amplification are
  materially lower than the baselines recorded above.

**2026-11-16 implementation checkpoint:** The evidence-first v2 orchestration now schedules immutable extraction units
independently, uses separate bounded queues for document coordination, extraction, adjudication, and narrative, and
checkpoints model admission before releasing the Core database connection during provider waits. Parallel unit workers
do not refresh shared workflow totals; document compaction performs the aggregate refresh once. The extraction role has
an explicit configurable 60 RPM / 1,000,000 estimated-input-TPM default instead of inheriting the conservative global
12 RPM / 200,000 TPM bucket. Retry/reuse identities and the final synthesis completeness barrier are unchanged. Still
open: compatible cross-document provider batches, direct routing for requests that fit one provider call, and the
representative 500-document measurement gate.

**2026-11-17 assessment scope correction:** Complete-source evidence and document decisions remain appropriate for
authoritative review, but they are unnecessarily strong semantics for Matter Definition assessment. Small assessment
documents retain direct analysis. Large assessment documents should reuse the frozen retrieval plan and persisted
query-attributed best passages, expand context only when needed, and produce cited assessment signals rather than an
exhaustive evidence record or negative document coding. The implementation boundary and gates are defined in the
[Large-Document Query-Directed Assessment Implementation Plan](large-document-assessment-query-directed-plan.md).

### Make assessment token and cost estimates execution-mode aware

**Current state:** Assessment preflight still estimates the legacy `document_analysis` map/reduce contract. It builds
the legacy paragraph-window plan, repeats that skill's stable prefix once per predicted request, adds a coarse reduce
overhead, and assumes a fixed output allowance per document. An assessment configured for `EVIDENCE_FIRST_V1` displays
that legacy estimate even though its actual plan fans out into evidence-extraction units, criterion adjudications,
optional narratives, and staged synthesis units using different prompts, schemas, models, and output behavior. The
estimate also has no explicit cache-hit assumption, provider consumption mode, retry allowance, or model-price
snapshot.

The 250-document evidence-first assessment `219a549c-0a9e-40a8-a3a9-12ae70652632` quantified the gap. Preflight
displayed 3,507,145 estimated input tokens. The immutable provider-usage ledger recorded 6,189,589 input tokens and
476,029 output tokens across 791 standard API requests: 376 extraction, 214 adjudication, 170 narrative, 30 synthesis,
and one retrieval-planning request. Actual input was 76.5% above the displayed estimate. No input tokens were served
from cache. Forty-nine documents required multiple extraction units, and the ten largest documents accounted for
31.4% of extraction input. Two complete 15-unit synthesis attempts also ran, but the duplicate synthesis work was a
small part of the total variance.

**Risk:** Operators cannot make an informed cost or quota decision before launching an assessment, and the displayed
estimate can appear precise while excluding entire execution stages. Rate-limit reservations and completion-time
expectations inherit the same blind spots. A cache-rate change, document-size skew, role-model change, or switch
between standard and provider-batch consumption can materially change cost without changing the estimate. Comparing
one aggregate estimate with one aggregate actual does not reveal which stage or assumption was wrong.

**Desired state:** Build the estimate from the frozen execution mode and the same versioned request builders used by
execution. Report per-stage request and token ranges for extraction, adjudication, deterministic decisions, narrative,
and synthesis, with the resolved model and consumption mode for each stage. Separate fixed/stable prompt tokens from
document- or unit-specific tokens. State cache assumptions explicitly and show zero-cache and expected-cache cost
scenarios rather than silently assuming a hit rate. Use a versioned pricing snapshot to present an estimated API-cost
range while keeping token estimates valid independently of price changes. Reconcile estimates against the immutable
usage ledger during and after the run and attribute variance to document fan-out, model-selected downstream work,
cache performance, repair/retry, or replay.

**Completion criteria:**

- `DIRECT_V1` and `EVIDENCE_FIRST_V1` select distinct, versioned estimators; an unsupported execution mode cannot
  silently fall back to the legacy estimate.
- Evidence-first preflight counts planned extraction units exactly and publishes bounded assumptions for
  observation-dependent adjudication and narrative calls, staged synthesis, and permitted repair/retry work.
- Every stage reports resolved model, standard/batch/flex consumption mode, estimated request count, input/output
  tokens, stable-prefix tokens, cache assumption, and estimated cost under a dated pricing snapshot.
- The UI labels point estimates versus lower/upper bounds and explains material assumptions, including the effect of
  zero cache reads and large-document fan-out.
- Runtime progress compares estimated and actual requests/tokens by stage; terminal diagnostics attribute material
  variance without requiring direct database inspection.
- Estimation uses the production prompt/schema assembly or a hash-verified equivalent so prompt, schema, skill, model,
  or routing changes invalidate stale estimator fixtures.
- Representative 250- and 500-document cohorts cover direct documents, multi-unit documents, zero-observation
  documents, modeled adjudication/narrative fan-out, synthesis regeneration, cache-hit and zero-cache scenarios, and
  standard versus provider-batch pricing.
- The 250-document cohort above is retained as a regression fixture. Its actual usage must fall within the estimator's
  published interval, and any point-estimate variance outside the accepted tolerance must identify the responsible
  stage and assumption.

### Align document summaries with the reusable summary contract

**Current state:** The document-analysis result contains cited summary paragraphs, but it does not produce a short
document title or enforce a caller-selected summary word budget. The model receives source and document identifiers
but not the useful descriptive metadata anticipated by `docs/doc-summary.txt`, such as filename, document type,
document date, email subject, or sender. Because the Matter Definition is supplied in the same request, the boundary
between a topic-neutral document summary and Matter Definition-focused responsiveness analysis is also implicit.

The assessment's paragraph map and map/reduce coverage envelope are more complete than the legacy template's single
`truncated` flag and should remain the source of truth for omitted or partially analyzed text.

**Risk:** Summaries may vary substantially in length, omit useful document identity and context, or overemphasize
facts relevant to the current Matter Definition. That reduces scanability and makes a `SUMMARY` artifact less useful
for reuse outside the assessment that created it.

**Desired state:** Keep the document summary topic-neutral and keep Matter Definition application in the separate
responsiveness fields. Add a concise generated title, a deterministic summary-length policy, and authorized
descriptive metadata to the structured input and output. Preserve paragraph citations and explicit coverage data.

**Completion criteria:**

- The structured result includes a subject-focused title of no more than 10 words.
- The caller supplies a versioned summary word budget, and the output is validated against it.
- The model receives an allowlisted metadata snapshot including available filename, type, date, email subject, and
  sender fields without exposing unrelated or unauthorized metadata.
- Summary instructions explicitly require a topic-neutral account of the document; responsiveness analysis remains
  conditioned on the pinned Matter Definition.
- The rendered analysis shows the title and accurately discloses partial coverage or omitted ranges.
- Schema-version, prompt, renderer, artifact, and fixture tests cover short, long, metadata-poor, and partially
  analyzed documents.

### Persist assessment synthesis as an immutable artifact

**Current state:** Each document analysis is stored as an immutable `SUMMARY` artifact derived from the source
document. The batch-level synthesis is stored as mutable JSON in
`matter_definition_assessment_run.synthesis_result`; its refinement questions and topic taxonomy are also projected
into application tables. Regenerating synthesis replaces the assessment's current JSON result rather than creating
a separately addressable artifact version.

**Risk:** The provenance chain stops at the document-analysis artifacts. It is harder to reproduce, compare, retain,
or audit multiple synthesis attempts because the exact synthesis output is not represented as an immutable artifact
derived from the inputs that produced it.

**Desired state:** Store every successful synthesis as its own immutable artifact. Link it to the assessment and
workflow/skill run, and record its derivation from the complete set of input `SUMMARY` artifacts. Keep
`synthesis_result` and the normalized question/topic tables as read-model projections of the selected synthesis
artifact rather than the authoritative result.

**Completion criteria:**

- Every successful synthesis and synthesis regeneration creates a new immutable artifact.
- The artifact records the assessment, workflow run, skill version, model configuration, Matter Definition revision,
  coverage envelope, and exact input summary artifact IDs/hashes.
- The assessment identifies which synthesis artifact is current without deleting prior versions.
- Refinement questions and batch topics can be traced to the synthesis artifact that produced them.
- Regeneration, API, retention, and audit tests cover the versioned provenance chain.

### Define and validate `NEAR_MISS` criterion semantics

**Current state:** Document analysis permits `PRIMARY`, `SECONDARY`, and `NEAR_MISS` criterion matches, but the
managed-skill instructions do not define a sufficiently strict boundary for `NEAR_MISS`. Assessment synthesis counts
each model-labeled near-miss criterion occurrence; one document can contribute more than one occurrence.

**Risk:** Different model calls may use `NEAR_MISS` inconsistently. The resulting count can look more authoritative
than it is and may introduce weak or noisy signals into Matter Definition refinement questions.

**Desired state:** Define a near miss as a document that is materially related to a stated criterion but falls
outside at least one identifiable instruction boundary. Require the analysis to name that boundary, explain the
minimal policy choice that would include or exclude the document, and cite supporting paragraphs. Keep near misses
distinct from low-confidence matches, missing-document limitations, and general topical similarity.

**Completion criteria:**

- The document-analysis prompt and schema define the classification and require boundary evidence.
- Fixtures cover representative true near misses and common false positives.
- The UI labels the value as criterion-level occurrences and also shows the distinct-document count.
- Synthesis groups recurring occurrences before proposing questions and does not treat the raw count as a finding.

## Analysis tasks

### Eliminate Q&A request amplification and cross-provider blocking

**Current state:** A 250-document Question Answering population is evaluated independently by each execution profile.
The typed decision and conditional paragraph evidence are separate provider operations, long JEV documents require
multiple classification windows, and JEV and OpenAI document workflows share one DBOS queue. In the measured
2026-11-17 privilege run, the first 250-document population produced 1,575 JEV requests and 451 OpenAI requests.
The JEV run took 52m 14s, which matched its 30-request-per-minute throttle floor, while fast OpenAI calls experienced
cross-provider head-of-line blocking.

**Risk:** Population size substantially understates provider work and expected runtime. Evidence localization rereads
documents after their decisions are known, provider contention makes one profile's completion time depend on another,
and operators cannot distinguish queue wait, throttle wait, provider processing, or request amplification from the
document progress shown in the UI.

**Desired state:** Return answer-bound paragraph evidence with the first classification response, aggregate compatible
evidence deterministically across long-document windows, isolate provider queues, minimize windows through exact
capability-aware request planning, and expose provider-request progress and throttle-aware ETA. Preserve complete
source coverage, typed-decision semantics, immutable provenance, and partial-coverage reporting.

The architecture, delivery phases, compatibility strategy, tests, measured baseline, and acceptance targets are in the
[Question-Answering Batch Performance Improvement Plan](question-answering-performance-plan.md).

**Completion criteria:**

- Inline-capable documents do not receive an automatic second evidence-localization request.
- Long documents use one normal provider request per planned window, with evidence bound to the aggregated answer.
- A slow JEV run cannot occupy OpenAI document capacity.
- The frozen 250-document privilege cohort reduces combined provider requests by at least 35% without a material
  decision or evidence-quality regression.
- The job UI reports planned/executed requests, windows, queue wait, throttle floor, provider time, and profile ETA.

### Support provider-bounded evaluation of large documents

**Current state:** Question Answering Analysis Tasks build one complete document state and reject it when its
serialized character count exceeds the configured single-request ceiling. Increasing that ceiling can send a request
that exceeds the selected provider/model token budget, such as Jev's state-plus-longest-question limit. The existing
Matter Definition assessment workflow has a specialized paragraph map/reduce path, but it is not a reusable Analysis
Task execution facility. Statistical Classification uses its separate feature/scoring pipeline.

**Risk:** Long email threads, OCR output, dense identifiers, and large extracted documents either fail permanently or
encourage unsafe limit increases. Provider-specific fixes would produce inconsistent coverage, retry, evidence, and
combination semantics between task types. Silent truncation or incomplete negative decisions would create a material
review-quality and auditability risk.

**Desired state:** Add a provider-aware, task-neutral execution layer that plans the complete serialized request,
uses a deterministic versioned paragraph-window splitter where required, extracts bounded evidence durably, and
combines results through reviewed code-owned operators or explicitly admitted criterion-level adjudication. Later
stages reference validated evidence IDs instead of regenerating citations. Every result records exact coverage and
immutable provenance. Task types whose inputs are already bounded, including the current statistical classifier,
participate through the same adapter boundary without being forced through text splitting.

The architecture, phases, persistence model, testing strategy, and acceptance criteria are documented in the
[Large-Document Evidence-First Analysis Implementation Plan](large-document-analysis-workflow-plan.md).

**Completion criteria:**

- Jev and OpenAI Decisions use versioned provider/model capability profiles and complete-request preflight.
- Oversized but supported documents run through deterministic windows without silent truncation.
- Every output has a validated combination or adjudication rule, and every result reports complete, partial, or
  insufficient coverage.
- Successful windows survive worker restart and targeted retry; unchanged oversized requests are not retried.
- Core stores one logical document result while complete window plans/results use verified Artifact-owned Parquet.
- Playground, review-batch, and whole-matter executions share the same evaluation semantics.

## Agent conversations

### Replace chatbot polling with server streaming

The current chatbot uses multiple polling loops for messages, runs, action requests, and conversation state. Replace
them with a durable, authenticated server-streaming path while keeping the database authoritative and preserving a
temporary polling fallback during rollout.

The architecture, delivery sequence, test plan, and acceptance criteria are documented in the
[chatbot server-streaming conversion plan](chatbot-server-streaming-plan.md). The follow-on work for progressive
assistant output is documented separately in the [chatbot token-streaming implementation plan](chatbot-token-streaming-plan.md).
