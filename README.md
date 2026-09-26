# Priv-View Application

Application with a FastAPI Core API, independent Artifact and Embedding services, and a Next.js web interface for administrators and end users.

## Included scope

- hierarchical tenants with one root tenant;
- bootstrapped root super administrator;
- bootstrapped, published Matter Definition Setup system agent;
- email/password login with tenant scope resolved server-side;
- signed JWT access and refresh tokens with refresh-token rotation;
- root-admin subtenant and subtenant-user creation;
- client creation with explicit creator `ADMIN` membership;
- matter creation with explicit creator `ADMIN` membership;
- automatic `edrm-core-v1` metadata definitions for every new matter;
- additional matter-level metadata-definition creation and validation;
- system, matter-wide, and personal metadata groups with per-user table/document visibility;
- tenant-wide and client-specific matter templates plus direct configuration cloning;
- durable DBOS jobs for adding collection documents to matters without copying artifacts;
- durable collection deletion with batched database cleanup and unshared object-store blob removal;
- matter overview document and distinct-custodian counts backed by Core data;
- immutable, typed matter-document metadata events with a transactionally maintained current-value projection;
- `ACTIVE`, `SUSPENDED`, and `ARCHIVED` persistence states;
- audit records for login and creation operations.

Document metadata values remain phase two. Client-level artifact collections and uploads are implemented separately by the Artifact Service; matter documents reference those collection items.

## Local setup

```bash
cp .env.example .env
pipenv install --dev
docker compose up -d postgres minio
pipenv run alembic upgrade head
pipenv run alembic -c artifact_alembic.ini upgrade head
pipenv run python -m app.bootstrap
pipenv run uvicorn app.main:app --reload
```

When upgrading data that already contains matter documents from before migration `0007`, copy their Artifact-owned custodian relationships into Core once:

```bash
pipenv run python -m scripts.backfill_matter_document_custodians
```

The backfill is idempotent and reports missing or client-mismatched custodian references without creating invalid links.

Start the durable workflow worker in another terminal before creating matter document-import jobs:

```bash
pipenv run python -m app.workflow_worker
```

Keep that terminal open while jobs run. It reports worker startup, model loading,
embedding job and batch progress, per-document failures, and completion. Repeated
`GET .../embedding-jobs` lines in the Core API terminal are only the Jobs page
refreshing its status; they are not worker progress messages.

DBOS stores its workflow state in the `dbos` schema of `pvr-core`. The application stores its user-visible job and batch records in the normal Core schema.

Set a unique `JWT_SECRET` and change the bootstrap password in `.env` before running the bootstrap command. Bootstrap is idempotent for the configured root tenant, administrator, and standard Matter Definition Setup agent. Existing installations can rerun `pipenv run python -m app.bootstrap` to provision the standard agent without replacing a previously configured agent with the same key.

To run the standard agent through Google AI Studio, set `AGENT_DEFAULT_MODEL` to a `google:<model-id>` value and provide `GOOGLE_API_KEY`. The Core API and workflow worker must be restarted after changing their environment.

All model requests share process-wide rate-limit buckets keyed by provider and model. The defaults (`MODEL_RATE_LIMIT_REQUESTS_PER_MINUTE=12` and `MODEL_RATE_LIMIT_INPUT_TOKENS_PER_MINUTE=200000`) stay below the Gemini developer free-tier limits. Set them to conservative values for the quota assigned to the configured provider project. A provider HTTP 429 pauses the shared bucket, honors `Retry-After` or structured provider retry guidance, and retries with bounded exponential backoff. These buckets coordinate concurrent calls within one process; deployments with multiple workflow-worker processes must divide the configured quota across those processes or provide a distributed limiter.

Interactive API documentation is available at `http://127.0.0.1:8000/docs`; the OpenAPI document is available at `/openapi.json`.

By default, `ARTIFACT_MODE=embedded` mounts the Artifact Service routes in the Core API process while keeping Artifact tables in the separate `pvr-artifact` database. Every tenant receives a dedicated S3-compatible bucket. The local setup uses MinIO at `http://127.0.0.1:9000`, with its console at `http://127.0.0.1:9001`.

