# Unified Job Execution Platform Implementation Plan

## 1. Purpose

Priv-View already runs many durable background operations, but their execution
state is represented in several different ways:

- Agent, JEV, assessment, and some Analysis Task operations use `WorkflowRun`
  and sometimes `WorkflowStepRun`.
- Embedding, matter publication, topic processing, bulk tagging,
  classification training, classification scoring, search projection, and
  Classification Analysis Task runs each persist their own workflow ID,
  statuses, counters, cancellation fields, and errors.
- Artifact-owned collection processing and deletion use Artifact database
  records even though Core dispatches their DBOS workflows.
- Some pages support cancellation and detailed progress while other pages can
  report only a domain status or a worker error.

This plan normalizes the execution control plane without removing the domain
records that describe what each operation means.

The result should give every asynchronous operation the same dependable
answers to these questions:

- Is it queued, running, waiting, completed, failed, or canceled?
- What stage is running and how much work is complete?
- Which execution attempt is this?
- Is cancellation requested, and has the worker acknowledged it?
- Is the worker still alive?
- Can the operation be retried safely?
- What failed, is the failure retriable, and where are its detailed outcomes?

### Current implementation status

The first vertical slices are implemented through migration `0075`:

- `WorkflowRun` contains the common attempt, progress, heartbeat,
  cancellation, and structured-failure fields.
- The code-owned adapter registry and transition service are active.
- Classification Analysis Task runs create a common execution before DBOS
  dispatch and project its state into their compatibility fields.
- JEV/decision-engine batch Analysis Task runs use the same common identity,
  progress, history, and failure contract.
- Matter embedding jobs create and backfill common execution attempts; their
  planning, provider wait, batch processing, search indexing, reconciliation,
  failure, and cancellation lifecycle is projected through the shared service.
- Matter document-import/publishing jobs create and backfill common execution
  attempts; frozen-selection planning, batch publication, imported-label
  counters, reconciliation, failure, and cancellation use the shared service.
- Interactive review-batch population creates and backfills common execution
  attempts. Membership resolution/materialization and the initial search
  projection expose shared stages, document progress, cancellation, and safe
  retry history. Assessment-created batches remain owned by their assessment
  execution rather than receiving a second competing job.
- Classification training jobs own one common top-level execution across
  dataset snapshotting, feature/split preparation, and the nested immutable
  model build. The training run remains a pinned domain child, while the
  parent exposes common cancellation, safe retry, stages, and attempt history.
- Classification scoring runs now create and backfill a common execution,
  expose stage/attempt data beside the immutable prediction artifacts, and
  retain their existing durable cancellation path.
- Classifier-to-batch projection runs now expose document progress and common
  execution history while preserving `ReviewBatchRun` and its isolated values
  as the domain output.
- Human and JEV coding promotions now expose a common attempt for eligibility
  resolution, chunked publication, search reindexing, failure, and retry
  history. Their metadata events remain the authoritative, idempotent output.
- Common execution history, cancel, and retry APIs are available.
- The Analysis Task UI displays the common stage, progress, heartbeat,
  attempt number, cancellation, and safe retry action.
- The matter Jobs UI displays common embedding and document-import stage,
  progress, and attempt data while retaining provider usage, imported-label
  counts, and domain batch details.
- Existing Classification Analysis Task, matter embedding, document-import,
  interactive review-batch, classification-training, scoring, classifier
  projection, and coding-promotion jobs are backfilled as attempt 1.

The remaining phases are intentionally still incremental. The embedding and
review-batch outcome-manifest follow-ups and Artifact-owned jobs have not yet
been migrated to the common adapter service. Failed classifier projections can
be safely retried because each attempt clears and rebuilds only its isolated
run values. Generic cancellation remains disabled for projection and coding
promotion until their interruption semantics are explicitly bounded;
promotion can create a new common attempt through its existing idempotent
failed-promotion retry path.

## 2. Architectural decisions

### 2.1 Keep three separate concerns

```text
Domain job or run
  describes the business operation, frozen inputs, outputs, and governance
            |
            | one logical job has one or more attempts
            v
WorkflowRun
  common execution attempt, DBOS identity, progress, cancel, retry, heartbeat
            |
            +---- WorkflowStepRun (durable stages/fan-out summaries)
            |
            +---- JobOutcomeManifest (optional item-level diagnostic artifact)
```

