# Review batches and evaluation runs

## Purpose

Review batches freeze a reproducible set of matter documents for reviewer assignment, quality-control sampling, and comparison of human or agent coding. They are Core records and contain document references, not copied artifacts.

## Data model

`review_batch` stores the matter, selection provenance, one optional assignee, visibility policy, workflow state, search-projection state, current common execution attempt, and final document count. `review_batch_document` is the permanent many-to-many membership snapshot and carries the stable sequence. PostgreSQL remains authoritative for membership.

`review_batch_coding_group` and `review_batch_coding_field` snapshot the selected matter metadata groups. The snapshot stores labels, ordering, and field schemas while retaining references to the source groups and definitions. This makes an evaluation reproducible if a matter administrator later edits a group.

Human coding is canonical per `(batch, reviewer)`. `review_batch_reviewer_coding` identifies that single reviewer source and carries a server-assigned monotonic revision. Every accepted save, explicit field clear, skip, or import appends immutable document, field, and typed-value revision rows. A field revision without value rows is an explicit **No value** event; an omitted field retains its earlier winner. The greatest revision affecting a document or field is current. `review_batch_reviewer_document` and `review_batch_reviewer_value` are transactionally maintained current-state projections—absence means `NOT_STARTED`—used for fast workspace, progress, and statistics reads. Human revisions never create `review_batch_run` records.

The document History panel reads the immutable ledger and shows reviewer revision, origin, status changes, prior values, and explicit clears. Interactive saves use `REVIEW_UI`; the CSV importer uses `IMPORT` plus a deterministic request key so retrying an identical row is idempotent. Migration `0086_reviewer_coding_history` creates one `SYSTEM_MIGRATION` baseline from each legacy reviewer's last known current state. Values overwritten before that migration cannot be reconstructed.

Executable evaluations use `review_batch_run`. Agent runs pin a published `agent_definition_version` and copy its prompt and model configuration into the run. Workflow runs pin their workflow record. Classifier runs pin a completed `classification_scoring_run`, its model version, prediction artifact hash, selected classes, and confidence window. `review_batch_run_document` stores per-run progress and `review_batch_run_value` stores typed, isolated values keyed by run, document, field, and ordinal. The database permits only `AGENT`, `WORKFLOW`, and `CLASSIFIER` run types.

Opening batch review uses an atomic get-or-create operation for the authenticated reviewer's canonical coding. An unassigned batch is assigned to the current user while the batch row is locked. Document coding reads and writes require that reviewer identity, and a save replaces the displayed fields in one request before marking the reviewer-document complete. Empty coding is valid and still records completion. Progress is derived from `review_batch_reviewer_document`, not from whether the reviewer happens to have values.

`OWN_VALUES` returns only the current reviewer's values. `ALL_REVIEWER_VALUES` may additionally return the current values from other reviewers, grouped by reviewer. Agent values remain excluded from the human review panel even under the broader visibility policy.

Reviewer values and run values intentionally do not update `metadata_event` or `document_metadata_current`. This prevents review-in-progress or an experimental evaluation from changing ordinary matter coding. One completed evaluation run at a time is selected as the batch's visible Batch Coding source. Each run that has been made searchable keeps its own rebuildable `batch_coding` OpenSearch entries, keyed by both batch ID and run ID. Switching back to a retained run only changes the selected-run pointer when its projection is current; it does not rewrite every batch document. Publishing reviewer coding or supported non-classifier results to the matter is a separate, explicit workflow that appends new matter metadata events. A reviewer promotion first snapshots the selected reviewer revision so later edits cannot change the queued promotion. Classifier runs cannot use the ordinary analysis promotion API.

## Classifier projections

A completed classification scoring artifact may be projected into a ready batch with `POST /v1/matters/{matter_id}/review-batches/{batch_id}/classifier-runs`. The scoring population and frozen batch membership must contain exactly the same document IDs, and the model target must be part of the batch's coding-field snapshot.

