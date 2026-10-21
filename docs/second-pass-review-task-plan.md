# Second Pass Review Task Implementation Plan

Status: proposed

## 1. Outcome

Add a `SECOND_PASS_REVIEW` Analysis Task that reviews a frozen batch of documents previously coded responsive. The
task verifies first-pass coding, applies already approved subcodes, discovers recurring or legally significant
concepts that may justify new subcodes, and develops sourced facts. It behaves like a senior attorney conducting a
second review: it evaluates each document against the current case understanding and improves that understanding as
the batch proceeds.

The agent's evolving understanding is durable, structured, evidence-backed memory. It is not an ever-growing chat
history. Documents are reviewed in waves against immutable memory snapshots. After each wave, bounded observations
are consolidated into candidate codes and a new memory snapshot. A human must approve a candidate before it becomes
an official matter code.

The initial release is review-batch scoped. A run may read and backfill only the documents in its frozen input
manifest. Approved codes become matter configuration and may be used by later batches, but the run never expands to
other matter documents implicitly.

## 2. Product decisions

| Topic | Decision |
| --- | --- |
| Task type | Add `SECOND_PASS_REVIEW` as a distinct Analysis Task type with a code-owned workflow. |
| Input | One frozen review batch containing documents selected for second-pass review, normally first-pass responsive documents. |
| Coding verification | Preserve first-pass coding and produce a separate second-pass recommendation. Do not silently overwrite original coding. |
| New codes | The agent may observe, consolidate, and propose codes. Only an authorized human may create an official code. |
| Memory | Use immutable, versioned snapshots of structured candidate-code memory. Do not use conversation history as the durable store. |
| Processing | Review documents in deterministic waves. Documents within one wave use the same memory snapshot and may run concurrently. |
| Facts | Compose the existing Fact Development capability and matter fact ledger. Do not create a second fact store. |
| Entities | Reuse the matter entity store. Seed entities only from the frozen batch metadata and retain existing review states. |
| Provenance | Keep complete document-level results in job outcome artifacts. Keep only aggregate counts and bounded representative evidence in candidate memory. |
| Backfill | When a code is approved, re-evaluate earlier documents from the same frozen batch against that code. Whole-matter backfill requires a separate explicit action. |
| Rejected concepts | Retain rejected candidates as tombstones so later waves and runs do not repeatedly propose the same code. |
| Run completion | Candidate decisions do not block the workflow. Attorneys may decide candidates between waves or after the run; the run completes after document processing and final synthesis even when proposals remain pending. |

## 3. Existing platform boundary

### 3.1 Reuse

The implementation should reuse these existing platform capabilities:

- `MatterAnalysisTask`, `MatterAnalysisTaskVersion`, and `MatterAnalysisTaskRun` for task definition, publication,
  launch, status, retry, and cancellation.
- Review batches and Analysis Task batch sets for explicit input scope.
- `AnalysisInputManifest` for the immutable population and per-document input contract.
- `WorkflowRun`, `WorkflowStepRun`, `SkillRun`, and `ModelInvocation` for durable execution and usage telemetry.
- Managed skills and `WorkflowSkillBinding` for model-role configuration.
- Job outcome manifests and fragments for scalable per-document results.
- Reviewer coding revisions and coding promotion for second-pass coding review and publication.
- `MetadataDefinition` and hierarchical enum values for official codes and subcodes.
- The matter entity store and its event history.
- The matter fact ledger, evidence validation, recall, consolidation, suggestions, and reviewer workflow.
- The batch-topic search projection pattern for indexing batch-scoped topic assignments.

### 3.2 Do not reuse as the canonical design

- Do not treat `BatchTopicTaxonomy` as candidate-code memory. It is tied to a Matter Definition assessment and has
  only active and retired states. It does not model proposal, approval, rejection, merge, definitions, or memory
  snapshots.
- Do not force Second Pass Review through the Jev typed-decision path. The workflow has generative and stateful
  stages in addition to bounded coding decisions.
- Do not store every candidate observation as a permanent Core row. The job outcome artifact is the authoritative
  run-level record of document observations.
- Do not create official metadata values directly from model output.
- Do not duplicate matter facts, entities, or their evidence in the candidate-code store.

## 4. Terminology and memory scopes

### 4.1 Terms

| Term | Meaning |
| --- | --- |
| First-pass coding | Coding that existed when the input manifest was frozen. |
| Coding verification | A second-pass determination that affirms, overturns, expands, or escalates first-pass coding. |
| Observation | A document-level signal that an existing code may be insufficient. It is not yet a durable candidate. |
| Candidate code | A durable proposed concept assembled from one or more observations. |
| Code card | The attorney-facing candidate definition, including purpose, parent, inclusion and exclusion rules, overlap analysis, counts, and representative evidence. |
| Official code | An approved matter metadata value available for coding and later tasks. |
| Wave | A deterministic group of documents reviewed against one immutable memory snapshot. |
| Memory snapshot | An immutable set of candidate revisions, approved codes, rejected tombstones, and open questions supplied to a wave. |

### 4.2 Run memory

Run memory contains document-level observations and coding recommendations from one execution. It is stored in job
outcome artifacts. It is complete for audit and retry purposes but is not loaded wholesale into later prompts.