The layers are intentionally different:

1. The domain record answers **what the user requested** and retains frozen
   business configuration and authoritative results.
2. `WorkflowRun` answers **what is happening while an attempt executes**.
3. `JobOutcomeManifest` answers **what happened to each item** when complete
   item-level diagnostics are useful.

Do not replace all domain job tables with a generic table. Embedding jobs,
classification scoring runs, Analysis Task runs, imports, topic jobs, and
other domains have real relationships and invariants that do not belong in
generic JSON.

### 2.2 Evolve `WorkflowRun`; do not add a competing execution table

`WorkflowRun` is already the common execution record for agent and JEV work.
Extend it into the platform execution-attempt record rather than adding a new
`job_execution` table.

During migration, existing domain status and workflow-ID columns remain as
compatibility projections. New code must mutate lifecycle state through one
shared execution service so the domain projection and `WorkflowRun` cannot
silently diverge.

### 2.3 One `WorkflowRun` per attempt

A logical domain job remains stable across a retry. Every actual attempt gets
a new `WorkflowRun` and a new DBOS workflow ID.

This preserves:

- The failure and measurements from earlier attempts.
- An explicit retry chain.
- Exactly one current attempt for the logical job.
- Idempotency of the domain operation independently from DBOS execution
  identity.

A user-requested rerun with changed scope, frozen inputs, configuration, or
model version is a new domain job, not a retry attempt.

### 2.4 Separate execution status from business status

The common execution lifecycle is:

```text
QUEUED
RUNNING
WAITING
COMPLETED
COMPLETED_WITH_ERRORS
FAILED
CANCELED
```

`WAITING` means the attempt is durably paused for an external event, provider,
approval, or another workflow. It is not a substitute for a domain state.

Domain states remain where they carry business meaning. For example,
`MatterTopicJob.AWAITING_REVIEW` describes a review and approval boundary. Its
current execution attempt may already be `COMPLETED`; a later publish action
may create a new attempt. Likewise, `APPROVED`, `ACTIVATED`, `PUBLISHED`, and
`RETIRED` are governance states, not worker statuses.

### 2.5 DBOS remains the durable orchestration engine

The database execution record does not replace DBOS. DBOS provides durable
workflow execution and recovery. `WorkflowRun` is the application-facing
control plane used for authorization, status, progress, cancellation, retry,
auditing, and UI reporting.

Every adopted asynchronous operation must have both:

- A persisted `WorkflowRun` committed before dispatch.
- A unique DBOS workflow ID owned by that execution attempt.

The HTTP request queues work and returns the persisted domain job plus current
execution immediately. It must not wait for the workflow to finish.

### 2.6 Respect service ownership

Core-owned operations use Core `WorkflowRun` rows. Artifact-owned collection
processing and deletion keep their execution records in the Artifact database
and expose the same response contract through the Artifact boundary. Core must
not create a duplicate shadow execution row for an Artifact-owned job.

The reusable lifecycle types and transition rules may live in a neutral module,
but each service owns and reconciles its own database records.

## 3. Goals and non-goals

### Goals

- One execution lifecycle and transition service for Core jobs.
- One cancellation and retry contract.
- Attempt history instead of overwriting the last failure.
- Bounded progress, stage, heartbeat, and sanitized failure reporting.
- Consistent APIs and reusable UI components.
- Preservation of domain-specific configuration, results, governance, and
  authorization.
- Compatibility with embedded and separately deployed Artifact,
  Classification, and Embedding services.
- Optional integration with the item-level outcome-manifest platform.

### Non-goals

- Replacing DBOS with a home-grown queue.
- Storing one PostgreSQL execution or step row per document.
- Replacing domain job tables with generic JSON.
- Copying Artifact-owned jobs into Core.
- Treating logs as authoritative job state.
- Automatically retrying non-idempotent work without a domain adapter.
- Making every synchronous API operation a job.
- Shipping arbitrary workflow types configured by tenant data.

## 4. Current-state inventory

The first implementation pass must produce a checked-in inventory of every
asynchronous entry point, its domain record, DBOS workflow, cancellation
behavior, and UI surface. The known families are:

