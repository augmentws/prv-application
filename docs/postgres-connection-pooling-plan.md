# PostgreSQL Connection Pooling Plan

## Outcome

Every service must use an intentional, observable PostgreSQL connection path.
The application must behave correctly against direct PostgreSQL first: each
process must apply bounded backpressure and release database transactions before
performing remote or CPU-heavy work. PgBouncer is a second-phase infrastructure
layer that protects PostgreSQL from aggregate process concurrency; application
correctness must not depend on it.

This plan addresses the failure observed during the TREC matter load:

- PostgreSQL `max_connections` is 100, with 3 superuser-reserved connections.
- Core's SQLAlchemy engine uses the implicit `QueuePool` defaults: pool size 5,
  overflow 10, and a 30-second checkout timeout.
- The search queue permits four concurrent operations, and each operation may
  create eight concurrent document-projection sessions. This can request at
  least 36 Core connections from one worker process before accounting for other
  workflows.
- Fifteen document-upsert operations failed at the local SQLAlchemy pool, and
  five more became DBOS `ERROR` workflows while remaining `QUEUED` in Core.
- The deployed/local worker is configured for direct PostgreSQL access rather
  than the available PgBouncer sidecar.
- A live snapshot showed 21 idle `dbos_transact` connections, one idle
  `dbos_transact_client` connection, six unnamed idle connections, and three
  unnamed connections idle in a transaction.

Reducing projection concurrency is an immediate recovery measure, not the
architectural fix.

## Phase 2 infrastructure connection classes

PgBouncer mode must be selected by connection behavior, not just by service.
The application remains unaware of PgBouncer: infrastructure supplies the
appropriate host, port, or database URL through existing configuration.

| Connection class | Users | Required endpoint | Reason |
| --- | --- | --- | --- |
| Short application transactions | Core API ORM, workflow ORM, Artifact API ORM, maintenance commands | PgBouncer transaction-mode listener | These connections should return a PostgreSQL backend after each committed or rolled-back transaction. |
| DBOS system connections | DBOS runtime and DBOS clients | PgBouncer session-mode listener, or direct PostgreSQL | DBOS uses session features including `LISTEN/NOTIFY`; transaction pooling is unsupported by DBOS. |
| Long-lived listeners | Core agent-event listener | PgBouncer session-mode listener, or direct PostgreSQL | PostgreSQL `LISTEN` state belongs to a session. |
| Schema administration | Core Alembic, Artifact Alembic, DBOS migrations | Direct PostgreSQL or the session-mode listener | DDL and migration locks must retain session identity and must not compete with request traffic. |

If the existing sidecar exposes only one listener, use **session mode** for the
first rollout. Do not route DBOS or the agent-event listener through a
transaction-mode-only listener. A later two-listener configuration can provide
better multiplexing for ordinary ORM traffic.

## Service inventory and target configuration

### Core API

Current state:

- `app.database.engine` uses direct PostgreSQL and implicit SQLAlchemy defaults.
- `DBOSClient` creates a separate system-database pool using the same URL unless
  `DBOS_SYSTEM_DATABASE_URL` is set.
- Each API process holds one long-lived agent-event `LISTEN` connection using
  the ordinary application URL.

Target state:

1. Give the Core ORM engine named settings for `pool_size`, `max_overflow`,
   `pool_timeout`, `pool_recycle`, and `application_name`.
2. Start with a bounded Core API client pool of 10 connections plus at most 2
   overflow connections per process. Validate this against actual API replica
   count before production rollout.
3. Configure the Core `DBOSClient` pool explicitly at 5 connections.
4. Label the ORM, DBOS client, and agent-event listener connections with
   distinct PostgreSQL `application_name` values.
5. Keep HTTP request transactions short; no response streaming or remote model,
   Artifact, S3, or OpenSearch calls may occur while a database transaction is
   open.
6. Verify all of the above against direct PostgreSQL before changing any
   deployment endpoint. In Phase 2, infrastructure routes these existing
   connections through a compatible PgBouncer listener.

### Workflow worker

Current state:

- The worker uses the same unbounded combination of workflow concurrency and
  implicit Core ORM pool limits as the API.
- DBOS creates its own system pool, which defaults to 20 connections.
- Search projection creates per-document sessions and can hold them while
  building projections and reading Artifact content.
- Some workflows also access the Artifact database through a separate implicit
  SQLAlchemy pool.

Target state:

1. Configure a worker ORM pool independently from the API pool. Start with 12
   connections and no overflow per worker process.
2. Configure DBOS `sys_db_pool_size` explicitly at 10 connections initially;
   DBOS recommends at least 5. Set DBOS polling concurrency below that budget.
3. Introduce one process-wide database-work semaphore. Per-queue and
   per-document concurrency must acquire from this shared budget rather than
   multiplying independently.
4. Reserve at least two worker ORM connections for control-plane work such as
   status transitions, cancellation, heartbeats, and error persistence.