### 4.3 Task memory

Task memory persists across waves and later runs of the same Second Pass Review task. It contains:

- emerging and proposed candidates;
- candidate definitions and revision history;
- aggregate document and batch counts;
- bounded representative supporting and counter-evidence;
- merges, rejections, and retired candidates;
- open taxonomy questions; and
- links to official codes after approval.

### 4.4 Matter memory

Only reviewed outputs become matter-wide memory:

- official approved codes;
- confirmed and working facts in the existing fact ledger;
- matter entities and their review state; and
- rejected-code tombstones when the reviewer chooses to suppress the concept across Second Pass Review tasks.

## 5. Task definition and specification

### 5.1 Task registration

Add `SECOND_PASS_REVIEW` to:

- the task-type schema and database constraints;
- `TASK_WORKFLOW_KEYS`;
- task creation, versioning, publication, and read models;
- batch-set task-type constraints;
- run-kind constraints and job execution adapters; and
- frontend task-type selection and detail rendering.

Use workflow key `second_pass_review_v1` and a dedicated job type such as
`SECOND_PASS_REVIEW_ANALYSIS_TASK`.

### 5.2 Second Pass Review Specification

Each published task version carries a validated `SecondPassReviewSpecification`. The initial schema should contain:

| Section | Required contents |
| --- | --- |
| Summary | The purpose of the second-pass review and the legal or investigative focus. |
| Coding sources | The first-pass metadata definitions and, when applicable, the reviewer coding source to freeze. |
| Verification rules | What constitutes affirm, overturn, expand, and needs-review decisions. |
| Destination taxonomy | Metadata definitions that may receive verified coding or new subcodes. |
| Existing code catalog | Stable IDs, keys, labels, parents, definitions, and active state for codes known at publication. |
| Candidate policy | Requirements for creating, strengthening, proposing, merging, rejecting, and retiring candidates. |
| Memory policy | Representative evidence limits, rejected-candidate behavior, and cross-run task-memory behavior. |
| Wave policy | Wave size, deterministic ordering, consolidation frequency, and retry rules. |
| Fact policy | Whether Fact Development runs, the pinned fact specification, and issue mappings. |
| Evidence policy | Required citations, quote validation, and minimum support for coding and candidate observations. |
| Limits | Per-document operations, candidate retrieval count, candidate count per wave, and model-input bounds. |

### 5.3 Specification compilation and publication

Add a `second_pass_specification_compiler` managed skill. It reads:

- the task definition;
- the pinned Review Guidance revision;
- the selected first-pass coding fields;
- active metadata definitions and hierarchical values; and
- the optional Fact Development Specification.

The compiler drafts the specification. Deterministic validation must confirm that all referenced fields and values
exist, are active, belong to the matter, and can accept the proposed output shape. A task author reviews and
publishes the draft using the existing compile, review, and publish lifecycle.

Publication freezes the specification content hash and all source references. Publishing does not create any new
codes.

## 6. Frozen run input

### 6.1 Population

Launching a run requires a review batch. The input resolver orders documents deterministically, with file date and
stable document identity as tie breakers, and publishes an `AnalysisInputManifest`. Each document appears at most
once.

The resolver must not query for additional documents after the manifest is finalized. A retry reads the same
manifest and source artifacts.

### 6.2 Per-document frozen record

For every document, freeze:

Here, `freeze` means one immutable manifest row containing stable references, hashes, coding values, and only the
metadata required by the task. It does not duplicate document content or create a provenance record for every email
header occurrence.

- matter document ID, collection item ID, input ordinal, family ID, and control number when available;
- input status (`READY`, `MISSING`, `FAILED`, or `EXCLUDED`) and a stable reason code for non-ready documents;
- selected text artifact ID, role, media type, filename, content hash, and paragraph-map version;
- file date and the date source used for ordering;
- the first-pass coding values for every configured source field;
- the coding source for each value: current matter coding or a specific reviewer-coding revision;
- the selected reviewer coding ID and revision when reviewer coding is the source;
- approved subcode values already present at snapshot time;
- custodian IDs and other configured evidence-document fields needed by Fact Development or review rules;
- email sender, recipients by role, subject, and sent date when available;
- the input contract version and resolver version; and
- a deterministic fingerprint covering the source content, frozen coding, and task specification.

Imported metadata remains unchanged in the manifest. Email cleaning occurs only when resolving entity identity.

### 6.3 Run-level frozen configuration

Freeze once per run rather than repeating on every document:

- task and task-version IDs;
- specification and source content hashes;
- initial memory snapshot ID and content hash;
- official code catalog snapshot;
- managed skill definition versions and workflow bindings;
- resolved model configurations;
- wave size and limit settings;
- entity email-normalizer version;
- Fact Development task or specification version, when enabled; and
- authorization and launch-estimate snapshots required by the job platform.

### 6.4 Input exclusions

The manifest keeps excluded and missing rows so coverage can be reported. Common reason codes include:

- not coded responsive under the configured first-pass rule;
- missing selected text;
- unsupported or unreadable artifact;
- missing required first-pass coding;
- source coding revision unavailable; and
- duplicate document in the requested scope.

## 7. Per-document result contract

The `second_pass_document_review` skill returns a structured result. Code validates every identifier and citation
before publishing the outcome.

