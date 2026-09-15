# Review batches and evaluation runs

## Purpose

Review batches freeze a reproducible set of matter documents for reviewer assignment, quality-control sampling, and comparison of human or agent coding. They are Core records and contain document references, not copied artifacts.

## Data model

`review_batch` stores the matter, selection provenance, one optional assignee, visibility policy, workflow state, and final document count. `review_batch_document` is the permanent many-to-many membership snapshot and carries stable sequence and review status.

`review_batch_coding_group` and `review_batch_coding_field` snapshot the selected matter metadata groups. The snapshot stores labels, ordering, and field schemas while retaining references to the source groups and definitions. This makes an evaluation reproducible if a matter administrator later edits a group.

Every coding pass creates a `review_batch_run`. Human runs identify the reviewer. Agent runs pin a published `agent_definition_version` and copy its prompt and model configuration into the run. `review_batch_run_value` stores typed, isolated values keyed by run, document, field, and ordinal.

Run values intentionally do not update `metadata_event`, `document_metadata_current`, or the search index. This prevents an experimental agent run from changing ordinary review coding. Publishing accepted results to the matter will be a separate, explicit workflow that appends new matter metadata events.

## Selection behavior

- `ALL_MATTER` freezes every document currently in the matter.
- `SEARCH_QUERY` re-executes a controlled keyword query and records the active physical search-index generation used to build the batch.
- `RANDOM_MATTER` deterministically orders all current matter documents by the stored seed and optionally takes a sample.
- `RANDOM_BATCH` applies the same deterministic sampling to an existing ready batch.

The initial search-query builder supports keyword searches only. Semantic and hybrid batch selection need a separately defined candidate-set policy because approximate vector retrieval is not an exhaustive corpus predicate.

## Workflow and resilience

Creation commits the batch and enqueues `review_batch_build` in the same transaction. The worker materializes membership idempotently. The batch moves through `QUEUED`, `BUILDING`, and `READY`; failures retain the error message. Deployments with DBOS disabled materialize inline for local development and tests.

## Comparisons

The comparison endpoint compares two runs over the same frozen batch by field and reports matches, mismatches, and values missing on either side. Multi-value fields are compared as sets. Agreement is the matching count divided by documents for which both runs supplied a value. This is a direct agreement measure, not yet a full precision/recall or adjudication system.

## Follow-on work

- Drive agent runs through a durable per-document execution workflow.
- Add blinded reviewer navigation and coding against human runs.
- Add explicit publish/adjudication into matter metadata.
- Add persisted comparison records and richer categorical metrics.
- Add retry/cancel controls and chunked search snapshot materialization for very large query batches.