5. Refactor search projection into three phases:
   - read and snapshot the required relational data in bounded batches;
   - commit/close the database session;
   - perform Artifact reads and projection construction concurrently, then use
     short database transactions only when more authoritative state is needed.
6. Reconcile Core operation state with terminal DBOS state so a durable `ERROR`
   workflow cannot remain indefinitely `QUEUED` in Core.
7. Gate batch search projection on index coverage for its matter documents, or
   make the dependency explicit in the durable workflow graph.
8. Prove the worker completes the TREC load against direct PostgreSQL without
   pool timeouts. Phase 2 then routes the same code through PgBouncer.

The temporary recovery setting `SEARCH_PROJECTION_DOCUMENT_CONCURRENCY=2`
should remain until the shared budget and session-lifetime changes are proven.

### Artifact API and Artifact workflow access

Current state:

- `artifact_service.database.artifact_engine` has its own implicit SQLAlchemy
  pool and uses the same PostgreSQL host as Core.
- The split Artifact API and some workflow code may therefore create Artifact
  database pools in different processes.

Target state:

1. Add Artifact-specific pool settings rather than inheriting Core values.
2. Start with 8 connections plus 2 overflow connections per Artifact API
   process, then tune using measured upload and text-processing concurrency.
3. Give worker-side Artifact access its own smaller explicit budget; do not
   silently instantiate a second 15-connection default pool.
4. Ensure S3 uploads/downloads and text extraction occur outside database
   transactions. Persist state in short transactions before and after external
   I/O.
5. Verify the Artifact API and worker-side Artifact access against direct
   PostgreSQL before infrastructure routes `ARTIFACT_DATABASE_URL` through
   PgBouncer.

### Classification and embedding services

The current standalone classification and embedding services do not construct
PostgreSQL engines. Keep them database-free. If either service later gains a
database, it must receive a separate connection class, URL, pool budget, and
metrics rather than reusing Core defaults.

### Migrations and maintenance commands

Core and Artifact Alembic already use `NullPool`, which is appropriate for
one-shot migrations. Route migrations through direct PostgreSQL or the
session-mode PgBouncer listener and serialize them at deployment time.

Maintenance scripts that import `SessionLocal` currently inherit the service
pool. Add a maintenance engine profile with a pool size of 1-2 and a distinct
`application_name`; do not let a one-shot script consume a full API or worker
pool budget.

## PostgreSQL and Phase 2 PgBouncer budget

Treat 97 connections as the absolute PostgreSQL ceiling and preserve at least
20 connections for administration, migrations, monitoring, failure recovery,
and temporary variance. The first production budget should therefore cap
ordinary service backends at no more than approximately 70-75 connections in
aggregate.

Before choosing final PgBouncer values, inventory the running sidecar with:

- `SHOW CONFIG` to confirm `pool_mode`, `max_client_conn`,
  `default_pool_size`, `reserve_pool_size`, `max_prepared_statements`, and
  timeouts;
- `SHOW POOLS` to inspect waiting clients and server allocation by
  database/user pair;
- `SHOW STATS`, `SHOW CLIENTS`, and `SHOW SERVERS` during a representative
  collection load and search projection;
- `pg_stat_activity` to identify connection counts, transaction age, and
  `application_name` at PostgreSQL.

Initial aggregate backend allocation for a single API/worker deployment:

| Purpose | Initial maximum PostgreSQL backends |
| --- | ---: |
| Core and worker short transactions | 24 |
| Artifact short transactions | 12 |
| DBOS runtime and clients, session mode | 16 |
| Long-lived listeners | 2 |
| Migrations and maintenance | 4 |
| Operational reserve | 20+ |

These are deployment-level limits, not per-replica limits. PgBouncer pools are
created per database/user pair, so replica count and the Core/Artifact database
split must be included in the calculation.

For a transaction-mode listener, confirm that the installed PgBouncer version
supports protocol-level prepared-statement tracking and that
`max_prepared_statements` is nonzero, or explicitly disable client-side server
preparation in psycopg. Test migrations that change result types and use
PgBouncer `RECONNECT` as part of the migration runbook when cached plans must be
discarded.

## Implementation phases

### Phase 0: recover and establish a direct-PostgreSQL baseline

1. Keep search projection document concurrency at 2.
2. Rebuild the incomplete matter index only after the worker is running with
   the reduced setting.
3. Capture PostgreSQL connection counts, queue concurrency, throughput, local
   pool wait time, transaction duration, and idle-in-transaction counts under
   the TREC load.

### Phase 1: fix application pooling and connection lifetimes

1. Create a shared SQLAlchemy engine factory supporting named service profiles.
2. Add validated settings for pool size, overflow, checkout timeout, recycle
   interval, and application name. Continue using the existing database URL
   interfaces so PgBouncer remains transparent to application code.
3. Add corresponding Artifact settings.
4. Add DBOS settings for system pool size and polling concurrency and pass them
   to both `DBOS` and `DBOSClient`.
5. Instrument SQLAlchemy checkout wait, checked-out count, overflow use,
   transaction duration, and connection invalidation.