### 7.1 Coding verification

For each configured first-pass code, return:

- source field and value IDs;
- decision: `AFFIRM`, `OVERTURN`, `EXPAND`, or `NEEDS_REVIEW`;
- recommended replacement or additional approved values, if any;
- a concise rationale;
- exact evidence citations; and
- confidence as a routing signal, not as permission to publish coding.

`EXPAND` means the original code remains correct and additional approved coding applies. `OVERTURN` means the
original value should be removed or replaced. `NEEDS_REVIEW` routes ambiguity to the attorney without changing
coding.

### 7.2 Approved subcode assignments

The result may assign only official active code IDs supplied in the memory snapshot. It must copy stable IDs rather
than generate keys from labels.

### 7.3 Candidate observations

An observation includes:

- a concise proposed concept;
- the parent code or coding field it refines;
- why supplied codes are insufficient;
- suggested inclusion and exclusion boundaries;
- whether it matches an emerging candidate ID supplied to the model;
- legal significance and recurrence signals;
- exact supporting citations; and
- possible overlap with existing codes or candidates.

The model may return `MATCH_EXISTING`, `PROPOSE_NEW`, or `NO_CANDIDATE`. It cannot create or mutate a durable
candidate directly.

### 7.4 Fact Development result

When enabled, fact extraction uses the existing validated operation contract. Facts, evidence, owners,
participants, consolidation, and reviewer suggestions continue through the existing Fact Development services.
The Second Pass Review outcome stores fact IDs and operation counts rather than a duplicate fact representation.

### 7.5 Quality and usage

Each document outcome records:

- status and stable failure code;
- input fingerprint and memory snapshot ID;
- skill run and model invocation IDs;
- request, token, and latency metrics;
- validation errors and rejected operations; and
- counts for verification decisions, approved assignments, observations, and fact operations.

## 8. Candidate-code data model

### 8.1 `second_pass_code_candidate`

The current projection of one candidate:

- `id`, `tenant_id`, `matter_id`, and `matter_analysis_task_id`;
- stable internal key;
- proposed label and description;
- target metadata definition ID and optional parent value key;
- status: `EMERGING`, `PROPOSED`, `APPROVED`, `MERGED`, `REJECTED`, or `RETIRED`;
- current revision and lock version;
- document count and batch count;
- first-seen and last-seen run IDs;
- merge target candidate ID;
- approved metadata definition and value key;
- rejection scope (`TASK` or `MATTER`) when rejected; and
- created and updated actor provenance.

Constraints must enforce the valid shape for approved and merged candidates. Candidate keys are internal until
approval; they are not official metadata value keys.

### 8.2 `second_pass_code_candidate_revision`

An immutable revision contains:

- label and description;
- purpose and why existing codes are insufficient;
- inclusion criteria;
- exclusion criteria;
- parent and overlap analysis;
- legal-significance rationale;
- aggregate statistics;
- bounded representative supporting evidence;
- bounded counterexamples; and
- author type, workflow run, and revision reason.

### 8.3 `second_pass_code_candidate_event`

Append-only events record creation, strengthening, renaming, proposal, approval, rejection, merge, unmerge if
supported, and retirement. Human decisions include actor, reason, before and after state, and authorization context.

### 8.4 `second_pass_memory_snapshot`

Each snapshot records:

- task ID and monotonically increasing version;
- content hash;
- included candidate and revision IDs;
- approved code catalog hash;
- rejected tombstone IDs;
- open taxonomy questions;
- source run and wave; and
- creation workflow and timestamp.

The snapshot is immutable after publication. A run pins the initial snapshot, and each wave pins exactly one input
snapshot and one output snapshot.

### 8.5 `second_pass_run_wave`

Each wave records:

- run ID and ordinal;
- manifest start and end ordinals;
- input and output memory snapshot IDs;
- status, attempt count, and workflow step ID;
- document outcome counts;
- observations considered;
- candidates created, strengthened, merged, or proposed; and
- started and completed timestamps.

### 8.6 Representative evidence policy

Candidate memory stores aggregate counts and a bounded evidence sample, not every occurrence. The default limit
should be five supporting examples and three counterexamples per candidate revision. Selection should favor:

- distinct custodians, authors, or organizations;
- different dates or phases of the matter;
- examples that clarify the inclusion boundary;
- examples that distinguish a neighboring code; and
- the strongest exact citations.

All other occurrences remain recoverable from job outcome artifacts and document search.

## 9. Candidate lifecycle

### 9.1 Emerging

The wave consolidator creates an `EMERGING` candidate after validating an observation and determining that it does
not match an existing candidate or rejected tombstone. One significant document may create an emerging candidate;
recurrence is not mandatory.

### 9.2 Proposed

The taxonomy synthesizer may advance an emerging candidate to `PROPOSED` when it can produce a complete code card.
The proposal must state:

- the review problem the code solves;
- why existing codes are insufficient;
- intended parent and hierarchy;
- inclusion and exclusion rules;
- overlap with existing codes and candidates;
- document and batch counts;
- representative evidence and counterexamples;
- expected review or case-development usefulness; and
- a recommended backfill strategy.

### 9.3 Approved

