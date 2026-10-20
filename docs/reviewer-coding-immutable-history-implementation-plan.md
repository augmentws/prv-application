# Immutable Human Reviewer Coding History Implementation Plan

## Decision

Every accepted human reviewer coding change must be retained. A reviewer still has
one canonical coding source per review batch, but that source is an append-only
sequence of revisions rather than only its latest mutable values.

The revision with the greatest server-assigned revision number that affects a
document, field, or status is the current winner for that item. Database timestamps
are descriptive and never decide ordering. Interactive review and imported coding
use the same history. Neither path creates a `review_batch_run` for a human reviewer.

The existing `review_batch_reviewer_document` and
`review_batch_reviewer_value` tables remain as fast current-state projections. The
immutable revision tables become the authoritative source for history, reproducible
comparisons, training-data provenance, and future parent/child disagreement batches.

## Implementation status

Implemented on October 14, 2026 through Core migration
`0086_reviewer_coding_history`:

- append-only reviewer revision, document revision, field revision, and typed value
  revision tables;
- one transactional append command shared by interactive save, explicit clear,
  skip, and CSV import;
- deterministic idempotency keys for imported rows;
- latest-revision current projections for the review workspace and statistics;
- immutable human history in the batch and matter document History panels;
- audit entries for every accepted reviewer mutation;
- as-of revision resolution used by human promotion snapshots;
- reviewer revision pinning in classification dataset label-policy snapshots; and
- a set-based baseline migration for existing current reviewer coding.

Persisted asynchronous comparison snapshots and an administrator-facing
projection-reconciliation command remain follow-on work. Batch Statistics reads
the latest current projection interactively; a disagreement child batch pins the
selected human revisions before its durable materializer replays the comparison.

## Goals

- Preserve every accepted human save, field replacement, explicit field clear,
  skip, and later recoding.
- Make the latest accepted revision the deterministic winner.
- Keep current batch review, progress, statistics, and search fast.
- Distinguish a field that was not changed from a field explicitly changed to
  **No value**.
- Allow callers to reproduce reviewer coding as of a pinned revision.
- Keep reviewer identity separate from agent, workflow, and classifier execution
  runs.
- Preserve the behavior of promotions already frozen from a reviewer revision.
- Give audit, comparison, export, and classification consumers stable provenance.

## Non-goals

- This change does not create human review runs or human pass IDs.
- It does not promote reviewer values into matter coding automatically.
- It does not itself implement parent/child disagreement batches. The later
  `COMPARISON_RESULT` batch workflow uses the revision-pinning prerequisite
  supplied here.
- It does not recover historical edits that were overwritten before this feature
  was deployed. Existing data can only be preserved as a baseline revision.
- It does not store a complete copy of every document's coding on every save.

## Current behavior and gap

`review_batch_reviewer_coding` currently supplies one stable source ID and a
monotonic revision for each `(review_batch_id, reviewer_user_id)`.
`review_batch_reviewer_document` stores the current document status, and
`review_batch_reviewer_value` stores only the current typed values. Saving a field
deletes its existing value rows and inserts replacements. Consequently, the old
value set cannot be reconstructed after the transaction.

The current revision number protects an in-flight promotion because promotion
copies the selected state into immutable snapshot rows. It does not, by itself,
preserve the revisions that produced the current state. Coding history,
comparisons, classification datasets, and future child batches therefore cannot
resolve an earlier reviewer state.

## Required invariants

1. There is exactly one `review_batch_reviewer_coding` row per batch and reviewer.
2. Its `revision` is a server-assigned, monotonically increasing integer.
3. A mutation locks that row, allocates the next revision, appends history, and
   updates the current projection in one database transaction.
4. A committed revision is immutable. Corrections are later revisions.
5. The greatest revision that touches a document determines its current status.
6. The greatest revision that touches a document and field determines that field's
   complete current value set.
7. A field-revision row with no value children means the reviewer explicitly
   cleared the field or recorded **No value**.
8. The absence of a field from a revision means the field was not changed; its
   previous winner remains in effect.
9. For a multiple-value field, every value attached to the winning field revision
   is the winning set. Individual values from different revisions are never merged.
10. The current projection must be reproducible from immutable history.
11. Human reviewer mutations never create `review_batch_run` rows.

## Target data model

### Existing canonical source

Keep `review_batch_reviewer_coding` as the stable identity for a reviewer's coding
in a batch. Its `revision` remains the highest committed revision, not merely a
count of API calls.

### `review_batch_reviewer_revision`

One immutable header per accepted mutation transaction:

- `reviewer_coding_id`: foreign key and first part of the composite primary key.
- `revision`: positive monotonic integer and second part of the composite primary
  key.