| Domain | Current durable record | Current orchestration | Important domain state | Outcome manifest |
| --- | --- | --- | --- | --- |
| Agent/JEV execution | `WorkflowRun`, `WorkflowStepRun`, `SkillRun`, `AgentRun` | DBOS | approvals and deferred tools | useful for document fan-out |
| Analysis Task compilation/playground | `WorkflowRun` plus task version fields | DBOS | draft/published specification | usually not needed |
| JEV batch analysis | `WorkflowRun`, `ReviewBatchRun` | DBOS | isolated batch result | useful for document results |
| Classification Analysis Task | `MatterAnalysisTaskRun`, `ClassificationScoringRun` | DBOS | isolated analysis result | scoring artifact already covers items |
| Matter embeddings | `MatterEmbeddingJob` and batches | DBOS | completed searchable embedding set | first generic manifest adopter |
| Publish documents to matter | `MatterDocumentImportJob` and batches | DBOS | links, imported labels, custodians | useful |
| Topic discovery/assignment | `MatterTopicJob` | DBOS | `AWAITING_REVIEW` | useful during assignment |
| Bulk coding/tagging | `MatterBulkTagJob` and batches | DBOS | coding changes | useful |
| Review-batch materialization | `ReviewBatch` and related runs | DBOS | frozen population | useful for population failures |
| Search projection | `SearchProjectionOperation` | DBOS | index generation/projection result | useful where item failures exist |
| Classification preparation/build | `ClassificationTrainingJob`, `ClassificationTrainingRun` | DBOS | candidate model version | stages; item diagnostics only for preparation |
| Classification scoring | `ClassificationScoringRun` | DBOS | immutable prediction artifact | reuse prediction artifact |
| Classifier/JEV coding projection | projection and search-coding run records | DBOS | isolated searchable coding | useful only if projection has row failures |
| Coding promotion | promotion records | DBOS | audited coding events | useful |
| Matter Definition assessment | assessment run plus `WorkflowRun` | DBOS | clarification/review boundaries | useful for selected population |
| Collection text processing | Artifact-owned processing run | DBOS | active normalized-text version | Artifact-owned contract |
| Collection deletion | Artifact-owned deletion job | DBOS | deletion lifecycle | Artifact-owned contract |

This inventory is a required deliverable rather than an informal note. It will
prevent a second execution abstraction from appearing during the migration.

## 5. Core data model

### 5.1 Extend `workflow_run`

Add the following columns while preserving the existing snapshots and usage
counters:

```text
workflow_run
  id                         UUID primary key
  tenant_id                  UUID not null
  client_id                  UUID nullable
  matter_id                  UUID nullable

  job_type                   VARCHAR(80) nullable during migration
  job_id                     UUID nullable during migration
  attempt_number             INTEGER not null default 1
  retry_of_workflow_run_id   UUID nullable -> workflow_run.id

  workflow_key               VARCHAR(100) not null
  code_version               VARCHAR(100) not null
  dbos_workflow_id           VARCHAR(255) unique not null
  idempotency_key            VARCHAR(500) nullable

  status                     VARCHAR(30) not null
  current_stage              VARCHAR(100) nullable
  progress_completed         BIGINT not null default 0
  progress_total             BIGINT nullable
  progress                   JSON not null

  cancel_requested_at        TIMESTAMPTZ nullable
  cancel_requested_by_id     UUID nullable
  last_heartbeat_at          TIMESTAMPTZ nullable

  failure_code               VARCHAR(100) nullable
  failure_summary            TEXT nullable
  failure_retriable          BOOLEAN not null default false

  initiated_by_user_id       UUID not null
  started_at                 TIMESTAMPTZ nullable
  completed_at               TIMESTAMPTZ nullable
  created_at                 TIMESTAMPTZ not null
  updated_at                 TIMESTAMPTZ not null
```

Retain `input_snapshot`, `binding_snapshot`, `configuration_snapshot`, token
usage, request counts, and tool-call counts.

Constraints and indexes:

- `attempt_number > 0`.
- `progress_completed >= 0`.
- `progress_total IS NULL OR progress_total >= 0`.
- Completed progress may be less than the total only for failed or canceled
  attempts.
- Unique `(job_type, job_id, attempt_number)` when the polymorphic job
  reference is present.
- `retry_of_workflow_run_id` must reference an earlier attempt for the same
  registered job.