To run the same Artifact Service as a separate process, set `ARTIFACT_MODE=remote` on Core, point `ARTIFACT_BASE_URL` at the service, and start:

```bash
pipenv run uvicorn artifact_service.main:app --host 127.0.0.1 --port 8001
```

The Core API then proxies Artifact requests and supplies a short-lived, scope-limited delegation token. The standalone service never accepts a Core user access token directly. For the containerized split deployment, run `docker compose --profile split up --build`; Core listens on port 8000, Artifact on port 8001, OpenSearch on port 9200, and the workflow worker runs alongside them.

## Embedding service

Embedding inference uses one contract whether it runs inside a Core/worker process or as a standalone FastAPI service. The configured model loads lazily on the first embedding request.

The default local configuration is:

```text
EMBEDDING_MODE=embedded
EMBEDDING_PROVIDER=sentence_transformers
EMBEDDING_MODEL=voyageai/voyage-4-nano
EMBEDDING_DIMENSIONS=1024
EMBEDDING_DEVICE=auto
```

The locked dependency versions are intentionally compatible with the trusted
remote model implementation. Run `pipenv sync --dev` after pulling dependency
changes, then restart the workflow worker so it loads the updated libraries.

`EMBEDDING_MODEL`, `EMBEDDING_MODEL_REVISION`, dimensions, device, normalization, and batch limits are environment settings. Production deployments should pin a reviewed model revision when `EMBEDDING_TRUST_REMOTE_CODE=true`.

To run the service as a separate process, configure Core and the workflow worker with `EMBEDDING_MODE=remote`, set the same model and dimensions on both sides, and start:

```bash
pipenv run uvicorn embedding_service.main:app --host 127.0.0.1 --port 8002
```

The standalone API requires `Authorization: Bearer <EMBEDDING_SERVICE_TOKEN>`. Its `POST /v1/embeddings` endpoint accepts a list of document or query strings; `GET /v1/models/current` reports the configured model and whether it is loaded. The service has no database and never stores source text or vectors.

For a containerized service, run `docker compose --profile embeddings up --build embedding-api`. The container uses CPU by default and retains downloaded model files in the `pvr_embedding_models` volume. On an Apple Silicon development machine, native embedded execution can use the Mac accelerator and will generally be faster than Docker CPU execution.

Hosted Voyage inference uses the same durable matter-embedding job. Configure both the API and workflow-worker processes with:

```bash
EMBEDDING_MODE=remote
EMBEDDING_PROVIDER=voyage_api
EMBEDDING_MODEL=voyage-4-lite
EMBEDDING_DIMENSIONS=1024
EMBEDDING_QUERY_MODE=embedded
EMBEDDING_QUERY_MODEL=voyageai/voyage-4-nano
VOYAGE_API_KEY=pa-...
EMBEDDING_VOYAGE_TOKENS_PER_MINUTE=3000000
EMBEDDING_VOYAGE_REQUESTS_PER_MINUTE=2000
```

The default hosted mode groups chunks into real-time requests, runs up to `EMBEDDING_VOYAGE_REALTIME_CONCURRENCY` durable matter batches concurrently, applies a process-local TPM/RPM limiter, and retries rate-limit and transient server responses. This is the mode to use with standard or free credits.

`EMBEDDING_QUERY_MODEL` optionally separates semantic-search query inference from document embedding. The configuration above embeds documents through hosted `voyage-4-lite` while running `voyage-4-nano` locally with `input_type=query`. Both use the same configured dimensions and normalization. Set `EMBEDDING_QUERY_MODE=remote` and `EMBEDDING_QUERY_BASE_URL` to use the private embedding service instead of loading the query model in Core. Voyage 4 models share an embedding space; do not configure a query model from an incompatible model family.

