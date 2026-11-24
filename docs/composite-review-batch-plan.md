# Composite Review Batch Implementation Plan

## Goal

Allow an operator to create one frozen Review Batch containing:

1. every document matched by a fixed query; and
2. a reproducible random sample of a requested size from a second query.

The result remains an ordinary immutable Review Batch. It can be assigned, reviewed, analyzed, compared, and used by
Review Programs without introducing a second membership system.

Example:

- include every document where `Trec Priv Coding = true`; and
- add 5,000 randomly selected documents where `Trec Priv Coding` has no value.

## Product behavior

Add **Fixed query + random sample** to the **Create review batch → Documents** selector.

The form contains:

- **Fixed population**: a required Keyword saved search. Every matching document is included.
- **Random population**: a required Keyword saved search.
- **Random sample size**: a required positive integer.
- **Include complete families**: the existing optional family-expansion policy.
- **Random seed**: generated automatically and available under advanced details for reproducibility.
- The existing assignment, reviewer visibility, coding-group, description, and note controls.

The operator must preview the population before creating it. The preview displays:

- fixed-query matches;
- random-query matches;
- documents matching both queries;
- random candidates after fixed-query documents are excluded;
- requested and selected random documents;
- family documents added, when family expansion is selected;
- final deduplicated batch size; and
- a warning when fewer random candidates exist than requested.

The batch is created as a new frozen population. This feature does not mutate an existing ready batch.

## Deterministic selection rules

1. Resolve both saved searches against the same pinned active physical search-index generation.
2. Enumerate fixed-query document IDs in deterministic `document_id` order.
3. Enumerate random-query document IDs from the same generation.
4. Remove every fixed-query document from the random candidate set. The fixed population always wins an overlap.
5. Sort the remaining random candidates by `SHA-256("{random_seed}:{matter_document_id}")` and take the first `N`.
6. Form the ordered union as fixed documents followed by sampled documents.
7. Deduplicate defensively.
8. If complete-family expansion is enabled, expand the union after sampling. Family additions may make the final batch
   larger than `fixed count + N` and do not count against `N`.
9. If fewer than `N` random candidates are available, include all available candidates and record a warning rather than
   failing the batch.
10. Persist final membership in `review_batch_document`; PostgreSQL remains authoritative.

Materialization retries must resolve the same saved-search snapshots, physical index generation, and random seed. They
must therefore produce identical ordered membership.

## API contract

### Selection type

Add `COMPOSITE_SAVED_SEARCH` to `ReviewBatchSelectionType` and `InteractiveReviewBatchSelectionType`.

Extend `ReviewBatchCreate` with:

```text
fixed_saved_search_id: UUID | null
sample_saved_search_id: UUID | null
sample_size: int | null
random_seed: string | null
expected_search_index_generation_id: UUID | null
preview_hash: string | null
```

For `COMPOSITE_SAVED_SEARCH`:

- both saved-search IDs are required;
- `sample_size` is required;
- both searches must use `KEYWORD` mode;
- both searches must be visible to the requesting principal;
- `source_batch_id`, `assessment_run_id`, the legacy single `saved_search_id`, and inline `search` are invalid; and
- creation requires a current preview hash and the generation ID used for that preview.

The persisted `selection_definition` snapshots both saved-search names, update timestamps, complete search requests,
the pinned generation identity, exclusion/deduplication policy, materialization counts, warning details, and schema
version. The batch must not depend on either saved-search row after creation.

`ReviewBatch.sample_size` and `ReviewBatch.random_seed` retain their existing meanings for the sampled component.

### Preview endpoint

Add:

```text
POST /v1/matters/{matter_id}/review-batches/composite-preview
```

The request contains the two saved-search IDs, sample size, optional seed, and family-expansion choice. The response
contains:

```text
fixed_match_count
sample_match_count
overlap_count
eligible_sample_count
requested_sample_count
selected_sample_count
family_added_count
final_document_count
random_seed
search_index_generation_id
preview_hash
warnings[]
```

The preview hash is a canonical hash of the normalized request, saved-search snapshots, generation identity, seed, and
family policy. Batch creation recomputes and verifies it. If the saved search or index generation changed, creation
returns `409` and requires another preview instead of silently creating a different population.

Preview is read-only and creates no membership rows or durable preview table.

## Backend implementation

### 1. Schema and migration