- Index `(job_type, job_id, attempt_number DESC)`.
- Index `(matter_id, status, created_at DESC)` for a unified matter-jobs feed.
- Index active heartbeats by `status` and `last_heartbeat_at`.

`progress` remains available for bounded job-specific details. The scalar
columns are authoritative for generic rendering and queries.

### 5.2 Link domain records to the current attempt

Add nullable `current_workflow_run_id` foreign keys to Core domain job/run
tables as each adapter migrates. The pointer is the current attempt for that
logical job. Attempt history is resolved by `(job_type, job_id)`.

Keep existing domain `workflow_id`, status, stage, progress, error, and
cancellation columns during compatibility phases. They become projections
written by the shared execution service; endpoints must not update them
directly.

After an adapter has completed dual-read verification, remove redundant fields
only when doing so does not erase a domain concept. Business statuses remain.

### 5.3 Use `WorkflowStepRun` only for meaningful stages

`WorkflowStepRun` represents durable stages or bounded fan-out groups, not one
row per document. Extend it, if needed, with:

- A stable `step_key` separate from its display label.
- `progress_completed` and nullable `progress_total` as `BIGINT`.
- `last_heartbeat_at`.
- Sanitized structured failure fields matching `WorkflowRun`.

The adapter declares its allowed stages in code. Workers may upsert a step by
stable key, but tenant input cannot invent executable stage definitions.

### 5.4 Do not use database foreign keys for polymorphic domain references

`(job_type, job_id)` points to multiple domain tables, so it cannot use one
foreign key. A code-owned registry validates every type and resolves its
domain record, scope, permissions, supported actions, and display behavior.

Unknown job types fail closed. Add an integrity scanner that detects execution
rows whose registered domain record does not resolve or has a scope mismatch.

## 6. Job adapter registry

Add a code-owned `JobExecutionAdapter` protocol. Each registered job type
provides:

```text
job_type
load(job_id)
resolve_scope(job)
display_name(job)
required_read_permission(job)
required_cancel_permission(job)
required_retry_permission(job)
current_execution_id(job)
set_current_execution(job, workflow_run_id)
project_execution_state(job, workflow_run)
build_retry_snapshot(job, failed_attempt)
supported_actions(job, workflow_run)
stage_catalog
outcome_adapter (optional)
```

The registry must also declare:

- Whether the logical job is retryable.
- Which domain states permit a new attempt.
- Whether cancellation is cooperative, remote-provider, or both.
- Whether child workflows must be canceled.
- Whether the operation owns item-level outcomes.
- How a domain completion is reconciled after worker or network loss.

Adapters contain lifecycle translation only. Domain services remain
responsible for domain validation and results.

## 7. Shared execution service

Create a Core execution module responsible for all lifecycle mutations.

### 7.1 Creation and dispatch

The command transaction:

1. Authorizes the initiating user for the domain action.
2. Creates the logical domain job with frozen configuration.
3. Creates `WorkflowRun(status=QUEUED, attempt_number=1)`.
4. Sets the domain job's `current_workflow_run_id`.
5. Appends the domain audit event.
6. Commits.
7. Dispatches the DBOS workflow using the persisted unique workflow ID.

If dispatch fails after commit, the run remains `QUEUED` with a dispatch
failure that a dispatcher/reconciler can retry. Do not delete the job or create
another logical job.

### 7.2 Transitions

Expose controlled methods such as:

```text
mark_started
set_stage
report_progress
heartbeat
request_cancel
mark_canceled
mark_completed
mark_completed_with_errors
mark_failed
create_retry_attempt
reconcile
```

Each method:

- Locks the current execution row where necessary.
- Validates a code-owned state transition.
- Rejects updates from superseded attempts.
- Updates the domain compatibility projection in the same transaction.
- Uses monotonic progress within a stage.
- Sanitizes and bounds user-visible failures.
- Is idempotent for repeated DBOS step delivery.

No router or workflow should assign lifecycle fields directly after its job
type is migrated.

### 7.3 Progress contract

Generic progress contains:

- Stable stage key and human-readable stage label.
- Completed and total work when the total is known.
- Optional unit such as `documents`, `batches`, `steps`, or `requests`.
- Optional bounded stage detail.
- Last heartbeat time.

Do not report invented percentages. If the total is unknown, show an
indeterminate stage plus completed count. Update durable progress at useful
batch or stage boundaries, not for every document.

