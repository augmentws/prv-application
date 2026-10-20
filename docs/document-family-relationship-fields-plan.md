# Parent and Child Document Relationship Fields Plan

Status: Core feature implemented (source-specific legacy audit/backfill tooling remains rollout work)

Related documents:

- `docs/collection-navigation-plan.md`
- `docs/search-index-lifecycle.md`
- `web/content/docs/metadata-definitions.mdx`

## 1. Outcome

Add explicit parent and child document identifiers to the base Priv-View data
model while preserving `family_id` as the complete-family grouping key.

After this work:

- a child document has one `parent_id` value identifying its direct parent;
- a parent document has zero or more `child_id` values identifying its direct
  children;
- a standalone document has neither value;
- every member of a document family continues to share one `family_id`;
- relationships are established during collection ingestion, before documents
  are added to a matter; and
- Parent ID and Child ID are read-only system metadata available in document
  tables, document details, search, and search-result exports.

The intended flow is:

```text
source relationship fields or extracted attachments
    -> importer-normalized child-to-parent edges
    -> Artifact collection_item.parent_collection_item_id
    -> Artifact relationship snapshots
    -> Core system metadata projection
    -> matter search, review display, and export
```

## 2. Current state

The collection model already contains most of the required canonical storage:

- `collection_item.family_id` is indexed.
- `collection_item.parent_collection_item_id` is an indexed self-referencing
  foreign key.
- the collection-item upload contract accepts both fields.
- `ImportItem` accepts `parent_source_item_id` and `family_id`.
- the base importer resolves `parent_source_item_id` to the already-uploaded
  parent's internal collection-item ID.
- generic email attachment extraction and the Jeb Bush inventory adapter
  already emit parent/child edges.
- collection-item reads expose `parent_collection_item_id`.

The missing pieces are:

1. The base metadata profile exposes only `family_id`.
2. Search snapshots do not carry parent or child identifiers.
3. Parents do not expose their inverse child list.
4. Vendor/load-file mappings do not have a shared ParentID/ChildID
   normalization contract.
5. Upload idempotency does not reconcile relationship metadata on an existing
   item with identical content.
6. Existing matters have no upgrade path for the new system definitions.

## 3. Locked data-model decisions

### 3.1 Canonical relationship

Store one canonical edge on the child:

```text
collection_item.parent_collection_item_id -> collection_item.id
```

Do not add a persisted child-ID array or join table. A parent's children are
the rows whose `parent_collection_item_id` points to that parent. Persisting
both directions would create two sources of truth.

The existing index on `parent_collection_item_id` is sufficient for direct
child lookup.

### 3.2 Base metadata definitions

Add these definitions to the default matter metadata profile:

| Key | Display name | Type | Cardinality | Source | Searchable | Facetable |
| --- | --- | --- | --- | --- | --- | --- |
| `parent_id` | Parent ID | `TEXT` | `SINGLE` | `SYSTEM` | yes | no |
| `child_id` | Child ID | `TEXT` | `MULTIPLE` | `SYSTEM` | yes | no |

Add both definitions to the existing `family` metadata group after
`family_id`. IDs are intentionally not facetable because they are
high-cardinality relationship values.

The singular `child_id` key matches common load-file terminology while its
metadata cardinality explicitly permits multiple direct children.

### 3.3 User-facing identifier

`parent_id` and `child_id` values use the same canonical document identifier
shown as Control number:

1. `BEGDOC`
2. `BEGBATES`
3. `CONTROL_NUMBER`
4. `source_item_id` as the fallback

Internal collection-item UUIDs remain the foreign keys and API identities;
they are not presented as Parent ID or Child ID. This keeps relationship
fields meaningful in review tables and exports while retaining stable internal
referential integrity.

The identifier resolver must be one shared, tested function. Control number,
Parent ID, and Child ID must not implement separate precedence rules.

### 3.4 Relationship meaning

- Parent and child fields represent direct edges only.
- `family_id` represents the complete family, including any descendants.
- A child has at most one direct parent.
- A parent may have any number of direct children.
- A document may be related to collection items that were not added to the
  current matter. Its Parent ID or Child ID still reflects the collection
  relationship.
- A family identifier alone does not prove which member is the parent. Do not
  infer edges merely because items share `family_id`.

### 3.5 Integrity rules

Every relationship write must enforce:

1. Parent and child exist in the same collection, tenant, and client.
2. A child cannot be its own parent.
3. A relationship cannot introduce a cycle.
4. Parent and child have the same non-null `family_id` after normalization.
5. A child has no more than one parent.
6. Repeating the same edge is idempotent.
7. Attempting to replace an existing parent is a conflict unless the caller
   uses the explicit reconciliation operation and supplies the expected
   current parent.