After provider billing is enabled, `EMBEDDING_VOYAGE_BATCH_ENABLED=true` switches newly created jobs to Voyage's asynchronous Batch API. The workflow uploads JSONL, persists provider file and batch identifiers, sleeps durably while polling, joins unordered results through stable IDs, and stores the same idempotent `CHUNK_VECTOR_SET` artifacts. Provider progress is available at `GET /v1/matters/{matter_id}/embedding-jobs/{job_id}/batches`, and application cancellation requests cancellation of active provider batches. The standalone `scripts/voyage_embedding_harness.py` utility is retained for diagnostics; production jobs do not depend on it.

Matter administrators start generation from the matter **Jobs** tab or with `POST /v1/matters/{matter_id}/embedding-jobs`. The durable workflow freezes the current matter-document IDs into batches, chooses extracted text, OCR text, or a supported native text artifact, and creates two collection-item artifacts:

- `CHUNK_SET`: sentence-aware text chunks and source offsets in Parquet;
- `CHUNK_VECTOR_SET`: one configured-dimension vector per chunk in Parquet.

After embeddings complete, matter administrators can start a durable topic-clustering job from the matter **Jobs** tab. Automatic mode learns a topic count from a bounded chunk sample; fixed mode accepts an explicit topic count. The worker assigns documents in durable `MATTER_TOPIC_BATCH_SIZE` batches, runs up to `MATTER_TOPIC_ASSIGNMENT_CONCURRENCY` assignment batches concurrently, and then performs one checkpointed OpenSearch projection for the full frozen scope. Approved values are published to the selected facetable, multi-value metadata field. Replacement is the default and is recorded as `CLEAR` plus `ADD` metadata events rather than destructive updates.

Deterministic derivation keys make reruns idempotent. Existing artifacts for the same source and configuration are reused, while a changed source, chunking configuration, model revision, dimensions, or normalization setting creates a new derivation. Successful batches update the OpenSearch document with a nested `chunks` array. The first request may download and initialize the configured model.

The Search & Review workspace supports keyword, semantic, and hybrid retrieval.
Semantic search embeds the user's query with the same configured model and returns
the best matching chunk for each document. Hybrid search combines keyword and
vector ranks using reciprocal-rank fusion. Filters apply in every mode.

OpenSearch is a rebuildable matter-search projection. Matter creation, searchable field changes, and document imports write durable projection operations that the workflow worker applies asynchronously. The local Compose service runs a single OpenSearch 3.8 node with its security plugin disabled; production deployments must use TLS, credentials, and an environment-appropriate cluster topology.

## Web application

With the API running, start the web application in another terminal:

```bash
cd web
cp .env.example .env.local
pnpm install
pnpm dev
```

Open `http://127.0.0.1:3000`. The UI supports login, tenant selection and creation, tenant users, clients, client-level evidence collections, collection-item browsing, matters, automatically created default matter metadata, additional matter-level metadata definitions, search and review, the approval-gated Matter Definition agent workspace, and root administration of reusable versioned system agents. Next.js acts as a thin BFF and keeps access and refresh tokens in protected cookies.

## Phase-one API