### 7.4 Failure contract

Every terminal failure records:

- Stable namespaced `failure_code`.
- Sanitized `failure_summary` suitable for the UI.
- `failure_retriable` determined by code, not by the browser.
- The stage that failed.
- Detailed server logs correlated by job, execution, DBOS workflow, and step
  IDs.

Do not expose stack traces, document contents, credentials, SQL parameters, or
provider secrets in the generic response.

## 8. Cancellation

Cancellation is a durable request and an eventual worker acknowledgement.

### API behavior

1. Reauthorize the caller through the job adapter.
2. Lock the logical job and current execution.
3. If terminal, return its current state idempotently.
4. Set `cancel_requested_at` and `cancel_requested_by_id`.
5. Append an audit record in the same transaction.
6. Commit.
7. Request DBOS cancellation and any adapter-declared remote-provider cancel.

The immediate response says `cancel_requested`; it must not claim `CANCELED`
until the workflow acknowledges the request or reconciliation proves no work
can continue.

### Worker behavior

- Check cancellation before expensive stages and at every bounded batch
  boundary.
- Stop creating child work after cancellation is requested.
- Cancel or drain child workflows according to the adapter policy.
- Preserve outputs that were already committed.
- Finalize item outcomes for processed and unstarted items where the outcome
  contract requires complete population coverage.
- Mark the execution `CANCELED` and update the domain projection.

Cancellation must allow API and worker shutdown to complete. A long-running
classification or embedding call must use bounded work or a cancellable remote
request; it cannot hold application shutdown indefinitely.

## 9. Retry and reconciliation

### 9.1 Retry

Retry creates a new execution attempt; it never resets the old row.

1. Reauthorize the caller.
2. Confirm that the adapter and failure permit retry.
3. Lock the logical job and confirm the failed/canceled attempt is still
   current.
4. Freeze a retry snapshot from the same logical inputs.
5. Create a new `WorkflowRun` with the next attempt number and
   `retry_of_workflow_run_id`.
6. Move the domain current-attempt pointer.
7. Append an audit record.
8. Commit and dispatch with a new DBOS workflow ID.

DBOS steps and domain derivation keys remain idempotent. A retry should reuse
already verified immutable outputs when the domain contract allows it.

### 9.2 Reconciliation

Add a periodic reconciler and startup-safe reconciliation command that:

- Redelivers committed `QUEUED` attempts that were not dispatched.
- Resolves DBOS state for stale `RUNNING` attempts.
- Verifies authoritative domain/artifact outputs before marking completion.
- Finishes cancellation that DBOS has acknowledged.
- Detects a missing or stopped worker through heartbeats without immediately
  declaring a logical failure.
- Never creates a second domain result when a lost response can be reconciled
  by derivation or artifact identity.

Use explicit time thresholds in settings. A stale heartbeat means
`attention_required`, not automatically `FAILED`; DBOS or domain output may
still prove the attempt is recoverable.

## 10. Item-level outcomes

The execution framework and outcome-manifest platform integrate but neither
depends on the other.

- `WorkflowRun` stores aggregate execution progress and attempt identity.
- `WorkflowStepRun` stores stage/fan-out summaries.
- `JobOutcomeManifest` stores one logical job's final item-level diagnostic
  view in Artifact-owned Parquet.
- Outcome fragments record the producing `WorkflowRun.id`, so retries retain
  provenance.
- The final logical-job manifest may reconcile reusable fragments across
  attempts.
- Existing complete domain artifacts, such as classification prediction
  Parquet, should be adapted rather than duplicated.

See [Reusable Job Outcome Manifest Platform Plan](job-outcome-manifest-platform-plan.md).

## 11. API contract

### 11.1 Shared response

Add common OpenAPI schemas:

```text
JobExecutionRead
  id
  job_type
  job_id
  attempt_number
  status
  current_stage
  stage_label
  progress_completed
  progress_total
  progress_unit
  cancel_requested_at
  last_heartbeat_at
  failure_code
  failure_summary
  failure_retriable
  initiated_by
  started_at
  completed_at
  created_at
  available_actions

JobExecutionHistoryRead
  current_execution
  attempts[]
```

`available_actions` is advisory for rendering. Every mutation is still
authorized by the backend at execution time.

### 11.2 Endpoints

Preserve existing domain routes. Add shared execution routes that resolve the
job through the adapter:

```text
GET  /v1/jobs/{job_type}/{job_id}/execution
POST /v1/jobs/{job_type}/{job_id}/cancel
POST /v1/jobs/{job_type}/{job_id}/retry
```

Add a matter-scoped feed for the UI:

```text
GET /v1/matters/{matter_id}/jobs
```

The global-looking job endpoints are safe only because the adapter resolves
the authoritative domain job and scope before authorization. Never authorize
from the supplied job type, ID, tenant ID, or matter ID alone.

During migration, existing cancel/retry endpoints call the shared service and
return their existing response shape plus the common execution object. They
remain compatibility aliases until generated clients and UI consumers have
moved.

### 11.3 Eventual live updates

Polling the common read contract is sufficient for the first release. The
shape should permit a later server-sent-event channel keyed by execution ID.
Do not make SSE or WebSockets a prerequisite for normalization.

## 12. UI design

### 12.1 Shared components

Build reusable components for:

- Status and cancel-requested pills.
- Stage label and determinate/indeterminate progress.
- Started, updated, heartbeat, and elapsed time.
- Sanitized failure details and retriable indicator.
- Cancel and retry actions with confirmation where required.
- Attempt-history drawer.
- Link to item outcome diagnostics when available.

Domain pages continue to show domain configuration and outputs. The common
component replaces bespoke lifecycle controls, not the page itself.

### 12.2 Matter Jobs view

The matter Jobs view becomes the consolidated operational view for Core-owned
matter operations. It shows the most recent jobs across registered types with
filters for type, status, date, and initiating user. Opening a row routes to
the domain page or a generic detail drawer according to its adapter.

Do not mix Artifact-owned collection jobs into the matter feed unless they
have an explicit matter relationship. Collection pages use the same visual
contract with data served by Artifact.

### 12.3 Honest actions

- Show Cancel only when the current attempt is cancellable.
- After cancel is requested, disable repeat mutation and show
  `Cancellation requested` until acknowledged.
- Show Retry only for terminal attempts whose adapter marks the failure
  retriable.
- Do not offer Restart for an active attempt.
- Do not imply that retry changes the job's frozen inputs.

## 13. Authorization and audit

Each adapter maps shared actions to the domain's normal permissions. There is
no platform-wide `job.admin` shortcut that bypasses domain authorization.

Required audit events:

- Job created and first attempt queued.
- Cancellation requested.
- Retry requested and new attempt created.
- Administrative reconciliation that changes terminal state.
- Manual recovery or force-fail action, if one is added later.
- Outcome export where required by policy.

Audit details include domain job ID, execution ID, attempt number, scope,
before/after state, and actor. Security changes and job mutations continue to
use the existing tenant audit infrastructure.

## 14. Migration strategy

Use an adapter-by-adapter strangler migration. Do not convert all workflows in
one release.

### Step A: Additive schema

- Add the new nullable `WorkflowRun` fields and indexes.
- Add current-execution pointers only to the first adopter tables.
- Keep every existing route and domain column.
- Add the registry and transition service with architecture tests.

### Step B: Dual write

- Create a `WorkflowRun` for every new job of the adopter type.
- Route all lifecycle changes through the shared service.
- Project state into legacy domain columns in the same transaction.
- Compare common and legacy responses in tests and operational metrics.

### Step C: Switch reads

- Return the common execution object from the domain endpoint.
- Move the UI to shared components.
- Keep legacy fields populated for older clients.

### Step D: Backfill

- Backfill historical domain jobs as synthetic single attempts using their
  recorded timestamps, status, workflow ID, counters, and sanitized error.
- Mark backfilled executions in configuration/provenance.
- Do not enqueue or rerun historical jobs.
- Where history cannot distinguish attempts, create one legacy attempt and do
  not fabricate retry history.
- Perform large backfills in bounded batches with a dry-run/reconciliation
  report.

### Step E: Enforce and clean up

- Make the current-execution pointer required for newly created asynchronous
  jobs of the migrated type.
- Prevent direct lifecycle writes outside the shared module with tests.
- Remove redundant domain execution fields only in a deliberate later
  migration and only after all generated clients have moved.

## 15. Phased implementation

### Phase 0: Inventory and contract

Deliver:

- Checked-in asynchronous-operation inventory.
- Final status, transition, progress, failure, cancellation, retry, and
  heartbeat contracts.
