# Priv-View Application Repository Instructions

These decisions are authoritative for this repository until the user explicitly changes them. Preserve them when designing, implementing, or reviewing work.

## Product and Repository Scope

Priv-View is an AI-enabled eDiscovery intelligence product designed to bolt onto existing document-review platforms. The Agent Platform must eventually support multiple agent types that perform different tasks over a corpus. The epoch subsystem is not part of phase one, but designs should avoid preventing its later addition.

This repository contains the Core API and will also contain the single React application used by both administrators and end users. Keep the backend and frontend in this repository:

- `app/`: FastAPI application and domain logic
- `alembic/`: database migrations
- `tests/`: backend tests
- `web/`: planned Next.js application

The platform service must not own artifact/blob storage. File and derived-content storage belongs in a logically independent Artifact Service. The Artifact Service may run embedded in the Application API process or as a separate container, but it always owns its database, migrations, object storage, and interfaces. It manages client collections, source containers, collection items, native files, OCR text, document chunk sets, vector sets, and similar binary or derived artifacts. Documents reference collection items and artifacts; they are not interchangeable concepts.

## Backend Architecture

Use:

- Python 3.11
- FastAPI
- SQLAlchemy
- Alembic
- Pipenv
- PostgreSQL
- Docker Compose

Never start an application background worker on the user's behalf. It may pick
up or interfere with live durable jobs. The user owns worker startup and
restart; diagnostics and tests must use direct, bounded commands instead.

After every implementation, explicitly state whether a database migration or
other database update is required. When one is required, name the migration and
provide the exact Pipenv command to apply it. Do not leave database deployment
steps implicit in a general implementation summary.

The local Core PostgreSQL database is named `pvr-core`. The Artifact Service uses the independent `pvr-artifact` database and its own Alembic configuration, even in embedded mode.

Embedding inference is a logically independent, stateless capability. `EMBEDDING_MODE=embedded` loads the configured model lazily in the calling Core/worker process; `EMBEDDING_MODE=remote` calls the separately deployable FastAPI Embedding Service. Both modes use the same gateway and response contract. The model, revision, dimensions, device, normalization, batching, and service location are environment configuration. Standalone requests require a private service token and must not be exposed directly to browsers.

Statistical-classification datasets are immutable Core-controlled snapshots backed by Artifact-owned Parquet artifacts. Any active asserted coding field with Boolean, enum, text, or long-text type may be a target; system and imported metadata are not coding targets, and `reviewable` controls review presentation rather than classifier eligibility. A human reviewer label source freezes both its canonical reviewer-coding ID and revision in the dataset label-policy snapshot, and label resolution reads that as-of revision so later recoding cannot alter a frozen dataset. Class keys are discovered from each resolved training-label population and frozen, in order, on that dataset snapshot and every model version built from it. Later snapshots may add or remove classes without rewriting or invalidating historical snapshots and versions. Feature materialization pins one completed matter-embedding job, pools its `CHUNK_VECTOR_SET` rows into deterministic document vectors for the labeled training population, and records missing or invalid labeled features rather than silently dropping them. Population, label, and split rows align exactly; feature rows align exactly with the labeled subset. Cross-validation splits keep transitive family and exact-duplicate groups together, pin their seed and algorithm version, and store raw plus per-training-fold normalized source weights. For one-vs-rest only, feature/split preparation may exclude an observed class that lacks sufficient feature-ready positive or negative documents, connected groups, or per-fold support; the snapshot and model version retain the complete observed class order plus the trained class subset and structured exclusions, while packages and scoring contain only trained classes. Binary and native multiclass remain fail-fast. Near-duplicate grouping is added when an authoritative source exists; its absence must remain explicit.

Delimited hierarchical Text classification targets may freeze an optional 1-based hierarchy level. Label resolution projects full stored values to the prefix at that level before class discovery, excludes values that do not reach the selected depth, and never mutates the source coding. The target snapshot and configuration hash must include the field delimiter and selected level, and pre-training distribution counts must use the same projection.

Statistical-classification training and scoring are a logically independent, stateless runtime. `CLASSIFICATION_MODE=embedded` invokes it in the Core worker process; `CLASSIFICATION_MODE=remote` uses the private Classification API on port `8003` through the same gateway contracts. The runtime reads frozen Artifact-owned inputs and writes immutable model-package, prediction, evaluation, and result-manifest artifacts using tenant/client-scoped delegation. The MVP trainer is weighted L2 logistic regression for binary, native multiclass, and atomic one-vs-rest strategies. It uses resolved source weights only and must never introduce automatic class balancing. Model packages are fixed-layout ZIP archives containing JSON and non-object NPZ data; pickle, joblib, executable/import-path loading, unexpected files, and unsafe paths are prohibited.

Statistical-classification model builds are durable DBOS workflows. Core freezes the configuration, dataset snapshot, trainer, validation policy, seed, runtime identity, and package schema into a derivation key; a logical derivation may produce only one training run and one candidate version. A retriable failure reuses that training run and derivation while incrementing its attempt number and assigning a fresh DBOS workflow ID. Worker execution must reauthorize the user who initiated the current attempt, validate authoritative Artifact-owned output scope, identity, references, and hashes on both normal completion and reconciliation, and keep `classification_model.active_model_version_id` null or unchanged during build finalization. Ambiguous failures after artifact publication are resolved by derivation-key reconciliation, never by creating a second logical package. Approval, activation, rejection, retirement, and rollback remain separate human-governed operations.

Starting model training creates a durable Core `classification_training_job` before dataset inspection begins. Its DBOS workflow owns label and population snapshotting, feature and split materialization, and the handoff to the existing immutable model-build workflow. The browser must receive the queued job immediately and observe its persisted stages; it must not orchestrate those preparation commands as a synchronous request chain. A job pins its model configuration, scope, embedding job, validation policy, seed, initiating administrator, and eventual snapshot and training-run identities.

