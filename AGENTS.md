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

The local Core PostgreSQL database is named `pvr-core`. The Artifact Service uses the independent `pvr-artifact` database and its own Alembic configuration, even in embedded mode.

Embedding inference is a logically independent, stateless capability. `EMBEDDING_MODE=embedded` loads the configured model lazily in the calling Core/worker process; `EMBEDDING_MODE=remote` calls the separately deployable FastAPI Embedding Service. Both modes use the same gateway and response contract. The model, revision, dimensions, device, normalization, batching, and service location are environment configuration. Standalone requests require a private service token and must not be exposed directly to browsers.

Matter embedding generation is a durable DBOS workflow. Core owns the job, immutable configuration snapshot, progress, and frozen batches of matter-document IDs. Artifact owns idempotent `CHUNK_SET` and `CHUNK_VECTOR_SET` Parquet artifacts, their deterministic derivation keys, processing metadata, and lineage. The sentence-aware chunk set is derived from the preferred extracted text, OCR text, or supported native text; the vector set is derived from the chunk set. OpenSearch stores all chunks as nested children of the owning matter document and is only a rebuildable projection. A completed job means both artifacts and the corresponding search projection were applied for every successful batch. Never store one Artifact row per chunk or treat OpenSearch as the authoritative vector store.

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
- Collection document discovery uses a search response containing paginated items, an exact total, and self-excluding facet counts. The initial implementation uses PostgreSQL while preserving a response shape that can later be backed by Elasticsearch without changing the browser workflow. Collection search covers original filename and source path. Initial facets are custodian, file extension, record type, and processing status; selections are ORed within a facet and ANDed across facets.
- Adding documents to a matter is a durable DBOS workflow. Core owns permanent jobs, batches, and matter-to-collection-item links; Artifact owns a short-lived frozen selection. The planner freezes the current query or explicit IDs, enqueues deterministic child batches, deletes the temporary selection after every batch is durably queued, and retains the Core job as audit/status history. Re-adding an existing collection item is idempotent and counted as already present. The same worker code supports embedded Artifact access or a separately deployed Artifact API.
- The import workflow also materializes Core-owned matter-document custodian associations from the Artifact selection. Matter overview document and distinct-custodian counts are authoritative Core database aggregates; they do not depend on the future search index.

### Authorization

Phase one uses a deliberately simple role model:

- Support the `ADMIN` role at tenant, client, and matter scope.
- An administrator can perform all actions within the authorized scope.
- The creator of a client receives explicit client `ADMIN` membership.
- The creator of a matter receives explicit matter `ADMIN` membership.
- Backend authorization is final even when the UI hides or disables an action.
- A more granular permission matrix and roles such as `PRODUCTION_MANAGER` are deferred. Production workflows are not currently in scope.

### Statuses

Use these initial resource statuses:

- `ACTIVE`
- `SUSPENDED`
- `ARCHIVED`

### Metadata

The application supports creating, reading, and configuring matter-level metadata field definitions, organizing them into system, matter-wide, and personal groups, and storing per-user visibility overrides for table and document surfaces. Matter-owned `ASSERTED` definitions may be updated without changing their stable key, type, or cardinality. Enum values have stable keys and may be added, relabeled, redescribed, or logically deactivated; they are never physically deleted. Human APIs and internal agents use the same audited command service, and agent mutations require explicit approval plus execution-time reauthorization. Every new matter atomically materializes the versioned `edrm-core-v1` default profile and its standard groups as matter-owned database rows. The immutable profile template lives in application configuration; definitions and groups record their template key/version. Tenant-wide and client-specific matter templates are immutable configuration snapshots; direct cloning uses the same snapshot/materialization path. Templates and clones include shared definitions, system/matter groups, ordering, and default visibility, but exclude personal groups, user preferences, evidence, values, members, audit history, runs, and integrations.

Matter-document values for `ASSERTED` definitions use an immutable `metadata_event` ledger plus the rebuildable `document_metadata_current` projection. Every supported write path must lock the matter document, append the event, resolve the affected field, replace its current projection, record an audit entry, and create a durable document-search upsert in one Core transaction. Do not use database triggers for metadata resolution and do not update projection rows directly. Internal agent, extractor, import, rule, and system writers must call the same command service as the human-facing API. `SYSTEM` and `IMPORTED` definitions remain read-only through the assertion API and resolve from their owning relationship or source projection.

Numeric metadata needs two storage categories:

- Integer values use a signed 64-bit integer backed by PostgreSQL `BIGINT` and stored in `value_long`.
- Decimal/floating-point values use the database floating-point type and are stored in `value_float`.
- Do not introduce a third `DECIMAL` metadata type unless requirements later demand exact fixed-precision arithmetic.
- Numeric identifiers that must preserve formatting, leading zeroes, or non-numeric characters use `TEXT`, not a numeric type.