- `JobExecutionAdapter` interface and code-owned registry.
- Decision table separating execution state from business state for every job
  family.
- Explicit service-ownership map for Core and Artifact jobs.

Exit when every current DBOS entry point has an owner, domain record, proposed
job type, permissions, and migration wave.

### Phase 1: Core execution foundation

Deliver:

- Additive `WorkflowRun` and `WorkflowStepRun` migration.
- Shared transition and projection service.
- Attempt creation and retry-chain support.
- Cancellation-request and heartbeat support.
- Reconciler skeleton and stale-run metrics.
- Shared read, cancel, and retry API contracts.
- Audit integration and negative authorization tests.

Exit when a fixture domain adapter can create, execute, cancel, fail, retry,
complete, and reconcile without direct lifecycle assignments.

### Phase 2: Normalize Analysis Tasks first

Deliver:

- Register JEV and Classification Analysis Task job types.
- Give `MatterAnalysisTaskRun` a current `WorkflowRun` attempt.
- Route classification population snapshotting, feature preparation, scoring,
  and finalization through common stages.
- Retain the nested `ClassificationScoringRun` as a domain result/dependency,
  not a competing top-level UI status.
- Use the same cancel, progress, heartbeat, retry, and attempt-history UI for
  JEV and classification runs.
- Preserve JEV batch result and classification prediction artifacts.

Exit when whole-matter classification has visible bounded progress, survives
worker restart, can be canceled, and can be retried without changing its
frozen analysis-run scope.

### Phase 3: Embeddings

Deliver:

- Register matter embedding jobs.
- Replace bespoke execution controls with the shared service.
- Preserve provider batch status as domain/provider detail.
- Add cooperative cancellation at every bounded batch boundary.
- Integrate the embedding outcome-manifest vertical slice from the separate
  outcome plan.
- Link common job details to Generated/Reused/Skipped/Failed outcomes.

Exit when the embedding UI and API no longer need custom lifecycle logic and
every new run has durable cancellation and heartbeat behavior.

### Phase 4: Matter publication and batch materialization

Deliver:

- Register publish-documents-to-matter import jobs.
- Register review-batch population materialization.
- Preserve frozen Artifact selections and Core batch/domain records.
- Normalize progress, cancel, retry, and reconciliation across planner and
  child batches.
- Add outcome manifests where complete per-document import/materialization
  diagnostics are valuable.

Exit when planning, child batching, imported-label publication, and terminal
counts reconcile through one execution attempt.

### Phase 5: Classification pipeline

Deliver:

- Register training preparation, immutable model build, scoring, classifier
  projection, and coding promotion job types.
- Decide which nested operations are child executions versus domain result
  records and enforce that decision in adapters.
- Preserve derivation-key idempotency and immutable Artifact outputs.
- Adapt prediction artifacts for item outcomes instead of copying every
  prediction into a new manifest.
- Normalize approval-safe cancellation and retry behavior.

Exit when every classification operation has one unambiguous current
execution, attempt history, and authoritative domain result.

### Phase 6: Remaining Core workflows

Recommended order:

1. Bulk coding/tagging and promotion.
2. Topic discovery and assignment, preserving `AWAITING_REVIEW` as business
   state.
3. Search projection and index lifecycle operations.
4. Matter Definition assessment and guidance refinement.
5. Remaining agent workflows and deferred-tool continuations.

Exit for each adapter when bespoke cancel/retry/status code is removed or is a
thin compatibility call to the shared service.

### Phase 7: Artifact-owned jobs

Deliver:

- Neutral lifecycle schema shared at the API/type level.
- Artifact-owned execution-attempt persistence for collection text processing
  and deletion.
- Artifact cancellation, retry, heartbeat, and reconciliation endpoints.
- Shared UI components consuming either Core or Artifact responses.
- No duplicate Core shadow rows.

Exit when collection job pages provide the same operator experience without
weakening Artifact database ownership.

### Phase 8: Unified operational UI and hardening

Deliver:

- Consolidated Matter Jobs view.
- Attempt history and item-outcome links.
- Stale-worker and reconciliation dashboards.
- Failure injection for dispatch loss, worker loss, cancellation races,
  provider timeouts, and lost completion responses.
- Bounded retention policy for step telemetry while preserving audit and
  logical job history.