Statistical-classification lifecycle decisions are append-only Core records. Approval or rejection changes a candidate's governance status but never activates it. Activation and rollback use a signed preview plus the classifier row version, execute through `classification_model_promote_v1`, reauthorize the requesting user, and atomically move only `classification_model.active_model_version_id`; an active version remains `APPROVED` rather than acquiring a duplicate active status. Every decision records its reason, policy and metric evidence, authorization context, and before/after active pointer. Rollback targets must have been active previously, and an active version cannot be retired.

Statistical-classification batch scoring is a durable DBOS workflow. A scoring run pins one approved model version at creation and never resolves the classifier's mutable active pointer during execution. Its derivation freezes the model package, dataset feature artifact and feature contract, configuration, and prediction-resolution policy. Core stores run state, reconciled counts, and Artifact references; Artifact stores one complete Parquet result row per population document, including successful predictions, abstentions, feature exclusions, and row-level failures. Worker execution reauthorizes the initiating administrator and verifies package, feature, output scope, producer, references, hashes, and manifest identity. Approved non-active versions may be selected explicitly; retirement is blocked only while a version has nonterminal scoring runs, and historical completed runs retain their pinned provenance.

Statistical classification is also a first-class `CLASSIFICATION` Matter Analysis Task type. A published classification task version pins one approved model version and its target coding field. Each durable task run selects a ready review batch or the whole matter, freezes an immutable `SCORING` population artifact, materializes features for every scoped document without labels or validation folds, and invokes the existing classification scoring workflow. Whole-matter membership remains Artifact-backed and must not create one Core membership row per document merely to run inference. Whole-matter feature preparation must stream bounded Parquet batches rather than retaining all document vectors in memory, persist ready/missing/failed progress at each batch boundary, and honor cancellation cooperatively at that boundary. Classification Analysis Task results remain isolated and never update matter coding automatically.

Document-oriented Analysis Tasks use the generic Analysis Input Manifest platform. Core stores one `analysis_input_manifest` control row per logical owner, while Artifact stores the full immutable `ANALYSIS_INPUT_MANIFEST` Parquet population. Every selected document remains represented with an explicit input status; the contract freezes its schema, resolver version, required logical paths, source-artifact policy, and typed extension columns. Task runtimes must validate and consume the frozen manifest rather than rediscovering population or mutable metadata. Classification dataset snapshots are the first adopter and retain their population-artifact fields only as compatibility aliases. Input manifests, domain result manifests, and generic job-outcome manifests remain separate contracts. Q&A adoption is deferred until its current database-backed execution path is migrated explicitly.

Searchable classification coding reuses the isolated review-batch run and batch-coding projection path. `CLASSIFIER` is a distinct `review_batch_run` type bound to one completed classification scoring run and a ready frozen batch with the exact same population. Each immutable classifier projection run reads the existing prediction artifact without rerunning inference, materializes only successful non-abstained resolved classes inside its inclusive-lower/exclusive-upper confidence window as `review_batch_run_value` rows, and then uses `review_batch_search_coding_run` plus `BATCH_CODING_SYNC`. Reprojecting one scoring/task run creates a new immutable run, but only its newest completed projection is offered as that task run's ordinary comparison source; superseded projections remain historical provenance rather than duplicate selectable tasks. Classifier runs never update matter coding and are ineligible for direct promotion. Batch-coding confidence ranges apply equally to JEV and classifier values; values without confidence do not match an active range.

The matter Classification tab is an admin-only backend-validation surface, not a reviewer or CAL workspace. It may orchestrate existing versioned Core commands but must not duplicate domain rules in the browser or publish predictions as coding. Prediction inspection uses the bounded Core preview endpoint; Core streams the Artifact-owned Parquet file, verifies its stored hash and embedded run/version identity, validates the required schema, and reads only the row groups needed for the requested page before returning rows.

Matter embedding generation is a durable DBOS workflow. Core owns the job, immutable configuration snapshot, progress, and frozen batches of matter-document IDs. Artifact owns idempotent `CHUNK_SET` and `CHUNK_VECTOR_SET` Parquet artifacts, their deterministic derivation keys, processing metadata, and lineage. The sentence-aware chunk set is derived from the preferred extracted text, OCR text, or supported native text; the vector set is derived from the chunk set. OpenSearch stores all chunks as nested children of the owning matter document and is only a rebuildable projection. A completed job means both artifacts and the corresponding search projection were applied for every successful batch. Never store one Artifact row per chunk or treat OpenSearch as the authoritative vector store.

Matter topic processing has an explicit approval boundary. Discovery stores editable topic proposals, centroids, keywords, representative excerpts, and a frozen document scope, then stops at `AWAITING_REVIEW` without creating metadata definitions or document values. A matter administrator may rename, redescribe, or exclude proposals. Explicit application records the reviewer and approval time, materializes only the approved topics as stable enum options, and starts the durable document-assignment workflow. Discovery must never silently publish topic values.

FastAPI remains the authority for domain behavior, authentication, authorization, sessions, and auditing. Do not move domain logic into the UI or its proxy layer.

The FastAPI OpenAPI document at `/openapi.json` is the API contract source. Interactive API documentation is available at `/docs`. When the frontend is added, generate its TypeScript types and API client from OpenAPI with Orval; do not manually edit generated output.

## Core Phase-One Domain

### Tenancy

- Tenants are hierarchical.
- There is exactly one root tenant.
- The root tenant has a bootstrapped super administrator.
- Root-tenant administrators can create subtenants and users for those subtenants.
- A user belongs to one tenant in phase one.
- Use the term `tenant`, never `customer`, for this boundary.

### Clients, Matters, Users, and Custodians

- A tenant has clients.
- A tenant has users.
- A client has matters and user members.
- A matter has user members.
- A custodian is not a user or membership concept. A custodian is client-scoped evidence metadata identifying the person or source from whom material was collected.
- Client collections, collection items, custodians, native artifact upload, and adding collection documents to matters are now in scope.

### Artifact Service First Slice

