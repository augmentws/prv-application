# Analysis Task batch sets implementation plan

## 1. Objective

Add task-specific batching without changing the existing Review Batch contract.

The platform must support three execution scopes:

| Scope mode | Meaning |
| --- | --- |
| `AD_HOC_BATCH` | Run against any ready Review Batch. The same document may belong to any number of these batches. This is the existing behavior. |
| `WHOLE_MATTER` | Run against every document in the matter as of a frozen population snapshot. |
| `PARTITIONED_SET` | Build a task-specific set of sibling Review Batches in which each selected document belongs to exactly one sibling batch. |

The non-overlap invariant is deliberately scoped to one partitioned set. It is not global to the matter or to
the Analysis Task for all time. A document may remain in ordinary Review Batches, in a later partitioned set
for the same task, and in sets owned by other tasks.

### Current implementation status

Delivery Slices 1 through 5 are implemented:

- task versions persist and expose the three-mode scope policy with compatibility defaults;
- `analysis_task_batch_set` is a durable Core domain record backed by a common `WorkflowRun`;
- `ALL_MATTER` and ready `SOURCE_BATCH` populations are supported;
- stable source-order partitioning by target batch size creates managed `TASK_BATCH_PARTITION` Review Batches;
- the database enforces one membership per `(batch set, matter document)` while leaving ordinary batches
  overlapping;
- list, get, and create APIs are available under the owning Analysis Task;
- read-only preview reports the exact population count, proposed child sizes, population hash, oversized families,
  and warnings without persisting a set;
- cancel and retry commands use the common execution history, with a new workflow attempt on retry and cooperative
  cancellation between durable partition boundaries;
- optional `FAMILY` keep-together planning resolves Artifact-owned family IDs, assigns atomic families with stable
  least-loaded balancing, and keeps oversized families intact with an explicit warning;
- direct keyword-search and accessible saved-search populations resolve against and pin the active physical search
  generation, snapshot the canonical query, and page all matching document IDs in stable order;
- managed children use deterministic IDs, remain unavailable while membership is incomplete, and become ready only
  after set-wide coverage verification;
- each managed child is projected through the existing Review Batch search projection; per-child search state and
  failures are exposed without invalidating authoritative Core membership, and partial projection failure completes
  the common execution with errors plus a durable warning;
- inline development execution and a DBOS worker workflow use the same materializer;
- question-answering, classification, and fact-development launches enforce the selected mode and the set's
  pinned task version;
- managed-child runs accept the set's pinned version explicitly, including a historically published version that
  has since been retired, while ordinary ad hoc runs remain bound to the current published version;
- question-answering, classification, and fact-development run records, execution snapshots, and audit events retain
  the Batch Set ID and child ordinal as first-class provenance;
- the Analysis Task detail surface provides a preview-first partition builder and monitoring detail for source
  provenance, population identity, ordered children, lifecycle actions, search projection state, and per-child run
  actions for every supported task type.
- a run-all command creates one common parent execution with ordered, typed references to the native child runs;
- run-all preserves child order, limits simultaneous children with a configurable concurrency window, and aggregates
  successful, partially successful, failed, and canceled child outcomes without copying document results;
- canceling a parent prevents pending launches and cancels active question-answering, classification, and
  fact-development children through their normal cancellation paths; retry creates a new parent attempt and resets
  only failed or canceled children;
- the Analysis Task detail surface exposes Run all, parent/child progress, cancellation, and failed-child retry.

The following remain planned: communication-thread and duplicate keep-together groups; fixed-count and randomized
allocation; and native whole-matter manifests for question answering and fact development.
Search-backed sources currently accept keyword searches; semantic and hybrid population freezing require a separately
versioned contract.

## 2. Existing behavior to preserve

A Review Batch remains a matter-scoped, permanent frozen set of matter-document IDs. PostgreSQL remains the
authority for membership, and ready membership continues to be projected into OpenSearch through the existing
`batch_ids` keyword array.

The following behavior does not change:

- ordinary batches may overlap;
- existing `ALL_MATTER`, search, saved-search, random, assessment, classification, and comparison selections
  retain their current meaning;