Only an authorized user may approve. Approval creates or selects an official metadata value through the metadata
command service, links it to the candidate, records an event and audit record, and publishes a new memory snapshot.

### 9.4 Merged

Merging preserves the absorbed candidate and points it to the surviving candidate or official code. Counts and
representative evidence are recomputed without losing history.

### 9.5 Rejected

Rejection requires a reason and creates a tombstone. The default scope is the Second Pass Review task. A reviewer
may choose matter scope when the concept should not be proposed by other second-pass tasks.

### 9.6 Retired

Retirement applies to a previously approved or useful candidate that is no longer active. It does not erase
historical document assignments or decision history.

## 10. Wave workflow

### 10.1 Stage 1: authorize and freeze

- Resolve the published task version and review batch.
- Estimate model and artifact usage.
- Create the job and workflow execution.
- Publish the Analysis Input Manifest.
- Pin the initial memory snapshot and official code catalog.
- Seed or reuse batch entities from frozen email metadata when Fact Development is enabled.

### 10.2 Stage 2: plan waves

- Use a configurable default wave size between 25 and 50 documents.
- Preserve deterministic manifest order.
- Create `second_pass_run_wave` rows and workflow steps.
- Do not create per-document Core execution rows.

### 10.3 Stage 3: review one wave

All documents in the wave receive the same:

- task specification;
- official code catalog;
- candidate memory snapshot;
- rejected tombstones;
- source coding contract; and
- evidence and operation limits.

Document reviews may run concurrently. Each result is independently validated and published as an outcome
fragment.

### 10.4 Stage 4: consolidate observations

The consolidator:

1. Loads bounded observation summaries from the completed wave.
2. Retrieves plausible existing candidates using parent, key terms, entities, and embeddings.
3. Applies deterministic exact matches first.
4. Uses the managed `candidate_consolidation` skill only for ambiguous semantic matches.
5. Locks affected candidates and checks their current revision.
6. Creates, strengthens, or merges candidates through the command service.
7. Selects bounded representative evidence.
8. Publishes the next memory snapshot.

Concurrent runs use optimistic candidate revisions plus partition or advisory locks. A late-candidate recheck avoids
creating duplicates after the model decision.

### 10.5 Stage 5: continue with the next wave

The next wave uses the newly published snapshot. It can recognize candidates discovered earlier, add stronger
evidence, identify counterexamples, or explain why a document does not match.

### 10.6 Stage 6: final synthesis

After all waves finish, `taxonomy_synthesis` reviews the final memory snapshot. It may strengthen definitions and
advance complete candidates to `PROPOSED`; it may not approve them. The run then finalizes its outcome manifest and
completes with aggregate coverage, coding-verification, candidate, fact, quality, and usage reports.

## 11. Managed skill roles

### 11.1 `second_pass_specification_compiler`

Drafts a specification from reviewed task guidance and the matter coding catalog. It runs only during task authoring.

### 11.2 `second_pass_document_review`

Reviews one document against first-pass coding, approved codes, current candidate memory, and the evidence policy.
It returns typed coding decisions and candidate observations.

### 11.3 `candidate_consolidation`

Decides whether one proposed observation matches a bounded set of existing candidates. It returns a typed match,
distinct, or uncertain decision with a concise rationale.

### 11.4 `taxonomy_synthesis`

Creates or revises attorney-facing code cards from the final candidate set. It does not create official codes.

### 11.5 Existing Fact Development roles

Reuse `fact_extraction` and `fact_consolidation` with their existing schemas and validators. The Second Pass Review
workflow supplies the same frozen document and issue context expected by Fact Development and records the resulting
fact IDs in its document outcome.

## 12. Coding review and promotion

### 12.1 Preserve original coding

First-pass coding is immutable run input. The second-pass result is a recommendation layer with direct references
to the frozen values. The UI must show original and recommended values side by side.

### 12.2 Attorney adjudication

An attorney may accept, edit, or reject a recommendation. Accepted decisions are appended through reviewer coding
revisions so the human action, source run, and prior value remain auditable.

### 12.3 Promotion

Use the existing coding-promotion workflow to publish adjudicated second-pass coding to matter metadata. Promotion
must pin the exact reviewer-coding revision and expose a destination preview before writing.

Agent recommendations alone are never eligible for direct promotion.

## 13. Approved-code backfill

Approval creates a separate idempotent backfill job tied to:

- the approved candidate revision;
- the official metadata value;
- the source Second Pass Review run;
- the frozen Analysis Input Manifest; and
- a reviewer-selected destination coding set.

The initial implementation re-evaluates every earlier `READY` document in the same manifest against the single
approved code definition. Later waves receive the code prospectively through their memory snapshot. Backfill results
are recommendations and follow the same attorney adjudication and promotion path as other second-pass coding.

Backfill must not:

- add documents outside the frozen manifest;
- reconsider unrelated first-pass codes;
- mutate the completed parent run's document outcomes; or
- write official document coding without the configured human gate.

A separate future whole-matter operation may reuse the approved code, but it requires its own scope preview,
authorization, manifest, estimate, and job.

## 14. Fact and entity integration

### 14.1 Facts

Second Pass Review uses the existing fact ledger as matter memory. Fact recall should include working, confirmed,
rejected, and merged facts according to current Fact Development rules so repeated evidence accumulates instead of
creating near-duplicates.