- `actor_user_id`: authenticated user responsible for the write.
- `origin`: controlled enum such as `REVIEW_UI`, `IMPORT`, or
  `SYSTEM_MIGRATION`.
- `request_key`: optional idempotency key for safely retrying an import or request.
- `created_at`.

Add a unique constraint on `(reviewer_coding_id, revision)`. If `request_key` is
supported in the first slice, also make it unique within the reviewer coding
source when non-null.

The header permits one revision to contain more than one document change. Normal
interactive saves will usually affect one document; a migration baseline or
future transactional import may affect many.

### `review_batch_reviewer_document_revision`

One immutable document outcome within a reviewer revision:

- `reviewer_coding_id` and `revision`: composite foreign key to the revision
  header.
- `matter_document_id`: the affected batch document.
- `status`: `IN_PROGRESS`, `COMPLETED`, or `SKIPPED`.
- `started_at` and `completed_at`, when applicable.

The composite primary key is
`(reviewer_coding_id, revision, matter_document_id)`.

This table is necessary because a skip or completion is meaningful even when no
field values are written.

### `review_batch_reviewer_field_revision`

One immutable replacement of a field on a document:

- `reviewer_coding_id`, `revision`, and `matter_document_id`: composite foreign
  key to the document revision.
- `metadata_definition_id`: the snapshotted coding field.

The composite primary key adds `metadata_definition_id` to the document revision
key.

The existence of this row means the field was explicitly replaced. Zero value
children is a durable explicit **No value** state. Without this level, the system
cannot distinguish a cleared field from an omitted field.

### `review_batch_reviewer_value_revision`

The immutable typed values belonging to one field revision:

- the composite reviewer, revision, document, and metadata-definition key of the
  field revision;
- `value_ordinal`: stable ordering within a multiple-value set.
- the same typed value columns and exactly-one-value shape constraint used by the
  current reviewer value table.

Add `value_ordinal` to that composite key. Validate
type and cardinality before inserting history so an immutable invalid event cannot
be committed.

### Current-state projections

Keep the existing tables:

- `review_batch_reviewer_document`
- `review_batch_reviewer_value`

They remain optimized for the review workspace, progress, current statistics, and
ordinary comparison reads. They are updated transactionally by the same command
that appends history. Application code must not update them directly.

Database triggers are not needed. Domain validation, history append, projection
replacement, and auditing stay in the Core command service.

## Mutation algorithm

Create one command service, provisionally
`append_reviewer_coding_revision`, and route every reviewer mutation through it.

For each mutation:

1. Authorize the actor and verify matter, batch, reviewer, membership, snapshotted
   field, type, cardinality, and status constraints.
2. Lock the `review_batch_reviewer_coding` row with `SELECT ... FOR UPDATE`.
3. Deduplicate an already-accepted request when a request key is present.
4. Allocate `next_revision = current_revision + 1`.
5. Insert the revision header.
6. Insert one document revision and a field revision for every explicitly supplied
   field, including fields supplied with an empty value list.
7. Insert the complete typed replacement set under each field revision.
8. Apply the same replacements to the current document/value projections.
9. Update `review_batch_reviewer_coding.revision` to `next_revision`.
10. Append an audit record containing batch, reviewer, document, revision, origin,
    affected field IDs, and status. The audit payload need not duplicate sensitive
    field values already retained in the immutable coding ledger.
11. Commit all history, projections, revision allocation, and audit data together.

The current row lock serializes concurrent writes. Therefore the last transaction
that successfully acquires the lock and commits receives the highest revision and
wins. A client timestamp or delayed network request cannot manufacture an earlier
or later order.

An optional `expected_revision` request field may later detect a stale editor and
return `409 Conflict`, but it must not change the stored ordering rule: among
accepted writes, the highest server revision wins.

## Operation semantics

### Save or recode

A save appends a new document revision. Every submitted field receives a new field
revision containing its full replacement value set. Fields omitted from the
request keep their earlier winners.

### Explicit No value

Submitting an empty value list appends a field revision with no value rows. That
empty field revision wins over all earlier non-empty revisions for the field.

### Skip

Skipping appends a document revision with `SKIPPED`. It does not erase earlier
field history. The current reviewer state treats the document as skipped and
ignores its values for comparisons and promotion unless a later revision returns
the document to a completed state.

### Return to and complete a skipped document

A later save appends a `COMPLETED` document revision and any supplied field
replacements. Previously coded fields that are not resubmitted retain their latest
field revisions. If the intended UI behavior is to clear every coding field when a
reviewer reopens a skipped document, the UI must explicitly submit empty values for
those fields; the storage layer must not infer a clear.

### Import

