# Search index schema planning and lifecycle

## Authority and concurrency

Core and Artifact data remain authoritative. OpenSearch is a matter-scoped projection behind a stable alias. Every schema or document projection takes the PostgreSQL advisory transaction lock for that matter, so alias changes, mapping updates, and document writes cannot race.

## Schema plans

The schema planner compares the active generation's complete `schema_snapshot` with the mapping and settings compiled from the current matter configuration. It returns one of three actions:

- `NO_CHANGE`: reuse the active physical index.
- `IN_PLACE`: send a compatible mapping addition to the active physical index and update its stored schema snapshot and hash.
- `REINDEX_REQUIRED`: stop the projection operation in `AWAITING_USER` and store the desired hash and human-readable reasons in `payload.schema_change`.

In-place changes are allowlisted rather than inferred optimistically. Additive root fields such as `batch_ids` use their bounded partial-update paths. A newly searchable field below `metadata` is also added to the active mapping in place; the worker refreshes only documents that already have a current asserted value for that field. A new field with no values therefore requires only a mapping update.

## Changes requiring confirmation

A full reindex is required when a change affects existing indexed content or cannot be proven compatible. This includes:

- changing a field type, analyzer, tokenizer, normalizer, or exact/facet representation;
- changing embedding dimensions, vector engine/method settings, or nested chunk mappings;
- changing an object or nested-field shape;
- removing or renaming a mapped field;
- changing an existing metadata field's type, analyzer, or exact/facet representation;
- changing index creation-time settings; and
- any schema difference the planner does not explicitly recognize.

An initial index is created automatically because there is no prior index to reprocess. A manually requested full rebuild is already an explicit confirmation. For an automatic schema sync, the UI displays the stored reasons and calls `POST /v1/matters/{matter_id}/search-operations/{operation_id}/confirm-reindex`. Confirmation records the actor and time in the operation payload, converts the operation to `REBUILD`, assigns a fresh workflow ID, and queues it at rebuild priority.

## Replacement and cleanup

A rebuild creates a new numbered physical index while the current alias remains searchable. Only after all matter documents have been projected and the new index refreshed does the worker atomically move the alias and commit the new active generation.

After that commit, cleanup resolves every physical index matching the matter's versioned index pattern. It deletes every index except the active one, then deletes every non-active `search_index_generation` row. If cleanup fails, the active index remains valid and the surviving tracking rows allow a later cleanup attempt. `search_projection_operation` records are retained as the audit and diagnostic history.

The maintenance utility `scripts/cleanup_search_indexes.py` applies the same policy to generations left behind by interrupted cleanup. It is a dry run unless `--execute` is supplied. Before deleting anything it requires exactly one database-active generation, requires the matter alias to resolve only to that physical index, validates every index name against the versioned naming contract, and takes the same matter advisory lock as projection workers. Physical indexes are deleted before obsolete generation rows, and review-batch generation provenance is copied into the immutable selection definition before a referenced row is pruned.

```text
pipenv run python scripts/cleanup_search_indexes.py
pipenv run python scripts/cleanup_search_indexes.py --execute
pipenv run python scripts/cleanup_search_indexes.py --matter-id <matter-uuid>
```

Search-query review batches copy the generation number, physical name, schema hash, and activation time into their immutable selection definition. Their provenance therefore remains available after the generation tracking row is removed.

## Pressure recovery

Search projection and embedding-index steps distinguish transient OpenSearch pressure from permanent request failures. HTTP 429/502/503/504 responses, connection failures, flood-stage disk blocks, circuit breakers, rejected execution, unavailable shards, and temporary cluster-manager failures retry with durable exponential backoff. Retries begin after 30 seconds, double to a maximum one-hour interval, and make 16 attempts. Mapping and validation failures are not retried automatically.

If pressure lasts beyond the automatic retry window, the operation retains the original OpenSearch error instead of only the DBOS retry wrapper. A failed embedding job whose embedding batches all completed can be resumed with `POST /v1/matters/{matter_id}/embedding-jobs/{job_id}/retry-index`, or **Retry indexing** on the matter Jobs tab. This queues only the search-index phase and reuses the completed chunks and vectors.

Embedding projections checkpoint their document offset after each successful bulk request. If OpenSearch partially rejects a bulk request, the operation also records the rejected document IDs. Automatic and user-initiated retries process those rejected IDs first, then continue after the checkpoint; they do not replay documents that were already accepted. Operations that failed before checkpointing was introduced have no reliable cursor and must make one full idempotent projection pass when retried.