Candidate codes and facts remain separate domains. A fact may retain existing approved issue keys. An unapproved
candidate relationship remains in the Second Pass Review outcome until the code is approved; it is not written as
an official fact issue.

### 14.2 Entities

Before document review, batch-scoped entity seeding may create missing email-only `PROPOSED` entities from the
frozen manifest. Email normalization must remove straight or smart quote wrappers, angle brackets, display-name
syntax, trailing punctuation, and semicolon-list artifacts while preserving valid internal apostrophes.

Seeding must:

- scan only the current review-batch manifest;
- reuse existing canonical or variant email identities;
- preserve `CONFIRMED`, `PROPOSED`, and `REJECTED` review states;
- avoid resurrecting rejected entities;
- be idempotent and concurrency-safe; and
- store only aggregate run metrics plus a creation event, not one provenance row per header occurrence.

## 15. API plan

### 15.1 Task and run APIs

Extend the existing Analysis Task APIs for `SECOND_PASS_REVIEW` creation, compilation, publication, launch,
estimate, retry, cancellation, detail, and batch-set execution.

Add run-specific reads:

- `GET /v1/matters/{matter_id}/analysis-tasks/{task_id}/second-pass-runs/{run_id}`
- `GET /v1/matters/{matter_id}/analysis-tasks/{task_id}/second-pass-runs/{run_id}/waves`
- existing job-outcome endpoints for document results

### 15.2 Candidate APIs

- `GET /v1/matters/{matter_id}/second-pass-candidates`
- `GET /v1/matters/{matter_id}/second-pass-candidates/{candidate_id}`
- `PATCH /v1/matters/{matter_id}/second-pass-candidates/{candidate_id}` for reviewer edits with lock version
- `POST .../{candidate_id}/propose`
- `POST .../{candidate_id}/approve`
- `POST .../{candidate_id}/reject`
- `POST .../{candidate_id}/merge`
- `POST .../{candidate_id}/retire`
- `GET /v1/matters/{matter_id}/second-pass-memory-snapshots/{snapshot_id}`

Decision requests require a reason, expected lock version, and destination preview token when they alter the
official taxonomy.

### 15.3 Backfill APIs

- `POST .../{candidate_id}/backfill/preview`
- `POST .../{candidate_id}/backfill`
- `GET /v1/jobs/SECOND_PASS_CODE_BACKFILL/{job_id}` through the unified job API

## 16. Frontend plan

### 16.1 Task authoring

Extend the Analysis Tasks panel with:

- Second Pass Review task creation;
- Review Guidance and coding-source selection;
- destination taxonomy selection;
- Fact Development enablement;
- compiled specification review; and
- validation and publication status.

### 16.2 Run launch and monitoring

The launch dialog shows:

- selected review batch and document count;
- coding source and pinned reviewer revision;
- task and memory versions;
- estimated document, model, and artifact usage;
- wave size; and
- whether Fact Development and entity seeding are enabled.

The run view shows wave progress, frozen coverage, coding decisions, candidate activity, fact operations, failures,
and usage.

### 16.3 Second-pass review workspace

Add a workspace with four primary queues:

1. **Coding verification** for affirm, overturn, expand, and needs-review results.
2. **Candidate codes** for emerging and proposed code cards.
3. **Facts** using the existing fact workspace and evidence viewer.
4. **Run quality** for exclusions, failures, invalid operations, and coverage gaps.

The candidate detail shows the definition, parent, overlaps, counts, representative evidence, counterexamples,
revision history, related documents, and available decisions.

### 16.4 Approval and backfill experience

The approval dialog previews:

- final label and official key;
- destination field and parent;
- inclusion and exclusion rules;
- conflicts with active values;
- estimated backfill document count; and
- the coding set that will receive recommendations.

After approval, the UI shows backfill progress separately from the completed Second Pass Review run.

## 17. Authorization and audit

Recommended permission boundaries:

| Permission | Capability |
| --- | --- |
| `analysis_task.manage` | Create, compile, edit, and publish the task specification. |
| `analysis_task.run` | Estimate, launch, retry, and cancel runs. |
| `second_pass.view` | Read runs, outcomes, memory, and candidates. |
| `second_pass.review` | Edit candidates and adjudicate coding recommendations. |
| `second_pass.taxonomy_manage` | Approve, merge, reject with matter scope, retire, and start code backfill. |
| `fact.view` / `fact.review` | Existing fact access and decisions. |
| `entity.view` / `entity.manage` | Existing entity access and decisions. |

Every candidate lifecycle decision, official-code mutation, coding adjudication, promotion, and backfill launch must
write an audit record with actor, authorization context, reason, source run, and before and after state.

## 18. Job states and progress

Add job stages without leaving the run open for human decisions:

1. `QUEUED`
2. `SNAPSHOTTING`
3. `SEEDING_ENTITIES` when enabled
4. `PLANNING_WAVES`
5. `REVIEWING_WAVE`
6. `CONSOLIDATING_WAVE`
7. `SYNTHESIZING`
8. `FINALIZING`
9. `COMPLETED`, `COMPLETED_WITH_ERRORS`, `FAILED`, or `CANCELED`