- a batch snapshots its coding layout and selection provenance;
- evaluation outputs remain isolated until an explicit promotion;
- existing batch search, facets, review, statistics, comparison, coding, and promotion paths continue to use
  `ReviewBatch` and `ReviewBatchDocument`;
- existing batch APIs remain valid and do not require callers to supply a task or batch-set ID.

Existing batches are not backfilled into sets. A null batch-set ID always means ordinary overlapping behavior.

## 3. Terminology and invariants

### 3.1 Analysis Task Batch Set

An Analysis Task Batch Set is one immutable processing campaign owned by one matter, one Analysis Task, and one
published task version. It freezes a source population and deterministically partitions that population into
one or more managed Review Batches.

### 3.2 Managed child batch

A managed child batch is an ordinary `ReviewBatch` whose membership was created by a Batch Set materializer.
It remains compatible with the existing review and Analysis Task execution paths, but its membership cannot be
edited independently of the set.

### 3.3 Required invariants

For every ready partitioned set:

1. Every child batch belongs to the same matter as the set and task.
2. Every child batch is associated with exactly one set ordinal.
3. Every selected matter document is assigned to exactly one child batch.
4. No selected matter document occurs in two child batches in the same set.
5. The union of child memberships equals the frozen set population.
6. A configured keep-together group is never split across child batches.
7. Membership, ordering, population provenance, and allocation configuration are immutable after readiness.
8. Retrying materialization produces the same assignments.
9. Archiving a set or child batch does not erase its historical membership or make its documents unassigned in
   that historical set.

The database must enforce invariant 4. Workflow finalization must verify invariants 3, 5, and 6 before making
the set or any child batch available.

## 4. Task execution-scope policy

Add a versioned `scope_policy` to `MatterAnalysisTaskVersion`:

```json
{
  "allowed_modes": ["AD_HOC_BATCH", "WHOLE_MATTER", "PARTITIONED_SET"],
  "default_mode": "PARTITIONED_SET"
}
```

The policy is part of the immutable published task version because changing the permitted population semantics
changes how that version may be executed. Publication validates that the selected modes are supported by the
task runtime.

Compatibility defaults are:

- question answering: `AD_HOC_BATCH`;
- classification: `AD_HOC_BATCH` and `WHOLE_MATTER`;
- fact development: `AD_HOC_BATCH`.

`PARTITIONED_SET` can be enabled for all three task types once the Batch Set workflow is present because its
children use ready Review Batches. Whole-matter support for question answering and fact development is a later
runtime adoption described below.

Run creation validates the requested scope against the pinned task version. A managed child batch may be run
only with the task version pinned by its set. Using a different task version requires an explicit clone into a
new set so provenance remains unambiguous.

## 5. Core data model

### 5.1 `analysis_task_batch_set`

Add a Core-owned domain table with:

- `id`;
- `tenant_id`, `matter_id`;
- `matter_analysis_task_id`;
- `matter_analysis_task_version_id`;
- `name`, `description`;
- `population_source_type`: `ALL_MATTER`, `SEARCH_QUERY`, `SAVED_SEARCH`, or `SOURCE_BATCH`;
- `selection_definition`, containing the submitted source and its immutable provenance;
- nullable `source_review_batch_id`;
- nullable `search_index_generation_id`;
- `allocation_policy`;
- `target_batch_size` or `requested_batch_count`, exactly one of which is present;
- `keep_together_policy`;
- `random_seed` and `allocation_algorithm_version`;
- `status`: `QUEUED`, `BUILDING`, `READY`, `FAILED`, `CANCELED`, or `ARCHIVED`;
- `population_count`, `batch_count`, and `population_hash`;
- warning and failure details;
- `workflow_id` and `current_workflow_run_id`;
- creator and lifecycle timestamps.

The set is a domain record. Its `WorkflowRun` is the common execution attempt used for durable planning,
progress, cancellation, and retry.

`selection_definition` must retain enough information to explain the population without rerunning a mutable
query. For search sources this includes the canonical search request and physical search generation. For a
saved search it also includes the saved-search ID, name, update timestamp, and resolved search request. For a
source batch it includes the source batch ID and population hash.

### 5.2 `review_batch`

Add nullable:

- `analysis_task_batch_set_id` with `ON DELETE RESTRICT`;
- `batch_set_ordinal`;
- an immutable managed-membership indicator, if set ownership alone is not used as that indicator.

Add `TASK_BATCH_PARTITION` to the internal Review Batch selection types. Its `selection_definition` records the
set ID, task and version IDs, ordinal, allocation algorithm, set population hash, and child membership hash.
It is not accepted by the existing interactive Review Batch creation endpoint.

Enforce a unique child ordinal within each set.

### 5.3 `review_batch_document`

Add nullable `analysis_task_batch_set_id`. Ordinary membership rows keep it null.

Add:

```text
UNIQUE (analysis_task_batch_set_id, matter_document_id)
```

PostgreSQL permits multiple null values, so existing overlapping membership is unaffected. A non-null set ID
makes duplicate assignment inside that set a database error.

Also add a composite relationship that proves a membership's set matches its parent batch:

```text
review_batch: UNIQUE (id, analysis_task_batch_set_id)
review_batch_document:
  FOREIGN KEY (review_batch_id, analysis_task_batch_set_id)
  REFERENCES review_batch (id, analysis_task_batch_set_id)
```

The existing `review_batch_id` foreign key remains in place for ordinary rows, whose set ID is null.

## 6. Population and allocation contract

### 6.1 Frozen population

`ALL_MATTER` means all matter documents visible to the authoritative Core query at the population snapshot.
Documents added afterward are not silently included.

Search-backed populations pin the physical search generation and collect all matching IDs through stable
pagination. Saved searches are resolved to their current definition before planning. Source-batch populations
read the source batch in sequence order.

The planner computes `population_hash` from the ordered matter-document IDs plus the population contract
version. It stores the exact population durably before or as part of deterministic assignment, so a retry never
reruns a mutable population source.

For the first implementation, the managed `ReviewBatchDocument` rows are the permanent population evidence.
If planning scale requires a staging artifact, use an Artifact-owned immutable population manifest and delete
only temporary planning state after every child membership is durable. The Core set and child membership remain
the permanent business record.

### 6.2 Allocation policy

Support one of:

- `target_batch_size`: target number of documents per child batch;
- `requested_batch_count`: requested number of child batches.

The configuration also freezes:

- deterministic ordering mode;
- random seed when randomized balancing is selected;
- allocation algorithm and version;
- keep-together keys;
- oversized-group behavior.

The initial deterministic algorithm should use stable population order unless randomized balancing is
explicitly requested. It builds atomic allocation units, then assigns whole units to the least-loaded eligible
child batch with deterministic tie breaking.

### 6.3 Keep-together groups

The first supported group is document family. Communication thread and authoritative exact-duplicate group may
be added as independent options when their source projections are available.

Keep-together expansion is transitive. If one configured relation connects A to B and another connects B to C,
all three documents form one atomic allocation unit.

Batch size is a target, not a hard limit. If an atomic family or group is larger than the target, place it in
one oversized child batch and persist a warning. Never split it silently. A future strict policy may reject the
plan instead, but that choice must be explicit in the frozen configuration.

## 7. Durable materialization workflow

Add the code-owned workflow `analysis_task_batch_set_materialization_v1`.

### 7.1 Command path

The create command:

1. requires `batch.create` and `analysis_task.run` for the matter;
2. validates the active task and ready published version;
3. validates `PARTITIONED_SET` in the version scope policy;
4. validates the population source and allocation configuration;
5. creates the Batch Set and common `WorkflowRun` in one transaction;
6. commits before DBOS dispatch;
7. returns `202 Accepted` with the persisted set.

### 7.2 Workflow stages

Use these observable stages:

```text
RESOLVING_POPULATION
FREEZING_POPULATION
RESOLVING_KEEP_TOGETHER_GROUPS
PLANNING_PARTITIONS
CREATING_CHILD_BATCHES
MATERIALIZING_MEMBERSHIP
VERIFYING_MEMBERSHIP
SYNCING_SEARCH
FINALIZING
```

### 7.3 Retry and concurrency