- Each root tenant and subtenant receives a separate bucket named from its creation-time slug plus a 10-character random suffix.
- Object keys are opaque `v1/blobs/{content_blob_id}` values; business hierarchy remains in the Artifact database.
- Preserve original MBOX, CSV, ZIP, and later DAT inputs as collection-scoped `SOURCE_CONTAINER` artifacts.
- Dataset-specific local importers perform preprocessing and upload individual emails, attachments, or files through APIs.
- Local import clients live under `scripts/import_client`, resolve their API operations from `web/openapi.json`, and share a dataset-neutral `BaseImporter`.
- `BaseImporter` owns source-container/item iteration, generic MBOX and ZIP traversal, basic email-header extraction, attachment extraction, and parent/family file relationships.
- Dataset adapters own source discovery and only the mappings or custom parsing that cannot be generalized, such as custodian mapping, record classification, and Enron's multiline `file,message` CSV rows. `top.csv` is the head-100 sample of the full `enron.csv` format, not a separate adapter format. Adapters must not bypass the API or place dataset parsing in the Artifact Service.
- Each extracted item has a `NATIVE` artifact with explicit lineage to its source container.
- `CHUNK_SET` and `CHUNK_VECTOR_SET` are later processing outputs stored as separate Parquet artifacts, not one artifact per row.
- The Artifact Service must not parse EMC-2, Enron, or vendor-specific formats in its core API.
- Collection document discovery returns paginated items and an exact total first. Self-excluding facet counts are separate, on-demand requests made only when a filter group is opened. The initial implementation uses PostgreSQL while preserving response shapes that can later be backed by Elasticsearch without changing the browser workflow. Collection search covers original filename and source path. Initial facets are custodian, file extension, record type, and processing status; selections are ORed within a facet and ANDed across facets.
- Collection text processing is Artifact-owned and versioned. The immutable system processor runs before ordered collection-specific regular-expression rules. The system processor may remove recognized transport wrappers and unmistakable attachment placeholders, but must preserve the substantive forwarded message body and remain conservative around signatures and ordinary prose. Synchronous tests are limited to 25 selected items and never persist output. Full runs freeze the processor version, profile revision, and rule snapshot, then create idempotent `NORMALIZED_TEXT` artifacts with lineage to the faithful source artifact. Original/native, extracted, and OCR artifacts are never changed. A completed run becomes the collection's active normalized-text version; items without a normalized output fall back to faithful extracted, OCR, or supported native text. Future indexing, chunking, and embedding must prefer only the collection's active normalized run, never incomplete output from a running job.
- Adding documents to a matter is a durable DBOS workflow. Core owns permanent jobs, batches, and matter-to-collection-item links; Artifact owns a short-lived frozen selection. The planner freezes the current query or explicit IDs, enqueues deterministic child batches, deletes the temporary selection after every batch is durably queued, and retains the Core job as audit/status history. Re-adding an existing collection item is idempotent and counted as already present. The same worker code supports embedded Artifact access or a separately deployed Artifact API.
- The import workflow also materializes Core-owned matter-document custodian associations from the Artifact selection. Matter overview document and distinct-custodian counts are authoritative Core database aggregates; they do not depend on the future search index.

### Authorization

Authorization uses code-owned permissions, roles, access groups, and scoped grants:

- Stable dotted permission keys describe one action, such as `document.review` or `classification.train`.
- Immutable system roles and tenant-owned custom roles bundle permissions.
- Direct users and tenant-owned access groups may receive roles at tenant, client, collection, or matter scope.
- Tenant and client grants inherit to supported descendants when the assigned role contains the requested permission. Collection and matter grants do not cross between those sibling boundaries.
- Grants are allow-only; effective permissions are the union of applicable direct and group grants.
- The root-administrator path remains a narrowly defined bootstrap and recovery exception.
- `app_user.tenant_role`, client memberships, matter memberships, and the `can_admin_*` helpers are migration compatibility surfaces. New users receive `USER`, and new access decisions should use explicit permissions.
- Every role, role-permission, group-membership, and grant mutation must update the tenant authorization revision and append an audit record in the same transaction.
- Backend authorization is final even when the UI hides or disables an action.
- Document-level restrictions, nested groups, and explicit deny grants remain deferred.

### Statuses

Use these initial resource statuses:

- `ACTIVE`
- `SUSPENDED`
- `ARCHIVED`

### Metadata

The application supports creating, reading, and configuring matter-level metadata field definitions, organizing them into system, matter-wide, and personal groups, and storing per-user visibility overrides for table and document surfaces. Matter-owned `ASSERTED` definitions may be updated without changing their stable key, type, or cardinality. Enum values have stable keys and may be added, relabeled, redescribed, or logically deactivated; they are never physically deleted. Human APIs and internal agents use the same audited command service, and agent mutations require explicit approval plus execution-time reauthorization. Every new matter atomically materializes the versioned `edrm-core-v1` default profile and its standard groups as matter-owned database rows. The immutable profile template lives in application configuration; definitions and groups record their template key/version. Tenant-wide and client-specific matter templates are immutable configuration snapshots; direct cloning uses the same snapshot/materialization path. Templates and clones include shared definitions, system/matter groups, ordering, and default visibility, but exclude personal groups, user preferences, evidence, values, members, audit history, runs, and integrations.

Matter-document values for `ASSERTED` definitions use an immutable `metadata_event` ledger plus the rebuildable `document_metadata_current` projection. Every supported write path must lock the matter document, append the event, resolve the affected field, replace its current projection, record an audit entry, and create a durable document-search upsert in one Core transaction. Do not use database triggers for metadata resolution and do not update projection rows directly. Internal agent, extractor, import, rule, and system writers must call the same command service as the human-facing API. `SYSTEM` and `IMPORTED` definitions remain read-only through the assertion API and resolve from their owning relationship or source projection.

Enum options may form a hierarchy through an immutable `parent_key` that references another option in the same definition. Parent keys must exist, hierarchies must be acyclic, and a parent with active children cannot be deactivated. Hierarchical enum filters match the selected node and all descendants; their facets aggregate direct children with unique root-document counts while retaining self-excluding facet behavior.