Progress is primarily documents finalized out of the frozen manifest. The run report separately exposes waves
completed, pending candidate proposals, facts created or updated, and backfill jobs launched.

Cancellation stops unstarted model work, finalizes already completed outcome fragments, marks remaining documents
canceled, and does not roll back facts or candidate revisions already committed. Retries reuse the same input
manifest and initial configuration, skip finalized chunks, and continue from the last published memory snapshot.

## 19. Search and reporting

### 19.1 Search projection

Project official second-pass assignments into the existing matter document index as ordinary coding. Candidate
observations remain run-scoped and should be searchable through outcome-detail APIs rather than the main document
index.

For the candidate workspace, index or query:

- status, task, parent, and target field;
- document and batch counts;
- labels, definitions, and rationales;
- related entities and issues; and
- first-seen and last-seen dates.

### 19.2 Run report

Report:

- frozen population and status counts;
- coding decisions by field and value;
- attorney acceptance and disagreement counts when available;
- observations and candidates created, strengthened, merged, proposed, approved, or rejected;
- rejected-tombstone matches;
- approved code assignments and backfill status;
- facts created, reused, refined, and suggested;
- entities created, reused, enriched, or skipped;
- invalid operations and evidence failures;
- request, token, latency, and cost estimates; and
- model and skill versions.

## 20. Testing strategy

### 20.1 Specification and publication

- Task-type registration and database constraints.
- Valid and invalid coding-source fields.
- Missing, inactive, or cross-matter metadata values.
- Source-reference and content-hash validation.
- Specification staleness after source changes.
- Publication requires a reviewed, valid specification.

### 20.2 Frozen input

- Only review-batch documents enter the manifest.
- First-pass values and reviewer coding revision remain unchanged after later edits.
- Every configured per-document field is frozen.
- Missing and excluded documents remain reportable.
- Source artifact and input fingerprints detect incompatible retries.
- A batch retry cannot resolve new documents or newer coding.

### 20.3 Document review

- The model can reference only supplied field, value, code, candidate, entity, and fact IDs.
- Citations must resolve to exact source paragraphs and quotes.
- Approved code assignments reject unknown or inactive codes.
- Candidate observations require evidence and an insufficiency rationale.
- Invalid operations are recorded and do not mutate domain state.

### 20.4 Memory and consolidation

- Every wave pins one immutable input snapshot.
- Parallel document work cannot mutate memory.
- Candidate IDs remain stable across waves and retries.
- Exact and semantic duplicate candidates consolidate correctly.
- Late-candidate rechecks prevent concurrent duplicates.
- Representative evidence remains within configured bounds.
- Rejected tombstones suppress repeated proposals.
- Task-scoped and matter-scoped rejections behave differently.

### 20.5 Human decisions

- Only authorized users can approve, merge, reject, or retire.
- Optimistic locking prevents lost edits.
- Approval creates exactly one official metadata value.
- Idempotent retries do not create duplicate codes or events.
- Merge and rejection preserve candidate history.
- Agent output alone cannot mutate the official taxonomy.

### 20.6 Coding and backfill

- First-pass coding remains unchanged until human adjudication and promotion.
- Accepted decisions append reviewer coding revisions.
- Promotion pins the exact revision and produces stable audit records.
- Backfill includes only earlier ready documents in the frozen manifest.
- Backfill cannot modify unrelated codes or parent outcomes.
- Later waves receive newly approved codes prospectively.

### 20.7 Facts and entities

- Existing Fact Development extraction and consolidation contracts continue to pass.
- Candidate relationships do not become official fact issues before code approval.
- Entity seeding is batch-scoped, idempotent, and concurrency-safe.
- Quoted and smart-quoted email addresses normalize correctly.
- Null display names remain valid for email-only entities.
- Confirmed and rejected entity states are preserved.

### 20.8 Workflow behavior

- Cancellation and retry at every stage.
- Provider batch fallback and partial failure behavior.
- Outcome fragments finalize exactly once.
- Memory snapshots resume from the last committed wave.
- Usage totals reconcile with model invocations.
- No worker process needs to be started by tests.

### 20.9 Frontend

- Task authoring and specification validation.
- Launch estimate and frozen-scope confirmation.
- Wave progress and failure rendering.
- Side-by-side first-pass and second-pass coding.
- Candidate filtering, detail, editing, and lifecycle actions.
- Approval conflict and stale-lock recovery.
- Backfill preview and progress.
- Fact and evidence navigation.

## 21. Delivery phases

### Phase 0: contracts and evaluation fixtures

Deliverables:

- Lock the task, specification, document-output, candidate, memory-snapshot, and backfill contracts.
- Create a small legal-review fixture corpus with first-pass coding, recurring patterns, one-off significant facts,
  overlapping concepts, and intentionally rejected candidates.
- Define evaluation measures for coding agreement, candidate duplication, candidate usefulness, citation validity,
  and rejected-candidate recurrence.

Exit criteria:

- The contracts can represent all fixture outcomes without free-form escape fields.
- Expected candidate lifecycles and backfill results are reviewed by a subject-matter reviewer.

### Phase 1: task type and durable candidate domain

Deliverables:

- Register `SECOND_PASS_REVIEW` in task, run, schema, and database constraints.
- Add candidate, revision, event, memory-snapshot, and wave tables.
- Add candidate command and query services.
- Add permissions, audit records, and read APIs.
- Add migrations and domain tests.

Exit criteria:

- A task and draft version can be created.
- Candidates can be created, revised, merged, rejected, and read through tested services without model execution.
- Memory snapshots are immutable and content-addressed.

### Phase 2: specification compilation and publication

Deliverables:

- Add `SecondPassReviewSpecification` and deterministic validation.
- Add the specification-compiler managed skill and workflow binding.
- Extend task APIs and UI for sources, destinations, Fact Development, and memory policy.
- Add compile, edit, stale, and publish behavior.

Exit criteria:

- A human can publish a valid task version that pins guidance, coding fields, destination taxonomy, and workflow
  configuration.
- Invalid or stale references block publication with actionable errors.

### Phase 3: frozen batch and single-wave vertical slice

Deliverables:

- Add launch estimate, run creation, job adapter, and input resolver.
- Freeze the full per-document contract in an Analysis Input Manifest.
- Add the `second_pass_document_review` skill and validator.
- Process one wave against an empty or existing initial memory snapshot.
- Publish per-document job outcomes and an aggregate run report.
- Expose coding verification and raw candidate observations as read-only results.

Exit criteria:

- A review-batch run is reproducible and retryable.
- Original coding, evidence, and output provenance are inspectable.
- The run cannot mutate coding or official taxonomy.

### Phase 4: multi-wave memory and consolidation

Deliverables:

- Plan deterministic waves and durable wave records.
- Add candidate retrieval, exact matching, and `candidate_consolidation` skill execution.
- Add locked candidate updates, late rechecks, representative-evidence selection, and snapshot publication.
- Feed each new snapshot to the next wave.
- Add rejected-tombstone recall.

Exit criteria:

- Later documents recognize candidates discovered earlier.
- Concurrent processing does not produce duplicate candidates or inconsistent snapshot reads.
- Memory size remains bounded as document count grows.

### Phase 5: taxonomy synthesis and attorney review

Deliverables:

- Add the `taxonomy_synthesis` role and final synthesis stage.
- Add candidate list and detail UI.
- Add edit, propose, approve, merge, reject, and retire workflows.
- Add official metadata-value preview and creation through the metadata command service.
- Publish memory snapshots after human decisions.

Exit criteria:

- The agent can produce complete code cards but cannot approve them.
- An authorized attorney can create an official subcode with complete audit history.
- Rejected and merged candidates remain durable and influence later runs.

### Phase 6: coding adjudication and batch backfill

Deliverables:

- Add the side-by-side coding-verification queue.
- Append accepted decisions to reviewer coding revisions.
- Integrate existing coding promotion and previews.
- Add approved-code backfill preview, job, outcomes, and review queue.
- Supply approved codes prospectively to later waves.

Exit criteria:

- Original and second-pass coding remain distinguishable.
- Approved codes are evaluated across all earlier ready documents in the frozen batch.
- No document outside the manifest is read or changed.
- Official document coding changes only through the configured human gate.

### Phase 7: Fact Development and entity composition

Deliverables:

- Invoke existing fact extraction and consolidation from the parent workflow.
- Reuse matter fact recall and reviewer suggestions.
- Seed batch email entities before review using the pinned normalizer.
- Add fact and entity metrics to document outcomes and run reports.
- Connect the Second Pass Review workspace to existing fact and entity detail views.

Exit criteria:

- One run can verify coding, develop candidate codes, and develop facts without duplicate fact or entity stores.
- Entity scope and review-state invariants hold under retry and concurrency.

### Phase 8: search, reporting, and full workspace

Deliverables:

- Add candidate search and filters.
- Add run and wave reporting.
- Add quality queues and related-document navigation.
- Project approved coding through existing document indexing.
- Complete task authoring, launch, run, candidate, coding, and backfill UI tests.

Exit criteria:

- Reviewers can move from a candidate or coding disagreement to exact document evidence and back.
- Reports reconcile frozen population, outcomes, domain mutations, and model usage.

### Phase 9: hardening and controlled rollout

Deliverables:

- Run shadow-mode pilots where recommendations cannot be promoted.
- Compare agent coding verification with senior-attorney adjudication.
- Review candidate acceptance, merge, rejection, and re-proposal rates.
- Tune wave size, candidate retrieval, evidence limits, and proposal policy.
- Add operational dashboards and alerts for failures, stalled waves, duplicate candidates, and usage anomalies.
- Document worker commands and operational recovery without automating worker restarts.

Exit criteria:

- Citation validity and scope isolation meet existing production standards.
- Candidate duplication and rejected-candidate recurrence are within agreed thresholds.
- Cost and latency are acceptable for the target batch sizes.
- A product owner and senior-attorney reviewer approve promotion-enabled rollout.

## 22. Operational metrics

Track at minimum:

- documents reviewed per hour and per model request;
- ready, missing, excluded, failed, and canceled counts;
- coding affirm, overturn, expand, and needs-review rates;
- attorney acceptance rate by decision type;
- observations per thousand documents;
- candidates created, strengthened, merged, proposed, approved, rejected, and retired;
- duplicate candidate and rejected re-proposal rates;
- representative evidence count and citation-validation failures;
- backfill document count, positive rate, and adjudication rate;
- facts created, reused, refined, and confirmed;
- entities created, reused, enriched, rejected, and merged;
- input, output, and cached tokens by workflow role;
- latency and failure rate by workflow stage; and
- estimated and actual model usage by run.