- Lock the set before every state transition.
- Use stable child IDs or deterministic child idempotency keys derived from set ID and ordinal.
- Resume membership insertion only from verified durable progress.
- Let the unique set/document constraint reject concurrent duplicate assignments.
- Do not expose any child as `READY` until set-wide verification succeeds.
- On retriable failure, preserve frozen population and completed deterministic work.
- Cancellation stops future chunks, marks incomplete children canceled, and never publishes a partial set.
- A retry gets a new `WorkflowRun` and DBOS workflow ID while retaining the same Batch Set domain identity.

### 7.4 Final verification

Before readiness, calculate and compare:

- expected population count and hash;
- assigned distinct-document count and hash;
- sum of child document counts;
- duplicate count, which must be zero;
- missing and unexpected IDs, both of which must be empty;
- keep-together group split count, which must be zero;
- child ordinal and sequence continuity.

Finalize all children and the set only after these checks pass. Search projection failure does not invalidate
authoritative membership, but it must leave the affected child and set search state visibly failed and
retryable under the existing projection rules.

## 8. Running a task

### 8.1 Single child run

Running one managed child batch uses the existing task-specific execution path:

- question answering creates its existing `ReviewBatchRun`;
- classification creates its existing `MatterAnalysisTaskRun` with `REVIEW_BATCH` scope;
- fact development creates its existing task run using the ready child Review Batch.

The run snapshots the Batch Set ID, pinned task version, set population hash, child ordinal, and child membership
hash in addition to its existing configuration.

### 8.2 Run all children

After single-child execution is stable, add a Batch Set run command that launches the pinned version across all
children. The parent is a domain orchestration record with one common execution attempt and typed references to
the existing child runs. It aggregates child counts and states but does not duplicate their document results.

Required behavior:

- do not launch until every child is ready;
- preserve child order;
- bound the number of simultaneously active child jobs;
- cancellation prevents unstarted children and requests cancellation for active children through their normal
  command paths;
- retry launches only failed or canceled-retriable children and never changes membership;
- `COMPLETED_WITH_ERRORS` reports child and document failures without losing successful results.

The parent must use the Unified Job Execution Platform rather than introducing a competing execution-state
model.

## 9. Whole-matter execution

Whole-matter execution is a task run scope, not a permanent non-overlap rule. It freezes the population at run
creation and processes that exact population even if the matter later changes.

Classification already supports `MATTER` and `REVIEW_BATCH` run scopes. Preserve that implementation.

Question answering currently consumes `ReviewBatchDocument` rows, and fact development currently launches from
a ready Review Batch. Deliver whole-matter support for those runtimes in two steps:

1. Compatibility step: allow the user to create or select the existing frozen `ALL_MATTER` Review Batch and run
   the task through its current path.
2. Target step: migrate the runtime to the generic Analysis Input Manifest contract, with one immutable
   Artifact-owned population manifest and bounded chunk execution. Do not create a synthetic permanent Review
   Batch solely to represent a large task run after this adoption.

The UI may label the compatibility behavior as Entire matter, but the API response and run provenance must make
the underlying frozen batch explicit until the target runtime is implemented.

## 10. API contract

Add task-owned endpoints:

```text
POST /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets/preview
POST /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets
GET  /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets
GET  /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets/{set_id}
POST /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets/{set_id}/cancel
POST /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets/{set_id}/retry
GET  /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets/{set_id}/runs
POST /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets/{set_id}/runs
POST /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets/{set_id}/runs/{run_id}/cancel
POST /v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets/{set_id}/runs/{run_id}/retry
```

Preview is read-only and returns population count, estimated child count and sizes, oversized groups, and
warnings. Creation always resolves and freezes the population again under the durable workflow; preview is not
authoritative.

The existing Review Batch endpoints remain unchanged. Reads add optional set ownership and ordinal fields.
Mutation endpoints reject attempts to change managed membership.

After backend changes, regenerate `web/openapi.json` and the Orval TypeScript client. Do not hand-edit generated
models.

## 11. UI plan

On the Analysis Task run surface, present:

- **Entire matter** for task types whose published version allows `WHOLE_MATTER`;
- **Existing batch** for `AD_HOC_BATCH`;
- **Partition into exclusive batches** for `PARTITIONED_SET`.

The partition builder collects:

- population source;
- target size or requested number of batches;
- stable or randomized allocation;
- keep-together rules;
- name and description.

