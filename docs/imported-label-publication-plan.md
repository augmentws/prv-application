# Imported Matter Labels During Document Publication

## 1. Purpose

Collection items may contain externally supplied coding values in
`CollectionItem.unmapped_metadata`. When those items are published into a
matter today, the publication workflow creates `MatterDocument` and custodian
records but does not turn matching external values into matter metadata labels.

This is why a value such as `tr2016_facets` can appear in Search while no label
or imported-source entry exists in the matter metadata history.

This plan fixes only that collection-to-matter publication behavior.

## 2. Scope boundary

Included:

- Match `unmapped_metadata` keys to matter metadata definitions.
- Publish matching values as normal metadata events with source `IMPORT`.
- Project those events into `DocumentMetadataCurrent` through the existing
  metadata resolution code.
- Show imported values and provenance through existing matter metadata and
  coding-history APIs.
- Reindex matter documents whose imported labels changed.
- Support idempotent republishing and backfill of existing matter documents.

Excluded:

- Any downstream use of the published metadata.
- A collection field catalog or configurable field-mapping system.
- Explicit records for documents that do not contain a matching value.

## 3. Architectural decision

Use the existing metadata event system. It already supports:

```text
source_type = IMPORT
```

Do not add a new imported-label table or a JSON column to `MatterDocument`.

For every matching external value, publication creates a `MetadataEvent` with
source `IMPORT`. The normal `project_field` path then maintains
`DocumentMetadataCurrent`. Imported values are therefore real matter labels
with provenance rather than a special-purpose projection.

No event is created when the external value is absent. Absence remains
implicit.

## 4. Field association

Association is intentionally simple:

1. Load the matter's active metadata definitions.
2. Build a case-insensitive lookup by `MetadataDefinition.key`.
3. For each key in a collection item's `unmapped_metadata`, find the matching
   definition.
4. If no definition exists, leave the value unmapped.
5. If a definition exists, validate and publish the value against that
   definition.

The stored event uses the canonical matter definition ID and key. Source key
casing is not significant.

The initial implementation publishes into matter-owned `ASSERTED` definitions,
because those are the definitions supported by the metadata event and current
projection APIs. System and authoritative imported definitions continue to
resolve through their existing collection-backed paths.

No per-collection binding or mapping configuration is introduced.

## 5. Artifact selection contract

The current collection-selection batch response contains item IDs and
custodians but not `unmapped_metadata`. Extend the internal Artifact contract so
Core can request values for a bounded set of field keys while publishing.

Conceptually:

```text
GET /collection-selections/{selection_id}/items
  ?offset=...
  &limit=...
  &metadata_key=tr2016_facets
  &metadata_key=tr2016_important
```

Each item may return:

```json
{
  "item_id": "...",
  "custodians": [],
  "unmapped_metadata": {
    "tr2016_facets": ["george w. bush", "marketing"]
  }
}
```

Requirements:

- Core requests the active asserted matter definition keys frozen for the
  import job.
- Artifact matches keys case-insensitively.
- Returned keys use the requested canonical spelling.
- Missing keys are omitted.
- Unrequested raw and unmapped metadata are not returned.
- Key count, value size, and total batch payload are bounded.
- Selection membership and tenant/client authorization remain enforced.
- Embedded and remote Artifact modes behave identically.

This avoids one Artifact request per document and avoids copying arbitrary
unmapped metadata through the workflow.

## 6. Publication workflow

### 6.1 Freeze definitions

When `freeze_matter_import_selection` runs, also snapshot the active asserted
metadata definitions needed by the import:

```text
definition ID
canonical key
type
cardinality
allowed enum values
assertion policy
resolution policy
normalization settings
```

Every batch in the job uses this same snapshot. A matter schema change during
publication must not cause different batches to interpret the same source key
differently.

### 6.2 Load selection batches

Pass the frozen keys to the Artifact selection-batch call and include returned
`unmapped_metadata` in the bounded DBOS batch input.

The DBOS payload must contain only matched values, not the collection item's
complete metadata dictionaries.

### 6.3 Validate values

Use the existing metadata `value_columns` validation rules:

- Text and long-text values must be strings.
- Enum values must be active allowed-value keys.
- Boolean, integer, decimal, date, datetime, and JSON values must satisfy the
  definition type.
- Single-cardinality definitions accept one value.
- Multiple-cardinality definitions deduplicate values deterministically.

Empty values create no event. Invalid values are omitted from publication and
reported through bounded batch warnings.

Do not invent enum options from source values. Adding or changing enum values
remains a matter schema operation.

### 6.4 Create import events

For a matching valid field:

- Single cardinality: create one `SET` event.
- Multiple cardinality: create one `ADD` event per distinct value.
- Set `source_type` to `IMPORT`.
- Set `actor_id` to the user who started the matter import.
- Set `source_id` to a deterministic identity derived from the collection item,
  external key, and value where necessary.
- Run `project_field` once per affected document and definition after inserting
  the batch of events.

Example source identities:

```text
collection-item:{item_id}:unmapped:tr2016_facets
collection-item:{item_id}:unmapped:tr2016_facets:{value_hash}
```

The source identity must fit the existing length limit and must not contain the
raw value itself.

The field's existing assertion and resolution policies continue to apply. For
example, an import into a definition requiring confirmation becomes pending,
and an existing human assertion participates according to that definition's
resolution policy. Publication does not introduce a second precedence system.

### 6.5 Create documents and labels atomically

Within each Core import-batch transaction:

1. Create or resolve the `MatterDocument`.
2. Create missing custodian relationships.
3. Create missing imported metadata events.
4. Rebuild affected `DocumentMetadataCurrent` projections.
5. Enqueue Search upserts for affected documents.
6. Complete the import-batch record.

A batch failure rolls back its documents, relationships, labels, projections,
and Search operations together.

## 7. Idempotency

DBOS retries and republishing must not append duplicate import events.

Add a database-enforced import-source uniqueness rule over:

```text
matter_document_id
metadata_definition_id
source_type = IMPORT
source_id
```

Every imported event must have a non-null deterministic `source_id`.

Before inserting, resolve existing import events for the same source identities.
The unique rule remains the final concurrency guard. An identical event is a
no-op. A source identity resolving to different typed content fails closed,
because collection item import metadata is expected to be immutable.

Republishing an existing matter document still reconciles its metadata. It is
not skipped merely because the `MatterDocument` already exists.

## 8. Current-value and history behavior

After publication:

- The imported event appears in coding history with source `IMPORT`.
- `DocumentMetadataCurrent` exposes the resolved label if the field policy
  makes the event effective.
- Search uses the current projection and shows the same effective label.
- A later human event remains distinguishable by source `HUMAN`.
- A later clear, replacement, confirmation, rejection, or conflict follows the
  existing metadata resolution rules.

The metadata event itself is the authoritative provenance.

## 9. Import accounting

Add aggregate counters to `MatterDocumentImportBatch` and
`MatterDocumentImportJob`:

```text
imported_label_document_count
imported_label_event_count
imported_label_warning_count
```

Definitions:

- `imported_label_document_count`: distinct processed documents for which at
  least one new import event was created.
- `imported_label_event_count`: new `SET` or `ADD` events created.
- `imported_label_warning_count`: matching source fields rejected during type or
  cardinality validation.

Do not count missing source keys as warnings and do not create per-document
missing-label records.

Batch error text and warning examples remain bounded. Detailed job outcome
diagnostics belong to the separate generic job-outcome initiative.

## 10. Existing matters and backfill

The current TREC matter can be repaired by republishing the same collection
selection:

1. Existing documents resolve through `(matter_id, collection_item_id)`.
2. Custodian links remain idempotent.
3. Missing `IMPORT` events are created from matching `unmapped_metadata`.
4. Existing deterministic import events are reused.
5. Affected current projections are rebuilt.
6. Affected documents are reindexed.

The import continues to report existing documents as duplicates while imported
label counters report the new metadata work separately.

If replaying the selection is inconvenient, expose a repair command that calls
the same batch reconciliation function. Do not build an independent backfill
implementation.

## 11. API and UI effects

### Matter import status

Return the aggregate imported-label counters with existing import progress.

### Document metadata

No new label-reading API is required. Existing current metadata and history
responses should expose the imported event and source. If a response currently
omits event provenance for a resolved value, extend that generic metadata
response rather than adding an import-specific endpoint.

### Search

Search upserts run after import events are projected. Search should no longer
need the `unmapped_metadata` fallback for a field once that field has a matching
matter definition and published import event. Keep fallback compatibility until
existing matters have been republished.

## 12. Tests

### Artifact contract

- Requested keys are returned case-insensitively and canonically.
- Missing and unrequested keys are omitted.
- Raw metadata is not returned by this contract.
- Selection membership and scope authorization are enforced.
- Key and payload limits are enforced.
- Embedded and remote modes are equivalent.

### Publication

- A matching single value creates one `SET` event with source `IMPORT`.
- Matching multiple values create deduplicated `ADD` events.
- Unknown keys remain unmapped.
- Empty keys create no events.
- Invalid types and enum values are warned and omitted.
- Matter definitions are frozen consistently across batches.
- Metadata projection and Search operation are committed with the document.
- An import batch rollback leaves no partial labels.

### Idempotency and backfill

- A DBOS retry creates no duplicate events.
- Republishing existing documents creates only missing imported labels.
- Concurrent retries are protected by the database uniqueness rule.
- Changed content for the same immutable source identity fails closed.
- Repaired documents are reindexed.

### Metadata behavior

- Current metadata reports the imported value when effective.
- Coding history reports source `IMPORT` and the deterministic source ID.
- Confirmation and resolution policies continue to work.
- Later human assertions retain source `HUMAN`.
- Missing external values have no events or placeholder rows.

### End-to-end TREC regression

Publish a collection item containing `tr2016_facets` and a matter definition
with the same key. Verify that the matter document receives imported metadata
events, its current label is projected, the label appears in Search, and a
second publication creates no duplicates.

## 13. Phases

### Phase 1: Artifact batch metadata

- Extend selection-batch request and response schemas.
- Implement filtered, case-insensitive `unmapped_metadata` reads.
- Add limits, authorization tests, and embedded/remote parity tests.

Exit when a frozen selection batch can return only requested external fields.

### Phase 2: Import event publication

- Freeze matter definitions with the import job.
- Carry matched metadata through DBOS batches.
- Validate and create `IMPORT` events.
- Add deterministic source identities and uniqueness protection.
- Project affected current values and enqueue Search updates atomically.

Exit when a newly published document receives effective imported labels.

### Phase 3: Idempotent repair

- Reconcile labels for existing duplicate matter documents.
- Add imported-label job and batch counters.
- Add the shared repair entry point if needed.
- Republish the TREC selection.

Exit when the existing TREC matter is repaired without document or event
duplication.

### Phase 4: UI and documentation

- Display import-source provenance in generic metadata history.
- Display imported-label counts in import status.
- Update collection-to-matter publication documentation.
- Complete end-to-end TREC verification.

Exit when an administrator can verify which labels were imported as part of
document publication.