Each request creates an immutable `CLASSIFIER` run for one lower-inclusive, upper-exclusive confidence interval. Only successful, non-abstained resolved predictions in that interval become run values. A successfully evaluated document that emits no value because it is below the confidence window, outside the selected classes, missing a usable class confidence, or abstained is recorded as completed **No value**. A feature-excluded document that the model could not evaluate remains **Skipped**, and a runtime failure remains **Failed**. Creating another interval reuses the same scoring artifact and does not rerun inference.

One classifier scoring/task run contributes only its newest completed projection to the ordinary Batch Statistics comparison selector. Reprojecting supersedes the earlier selectable projection instead of adding another apparent task. Older immutable projection records and artifacts remain available as historical provenance.

Multiclass and one-vs-rest class keys map directly to destination coding values. A binary model may project only when each binary class maps to exactly one destination value; a combined positive or negative class representing several different coding values is rejected because the model did not resolve which value applies.

The workflow verifies the Artifact hash and embedded scoring-run, model-version, and class-order identities before writing any values. It then selects the completed classifier run for the existing `BATCH_CODING_SYNC` path unless another coding sync is already active. That selection conflict leaves the classifier run complete and records an actionable result-summary error rather than discarding its values.

Batch Coding filters accept an inclusive `minimum_confidence` and exclusive `maximum_confidence`. Both bounds are compiled inside the same nested OpenSearch entry as the batch, selected run, field, and typed value. The same bounds apply to facet counts. These controls work for JEV or other run values with scores as well as classifier probabilities, but scores from different systems are not assumed to be calibrated equivalently.

## Selection behavior

- `ALL_MATTER` freezes every document currently in the matter.
- `SEARCH_QUERY` re-executes a controlled keyword query and records the active physical search-index generation used to build the batch. The selection definition snapshots its generation number, physical name, schema hash, and activation time so provenance survives normal index-generation cleanup.
- `RANDOM_MATTER` deterministically orders all current matter documents by the stored seed and optionally takes a sample.
- `RANDOM_BATCH` applies the same deterministic sampling to an existing ready batch.
- `RANDOM_SAVED_SEARCH` authorizes and snapshots an accessible Keyword saved search, exhaustively enumerates its matches from the recorded active physical index generation, deterministically orders those IDs by the stored seed, and takes the requested sample. The batch does not depend on the saved-search row after creation.

The initial search-query builder supports keyword searches only. Semantic and hybrid batch selection need a separately defined candidate-set policy because approximate vector retrieval is not an exhaustive corpus predicate.

## Workflow and resilience

Creation persists a `REVIEW_BATCH_MATERIALIZATION` execution attempt and enqueues `review_batch_build` in the same transaction. The worker resolves the population, commits membership in bounded chunks, then projects that membership into the matter search index. The common stages are `RESOLVING_POPULATION`, `MATERIALIZING_POPULATION`, and `SYNCING_SEARCH`; document progress and heartbeats are visible through the batch response. The batch's membership state moves through `QUEUED`, `BUILDING`, and `READY`; its independent `search_status` moves through `QUEUED`, `SYNCING`, and `READY` (or `NOT_CONFIGURED` when search is disabled).

Cancellation is cooperative at materialization chunk boundaries and produces a durable `CANCELED` attempt. Failed or canceled materialization can be safely retried as a new attempt with a new DBOS workflow identity. A membership failure marks the attempt `FAILED`. A search-projection failure does not discard valid frozen membership: the batch remains `READY`, `search_status` is `FAILED`, and the common attempt is `COMPLETED_WITH_ERRORS`. Deployments with DBOS disabled run the same transitions inline for local development and tests.

`DEFINITION_ASSESSMENT` batches are different: their membership is produced inside the owning Matter Definition assessment workflow. They therefore do not receive a second review-batch materialization execution.

