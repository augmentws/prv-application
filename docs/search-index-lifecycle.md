# Search index schema planning and lifecycle

## Authority and concurrency

Core and Artifact data remain authoritative. OpenSearch is a matter-scoped projection behind a stable alias. Every schema or document projection takes the PostgreSQL advisory transaction lock for that matter, so alias changes, mapping updates, and document writes cannot race.

## Schema plans

The schema planner compares the active generation's complete `schema_snapshot` with the mapping and settings compiled from the current matter configuration. It returns one of three actions:

- `NO_CHANGE`: reuse the active physical index.
- `IN_PLACE`: send a compatible mapping addition to the active physical index and update its stored schema snapshot and hash.
- `REINDEX_REQUIRED`: stop the projection operation in `AWAITING_USER` and store the desired hash and human-readable reasons in `payload.schema_change`.

In-place changes are allowlisted rather than inferred optimistically. The first allowlisted field is the root keyword array `batch_ids`, because batch membership has a separate bounded partial-update path. Adding or changing fields below `metadata` is deliberately not classified as in-place: older OpenSearch documents may not contain authoritative current values and therefore need reprojection.

## Changes requiring confirmation

A full reindex is required when a change affects existing indexed content or cannot be proven compatible. This includes:

- changing a field type, analyzer, tokenizer, normalizer, or exact/facet representation;
- changing embedding dimensions, vector engine/method settings, or nested chunk mappings;
- changing an object or nested-field shape;
- removing or renaming a mapped field;
- adding searchable or facetable metadata that requires historical values to be loaded;
- changing index creation-time settings; and
- any schema difference the planner does not explicitly recognize.

An initial index is created automatically because there is no prior index to reprocess. A manually requested full rebuild is already an explicit confirmation. For an automatic schema sync, the UI displays the stored reasons and calls `POST /v1/matters/{matter_id}/search-operations/{operation_id}/confirm-reindex`. Confirmation records the actor and time in the operation payload, converts the operation to `REBUILD`, assigns a fresh workflow ID, and queues it at rebuild priority.

## Replacement and cleanup

A rebuild creates a new numbered physical index while the current alias remains searchable. Only after all matter documents have been projected and the new index refreshed does the worker atomically move the alias and commit the new active generation.

After that commit, cleanup resolves every physical index matching the matter's versioned index pattern. It deletes every index except the active one, then deletes every non-active `search_index_generation` row. If cleanup fails, the active index remains valid and the surviving tracking rows allow a later cleanup attempt. `search_projection_operation` records are retained as the audit and diagnostic history.

Search-query review batches copy the generation number, physical name, schema hash, and activation time into their immutable selection definition. Their provenance therefore remains available after the generation tracking row is removed.