- Add the new selection literal and request/response schemas.
- Add an Alembic migration updating `ck_review_batch_selection_type` to permit `COMPOSITE_SAVED_SEARCH`.
- Do not add an application table. Existing `review_batch`, `review_batch_document`, coding snapshots, workflow
  execution, and search projection remain sufficient.
- Regenerate OpenAPI and TypeScript clients.

### 2. Shared population resolver

Refactor saved-search enumeration so preview and materialization call one resolver that accepts:

- the two normalized search snapshots;
- the pinned physical index generation;
- the sample size and seed; and
- the family policy.

Return ordered document IDs plus the count/provenance summary. Keep selection/deduplication logic outside the router so
it can be unit tested independently. Resolve database metadata before the OpenSearch paging loop and avoid holding an
idle database transaction while waiting on OpenSearch.

### 3. Creation and durable materialization

- Authorize and snapshot both saved searches during batch creation.
- Verify the preview hash and generation.
- Persist the complete composite selection definition before enqueueing materialization.
- Extend `materialize_review_batch` to resolve `COMPOSITE_SAVED_SEARCH` through the shared resolver.
- Continue using the existing bounded membership inserts, job progress, cancellation, retry, and search-projection
  stages.
- Store component counts and warnings in `selection_definition` and execution progress so the UI and operators can
  explain the final size.

### 4. Read presentation

- Display the selection label as **Fixed query + random sample**.
- Show both saved-search names and the component counts in Batch details.
- Continue showing the final frozen `document_count` everywhere downstream.
- Exports, analysis runs, coding comparisons, and reviewer assignment require no special handling because they consume
  frozen membership.

## Frontend implementation

1. Add the new document-source choice to `CreateReviewBatchDialog`.
2. Provide separate selectors for **Fixed population** and **Random population** saved searches.
3. Require a sample size and show the generated seed under advanced details.
4. Add **Preview population**. Disable **Create batch** until the preview succeeds.
5. Invalidate the preview whenever a source, sample size, seed, or family option changes.
6. Present the count breakdown and any insufficient-candidate warning before confirmation.
7. Submit the generation ID and preview hash with creation.
8. After creation, show the component breakdown on the batch card/detail without implying that membership remains
   editable.

The MVP uses saved searches rather than two plain text boxes. Saved searches preserve structured metadata filters,
which are required for examples such as `Privilege = true` and `Privilege has no value`; a keyword-only text box cannot
express those populations reliably.

## Tests

### Backend

- schema rejects either missing search, a missing sample size, unsupported search modes, or conflicting source fields;
- authorization is enforced independently for both saved searches;
- fixed and random populations with no overlap produce the expected union;
- overlapping documents are included once and excluded before sampling;
- sampling is stable for the same seed and changes for a different seed;
- insufficient random candidates produce a warning and include all candidates;
- family expansion occurs after sampling and reports additions accurately;
- preview and creation reject a changed saved-search snapshot or search generation;
- materialization retry produces identical membership and sequence numbers;
- paged OpenSearch enumeration works for both components;
- cancellation and projection failure retain existing durable behavior; and
- tenant and matter boundaries cannot be crossed.

### Frontend

- selecting the composite source exposes both saved-search selectors and sample size;
- preview displays every component count and warning;
- changing an input invalidates the preview;
- creation is disabled before preview and submits the exact preview seed/hash/generation afterward;
- family expansion is reflected in the final preview total; and
- the batch list labels the resulting selection correctly.

### Regression

- existing `SAVED_SEARCH`, `RANDOM_SAVED_SEARCH`, `SEARCH_QUERY`, random, assessment, classifier, comparison, explicit,
  and batch-set materialization tests continue to pass;
- OpenAPI generation and TypeScript type checking pass; and
- existing Review Program and batch statistics behavior is unchanged.

## Delivery sequence

1. Add schemas, migration, shared resolver, and resolver tests.
2. Add preview and creation validation with API tests.
3. Extend durable materialization and retry tests.
4. Add the creation UI, preview summary, and UI tests.
5. Regenerate clients, update `docs/review-batches.md`, and run the focused backend/frontend regression suites.

## Acceptance criteria

- An operator can include all documents from one saved search and request `N` random documents from another.
- The UI explains the exact population before creation.
- No document appears twice, including documents matched by both searches.
- Repeating resolution with the same snapshots and seed produces identical ordered membership.
- The created batch is an ordinary immutable Review Batch usable by every existing downstream workflow.
- No new application table or per-preview persisted data is introduced.