For a new child with a supplied parent:

- if the child has no family ID, inherit the parent's family ID;
- if the parent has no family ID, assign one deterministic family ID to the
  parent and child; and
- if both have different family IDs, reject the relationship rather than
  silently moving either item.

## 4. Collection-ingest contract

### 4.1 Importer input

Keep the importer-facing canonical input as:

```text
ImportItem.source_item_id
ImportItem.parent_source_item_id nullable
ImportItem.family_id nullable
```

Adapters may receive any of these source shapes:

- ParentID on each child;
- ChildID or a delimited child list on the parent;
- family range fields such as BEGATTACH/ENDATTACH;
- an email/native item followed by extracted attachments; or
- an explicit family ID plus directed relationship fields.

Each adapter converts its source shape into child-to-parent edges. Preserve the
original source fields in `unmapped_metadata` for provenance, but do not make
raw vendor field names authoritative.

When both ParentID and ChildID representations exist, normalize both and fail
the adapter row or import validation if they disagree.

### 4.2 Ordering

The current uploader requires the parent to have been uploaded before its
child. The normalized importer must therefore group and order family members
so the parent is submitted first. Independent families may continue uploading
concurrently.

Adapters that cannot determine all edges in a single streaming pass must use a
bounded preprocessing pass or a temporary on-disk relationship spool. Do not
hold an unbounded corpus-wide relationship map in memory.

### 4.3 Relationship reconciliation API

Add a collection-scoped batch command for relationships rather than changing
the semantics of idempotent native upload:

```text
POST /v1/collections/{collection_id}/item-relationships:reconcile
```

Each command row contains:

```text
child_source_item_id
parent_source_item_id
family_id nullable
expected_parent_source_item_id nullable
```

The endpoint:

- resolves source IDs only within the named collection;
- accepts bounded batches;
- validates the full batch before applying it;
- applies idempotent relationships atomically per batch;
- supports attaching relationships to items that already exist with identical
  content;
- returns created, unchanged, conflicted, and failed counts plus row-level
  bounded errors; and
- records source relationship fields in item metadata only as provenance.

The importer can continue supplying the relationship during ordinary
parent-first upload. It uses reconciliation for existing items, out-of-order
source formats, or a retry after item upload succeeded but relationship
application did not.

Native upload must not silently ignore contradictory supplied relationship
metadata when an item already exists. It should either verify an exact match
or instruct the client to use the reconciliation command.

## 5. Artifact Service read contract

Add a bounded bulk relationship snapshot endpoint rather than adding inverse
child queries to every item in collection list responses:

```text
POST /v1/collection-items/relationship-snapshots
```

Request:

```text
tenant_id
client_id
item_ids[1..5000]
```

Each response row contains:

```text
item_id
family_id nullable
parent_item_id nullable
parent_source_item_id nullable
parent_identifier nullable
child_item_ids[]
child_source_item_ids[]
child_identifiers[]
```

Child arrays use one deterministic ordering: canonical identifier, then
collection-item UUID as a tie-breaker. The three child arrays must remain
positionally aligned.

The embedded gateway and remote Artifact API must return the same shape and
ordering. Scope checks must match the existing grouping-snapshot API.

Use one set-oriented query for requested items, their parents, and their
children. Do not issue one child lookup per requested item.

## 6. Core metadata and search projection

### 6.1 Base profile

Increment `DEFAULT_MATTER_METADATA_PROFILE.version` and add `parent_id` and
`child_id` to its definitions and Family group.

Add an idempotent `ensure_family_relationship_metadata(...)` service for
matters created under older profile versions. It must:

- create missing compatible definitions;
- reject an existing incompatible use of either stable key;
- activate compatible archived definitions;
- create or reuse the Family group;
- add missing group memberships in deterministic order; and
- optionally queue the required search projection work.

Full search rebuild preparation must call this ensure service alongside the
existing file-date, email-domain, and fact-state upgrades.

### 6.2 Search snapshot

Extend the Artifact gateway with `CollectionItemRelationshipSnapshot` and
embedded/remote bulk readers. The search projection resolves:

```text
values["parent_id"] = relationship.parent_identifier
values["child_id"] = relationship.child_identifiers
```

Project these through the ordinary metadata object, not as another special
top-level OpenSearch mapping. The base metadata definitions then provide the
existing table, document-details, filter, and export behavior.

`family_id` may remain as its existing top-level compatibility field and
system metadata value.