Facetable searchable Text definitions may declare an optional `hierarchy_separator`. Collection values remain authoritative and unchanged, while search projection splits them into trimmed segments and emits every full path prefix into `hierarchy_facets`. Full prefixes, rather than leaf labels, are node IDs. Delimiter parsing is opt-in; never infer hierarchy from punctuation in collection data or mutate a controlled enum from observed values. Changing a hierarchy separator is a schema change that requires a full reindex.

Numeric metadata needs two storage categories:

- Integer values use a signed 64-bit integer backed by PostgreSQL `BIGINT` and stored in `value_long`.
- Decimal/floating-point values use the database floating-point type and are stored in `value_float`.
- Do not introduce a third `DECIMAL` metadata type unless requirements later demand exact fixed-precision arithmetic.
- Numeric identifiers that must preserve formatting, leading zeroes, or non-numeric characters use `TEXT`, not a numeric type.

### Search Projection

- OpenSearch is the initial search engine. It is a rebuildable projection; Core and Artifact records remain authoritative.
- Callers use the Core Search API and never submit raw OpenSearch Query DSL or connect directly to OpenSearch.
- Matter indexes use versioned physical names behind stable aliases. A matter alias must resolve to exactly one active physical generation. Classify schema differences before applying them: an allowlisted compatible additive mapping may update the active index in place; incompatible, backfill-requiring, or unknown changes must pause in `AWAITING_USER` until a matter administrator confirms a full reindex. A confirmed replacement is populated before the alias moves. After successful activation, delete every non-active physical index and then its `search_index_generation` row; retain projection-operation history as the audit trail. Document mutations target the active physical index so stale alias state cannot make a write ambiguous.
- Serialize search projection work with a PostgreSQL advisory transaction lock keyed by matter, never by locking the authoritative `matter` row. Artifact reads and OpenSearch writes must not block document ingestion through foreign-key row locks. Physical generation allocation must skip orphaned OpenSearch indexes left by an interrupted attempt rather than repeatedly failing on the same name.
- Matter creation and searchable-field changes enqueue schema synchronization. Document imports and resolved metadata changes enqueue idempotent document upserts through DBOS.
- User-driven document updates use higher DBOS priority than import/agent batches; full rebuilds use the lowest priority.
- The initial text profile uses the standard tokenizer plus lowercase normalization, no stop-word removal, positions and offsets, and separately named/versioned index, search, and quoted-search analyzers.
- The document projection indexes body text from the active collection `NORMALIZED_TEXT` artifact when available, then `EXTRACTED_TEXT`, `OCR_TEXT`, or a text-compatible native artifact such as EML or plain text. The configured byte limit bounds text loaded into one projection; existing matter documents require a matter-index rebuild after a collection activates a new normalized-text run.
- Matter search supports `KEYWORD`, `SEMANTIC`, and `HYBRID` modes. Semantic queries use the configured embedding gateway with query prompting, apply authorized matter and metadata filters inside nested k-NN retrieval, and expose the best matching chunk as a passage. Hybrid queries combine lexical and nested-vector clauses using an OpenSearch reciprocal-rank-fusion search pipeline. Keyword remains the default and non-keyword modes require a query and relevance sorting.
- Every matter search includes a built-in `coded_by` facet derived from the immutable `metadata_event` ledger. It tracks every document ever changed by a `HUMAN` or `AGENT` coding source across any asserted field, uses stable opaque actor tokens in OpenSearch, and resolves human display names plus email addresses and agent names through Core. Import, extractor, rule, and system events do not count. This is a protected search-projection field rather than a configurable metadata definition, and introducing it to an existing matter requires a full reindex to backfill historical provenance.
- Matter search-result export is a first-class `MATTER_SEARCH_EXPORT` job. Core freezes the controlled search, ordered field/display-label contract, CSV policy, initiating user, and physical search-index generation; DBOS enumerates one OpenSearch point-in-time view with `search_after`; Artifact owns the immutable `SEARCH_RESULT_EXPORT` CSV bytes and matter-scoped artifact inventory. Never materialize one Core membership or value row per exported document. Creation and download both require `document.export` plus `document.view`, and downloads always stream through Core after fresh authorization.
- Facetable short text has an exact keyword subfield. Identifiers and enums use keyword mappings, `INTEGER` maps to OpenSearch `long`, and `DECIMAL` maps to OpenSearch `double`.
- Only matters with an active searchable hierarchical enum or delimited Text hierarchy include the root `hierarchy_facets` nested mapping and document projection. Flat enums do not emit hierarchy entries. Introducing the first hierarchy into an existing matter is therefore a schema change that requires a confirmed full reindex; matters without hierarchical fields must not rebuild merely to reserve hierarchy support. Hierarchical enum assignments project the assigned node plus each ancestor, and delimited Text values project each full path prefix. Hierarchy filtering and direct-child aggregation must constrain the metadata field and node relationship in the same nested scope, and bucket counts must use `reverse_nested` so they count matter documents rather than nested entries.

### Saved Searches

- Saved searches are authoritative Core records scoped to one matter. They store a controlled `MatterSearchRequest`, not result IDs or a frozen result set.
- Saving resets the request offset to zero. Running a saved search re-executes its stored definition against the matter's current active search index, so matching documents and facet counts can change as the corpus and metadata change.
- `PRIVATE` searches are visible only to their owner. `PUBLIC` searches are visible to every user authorized to access the matter. `SHARED` searches are visible to their owner and explicitly selected active users in the matter's tenant who are authorized to access the matter.
- Only the owner can update or delete a saved search. Names are unique per owner within a matter, using whitespace-normalized, case-insensitive comparison.
- Explicit user shares use a separate join table. Future group sharing must use a separate group-share relation without changing the saved search definition or overloading user shares.
- Saved-search access is enforced in the Core API for list, read, execute, update, and delete operations. UI visibility is not an authorization boundary.

### Review Batches, Reviewer Coding, and Evaluation Runs