`DELETE /v1/matters/{matter_id}/review-batches/{batch_id}` is a soft delete governed by `batch.delete`. It changes a terminal batch to `ARCHIVED`, excludes it from the ordinary list endpoint and all list-backed selectors, and appends a `review_batch.deleted` audit record. It does not delete batch membership, run documents, values, projections, artifacts, or provenance. Active materialization, review/analysis runs, and coding promotions block archival.

Jobs use the independent `job_archive` tombstone keyed by logical job type and job ID because their domain status constraints are intentionally type-specific. `DELETE /v1/jobs/{job_type}/{job_id}` requires `job.delete`, accepts terminal jobs only, excludes the logical job from ordinary matter job lists, and retains every underlying domain and execution record. The archive action appends a `job.deleted` audit record.

Each matter search document has a keyword-array `batch_ids` projection. A full index rebuild derives this array from ready `review_batch_document` rows and reconstructs every retained READY Batch Coding run from PostgreSQL, so OpenSearch never becomes the source of truth. The batch-search and batch-facet endpoints inject the required `batch_ids` filter on the server; callers cannot omit or replace it. Batch Coding queries additionally pin the currently selected run ID. A newly materialized batch is unavailable for batch review search until its projection is `READY`.

Publishing a Batch Coding run uses the active index directly. If the index predates the `batch_coding` mapping, the service adds that mapping in place. Unrelated pending schema changes, such as a new ordinary search field that would require historical backfill, do not force or block Batch Coding with a full matter reindex.

The batch review workspace uses those scoped endpoints for keyword, semantic, and hybrid search, facets, type-ahead facet values, indexed document titles, and passages. It separately loads the authenticated reviewer's current document status from Core. Search result order may change with the active query and sort, while `review_batch_document.sequence_number` remains the stable frozen order and is shown with each result.

## Comparisons

Batch Statistics is a dedicated page at `/review/matters/{matter_id}/batches/{batch_id}/statistics`. Every Statistics action opens it in a separate browser tab or window so navigating and filtering statistics cannot replace an active document-review workspace. **Batch statistics → Compare coding** compares two to six coding sources over the same frozen batch. Each reviewer appears once using the canonical `review_batch_reviewer_coding` source. Evaluation sources are successfully completed Jev/workflow, agent, and classifier-projection `review_batch_run` records that persisted a value for the selected field. Failed, canceled, queued, running, zero-value, playground, and legacy assessment workflow records are omitted from the selector. The user selects one coding field and the sources explicitly; the UI never substitutes whichever run happens to be projected into Batch Coding search.

Workflow and classifier-projection labels resolve the current `matter_analysis_task.name` through the task identity pinned by the run. The immutable configuration snapshot's `task_name` is only a fallback for legacy records without a resolvable task identity.

The comparison reads PostgreSQL reviewer document/value tables and evaluation run document/value tables in bounded membership chunks. It does not republish a source, modify matter coding, require an OpenSearch projection, or create per-document comparison rows. Completed documents with an intentionally blank field are counted as **No value**. Failed, skipped, in-progress, and never-evaluated documents remain distinct and are not converted to negative coding.

The view provides:

- Per-value document counts for every selected source.
- Coverage counts for evaluated, no-value, skipped, failed, and incomplete documents.
- Pairwise exact-set agreement and mean Jaccard similarity.
- A confusion matrix for single-value enum and boolean fields.
- Per-value overlap across every selected source, with one named source column plus all-selected and any-selected counts.
- A paged document-by-source matrix filterable to exact agreements, disagreements, or incomplete rows.
- CSV and Parquet exports of the document matrix. Export columns retain the selected source identities in the Parquet schema metadata, and duplicate display names receive a short source-ID suffix.

### Comparison state and metric semantics

For each selected source and batch document, the comparison resolves one source state:

- `EVALUATED`: the run-document is complete, or typed values exist; an empty value set is counted separately as **No value**.
- `SKIPPED` or `FAILED`: the durable run-document has that terminal state.
- `INCOMPLETE`: the run-document is queued or in progress.
- `NOT_EVALUATED`: no completed progress or value record exists for the run-document.