## 23. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Code explosion | Require insufficiency rationale, overlap analysis, full inclusion and exclusion rules, bounded proposal policy, and human approval. |
| Synonymous candidates | Retrieve existing candidates before creation, use exact and semantic consolidation, lock updates, and perform late duplicate rechecks. |
| Prompt growth | Supply a bounded memory snapshot with selected candidates and representative evidence rather than all prior documents. |
| Cross-batch leakage | Pin the review-batch manifest and enforce it in every document, consolidation, and backfill query. |
| Silent recoding | Preserve original values, store recommendations separately, and require attorney adjudication and promotion. |
| Repeated rejected proposals | Include task- or matter-scoped tombstones in every relevant snapshot and consolidation decision. |
| Early documents miss later codes | Re-evaluate earlier ready documents in the same frozen batch after approval. |
| Candidate counts overstate support | Count distinct documents and batches deterministically; keep model-produced significance separate from counts. |
| Memory races | Use immutable snapshots, candidate lock versions, transactional events, and one output snapshot per wave. |
| Weak provenance | Require exact paragraph citations and retain complete document outcomes in the job artifact. |
| Excess Core data | Store observations in outcome artifacts and only bounded examples in candidate revisions. |
| Fact or entity duplication | Reuse existing recall, consolidation, unique email constraints, locks, and rejected tombstones. |

## 24. MVP completion criteria

The MVP is complete when:

- `SECOND_PASS_REVIEW` is a published, versioned Analysis Task type.
- A run freezes exactly one review batch, its source artifacts, and configured first-pass coding.
- Documents are processed in at least two waves against immutable memory snapshots.
- The agent produces validated coding verification and candidate observations with exact evidence.
- Candidate memory persists across waves without loading all prior document content.
- Candidates have stable IDs, revisions, events, bounded evidence, and complete lifecycle states.
- Rejected candidates are not repeatedly reproposed.
- The agent cannot mutate the official taxonomy.
- An authorized attorney can approve a candidate into an official subcode.
- Earlier ready documents in the frozen batch can be backfilled against that code.
- Original and second-pass coding remain separately auditable.
- Fact Development uses the existing matter fact ledger.
- Entity seeding is limited to the frozen batch and preserves entity state.
- Retry, cancellation, partial failure, idempotency, authorization, and audit tests pass.
- The workspace supports evidence review for coding disagreements and candidate decisions.

## 25. Explicit non-goals for the initial release

- Fully autonomous taxonomy creation or publication.
- Automatic whole-matter recoding.
- Unbounded retention of every candidate observation in Core tables.
- Replacing Review Guidance or first-pass review.
- Replacing the existing fact or entity review interfaces.
- Treating projects, products, agreements, or other subject concepts as matter entities.
- Training a statistical classifier from approved subcodes during the same run.
- Real-time memory mutation between documents in the same wave.
- Cross-matter candidate or fact memory.
- Automatic worker start, restart, or process management.

## 26. Expected implementation locations

| Area | Expected location |
| --- | --- |
| Models and constraints | `app/models.py`, Alembic migration |
| Task type and publication | `app/analysis_tasks.py`, `app/schemas.py`, analysis-task router |
| Specification | new `app/second_pass_specifications.py` |
| Specification compilation | new `app/second_pass_specification_compilation.py` |
| Workflow orchestration | new `app/second_pass_review.py` and unified job adapter |
| Candidate commands | new `app/second_pass_candidates.py` |
| Memory snapshots | new `app/second_pass_memory.py` |
| Managed skills | new `app/second_pass_skills.py`, `app/workflow_specs.py` |
| Frozen inputs | `app/analysis_input_manifests.py` plus the Second Pass resolver |
| Coding adjudication | `app/reviewer_coding.py`, `app/analysis_task_promotions.py` |
| Facts and entities | existing `app/fact_development.py`, `app/facts.py`, `app/matter_entities.py` |
| APIs | analysis-task router plus a new Second Pass router |
| Search | `app/search/service.py`, mappings and query helpers as needed |
| Frontend | `web/src/components/analysis-tasks-panel.tsx` and new second-pass workspace components |
| Tests | task, run, candidate, memory, coding, backfill, fact, entity, authorization, and frontend suites |

## 27. Remaining product decisions

These decisions should be resolved during Phase 0 without blocking the architecture:

1. Whether task-scoped rejection or matter-scoped rejection is the default in the attorney UI.
2. Whether all accepted second-pass coding requires individual adjudication or whether an attorney may bulk-accept a
   reviewed rule with a preview.
3. Whether code approval automatically launches batch backfill or presents it as a separate confirmed action.
4. Whether candidate memory should carry across every run of a task by default or require an explicit "start from
   prior memory" launch choice.
5. Which official metadata field is the default destination when the first-pass field does not support hierarchy.
6. Whether a code approved late in a run should be supplied immediately to remaining waves or only after the run
   completes. The recommended default is immediate use at the next wave boundary, never mid-wave.