| Method | Path | Authorization | Purpose |
|---|---|---|---|
| `POST` | `/v1/auth/login` | Public | Mint an access/refresh JWT pair |
| `POST` | `/v1/auth/refresh` | Refresh token | Rotate refresh token and mint a new pair |
| `POST` | `/v1/auth/logout` | Access token | Revoke the current session |
| `GET` | `/v1/auth/me` | Access token | Return the current user |
| `POST` | `/v1/tenants` | Root tenant `ADMIN` | Create a subtenant and its initial admin atomically |
| `GET` | `/v1/tenants` | Root tenant `ADMIN` | List tenants available to the root administrator |
| `GET` | `/v1/tenants/{tenant_id}` | Root or matching tenant `ADMIN` | Read a tenant |
| `POST` | `/v1/tenants/{tenant_id}/users` | Root or matching tenant `ADMIN` | Create a tenant `ADMIN` user |
| `GET` | `/v1/tenants/{tenant_id}/users` | Root or matching tenant `ADMIN` | List tenant users |
| `POST` | `/v1/tenants/{tenant_id}/clients` | Root or matching tenant `ADMIN` | Create a client |
| `GET` | `/v1/tenants/{tenant_id}/clients` | Root or matching tenant `ADMIN` | List clients |
| `POST` | `/v1/clients/{client_id}/matters` | Authorized `ADMIN` | Create a matter from the default profile, template, or cloned configuration |
| `GET` | `/v1/clients/{client_id}/matters` | Authorized `ADMIN` | List matters |
| `POST` | `/v1/clients/{client_id}/custodians` | Authorized `ADMIN` | Create client-level custodian metadata |
| `GET` | `/v1/clients/{client_id}/custodians` | Authorized `ADMIN` | List client-level custodians |
| `POST` | `/v1/matters/{matter_id}/metadata-definitions` | Authorized `ADMIN` | Create a field definition |
| `GET` | `/v1/matters/{matter_id}/metadata-definitions` | Authorized `ADMIN` | List field definitions |
| `POST` | `/v1/matters/{matter_id}/metadata-groups` | Authorized `ADMIN` | Create a personal or matter-wide metadata group |
| `GET` | `/v1/matters/{matter_id}/metadata-groups` | Authorized `ADMIN` | List available groups with effective user visibility |
| `PUT` | `/v1/matters/{matter_id}/metadata-groups/{group_id}/visibility` | Authorized `ADMIN` | Set the current user's table or document visibility |
| `POST` | `/v1/matters/{matter_id}/templates` | Authorized `ADMIN` | Save shared matter configuration as a template |
| `GET` | `/v1/clients/{client_id}/matter-templates` | Authorized `ADMIN` | List tenant and client templates available to a client |
| `POST` | `/v1/matters/{matter_id}/document-imports` | Authorized `ADMIN` | Queue a collection selection for durable addition to the matter |
| `GET` | `/v1/matters/{matter_id}/document-imports` | Authorized `ADMIN` | List recent matter jobs and progress |
| `POST` | `/v1/matters/{matter_id}/document-imports/{job_id}/cancel` | Authorized `ADMIN` | Cancel an active matter job |
| `GET` | `/v1/matters/{matter_id}/documents` | Authorized `ADMIN` | List matter links to source collection items |
| `GET` | `/v1/matters/{matter_id}/overview-counts` | Authorized `ADMIN` | Count linked documents and their distinct custodians without a search index |
| `POST` | `/v1/matters/{matter_id}/embedding-jobs` | Authorized `ADMIN` | Queue chunk and embedding generation for the matter |
| `GET` | `/v1/matters/{matter_id}/embedding-jobs` | Authorized `ADMIN` | List embedding job progress and history |
| `GET` | `/v1/matters/{matter_id}/embedding-jobs/{job_id}/batches` | Authorized `ADMIN` | Inspect local and hosted provider batch progress |
| `POST` | `/v1/matters/{matter_id}/embedding-jobs/{job_id}/cancel` | Authorized `ADMIN` | Cancel future embedding batches |
| `GET` | `/v1/matters/{matter_id}/documents/{document_id}/metadata-values` | Authorized `ADMIN` | Read current asserted metadata field states and values |
| `GET` | `/v1/matters/{matter_id}/documents/{document_id}/metadata-values/{definition_id}/events` | Authorized `ADMIN` | Read immutable value history and derived event states |
| `POST` | `/v1/matters/{matter_id}/documents/{document_id}/metadata-values/{definition_id}/events` | Authorized `ADMIN` | Append a typed metadata operation and resolve the current projection |

All authenticated requests use `Authorization: Bearer <access_token>`. Login requires `email` and `password`; the Core API derives tenant scope from the authenticated user's server-side assignment and includes it in the signed tokens. Passwords require at least 12 characters in this first implementation.

Only the `ADMIN` role is implemented in phase one. The schema keeps tenant, client, and matter assignments separate so a complete permission model can be added without changing resource ownership.