Show a preview before creation, including population count, expected child sizes, atomic groups that exceed the
target, and the precise statement that exclusivity applies only within this set.

The set detail view shows:

- pinned task and version;
- frozen source provenance and population hash;
- allocation policy;
- materialization execution progress;
- ordered child batches and counts;
- warnings and verification result;
- per-child and run-all actions.

Managed child batches remain visible in the ordinary Batches UI with a label such as `October Corpus Pass · 3
of 12`. Their membership controls are disabled, while review, search, coding, analysis, comparison, and result
inspection continue to use the existing interfaces.

## 12. Authorization and audit

- Creating, canceling, or retrying a set requires `batch.create` and `analysis_task.run`.
- Reading a set requires `batch.view` and access to the owning matter.
- Running a child or entire set uses the existing task-run permission and reauthorizes the initiating user in
  the worker.
- Existing batch assignment rules continue to apply to individual child review workspaces.

Audit at least:

- set creation with submitted source and allocation policy;
- frozen population identity and search generation;
- successful or failed materialization;
- cancellation and retry;
- child batch identities and hashes;
- task run launch for one child or the entire set.

## 13. Search projection

No new exclusivity logic belongs in OpenSearch. Each child remains a normal batch ID in the document's existing
`batch_ids` array. A document can therefore carry ordinary batch IDs and one child ID from each independent
partitioned set.

Core validates set membership. Full index rebuilds derive the same child membership from PostgreSQL. Batch
search and facets continue injecting the selected child batch ID server-side.

Do not add all Batch Set IDs to every document unless a measured UI query requires it. Set-to-child resolution
belongs in Core and the child batch filter is sufficient for current search behavior.

## 14. Migration and compatibility

Implementation uses additive Core Alembic migrations. The initial migration:

1. creates `analysis_task_batch_set`;
2. adds task-version scope policy storage;
3. adds nullable set ownership and ordinal columns to `review_batch`;
4. adds nullable set ownership to `review_batch_document`;
5. adds the composite foreign key and scoped uniqueness constraints;
6. extends the Review Batch selection-type constraint with `TASK_BATCH_PARTITION`;
7. derives compatibility scope policies for existing task versions by task type.

A follow-up migration extends the source constraint for `SEARCH_QUERY` and `SAVED_SEARCH` and adds the restricted,
indexed foreign key to the pinned `search_index_generation` row. Existing sets and ordinary Review Batches are not
rewritten.

A second follow-up migration adds nullable Batch Set ID and child ordinal provenance to question-answering Review
Batch runs and to classification/fact-development Analysis Task runs. Both fields are either present together or null,
so ordinary run rows retain their existing representation.

No existing Review Batch receives a set ID. No existing membership is rewritten. The interactive Review Batch
create contract retains its current selection literals.

The deployment order is:

1. additive database migration;
2. backend models and read compatibility;
3. materialization command and workflow;
4. task scope-policy enforcement;
5. API creation and lifecycle endpoints;
6. regenerated OpenAPI client;
7. UI creation and monitoring;
8. optional run-all orchestration;
9. whole-matter manifest adoption for question answering and fact development.

## 15. Test plan

### 15.1 Database and domain tests

- The same document may belong to multiple ordinary batches.
- The same document may belong to an ordinary batch and a managed child.
- The same document may belong to different Batch Sets.
- A document cannot belong to two children of the same Batch Set.
- A child cannot claim a set different from its parent batch.
- Child ordinals are unique and continuous.
- A task, task version, matter, source batch, or search generation mismatch is rejected.
- Published set configuration and membership cannot be edited.

### 15.2 Planner tests

- Every source population type freezes complete provenance.
- Stable inputs and algorithm version produce identical assignments.
- Retry after every chunk boundary produces the same final hashes.
- Fixed-size and fixed-count policies cover the exact population.
- Families and other configured transitive groups remain together.
- Oversized groups create the configured warning or rejection.
- Concurrent materializers cannot assign a document twice.
- Cancellation never exposes a partial ready set.
- Documents added after population freeze are excluded.

### 15.3 Execution tests