- A review batch is a matter-scoped, permanent frozen set of matter-document IDs. Documents may belong to multiple batches. `ALL_MATTER` means every document currently in the matter; search-query and random selections record their complete provenance, including the physical search generation or deterministic random seed.
- Batch membership is materialized by a durable DBOS workflow. Creating a batch does not create a temporary copy of document content and does not depend on the later state of the search query. PostgreSQL is authoritative; ready memberships are projected into each OpenSearch matter document as a keyword-array `batch_ids` field. Batch search and facet endpoints must inject the batch filter server-side, and full index rebuilds must derive batch membership from Core.
- A batch has a name, description, chronological notes, at most one current human assignee, and a reviewer-value visibility policy of `OWN_VALUES` or `ALL_REVIEWER_VALUES`.
- Reuse matter metadata groups as coding field sets. Snapshot the selected shared groups, field order, and field schemas into the batch so later configuration changes do not silently change an active review or evaluation corpus. Personal groups cannot be assigned to a batch.
- Human reviewer coding has one canonical source per `(batch, reviewer)` and an append-only revision ledger. Every accepted save, explicit field clear, skip, and CSV import appends an immutable server-ordered revision; the highest revision affecting a document or field is the current winner. `review_batch_reviewer_document` and `review_batch_reviewer_value` are rebuildable current-state projections used for fast review reads. A human reviewer never receives a `review_batch_run`. Imported and interactive changes use the same command service, create audit records, and remain distinguishable by origin. Reproducible consumers pin the canonical reviewer source and revision.
- Agent, workflow, and classifier evaluations retain immutable `review_batch_run` identities. An agent run pins the published `agent_definition_version` and copies its prompt, model, policy, output schema, and limits into a configuration snapshot. A rerun creates a new run; it never mutates an earlier run.
- Reviewer coding and evaluation-run values are isolated from the matter-wide `metadata_event` ledger and `document_metadata_current` projection. Comparisons expose one source per reviewer plus the selected immutable evaluation runs. Explicit promotion appends new matter-scope metadata events; it never moves or implicitly merges isolated results.
- Reviewer-coding promotion requires `batch.publish_coding` and explicit selection of one reviewer. Queueing the promotion freezes that reviewer's current coding revision so later edits cannot change an in-flight promotion. It applies replace semantics to every snapshotted batch coding field for reviewed documents only; blank fields clear current matter values, while skipped and unfinished documents remain unchanged. Events retain the original reviewer as the coding actor, the requesting administrator in the audit record, and trigger document reindexing.
- Agent batch runs are created through the authorized Core API. Analysis Task promotion is a distinct `batch.publish_analysis` operation and uses the pinned task-version destination fields rather than the batch coding-group snapshot.
- A Matter Definition assessment document count is a target, not a maximum. Reserve the requested control sample inside that target, test every planned topic query against the pinned search generation, and allow bounded replanning with multiple queries per topic. Never create or analyze a partial assessment batch: if any topic still has no results or unique candidates cannot fill the target, stop before batch creation and surface the measured query results as a clarification request.

### Agent Runtime and Matter Definitions

- Use one stable, generic Pydantic AI harness with DBOS durability. Agent definitions are versioned data; tenant definitions must not dynamically register Python workflow classes.
- Root administrators manage `SYSTEM` agents. Tenant administrators manage `TENANT` agents for their tenant. Conversations pin an immutable published version.
- Models and tools come from code-owned registries. Tenant prompts cannot upload Python, add arbitrary endpoints, or weaken the code-controlled security layer.
- Approval-required Pydantic AI tools use the stop-the-world flow: persist provider message history and action requests, record each user decision, then create a new correlated `agent_run` within the same user turn using `DeferredToolResults`.
- Approval is not authorization. The API authorizes the decision maker and each executing tool re-authorizes the approving user at execution time.
- Core owns Matter Definitions and immutable Markdown revisions. The Artifact Service will own uploaded source files and normalized source artifacts. Agent edits use the same Matter Definition command service as direct user edits and record `agent_run_id` provenance.
- Publishing a Matter Definition remains an explicit matter-admin UI/API action; agents cannot silently publish it.

### Fact Development

The full design and delivery slices are in `docs/fact-development-plan.md`. The task-definition layer, the entity store, the fact ledger with its API, fact export, Postgres fact search, extraction runs, duplicate consolidation, confirmed-fact suggestions, the final duplicate sweep, the launch estimate, and document integration (`fact_state` and the per-document facts endpoint) are implemented; the Facts/Entities UI is planned.