### 6.3 Relationship change propagation

A relationship change affects at least two search documents:

- the child whose `parent_id` changed; and
- the former and/or new parent whose `child_id` list changed.

Collection relationship reconciliation must produce the affected collection
item IDs. Core must translate matter-linked affected items into coalesced
`DOCUMENT_UPSERT` operations for every matter containing them. If immediate
cross-service projection notification is not available in the first slice,
the endpoint must explicitly report that a full matter search rebuild is
required; it must not imply that active indexes are current.

The preferred completed design queues targeted upserts through an authorized
Core command rather than giving Artifact direct access to Core tables.

## 7. Existing-data upgrade and backfill

Classify existing collection items into these cases:

1. **Canonical edge present:** project Parent ID and derive Child ID without
   modifying Artifact data.
2. **Source relationship fields present but canonical edge absent:** run the
   source-specific adapter or reconciliation backfill and validate the edge.
3. **Only family ID present:** leave Parent ID and Child ID empty unless an
   authoritative source relationship can be recovered.
4. **Contradictory edge/family data:** report the family as conflicted and make
   no automatic changes.

Do not infer a parent from record type, creation order, filename, or the first
member of a family. Those heuristics can corrupt productions and downstream
coding propagation.

Provide a read-only audit/report command before any backfill. It reports:

- total items and families;
- items with canonical parents;
- roots with derived children;
- family-ID mismatches;
- missing referenced source IDs;
- multiple-parent conflicts;
- cycles;
- source fields that could be reconciled; and
- family-only records that remain unresolved.

The mutating backfill consumes that report or the same deterministic resolver,
supports dry-run, is restartable, and uses bounded transactions.

## 8. User experience and export

Once the metadata definitions and index projection exist, the current metadata
table and document-detail mechanisms should render them without a custom UI.
Verify the following explicitly:

- Family group order is Family ID, Parent ID, Child ID.
- Empty relationship values render as empty, not `[]`, `null`, or a sentinel.
- Multiple Child IDs use the standard multi-value display.
- Parent ID and Child ID can be selected as visible table columns.
- Exact-value metadata search works for each identifier.
- Search-result export offers both fields in the Family group.
- Child ID exports use the existing compact JSON-array representation for
  multiple values.

Update user documentation to explain the distinction between a family group
and a direct parent/child edge.

## 9. Implementation sequence

### Phase 1: Shared semantics and Artifact reads

1. Extract and test the canonical document-identifier resolver.
2. Add Artifact relationship snapshot request/response schemas.
3. Implement set-oriented embedded and remote reads.
4. Add scope, ordering, missing-item, and large-batch tests.

Exit criterion: given parent and child collection rows, one bulk call returns
stable, aligned direct relationship identifiers in embedded and remote modes.

### Phase 2: Ingest normalization and reconciliation

1. Add normalized adapter helpers for ParentID, ChildID lists, and supported
   family-range formats.
2. Preserve source fields as provenance.
3. Add relationship integrity validation.
4. Implement the batch reconciliation command.
5. Update importer retry and existing-item behavior.
6. Add dry-run relationship audit tooling.

Exit criterion: parent-first, child-first, retry, and existing-item imports all
produce the same canonical edges, while contradictory source data fails
without partial relationship writes.

### Phase 3: Base metadata and search

1. Add the two system metadata definitions and bump the profile version.
2. Implement the legacy-matter ensure service.
3. Extend Artifact gateway relationship snapshots.
4. Populate Parent ID and Child ID in document projections.
5. Wire targeted reindexing for relationship changes or enforce the documented
   full-rebuild fallback.
6. Regenerate OpenAPI and the TypeScript client.

Exit criterion: new and upgraded matters can search and display both fields,
and changing an edge updates the child and both affected parent projections.

### Phase 4: Backfill, export, documentation, and rollout

1. Run the audit against representative collections.
2. Reconcile only authoritative recoverable relationships.
3. Upgrade existing matter metadata.
4. Rebuild affected matter indexes.
5. Verify table, document-details, filter, and export behavior.
6. Update user and operator documentation.

Exit criterion: reconciled counts match the audit, unresolved families are
reported, and active search indexes contain the new fields for all indexed
documents.

## 10. Test plan

### Artifact model and API

- Same-collection parent is accepted.
- Cross-tenant, cross-client, and cross-collection parents are rejected.
- Self-parent and cycle attempts are rejected.
- Missing family ID is inherited or assigned according to policy.
- Conflicting family IDs are rejected.
- One parent returns all direct children in deterministic order.
- Nested families return only direct children at each level.
- Snapshot requests preserve requested item order and omit or report missing
  items according to the established bulk-snapshot convention.
