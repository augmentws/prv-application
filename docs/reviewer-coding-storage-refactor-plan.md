# Reviewer Coding Storage Refactor

> **Status:** This plan established the single canonical human reviewer source and
> current-state tables. The follow-on decision to retain every reviewer change is
> specified in
> [`reviewer-coding-immutable-history-implementation-plan.md`](reviewer-coding-immutable-history-implementation-plan.md).

## Decision

Human coding is not an execution run. Each reviewer has one current coding set for
a review batch, whether the values were entered in the review workspace or
imported from CSV. Agent, workflow, and classifier evaluations remain immutable
runs because they pin executable configuration and may be rerun.

The current `HUMAN` `review_batch_run` implementation violates this boundary and
causes duplicate reviewer sources whenever coding is imported more than once.
The fix is a storage and API migration, not a display-only deduplication.

## Target storage

### `review_batch_reviewer_coding`

One row per `(review_batch_id, reviewer_user_id)`, with a monotonic `revision`.
This stable ID is the reviewer source used by comparison, classification label
selection, coding history, and promotion.

### `review_batch_reviewer_document`

One row per reviewer and batch document:

- `reviewer_coding_id`
- `matter_document_id`
- `status`: `IN_PROGRESS`, `COMPLETED`, or `SKIPPED`
- `revision`
- `started_at`, `completed_at`, and `updated_at`

The primary key is `(reviewer_coding_id, matter_document_id)`.

### `review_batch_reviewer_value`

One or more typed values per reviewer field:

- `reviewer_coding_id`
- `matter_document_id`
- `metadata_definition_id`
- `value_ordinal`
- the existing typed value columns
- `revision`, `created_at`, and `updated_at`

The primary key is the coding ID, document, field, and ordinal. Values remain isolated from matter
coding until promotion.

### Promotion snapshots

Every document save, skip, or import transaction increments the canonical coding
revision. A promotion records that revision and copies its completed documents
and typed values into immutable `review_batch_promotion_document` and
`review_batch_promotion_value` rows before the durable workflow is queued. Later
reviewer edits therefore cannot change an in-flight or historical promotion.

## API changes

1. Replace `POST /review-run` with reviewer endpoints scoped by the authenticated
   user. Opening review reads progress; it does not create an execution record.
2. Replace human document reads, value replacement, skip, progress, and complete
   endpoints with `/reviewer-coding/...` endpoints that do not accept a run ID.
3. Keep `/runs` and run-document APIs for `AGENT`, `WORKFLOW`, and `CLASSIFIER`
   only. Remove `HUMAN` from run schemas and database constraints.
4. Change comparison requests from run IDs to source IDs resolved as either a
   canonical reviewer-coding ID or an evaluation-run ID.
5. Change reviewer promotion to target `reviewer_coding_id` plus a server-frozen
   reviewer revision. Analysis promotion continues to target an immutable run.
6. Update coding history so reviewer entries identify the reviewer directly and
   evaluation entries identify their run.

## Import behavior

The batch-coding importer authenticates as the named reviewer and upserts the
same reviewer-owned coding set used by the workspace. Re-importing a document
replaces that reviewer's current values for the supplied fields. It never creates
a source, pass, or run. Dry-run validation remains side-effect free.

## Existing-data migration

1. Create the reviewer document, value, and revision storage.
2. For every existing human run, order writes by value/document `updated_at`,
   then by run creation time and ID for a deterministic tie-break.
3. Merge each `(batch, reviewer, document, field)` into the canonical reviewer
   coding set using the newest explicit field state. Preserve explicit empty
   fields and the newest document status.
4. Convert human promotion provenance to reviewer plus frozen revision/snapshot.
5. Verify per-reviewer document and field counts before and after migration.
6. Delete migrated human run values, documents, and runs; tighten the run-type
   constraint so new human runs are impossible.

## UI changes

- The review workspace no longer starts, resumes, or completes a run. It reads
  and saves the current user's reviewer coding directly.
- Batch statistics always contains one reviewer row per user.
- Comparison offers one reviewer source per user and completed evaluation runs.
- CSV imports immediately update the same reviewer row and counts.
- Human-facing copy uses *reviewer coding*, never *human run*.

## Verification

- Interactive saves followed by imports update one reviewer source.
- Repeated imports never add statistics or comparison entries.
- Two different reviewers remain isolated.
- Explicit no-value, skipped, incomplete, and completed states remain distinct.
- Promotion is stable when the reviewer edits after it was queued.
- Existing human-run data migrates without count or value loss.
- No API or database write path can create a `HUMAN` review batch run.
