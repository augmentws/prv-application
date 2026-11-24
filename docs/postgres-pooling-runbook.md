# Direct PostgreSQL Pooling Runbook

This runbook covers the application-side pooling design. It deliberately does
not configure or require PgBouncer.

## Process budgets

| Process | Core ORM pool | Overflow | DBOS pool | Notes |
| --- | ---: | ---: | ---: | --- |
| Core API | 10 | 2 | DBOS client 5 | `DATABASE_POOL_PROFILE=api` |
| Workflow worker | 12 | 0 | runtime 10, client 5 | Ten data-plane slots leave two ORM connections for control work. |
| Artifact API | 8 | 2 | none | Separate Artifact database and settings. |
| Maintenance command | 1-2 | 0 | none | Use a dedicated maintenance profile or the pool-health script. |

The worker entry point selects its worker profile before importing the shared
database engine. For a local foreground worker, the operator command remains:

```bash
pipenv run python -m app.workflow_worker
```

Use `WORKER_DATABASE_POOL_SIZE` to override the default persistent worker pool;
worker overflow is always forced to zero.

Starting or restarting the worker remains an operator action.

## Runtime protection

- PostgreSQL connections are labeled with a process-specific
  `application_name`.
- SQL statements have a default 120-second server deadline.
- Transactions idle for 30 seconds are terminated by PostgreSQL.
- Connections recycle after 30 minutes and are pre-pinged before reuse.
- Provider, Artifact, S3, and OpenSearch waits occur after committing the
  relational snapshot needed for that call.
- High-fan-out worker document operations share one data-plane semaphore. Capacity contention waits and emits
  diagnostics; it does not consume a durable workflow retry. Q&A releases its lease around Artifact and provider
  calls, then reacquires it before persisting results.
- A scheduled DBOS reconciliation runs every minute and terminalizes Core jobs
  when their durable workflow terminated outside the workflow body.

`GET /health` on Core and Artifact includes the local pool counters: checkout
count, checkout wait, slow checkout count, invalidations, checked-out count,
and overflow use.

## Inspect direct PostgreSQL

Capture one snapshot:

```bash
pipenv run python -m scripts.check_postgres_pool_health
```

Monitor a 30-minute load and fail the command if the direct-PostgreSQL
acceptance thresholds are exceeded:

```bash
pipenv run python -m scripts.check_postgres_pool_health \
  --watch-seconds 1800 \
  --interval-seconds 5 \
  --max-utilization 0.8 \
  --max-idle-transaction-seconds 30
```

Every output line is JSON and includes `pg_stat_activity` grouped by
`application_name` and state.

## Acceptance workload

Run the monitor while performing the repeatable TREC workload:

1. Publish all 20,365 documents.
2. Project the matter index to exactly 20,365 documents.
3. Create and project the full review batch.
4. Import the Qrels reviewer coding.
5. Repeat with concurrent API reads and one Artifact upload stream.

Accept the configuration only when:

- there are no SQLAlchemy checkout timeouts or PostgreSQL
  `too many clients` errors;
- no transaction is idle for more than 30 seconds;
- total client connections stay below 80% of usable PostgreSQL connections;
- no Core job remains active after its DBOS workflow is terminal;
- Core, DBOS, Artifact, and OpenSearch counts reconcile; and
- throughput is within 10% of the best stable baseline.

Do not increase document concurrency to address pool waits. Increase it only
when the monitor remains clean and the change improves sustained throughput.