- A child rejects a task version other than the set's pinned version.
- Each supported task type can run against a managed child through its existing runtime.
- Existing ad hoc batch execution is unchanged.
- Whole-matter classification remains unchanged.
- Run-all concurrency, cancellation, partial failure, and retry preserve successful child results.

### 15.4 Search and UI tests

- Managed child membership projects and rebuilds like ordinary `batch_ids`.
- Search and facets never cross the selected child batch.
- Set and ordinal labels appear on child batch rows.
- Managed membership controls are disabled.
- Preview warnings and exact exclusivity scope are visible before creation.
- Existing Review Batch creation and review flows have no regression.

## 16. Delivery slices

### Slice 1: schema and read compatibility

Add the set model, nullable child ownership, scoped membership uniqueness, task-version scope policy, read models,
and compatibility defaults. Do not expose creation yet.

Status: implemented. The initial implementation also delivered compatible create/read APIs and task-version scope
enforcement that had originally been assigned to later slices.

### Slice 2: deterministic materialization

Add preview, create, status, cancel, and retry APIs plus the durable planner. Initially support `ALL_MATTER`,
`SOURCE_BATCH`, stable ordering, target batch size, and family keep-together.

Status: implemented. Preview is non-authoritative and read-only; creation freezes its own snapshot. Retries retain
the Batch Set and frozen source contract, create a new common execution attempt, and deterministically recreate child
IDs and membership. `FAMILY` is the first keep-together option, with oversized groups preserved and reported.

### Slice 3: search-backed populations and UI

Add search and saved-search sources, pinned generation handling, OpenSearch projection status, the partition
builder, and Batch Set detail UI.

Status: implemented. Search sources currently require keyword mode and are frozen as a canonical request plus the
physical generation identity. Managed children reuse the normal Review Batch projection path. The set becomes ready
after authoritative membership verification even if a child projection fails; the common execution records
`COMPLETED_WITH_ERRORS`, the failed child IDs are retained as a warning, and the projection remains retryable through
the existing batch search lifecycle. The preview-first UI supports all four population sources, family keep-together,
target batch size, cancel/retry monitoring, pinned provenance, warnings, and ordered child status.

### Slice 4: task execution integration

Enforce published scope policies, add set provenance to child runs, and expose per-child launch actions for all
supported task types.

Status: implemented. Ordinary batches still require `AD_HOC_BATCH` and the current published version. Managed children
resolve the immutable version pinned by their set and may continue using it after a later publication retires it. Run
rows, workflow input/configuration snapshots, API reads, and audit details persist the set ID and ordinal. Each ready
child exposes a task-type-aware launch dialog: direct decision execution for question answering, compatible embedding
selection for classification, and preview-first current-matter-coding execution for fact development.

### Slice 5: run-all orchestration

Add the parent domain run, bounded child concurrency, aggregate status, cancellation, and failed-child retry.

Status: implemented. A Batch Set run is a common-execution-backed parent domain record whose ordered children retain
typed references to the existing question-answering, classification, or fact-development runs. The durable parent
workflow launches at most the configured number of children, records launch failures without blocking siblings,
aggregates partial failure as `COMPLETED_WITH_ERRORS`, cancels pending and active children, and retries only failed or
canceled children while preserving successful child run references. The generated API client and Analysis Task UI
support creation, monitoring, cancellation, and retry.

### Slice 6: native whole-matter manifests

Migrate question answering and fact development from Review Batch-only population discovery to immutable
Analysis Input Manifests. Retain `ALL_MATTER` Review Batches for review use, but stop requiring them solely for
whole-corpus task execution.

## 17. Acceptance criteria

The feature is complete when:

1. Existing ordinary batches still overlap with no API or behavior change.
2. An administrator can create a task-version-pinned partitioned set from a supported frozen population.
3. The database prevents any document from appearing in two children of that set.
4. Set finalization proves exact population coverage and keep-together integrity.
5. Each child works with existing review, search, coding, and supported Analysis Task execution paths.
6. Users can clearly distinguish ad hoc batches, whole-matter execution, and managed partitioned sets.
7. Retrying or rebuilding search never changes authoritative membership.
8. New documents and later task versions never silently alter a ready set.
9. Tests demonstrate backward compatibility, deterministic planning, concurrent safety, and durable recovery.