- `FACT_DEVELOPMENT` is a distinct Matter Analysis Task type with workflow key `fact_development_v1`. It is generative and stateful across documents; it does not use the Decision Specification, the JEV/decision-provider path, destination coding fields, or `review_decision_result`/`review_batch_run_value`.
- Every fact-development task version pins one Review Guidance profile, the exact revision that was published when pinned, and that revision's content hash. The pin never follows the guidance's moving `published_revision`; a newer published revision marks the pin stale, and adopting it creates a new draft task version that must be recompiled and republished.
- The version carries a Fact Development Specification (`fact_development_specification`): an issue catalog in which each issue cites exact source excerpts from the pinned guidance or the task definition and maps to the active, matter-owned ENUM, BOOLEAN, TEXT, or LONG_TEXT coding field and value that hold that issue's coding. Hierarchical enum mappings include descendants unless disabled.
- A managed compiler skill drafts the catalog through the durable `fact_development_specification_compilation_v1` workflow. Every candidate is validated deterministically (exact source hashes, field existence and type, active enum values), and reviewers may replace it through the specification endpoint, which applies the same validation. Publication revalidates against current coding fields; the compiler can never publish.
- Issue membership is deterministic: a document belongs to an issue when any mapping matches its coding, read from `document_metadata_current` (resolved `VALUE` rows only) or from one reviewer's batch coding pinned at a revision so later edits cannot change a prepared run.
- The matter entity store holds people and organizations only, in one `matter_entity` table: a canonical row (`parent_id` null) or a variant row holding another name and/or one email address. A partial unique index enforces one email per matter. Merging sets `parent_id` on the source and its variants (never chains); unmerging clears it. Status, title, organization, and custodian live only on canonical rows. Every change goes through `app/matter_entities.py` and appends `matter_entity_event`.
- Facts are a Core-owned ledger: `matter_fact_event` is authoritative; `matter_fact` and its issue, participant, and evidence rows are the current projection, and every content change appends an immutable `matter_fact_revision`. `app/facts.py` is the only write path for people and agents. Human mutations carry the loaded `lock_version`.
- Facts link to the matched entity row (canonical or variant) and are displayed and filtered by its canonical entity, so entity merges never rewrite facts. Rejecting an entity removes it from facts as new revisions.
- Evidence quotes are verified by code against the document's current faithful text: an exact substring of one paragraph of the `paragraph-v1` map, with offsets computed by code and the source artifact and content hash recorded. Quotes never span paragraphs.
- Facts start `WORKING`. Only a person can confirm or reject; confirming is what places a fact on the timeline, with no separate publish step. Merged facts are immutable until unmerged; unmerge restores the moved evidence and the issues and participants the merge added.
- Permissions: `fact.view`, `fact.review`, `fact.export`, `entity.view`, `entity.manage`. Every fact endpoint also requires `document.view` because facts contain document quotes. `fact.export` is administrator-only (tenant, client, and matter administrators). Reviewers can review facts and manage entities; Analysis Operator and Read-only can view.
- Facts are not projected into OpenSearch for the MVP; fact search, facets, the date histogram, and agent recall run on the Core tables. Evidence-document filters read only Core data (`matter_document_custodian`, `document_metadata_current`), never Artifact or a search index. An OpenSearch projection may be added later as a rebuildable copy.
- Fact list filtering and date ordering live in `app/fact_queries.py` and are shared by the list endpoint and exports, so an export contains exactly the facts the same filter lists, in the same order.
- Fact export runs within the request, not as a job (decided; it replaced the original `MATTER_FACT_EXPORT` job). Core requires `fact.export`, `fact.view`, and `document.view`, allows one export per matter at a time (a PostgreSQL session advisory lock; 429 otherwise), keeps no request transaction open while the files are written and uploaded, caps each export at `FACT_EXPORT_MAX_BYTES` (500 MB by default; 422 above it), reads all matching facts from one consistent snapshot (`REPEATABLE READ` on PostgreSQL), and writes a facts CSV (one row per fact) and an evidence CSV (one row per active quote, with control number). File names carry the snapshot time ("as of"). Artifact owns both files as `FACT_EXPORT` artifacts; Core then records one completed `matter_fact_export` row and a `fact_export.created` audit event in the same transaction, and a failed request records `fact_export.failed` and leaves no export. A repeated idempotency key returns the first export. Downloads always stream through Core after fresh authorization and provenance checks and are audited. Never write one Core row per exported fact. Document search-result export remains a `MATTER_SEARCH_EXPORT` job.
- A fact-development run is a `matter_analysis_task_run` with `run_kind = FACT_DEVELOPMENT` (classification runs keep `run_kind = CLASSIFICATION`, and every classification query filters on it) and the `FACT_DEVELOPMENT_ANALYSIS_TASK` job type, so status, progress, cancel, and retry use the unified job execution platform. Launch requires `analysis_task.run` and freezes the task version, issue-catalog hash, review batch, coding source (with the reviewer-coding revision), chunk size, skill version, and model in `configuration_snapshot`.
- Fact-development runs adopt the generic Analysis Input Manifest platform. Preparation freezes the review batch into one `ANALYSIS_INPUT_MANIFEST` owned by the run (`MATTER_ANALYSIS_TASK_RUN`, contract `fact-development.population`): one row per batch document in chronological order (File Date, undated last, families together), with the matched issues, File Date and email headers, the extraction fingerprint, and the pinned source text artifact and hash. Documents coded to no issue are `EXCLUDED`; documents without a text source are `MISSING`. Inputs are resolved through the bulk, content-free `get_collection_item_analysis_input_snapshots` gateway read (Core applies its text-source precedence to the Artifact candidates). `matter_analysis_task_run_chunk` rows hold only execution state for fixed ordinal ranges of the manifest. Each chunk reads only its rows with the verified bounded `AnalysisInputManifestReader`, reads the pinned text with `read_text_source` (verifying its hash), and never re-resolves population, text, or metadata. Chunks are dispatched in chronological manifest order through an ordered window (at most `chunk_window` in flight; the next chunk starts when the oldest in-flight chunk finishes), so later documents recall facts from earlier ones without wave barriers. Each document calls the `fact_extraction` managed skill and applies only operations that pass deterministic validation (issues within the document's coding, verbatim quotes, valid dates, `ADD_EVIDENCE` and `REFINE` only to recalled facts sharing an issue) through the fact and entity command services as the agent. Invalid operations are recorded with errors and never block valid ones. Unknown people and organizations become `PROPOSED` entities (email match first, then a unique exact name).
- Duplicate control: before a `CREATE` is applied, its summary is compared with existing facts that share an issue in a compatible date window (rejected facts included). Above the frozen `duplicate_threshold`, the `fact_consolidation` managed skill rules SAME or DISTINCT without any lock held. SAME adds the quotes to the existing fact, or blocks the `CREATE` when that fact was rejected (tombstone). The worker then takes PostgreSQL advisory transaction locks per matter, task, and issue (sorted), rechecks for close facts created since the ruling that it did not already consider, and rules again (bounded) before writing. An unavailable duplicate check is counted and never loses the fact.
- Confirmed facts are reviewer-owned. Agents may add evidence to them, but a `REFINE` of a confirmed fact becomes a pending `matter_fact_suggestion` (with its verified quotes) that a person with `fact.review` accepts (applied as their own revision, with `lock_version`) or dismisses. `REFINE` revises a working fact directly and is refused for rejected or merged facts.
- After the last chunk, a duplicate sweep compares the run's working facts with working and confirmed facts that share an issue and creates `MERGE` suggestions above the frozen `merge_suggestion_threshold`. Nothing is merged automatically: a reviewer accepts a merge (with both facts' `lock_version`; undoable with unmerge) or dismisses it, and a dismissed or pending pair is never suggested again. A confirmed fact is never the absorbed side. A sweep failure is reported on the run, not fatal.
- `POST .../fact-runs/estimate` (requires `analysis_task.run`) resolves the population exactly as a run's freeze does and reports issue counts, excluded and missing-text documents, documents an earlier run already extracted, and approximate token volume. It writes nothing.
- Documents carry one derived `SYSTEM` ENUM metadata field, `fact_state` ("Facts"), installed by the default profile (version 5) or `ensure_fact_state_metadata` for older matters, like `email_domain` and `file_date`. `confirmed` means an active quote belongs to a confirmed fact, `unconfirmed` that quotes belong only to working facts; no value means no live fact cites the document (rejected and merged facts never count). Core is authoritative and stores no per-document value: the projection derives it from `matter_fact_evidence`, full rebuilds recompute it, and the explicit `SCHEMA_SYNC` backfill updates only cited documents. Every fact command that can change it records the affected documents' states before writing and queues one coalesced `DOCUMENT_UPSERT` for only the documents whose state changed (default priority for people, `100` for agents). Search filters and facets use the ordinary metadata path; "None" is the missing-value filter.
- `GET /v1/matters/{matter_id}/documents/{document_id}/facts` (requires `fact.view` and `document.view`) reads Core directly and lists the working and confirmed facts citing a document, oldest first, with only that document's quotes. Fact search supports `has_pending_suggestion`, and fact rows report `pending_suggestion_count` (a merge suggestion counts on both facts).
- A fact-development run may be launched with `use_batch_api` when its resolved extraction model has a registered provider Batch API adapter (`app/model_batching.py`); the choice is frozen in `configuration_snapshot`. Batch runs use a chunk window of 1 and their own DBOS queues (`fact-development-batch-runs` and `fact-development-batch-chunks`, sized by `FACT_DEVELOPMENT_BATCH_RUN_CONCURRENCY`), because a waiting run and chunk hold their queue slots for hours: each chunk submits its documents' extraction requests as one provider batch when the previous chunk has been applied (recall is taken when the request is built, so documents in one chunk see every earlier chunk's facts but not each other's), waits durably with DBOS sleep and polling, and applies the results in manifest order through the same validation, consolidation, and write path as real-time extraction (`app/fact_batches.py`). The chunk row records the provider batch, its status, and a manifest of request, document, skill run, and recalled fact IDs; it is claimed (SUBMITTED, phase "preparing", a unique display name) before its requests are built and moves to phase "sending" before the provider is called, so a retried step adopts the batch the provider accepted (adapter `find`; a lookup error retries the step) or falls back instead of submitting twice, and a chunk canceled after an interrupted submission has its batch found and canceled. Submission, polling, and cancellation lock the run row and then the chunk row (the cancel endpoint's order) and never call the provider while holding them; provider cancels are sent after the cancellation commits, and a cancel that arrives during submission cancels the new batch when the provider call returns. Results that cannot be read are retried by the chunk step before falling back. Missing or invalid results, failed or unsubmittable batches, repeated polling errors, unreadable results, batches past the deadline, and documents over the request size limit fall back to real-time calls; consolidation calls are always real time. Canceling a run cancels its pending provider batches and closes the skill runs of results never applied.
- Per-document results are job outcomes (`fact.*` reason codes) published as Artifact-owned fragments and a final manifest; the aggregate report and QC signals live on the run. Like embedding reuse, extraction is content-addressed: each finished document records a fingerprint (document, pinned source text content hash, paragraph-map version, issue-catalog hash, and its coded issues; deliberately not the skill version or model) on its chunk in the same transaction as its facts. A later attempt, or a later run of the same task (marked in its manifest when it is frozen), skips a document whose fingerprint matches and reports it `REUSED` (`fact.reused_prior_extraction`); changed text, coding, or catalog re-extracts it. Cancellation is final (unfinished documents become `CANCELED` outcomes); only a retryable failed run resumes, from the chunks that have no published fragment.