### Search Projection

- OpenSearch is the initial search engine. It is a rebuildable projection; Core and Artifact records remain authoritative.
- Callers use the Core Search API and never submit raw OpenSearch Query DSL or connect directly to OpenSearch.
- Matter indexes use versioned physical names behind stable aliases. A matter alias must resolve to exactly one active physical generation. Mapping changes create and populate a new generation, atomically reconcile the alias to that generation, and retain prior generations as `RETIRED` rather than deleting them automatically. Document mutations target the active physical index so stale alias state cannot make a write ambiguous.
- Serialize search projection work with a PostgreSQL advisory transaction lock keyed by matter, never by locking the authoritative `matter` row. Artifact reads and OpenSearch writes must not block document ingestion through foreign-key row locks. Physical generation allocation must skip orphaned OpenSearch indexes left by an interrupted attempt rather than repeatedly failing on the same name.
- Matter creation and searchable-field changes enqueue schema synchronization. Document imports and resolved metadata changes enqueue idempotent document upserts through DBOS.
- User-driven document updates use higher DBOS priority than import/agent batches; full rebuilds use the lowest priority.
- The initial text profile uses the standard tokenizer plus lowercase normalization, no stop-word removal, positions and offsets, and separately named/versioned index, search, and quoted-search analyzers.
- The document projection indexes body text from the newest `EXTRACTED_TEXT` artifact when available, then `OCR_TEXT`, then a text-compatible native artifact such as EML or plain text. The configured byte limit bounds text loaded into one projection; existing documents require a matter-index rebuild after this behavior changes.
- Matter search supports `KEYWORD`, `SEMANTIC`, and `HYBRID` modes. Semantic queries use the configured embedding gateway with query prompting, apply authorized matter and metadata filters inside nested k-NN retrieval, and expose the best matching chunk as a passage. Hybrid queries combine lexical and nested-vector clauses using an OpenSearch reciprocal-rank-fusion search pipeline. Keyword remains the default and non-keyword modes require a query and relevance sorting.
- Facetable short text has an exact keyword subfield. Identifiers and enums use keyword mappings, `INTEGER` maps to OpenSearch `long`, and `DECIMAL` maps to OpenSearch `double`.

### Saved Searches

- Saved searches are authoritative Core records scoped to one matter. They store a controlled `MatterSearchRequest`, not result IDs or a frozen result set.
- Saving resets the request offset to zero. Running a saved search re-executes its stored definition against the matter's current active search index, so matching documents and facet counts can change as the corpus and metadata change.
- `PRIVATE` searches are visible only to their owner. `PUBLIC` searches are visible to every user authorized to access the matter. `SHARED` searches are visible to their owner and explicitly selected active users in the matter's tenant who are authorized to access the matter.
- Only the owner can update or delete a saved search. Names are unique per owner within a matter, using whitespace-normalized, case-insensitive comparison.
- Explicit user shares use a separate join table. Future group sharing must use a separate group-share relation without changing the saved search definition or overloading user shares.
- Saved-search access is enforced in the Core API for list, read, execute, update, and delete operations. UI visibility is not an authorization boundary.

### Agent Runtime and Matter Definitions

- Use one stable, generic Pydantic AI harness with DBOS durability. Agent definitions are versioned data; tenant definitions must not dynamically register Python workflow classes.
- Root administrators manage `SYSTEM` agents. Tenant administrators manage `TENANT` agents for their tenant. Conversations pin an immutable published version.
- Models and tools come from code-owned registries. Tenant prompts cannot upload Python, add arbitrary endpoints, or weaken the code-controlled security layer.
- Approval-required Pydantic AI tools use the stop-the-world flow: persist provider message history and action requests, record each user decision, then create a new correlated `agent_run` within the same user turn using `DeferredToolResults`.
- Approval is not authorization. The API authorizes the decision maker and each executing tool re-authorizes the approving user at execution time.
- Core owns Matter Definitions and immutable Markdown revisions. The Artifact Service will own uploaded source files and normalized source artifacts. Agent edits use the same Matter Definition command service as direct user edits and record `agent_run_id` provenance.
- Publishing a Matter Definition remains an explicit matter-admin UI/API action; agents cannot silently publish it.

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
- Do not implement review-platform integrations, productions, the epoch subsystem, or a granular permission system unless the user explicitly advances that scope. Client-scoped custodians, collections, source containers, collection items, native artifact storage, matter-document linking, and asserted document metadata values are explicitly in scope.
- Keep designs extensible for external identity providers, review-platform integrations, multiple agent types, and later corpus processing without building those systems now.
- Add an Alembic migration for every persistent schema change.
- Keep Core and Artifact migrations separate and exercise both upgrade/downgrade paths.
- Preserve user changes and unrelated work already present in the repository.
- Update this file when an architectural or product decision is explicitly changed.