The batch-coding import path calls the same command service with `origin=IMPORT`.
It updates the reviewer's canonical coding and appends history; it never creates a
run. A stable request key should make a retried import row idempotent without
discarding legitimate later imports of changed data.

## Resolving current and historical state

Implement one resolver with two modes:

- `current`: read the current projection for low-latency application paths.
- `as_of_revision`: resolve document status and each field's latest revision whose
  reviewer revision is less than or equal to the pinned revision.

The as-of resolver must return:

- resolved document status and the revision that established it;
- resolved field value set and the revision that established it;
- an explicit No value marker when the winning field revision has no children;
- no field state when no qualifying field revision exists.

Add indexes for the common access paths:

- `(reviewer_coding_id, revision DESC)` on revision headers;
- document lookup by reviewer source, document, and revision;
- field lookup by document revision and metadata definition;
- the existing current-projection indexes used by batch review and comparison.

Do not replace current reads with history scans until measurements justify it. The
projection is intentionally retained so append-only correctness does not make the
review workspace slower.

## API and UI changes

### API

- Keep existing save and skip endpoint shapes where possible; their implementation
  moves to the append command.
- Return the committed reviewer revision from every mutation.
- Optionally accept an idempotency/request key and expected revision.
- Extend coding history to return human revision entries, not only current values.
- Add an authorized reviewer-history endpoint with paging by descending revision.
- Add an internal `as_of_revision` resolver for comparisons, classification
  snapshots, exports, promotions, and future child-batch creation.
- Regenerate the OpenAPI client after contract changes; never hand-edit generated
  frontend types.

### UI

- Keep the coding panel focused on the current winning values.
- Show saves only after the API returns the committed revision.
- Extend the History tab to group changes by revision and show actor, origin, time,
  document status, changed fields, and value transitions.
- Display an explicit **Cleared / No value** entry for an empty field revision.
- Do not present reviewer revisions as analysis runs or selectable human passes.

## Downstream consumers

### Batch Statistics and comparisons

Default statistics continue to use each reviewer's current projection, so each
reviewer appears exactly once. Comparison requests that need reproducibility must
pin the selected reviewer source and its revision. The comparison result and export
must report that revision.

### Classification datasets

When human reviewer coding supplies labels, dataset creation must pin
`reviewer_coding_id` and `revision`, then resolve values as of that revision. Later
reviewer edits cannot change the labels or derivation hash of an existing dataset.

### Promotion

Keep the existing immutable promotion document/value snapshots. Queueing a
promotion pins the current reviewer revision and populates the snapshots through
the as-of resolver. Add a reconciliation check proving the snapshot exactly matches
the pinned revision before the durable promotion begins. Later edits remain new
reviewer revisions and cannot alter the queued promotion.

### Disagreement child batches

The `COMPARISON_RESULT` workflow retains the parent batch plus a pinned revision
for every selected human source. The child therefore reproduces the exact values
that caused the disagreement even after a reviewer changes the parent-batch coding.
Analysis sources continue to pin immutable run IDs. A specialized adjudication
panel beyond the ordinary child-batch review workspace remains follow-on work.

### Audit log

Every reviewer mutation continues to create a security/audit entry. The audit log
answers who performed the operation and where; the reviewer revision ledger answers
exactly what coding state changed. Neither substitutes for the other.

## Existing-data migration and rollout

Historical values overwritten before deployment cannot be reconstructed. Migrate
the current known state as a synthetic baseline without changing its externally
visible revision number.

### Additive schema deployment

1. Add the four immutable-history tables, constraints, foreign keys, and indexes.
2. Deploy the centralized append command and route all live writers through it.
3. Temporarily support reconciliation between the history and current projection
   while the baseline is populated.

### Baseline backfill

For every existing `review_batch_reviewer_coding` with stored state:

1. Insert one `SYSTEM_MIGRATION` revision header using its existing revision. Do not
   increment or renumber it.
2. Add a document revision for every current reviewer-document row.
3. Copy every current reviewer value into field/value history.
4. For a completed document, create field revisions for every snapshotted batch
   coding field. A field with no current values becomes an explicit baseline
   **No value** state.
5. Preserve skipped and in-progress status. If legacy rows retain values beneath a
   skipped document, retain those values in history even though status resolution
   excludes them from current comparisons.
6. Leave existing promotion snapshots and their pinned revision numbers unchanged.

If a reviewer source has current data but revision `0`, use a documented baseline
revision of `1` and update its canonical revision in the same migration transaction.
The migration must first report how many such rows exist so this exceptional case
is visible.

### Reconciliation and cutover

Build a bounded reconciliation command that reconstructs each reviewer's latest
state from history and compares it with the current projections. It must report, at
minimum:

- reviewer sources checked;
- document-status mismatches;
- field-presence mismatches;
- typed value and ordinal mismatches;
- canonical revision mismatches.

Do not declare history authoritative until reconciliation is clean. Once clean,
remove any compatibility write path that can mutate the current projections without
appending a revision. Retain reconciliation as an administrative diagnostic and
repair tool; repair must rebuild projections from history, never rewrite history to
match a projection.

Archived batches retain their reviewer history. Hard deletion, if a later retention
policy permits it, may cascade only as part of deleting the entire batch and its
provenance.

## Implementation phases

### Phase 1 — Schema and contracts

- Add ORM models and an Alembic migration for the four history tables.
- Add controlled origin and status enums plus constraints and indexes.
- Define immutable domain input/output types for one reviewer mutation.
- Add resolver tests that express the latest-revision-wins rules before changing
  live writers.

**Exit:** the schema can represent value replacement, multiple values, explicit No
value, document completion, and skip history without ambiguity.

### Phase 2 — Central append command

- Implement row locking, revision allocation, validation, history insertion,
  current-projection update, and audit write in one transaction.
- Add request idempotency support for safe retry where the caller supplies a stable
  key.
- Prohibit direct projection mutation outside migration/reconciliation code.

**Exit:** one bounded service can atomically append history and update the current
winner.

### Phase 3 — Move every writer

- Route interactive save through the command.
- Route skip and later completion through the command.
- Route `scripts/batch_coding_import.py` through the same API behavior.
- Search the repository for direct writes to reviewer document/value projections
  and remove or migrate them.

**Exit:** all accepted live mutations append history, and no human mutation creates
a run.

### Phase 4 — Backfill and reconcile

- Backfill a synthetic baseline for existing reviewer sources.
- Run projection-versus-history reconciliation.
- Correct only migration defects; do not fabricate unavailable pre-migration
  history.
- Make the history ledger the declared authority after reconciliation succeeds.

**Exit:** every existing current reviewer state has an equivalent immutable
baseline and all current projections match their latest history winners.

### Phase 5 — History and as-of APIs

- Return committed revisions from mutations.
- Add paged human coding history.
- Add internal and authorized as-of reads.
- Regenerate OpenAPI and frontend clients.

**Exit:** callers can inspect every post-cutover change and reproduce a pinned
revision.

### Phase 6 — UI history

- Render reviewer revisions in the document History tab.
- Show old and new value sets, explicit clears, status changes, actor, origin, and
  timestamps.
- Preserve the ordinary coding panel's current-winner behavior.

**Exit:** a reviewer or authorized manager can understand how current coding was
reached without seeing duplicate reviewer sources.

### Phase 7 — Pin downstream consumers

- Pin reviewer revisions in reproducible comparison requests and exports.
- Resolve classification training labels as of the dataset's pinned revision.
- Populate promotion snapshots through the as-of resolver and verify them.
- Document the revision references required by future disagreement child batches.

**Exit:** later reviewer edits cannot silently alter an existing comparison,
dataset, export, promotion, or inherited disagreement source.

### Phase 8 — Hardening and operations

- Add metrics for appended revisions, projection failures, reconciliation
  mismatches, and idempotent retry hits.
- Measure history growth and resolver performance with representative matters.
- Document backup, retention, and projection-rebuild procedures.
- Remove temporary migration compatibility code.

**Exit:** operations can detect drift and rebuild current projections solely from
immutable history.

## Verification matrix

- First save creates revision 1 and the matching current projection.
- Changing a single value retains both revisions and exposes only the later value
  as current.
- Replacing a multiple-value field preserves each historical set without merging
  revisions.
- An explicit empty list creates a zero-value field revision and wins as No value.
- Omitting a field leaves its previous winning value set unchanged.
- Complete, skip, and complete again retain all status transitions.
- Concurrent accepted saves serialize and the higher committed revision wins.
- A retried request with the same request key does not create another revision.
- Interactive and imported edits update one reviewer source and both retain history.
- Current statistics still show one entry per human reviewer.
- No save, skip, or import path creates a `HUMAN` run.
- A comparison can reproduce both current and pinned historical reviewer values.
- A classification dataset retains labels after its reviewer edits later.
- A queued promotion remains unchanged after later reviewer edits.
- Baseline migration preserves current statuses, typed values, ordinals, and
  canonical revision numbers.
- Rebuilding current projections from history yields no reconciliation differences.
- Tenant, matter, batch, and reviewer authorization boundaries remain enforced for
  both current and history endpoints.

## Database impact

Implementation requires Core Alembic migration
`0086_reviewer_coding_history`, which creates the immutable-history tables,
constraints, foreign keys, indexes, and legacy baseline. Apply it with:

```bash
pipenv run alembic upgrade head
```