- Removal plan for redundant legacy execution columns.

Exit when administrators can diagnose and control every registered job without
worker-log or direct database access.

## 16. Testing strategy

### State-machine tests

- Every allowed transition succeeds.
- Every invalid transition fails without partial projection updates.
- Terminal transitions are idempotent.
- Superseded attempts cannot update the current logical job.
- Progress is monotonic within its declared stage.
- Business states are not coerced into execution states.

### Dispatch and recovery tests

- Commit succeeds and dispatch fails.
- Dispatch succeeds and the HTTP response is lost.
- Worker stops before first stage, between stages, and during a batch.
- Completion output is committed but final status is not.
- Reconciliation resolves an existing derivation instead of duplicating it.
- Repeated reconciliation is idempotent.

### Cancellation tests

- Cancel queued, running, waiting, terminal, and already-requested attempts.
- Cancellation stops future bounded batches.
- Child and remote-provider cancellation follows adapter policy.
- API and worker shutdown are not blocked indefinitely.
- Processed outputs and outcome fragments remain valid.

### Retry tests

- Retriable failure creates the next attempt with a fresh DBOS ID.
- Non-retriable failure is rejected.
- Concurrent retries create only one next attempt.
- Retry retains frozen logical inputs.
- A changed configuration creates a new domain job instead.

### Authorization and audit tests

- Read, cancel, and retry use domain permissions.
- Cross-tenant and wrong-scope references fail closed.
- Advisory UI actions cannot bypass backend authorization.
- Every cancel, retry, and administrative reconciliation writes an audit event.

### API and UI tests

- Generated OpenAPI clients expose the common execution shape.
- Existing domain endpoints remain compatible during migration.
- Indeterminate and determinate progress render honestly.
- Cancel-requested, stale heartbeat, retriable failure, and attempt history are
  distinguishable.
- Domain result details remain available after execution normalization.

### Scale tests

- Progress writes occur per bounded batch/stage, not per document.
- Matter job feeds use indexed queries and bounded pages.
- Attempt history remains fast for high-volume job types.
- Outcome manifests, not Core rows, carry million-document diagnostics.

## 17. Observability

Emit platform metrics by registered job type, never by user-provided labels:

- Queued, running, waiting, failed, canceled, and completed attempts.
- Queue latency and execution duration.
- Stage duration.
- Heartbeat age and stale attempt count.
- Cancellation request-to-acknowledgement latency.
- Retry count and retry success rate.
- Dispatch and reconciliation failures.
- Domain-projection mismatch count.

Structured logs include tenant, domain job, execution, attempt, DBOS workflow,
and step IDs. They exclude document contents, credentials, and raw provider
payloads.

## 18. Rollout safeguards

- Feature-flag adapters independently.
- Start with dual-write and shadow-read comparison.
- Provide a per-adapter kill switch that returns reads to the legacy domain
  projection without deleting `WorkflowRun` history.
- Alert on state/progress mismatches before switching UI reads.
- Never migrate more than one high-volume job family in the same release.
- Run Alembic through Pipenv in all operator instructions, for example
  `pipenv run alembic upgrade head`.
- Document worker restart and reconciliation procedures before enabling an
  adapter in production.

## 19. Deferred boundaries

Deferred until the common platform is proven:

- Automatic retry policies based on arbitrary tenant configuration.
- Cross-service distributed transactions between Core and Artifact.
- A global job feed spanning every tenant and service.
- WebSocket/SSE live updates.
- Per-document `WorkflowStepRun` rows.
- Arbitrary tenant-authored workflow graphs.
- Hard deletion of attempt history.
- A generic force-kill action that bypasses domain cleanup.

## 20. Definition of done

The normalization effort is complete when:

- Every registered asynchronous operation has a persisted execution attempt
  committed before dispatch.
- The UI can show its current stage, honest progress, heartbeat, failure, and
  attempt history through one contract.
- Cancellation and retry are durable, authorized, audited, and adapter-aware.
- Worker or response loss can be reconciled without duplicating domain output.
- Domain business states and immutable outputs remain authoritative.
- Artifact-owned jobs retain Artifact ownership.
- High-volume item diagnostics use artifacts rather than unbounded Core rows.
- No adopted router or workflow directly mutates lifecycle fields outside the
  shared execution service.