## Existing Phase-One API

Preserve the current versioned API shape unless a deliberate contract change is requested:

- `GET /health`
- `POST /v1/auth/login`
- `POST /v1/auth/refresh`
- `POST /v1/auth/logout`
- `GET /v1/auth/me`
- tenant creation, retrieval, and tenant-user creation under `/v1/tenants`
- tenant and tenant-user listing under `/v1/tenants`
- client creation and retrieval under `/v1/tenants/{tenant_id}/clients` and `/v1/clients/{client_id}`
- matter creation and retrieval under `/v1/clients/{client_id}/matters` and `/v1/matters/{matter_id}`
- metadata-definition creation, retrieval, configuration updates, and enum-value lifecycle under `/v1/matters/{matter_id}/metadata-definitions`

The first vertical workflow is: bootstrap root administrator → create subtenant → log in → create client → create matter → create matter metadata definition.

## Authentication and Browser Security

Phase one must support a simple local email/password login while allowing an external identity provider to be added later.

- Login accepts email and password only. The server resolves the user's tenant from the authenticated user record; the browser must never supply or select a tenant as part of authentication.
- User email addresses are globally unique in phase one because each user belongs to exactly one tenant. If tenant memberships become many-to-many, authentication must still establish identity first and tenant selection must happen only against server-side memberships.
- Hash passwords with Argon2id and keep credentials separate from the user record.
- FastAPI mints signed JWT access and refresh tokens.
- Preserve refresh-token rotation, session records, revocation, and logout behavior.
- Bootstrap the root super-admin credentials from environment settings with `python -m app.bootstrap`.
- Never commit `.env` files or credentials.

For the browser application, use Next.js as a thin backend-for-frontend (BFF):

- The browser calls Next.js, not FastAPI directly.
- Next.js stores access and refresh tokens in protected `HttpOnly`, `Secure`, `SameSite` cookies.
- Browser JavaScript must never receive JWTs or store them in `localStorage` or `sessionStorage`.
- Next.js forwards requests to FastAPI with the access-token Bearer header and coordinates refresh.
- Add CSRF protection to cookie-authenticated, state-changing BFF endpoints.
- Prefer a same-origin production deployment.
- Next.js may own route protection, redirects, forwarding, refresh coordination, and UI-specific response aggregation.
- Next.js must not access the application database or duplicate tenant, membership, metadata, authentication, authorization, or other domain rules.