A document is `EXACT` when every selected source is evaluated and all complete value sets are equal. It is `PARTIAL` when all sources are evaluated, their sets are not equal, and at least one source pair has a non-empty intersection. It is `DISAGREEMENT` when all are evaluated and no pair shares a value. It is `INCOMPLETE` whenever any selected source is not `EVALUATED`. The UI's **Disagreements** filter includes both `PARTIAL` and `DISAGREEMENT`; **Exact agreement** includes only `EXACT`.

For each source pair, `compared_document_count` includes only documents evaluated by both. `incomplete_document_count` contains every other batch document. Exact-set agreement is:

```text
exact_match_count / compared_document_count
```

Jaccard similarity is computed per comparable document as:

```text
|left value set ∩ right value set| / |left value set ∪ right value set|
```

Two empty evaluated sets have Jaccard `1.0`. `mean_jaccard` is the arithmetic mean of the per-document scores over `compared_document_count`; incomplete documents are excluded.

The all-source per-value table uses only documents evaluated by every selected source. For each value it returns a count for every selected source, `all_selected_count`, and `any_selected_count`. Its overlap is:

```text
all_selected_count / any_selected_count
```

Documents where no selected source assigned the value are excluded from that denominator. A value never assigned by any selected source has a null overlap. The UI exposes both definitions through a keyboard-focusable and hoverable rollover.

The pairwise confusion matrix is emitted only for `SINGLE` enum and Boolean fields. It includes intentionally empty evaluated coding under the `__NO_VALUE__` key, and the UI names both selected sources in its columns. The all-source per-value overlap is returned independently of the selected pair. The document matrix shows the maximum stored confidence for each evaluated run-document/field combination; it does not combine or calibrate scores from different source types.

### API, paging, and export behavior

`GET .../comparison-options` returns eligible fields and source runs. `POST .../coding-comparison` accepts one field, two to twelve distinct runs, a row filter, an offset, and a limit of at most 200; the current UI deliberately limits selection to two to six runs and pages 50 matrix rows at a time. Core streams frozen membership in chunks of 2,000 rather than loading the whole matrix into memory.

Run summaries return at most the 200 most frequent values per source, and the UI chart shows the 40 most frequent values across selected sources. These display limits do not truncate document comparison or export. `GET .../coding-comparison/export` streams every row matching `ALL`, `AGREEMENT`, `DISAGREEMENT`, or `INCOMPLETE`. CSV and Parquet include `matter_document_id`, `collection_item_id`, stable `batch_sequence`, `agreement_status`, and one display-valued column per run. Non-evaluated states use bracketed markers and an evaluated empty set is `[NO VALUE]`. Parquet metadata stores the field identity and a run-column-to-run-ID map.

Agreement is symmetric and is not presented as accuracy. A later reference-source workflow may add precision, recall, and F1 only after a user explicitly designates an adjudicated or other reference source.

The older `GET .../comparisons` endpoint remains as a lightweight two-run field summary for compatibility. The richer batch view uses `comparison-options`, `coding-comparison`, and `coding-comparison/export`.

The durable, artifact-backed replacement for classifier/Jev and large-population comparisons is specified in `docs/analysis-result-comparison-artifact-implementation-plan.md`.

## Follow-on work

- Use pinned human reviewer revisions when persisted asynchronous comparison
  artifacts and disagreement child batches are implemented. The immutable ledger
  and as-of resolver are specified in
  [`reviewer-coding-immutable-history-implementation-plan.md`](reviewer-coding-immutable-history-implementation-plan.md).
- Drive agent runs through a durable per-document execution workflow.
- Add explicit publish/adjudication into matter metadata.
- Add persisted comparison records and asynchronous artifact generation for repeated whole-matter or very large-cohort analysis.
- Add an outcome manifest if per-document materialization failures become possible; ordinary membership insertion is currently atomic per bounded chunk.