Metadata events and their affected `document_metadata_current` rows are written in the same Core transaction. The application command service—not a database trigger—enforces matter ownership, field type, cardinality, assertion policy, explicit event relationships, and deterministic resolution. The same transaction records the audit entry and creates a durable search-upsert operation; search indexing may finish asynchronously without becoming the source of truth.

## Artifact API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/tenants/{tenant_id}/artifact-storage/ensure` | Provision or return the tenant bucket |
| `POST` | `/v1/tenants/{tenant_id}/clients/{client_id}/collections` | Create a client-level collection |
| `GET` | `/v1/tenants/{tenant_id}/clients/{client_id}/collections` | List client-level collections |
| `GET` | `/v1/collections/{collection_id}` | Read one client-level collection |
| `DELETE` | `/v1/collections/{collection_id}` | Queue durable removal of an unreferenced collection, its rows, and unshared blobs |
| `GET` | `/v1/collections/{collection_id}/deletion` | Read the latest deletion job for a collection |
| `GET` | `/v1/collection-deletions/{job_id}` | Read durable deletion progress and errors |
| `POST` | `/v1/collection-deletions/{job_id}/retry` | Resume a failed deletion from its remaining work |
| `POST` | `/v1/collections/{collection_id}/source-containers:upload` | Preserve an original `SOURCE_CONTAINER` such as CSV, MBOX, DAT, or ZIP |
| `POST` | `/v1/collections/{collection_id}/items:upload` | Upload one preprocessed collection item and its `NATIVE` artifact |
| `GET` | `/v1/collections/{collection_id}/search` | Search filenames and source paths with an exact total and pagination |
| `GET` | `/v1/collections/{collection_id}/search/facets/{facet}` | Load one self-excluding collection facet on demand |
| `GET` | `/v1/collections/{collection_id}/items` | Query items by custodian, type, filename, dates, hash, or size |
| `GET` | `/v1/collections/{collection_id}/custodians` | List custodians represented in a collection with item counts |
| `POST` | `/v1/collections/{collection_id}/selections` | Freeze query results or explicit item IDs for a durable workflow |
| `GET` | `/v1/collection-selections/{selection_id}/items` | Read a deterministic selection batch |
| `DELETE` | `/v1/collection-selections/{selection_id}` | Remove a temporary frozen selection |
| `GET` | `/v1/collection-items/{collection_item_id}` | Read item, email, custodian, and native-artifact metadata |
| `GET` | `/v1/artifacts/{artifact_id}` | Read artifact metadata |
| `GET` | `/v1/artifacts/{artifact_id}/lineage` | Read the artifact's source relationships |
| `GET` | `/v1/artifacts/{artifact_id}/content` | Stream the immutable artifact bytes |

The upload endpoints use `multipart/form-data`. Item uploads contain a binary `file` part plus a JSON `metadata` part. `source_item_id` is the idempotency key within a collection: retrying with the same bytes returns the existing item; changing the bytes produces a conflict. Files extracted by a dataset-specific client can reference the preserved source container using `source_container_artifact_id`, recording `EXTRACTED_FROM_CONTAINER` lineage.

Every collection item also exposes a canonical `file_date`. The Artifact Service derives it from the email sent date, the parent email date for attachments, or `source_modified_at` for a standalone collected file. The original source timestamps remain unchanged.

## Local dataset import client

The OpenAPI-driven Python importer lives in [`scripts/import_client`](scripts/import_client/README.md). Its base importer owns API orchestration and common preprocessing while dataset adapters own source-specific mapping. Initial adapters support the full Enron `file,message` CSV schema and EMC-2 MBOX/ZIP inputs; `top.csv` is the head-100 Enron CSV sample.

Inspect a dataset without changing server state:

```bash
pipenv run python -m scripts.import_client enron-csv --source ../enron/top.csv --dry-run
pipenv run python -m scripts.import_client emc2 --source ../EMC-2 --dry-run
```

## Checks

```bash
pipenv run ruff check .
pipenv run pytest
pipenv run alembic check
pipenv run alembic -c artifact_alembic.ini check
```