The existing FastAPI token responses may be consumed server-side by the BFF, which then sets the protected cookies.

## Frontend Architecture

Build one application for both administration and end-user workflows. Separate experiences with route groups such as `/admin/*` and `/app/*`; do not create two frontend applications.

Use:

- React with strict TypeScript
- Next.js App Router; do not add Vite or React Router
- shadcn/ui
- Tailwind CSS
- TanStack Query for server state
- React Hook Form and Zod for forms and validation
- Orval for the generated FastAPI client and models
- TanStack Table for phase-one tables
- Lucide React for icons
- pnpm as the JavaScript package manager
- Vitest and React Testing Library for unit/component tests
- Playwright for end-to-end tests
- MSW for API mocks

For a future high-density document-review grid, evaluate TanStack Virtual or a specialized grid against concrete requirements instead of choosing one prematurely.

Lucide is open source under the ISC license, with some Feather-derived icons under MIT. Preserve the complete applicable license notices in a `THIRD_PARTY_NOTICES` or equivalent licenses file. Visible attribution in the UI is not required.

## UI and Styling Rules

Tailwind and shadcn/ui are sufficient. Build a product theme rather than scattering raw colors through features.

Use the following palette as the Priv-View product color foundation for digital UI work:

- Primary: Priv-View Blue `#004179` and Priv-View Gold `#F3C404`
- Blue/teal: `#009DEA`, `#C3D7FE`, `#487B81`, `#71CDA1`
- Green: `#C1E292`, `#4A9566`
- Gold/brown: `#FBD97E`, `#CDA17F`, `#957A65`
- Orange/red/plum: `#E87928`, `#9B3426`, `#522953`
- Neutrals: `#373938`, `#415569`, `#BCCAD2`, `#D9DAE4`

Priv-View Blue is the default primary/action color. Priv-View Gold is the principal accent and highlight color; do not use gold as small body text on a light background. Use the secondary palette selectively for hierarchy, charts, classifications, and agent/source distinctions. Use the neutral palette for application chrome, surfaces, borders, and subdued content. Derive dark-theme surfaces from the palette while preserving the visual relationship to the light theme.

- Define semantic CSS variables for background, foreground, cards/panels, muted surfaces, primary, secondary, accent, destructive, borders, inputs, and focus rings.
- Define semantic state tokens for success, warning, error, info, pending, confirmed, rejected, human, agent, extractor, conflict, and confidence.
- Map palette colors to semantic tokens; feature components must not depend on the source color names.
- Validate text, icon, focus, and interactive-state contrast against WCAG 2.2 AA. Accessibility requirements override literal palette use when a source color lacks sufficient contrast.
- Feature code should use semantic utilities such as `bg-background`, `text-muted-foreground`, and `border-border`; avoid raw palette choices such as `bg-blue-600` or `text-gray-500`.
- Support light, dark, and system themes from the start with `next-themes`.
- Use comfortable density for administration and forms, and compact density for tables and investigation workflows, within one theme.
- Keep shadcn primitives in `components/ui`. Feature code should compose them rather than casually modifying shared primitives.
- Use `class-variance-authority` and a shared `cn()` helper for variants and class composition.
- Do not add Sass, Emotion, styled-components, or another styling framework without a specific need.

## Testing and Verification

This repository uses Pipenv. All user-facing Bash commands and command suggestions must invoke project tools through `pipenv run` (for example, `pipenv run pytest` or `pipenv run alembic upgrade head`). Direct `.venv/bin/...` commands may be used internally only when repository guidance specifically requires an explicit virtual-environment executable, and must not be presented as the normal user workflow.

Run checks appropriate to every change.

For backend work, normally run:

```text
pipenv run ruff check .
pipenv run pytest
pipenv run alembic check
```

Migration changes must also be exercised through upgrade and downgrade paths against an appropriate database. Keep bootstrap behavior idempotent.

When a migration check targets a temporary database, verify `current_database()` before running a downgrade. Invoke the virtual-environment Alembic executable directly with an explicit `DATABASE_URL`; do not rely on `pipenv run` for this override because Pipenv loads the repository `.env` and can redirect the command to the development database.

## User Documentation

- Treat user documentation as part of the definition of done for every user-facing feature.
- Add or update the relevant end-user pages under `web/content/docs` whenever a feature, workflow, permission, status, limitation, or UI label changes.
- Describe what the feature is for, who can use it, prerequisites, the normal workflow, important states, and current limitations. Document only behavior that is actually available; clearly label planned behavior.
- Add or update contextual help links for non-obvious workflows using the shared help-topic registry so in-app guidance and full documentation stay connected.
- Keep navigation and search metadata current, and verify that the documentation route and search index build successfully with the frontend checks.

For frontend work, run the configured type check, lint, unit/component tests, production build, and relevant Playwright flows. Test authorization on the backend; UI visibility checks are not a substitute.

## Local Development

Typical backend startup:

```text
cp .env.example .env
pipenv install --dev
docker compose up -d postgres
pipenv run alembic upgrade head
pipenv run python -m app.bootstrap
pipenv run uvicorn app.main:app --reload
```

## Change Discipline

- Keep phase-one work focused on authentication, hierarchical tenants, users, clients, matters, memberships, and matter-level metadata definitions.
- Do not implement review-platform integrations, productions, or the epoch subsystem unless the user explicitly advances that scope. The role/permission/content-access system described above is explicitly in scope. Client-scoped custodians, collections, source containers, collection items, native artifact storage, matter-document linking, and asserted document metadata values are also explicitly in scope.
- Keep designs extensible for external identity providers, review-platform integrations, multiple agent types, and later corpus processing without building those systems now.
- Add an Alembic migration for every persistent schema change.
- Keep Core and Artifact migrations separate and exercise both upgrade/downgrade paths.
- Preserve user changes and unrelated work already present in the repository.
- Update this file when an architectural or product decision is explicitly changed.