6. Remove database sessions from remote-I/O scopes in search projection,
   classification, embedding, Artifact, and model-provider workflows.
7. Replace multiplicative per-queue concurrency with the shared worker database
   budget.
8. Bulk-load relational projection inputs where practical instead of opening a
   session per document.
9. Add transaction deadlines and detect sessions idle in a transaction.
10. Make durable workflow/Core status reconciliation periodic and idempotent.
11. Pass the automated and TREC load acceptance tests against direct PostgreSQL.

### Phase 2: deploy PgBouncer transparently

1. Inventory the sidecar with `SHOW CONFIG`, `SHOW POOLS`, `SHOW STATS`,
   `SHOW CLIENTS`, and `SHOW SERVERS`.
2. Record the PgBouncer version, host, ports, pool modes, authentication method,
   prepared-statement handling, and per-database/user limits.
3. Configure session-compatible routing for DBOS and `LISTEN/NOTIFY`. If
   infrastructure exposes a transaction-mode listener for ordinary ORM traffic,
   keep DBOS and listeners on a session-mode listener or direct PostgreSQL.
4. Export PgBouncer client/server/waiting metrics alongside PostgreSQL
   connection and transaction-age metrics.

Then roll out one connection class at a time by changing deployment environment
or service discovery, without changing application behavior:

1. Artifact API ordinary transactions.
2. Core API ordinary transactions.
3. Workflow worker ordinary transactions.
4. DBOS runtime and clients through the session listener.
5. Agent-event listener through the session listener.
6. Migration and maintenance profiles through the session listener or direct
   endpoint.

For each step, verify login, migrations, transaction rollback, DBOS enqueue and
dequeue, workflow recovery, cancellation, agent-event notifications, and
Artifact writes before continuing.

### Phase 3: verify pooled operation and failure recovery

1. Repeat the direct-PostgreSQL acceptance workload through PgBouncer.
2. Compare throughput, pool wait, transaction duration, and error rates with the
   direct baseline.
3. Restart PgBouncer and PostgreSQL independently and verify reconnect,
   rollback, durable workflow recovery, and accurate Core status.
4. Verify DBOS and the agent-event listener retain session behavior.
5. Verify prepared statements and post-migration cached-plan handling.

### Phase 4: restore throughput deliberately

Increase document concurrency in small steps while observing pool wait time and
PgBouncer waiting clients. Stop increasing concurrency when database wait rises
without a corresponding throughput gain. The target is maximum sustained
documents per second with zero pool timeouts, not maximum thread count.

## Verification

### Automated tests

1. Unit-test every engine profile and reject impossible pool/concurrency
   combinations at startup.
2. Integration-test Core, Artifact, DBOS, and the agent listener against the
   intended PgBouncer mode.
3. Verify DBOS and `LISTEN/NOTIFY` fail configuration validation when pointed at
   a transaction-only endpoint.
4. Run four simultaneous search operations plus a matter import and confirm no
   SQLAlchemy checkout timeout occurs.
5. Interrupt PgBouncer and PostgreSQL separately and verify reconnection,
   rollback, workflow recovery, and accurate Core terminal status.
6. Assert that no completed or errored DBOS workflow remains `QUEUED` or
   `RUNNING` in Core after reconciliation.

### Load acceptance test

Use the 20,365-document TREC matter as the first repeatable test:

- publish all documents;
- project the matter index to exactly 20,365 documents;
- create and project the full review batch;
- import Qrels reviewer coding;
- repeat with concurrent API reads and one Artifact upload stream.

Acceptance criteria:

- zero `QueuePool` timeouts and zero PostgreSQL `too many connections` errors;
- zero PgBouncer clients waiting longer than the agreed checkout SLO;
- no session idle in a transaction for more than 30 seconds;
- PostgreSQL connections remain below 80% of the usable ceiling;
- Core, DBOS, and OpenSearch counts reconcile at completion;
- failed workflows become retryable terminal Core records automatically;
- throughput is measured and does not regress by more than 10% from the best
  stable baseline without an explicit reliability tradeoff.

## Deliverables

1. Central SQLAlchemy engine factory and per-service settings.
2. Explicit DBOS runtime/client pool configuration.
3. Worker-wide database concurrency budget.
4. Search-projection session-lifetime refactor.
5. Durable workflow/Core reconciliation repair.
6. Direct-PostgreSQL pooling metrics and repeatable TREC load test.
7. Phase 2 PgBouncer configuration and transparent deployment wiring.
8. PgBouncer, PostgreSQL, and transaction-duration dashboards and alerts.
9. Pooled-operation acceptance test and operational runbook.

## References

- [DBOS production checklist](https://docs.dbos.dev/production/checklist)
- [DBOS pooling FAQ](https://docs.dbos.dev/faq#can-i-use-dbos-with-an-external-postgres-connection-pooler)
- [PgBouncer configuration](https://www.pgbouncer.org/config)
- [PgBouncer pooling feature matrix](https://www.pgbouncer.org/features.html)