- Embedded and remote results are equivalent.

### Importer

- Extracted email attachments retain the existing behavior.
- ParentID-on-child load files normalize correctly.
- ChildID-on-parent load files normalize correctly.
- Both directions agree or fail deterministically.
- Parent and contiguous children remain on one upload worker.
- An existing same-content item can gain a missing authoritative edge through
  reconciliation.
- A retry does not duplicate or replace an edge.
- Partial failures are restartable.

### Core metadata and search

- New matters receive both definitions with the expected shapes.
- Existing compatible matters upgrade idempotently.
- Incompatible key reuse returns a clear conflict.
- Parent, child, standalone, and nested-family projections are correct.
- A parent outside the matter can still be identified from collection data.
- Search filters match exact Parent ID and Child ID values.
- Search rebuild schema planning includes the new metadata fields.
- Relationship changes enqueue every affected matter document.

### UI and export

- Family group fields appear in the intended order.
- Child IDs render as multiple values.
- Both fields are selectable table columns.
- Both fields appear in the export catalog.
- CSV serialization of multiple Child IDs is lossless.

### Backfill

- Dry-run performs no writes.
- Canonical existing edges are unchanged.
- Recoverable source relationships reconcile deterministically.
- Family-only and contradictory records remain unchanged and are reported.
- Repeated execution is idempotent.

## 11. Observability and audit

Record or log bounded counts for:

- edges created, unchanged, moved, conflicted, and failed;
- family IDs inherited, assigned, and rejected;
- items and families inspected by backfill;
- relationship snapshots requested and returned;
- affected Core matter documents queued for projection; and
- matters requiring a full search rebuild.

Audit human-initiated reconciliation and backfill commands with collection,
actor, counts, dry-run state, and a bounded error summary. Do not place source
document content or full load-file rows in audit events.

## 12. Rollout and database updates

No Artifact schema migration is expected: `family_id`, the self-referencing
parent foreign key, and its index already exist. Do not add a persisted
`child_id` column.

No Core schema migration is expected: the new base fields are ordinary
`metadata_definition` and `metadata_group_field` rows. Deployment does require:

1. application code and API contract deployment;
2. default metadata profile version increment;
3. an idempotent metadata upgrade for existing matters;
4. an optional authoritative collection relationship backfill; and
5. a full search-index rebuild for affected matters unless targeted projection
   has already brought every document current.

If implementation reveals a need for any new persisted column, stop and revise
this plan before adding it. Artifact and Core databases have separate Alembic
histories and must not be changed implicitly.

## 13. Acceptance criteria

The feature is complete when all of the following are true:

1. Every newly ingested attachment or explicit child stores one validated
   canonical parent edge at collection ingest.
2. A child displays its parent's canonical document identifier as Parent ID.
3. A parent displays every direct child's canonical document identifier as
   Child ID.
4. Parent ID and Child ID are read-only system metadata in the base profile.
5. Both fields work in document tables, document details, exact search, and
   search-result CSV export.
6. Existing canonical edges become visible after metadata upgrade and search
   rebuild without rewriting collection data.
7. Family-only records are not assigned speculative edges.
8. Relationship retries and backfills are idempotent and report conflicts.
9. Embedded and remote Artifact modes return equivalent values.
10. Automated tests cover ingestion, integrity, upgrades, projection,
    rendering, export, and backfill behavior.

## 14. Expected implementation touch points

- `artifact_service/models.py`
- `artifact_service/schemas.py`
- `artifact_service/api.py`
- `app/artifact_gateway.py`
- `app/metadata_profiles.py`
- a new family-metadata upgrade module under `app/`
- `app/routers/search.py`
- `app/search/service.py`
- `app/search_exports.py` only if export catalog behavior needs an explicit
  compatibility field
- `scripts/import_client/models.py`
- `scripts/import_client/importer.py`
- applicable dataset adapters under `scripts/import_client/adapters/`
- Artifact API, importer, search, metadata-profile, export, and frontend tests
- `web/openapi.json` and generated TypeScript models
- `web/content/docs/metadata-definitions.mdx`

## 15. Out of scope

- Inferring relationships from model predictions or filename similarity.
- Treating all family members as direct children of the first item.
- Persisting a redundant child-ID array.
- Automatically adding every related collection item to a matter.
- Coding propagation or suppression across families.
- Changing classification family grouping, which continues to use
  `family_id`.
- Replacing internal collection-item UUIDs with source identifiers.
