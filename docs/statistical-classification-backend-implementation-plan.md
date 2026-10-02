# Statistical Classification Backend Implementation Plan

Status: Proposed phased implementation plan

Related documents:

- `docs/statistical-classification-model-infrastructure-plan.md`
- `docs/jev-cal-review-program-workflow.md`

## 1. Outcome and boundary

Implement the backend required to configure, build, evaluate, approve,
activate, and batch-score statistical classification models attached to a
specific matter metadata field.

```text
Core API
  -> Core PostgreSQL: identity, policy, runs, decisions, active pointer
  -> DBOS workflow worker
       -> ClassificationGateway
            -> embedded ClassificationBackend
            -> remote Classification Runtime API
                 -> Artifact Service
```

Core owns the control plane: tenant/matter/field-scoped classifier identity,
configuration revisions, run records, authorization, orchestration, decisions,
and active-model selection.

The Classification Runtime owns computation: trainer registration, training,
cross-fitting, calibration, evaluation, safe packaging/loading, and batch
inference. It is stateless and has no authoritative registry or workflow DB.

Artifact Service owns immutable population, feature, label, split, package,
prediction, evaluation, and result-manifest artifacts.

Deployment modes are:

- `CLASSIFICATION_MODE=embedded`: the DBOS worker invokes the runtime backend
  in process.
- `CLASSIFICATION_MODE=remote`: the DBOS worker invokes a private FastAPI
  Classification Runtime using the same request/result schemas.

Production training should run in the workflow worker or remote runtime, not in
an API request handler.

## 2. Locked MVP decisions

1. Every classifier is permanently linked to one tenant, matter, and metadata
   definition.
2. Field/value stable keys are machine identity; names/labels are immutable
   audit snapshots.
3. Classifier configurations are immutable after use by a training run.
4. Training scope is exactly `MATTER` or `REVIEW_BATCH`.
5. Missing target values are unlabeled and excluded, never inferred negative.
6. Supported strategies are `BINARY`, `MULTICLASS`, and `ONE_VS_REST`.
7. A one-vs-rest build is one atomic version containing all child classifiers.
8. Source authority resolves a label before source weight affects training.
9. The MVP has only source training weights. There is no automatic class
   balancing or training use of statistical sampling weights.
10. The first trainer is regularized logistic regression over deterministic
    document embeddings.
11. `classification_model.active_model_version_id` is the only active-state
    authority; versions do not also carry an `ACTIVE` status.
12. Core and Artifact Service remain authoritative if the runtime or any future
    external MLOps integration is unavailable.

## 3. Phase overview

| Phase | Deliverable | Depends on |
| --- | --- | --- |
| 0 | Contracts, policy fixtures, and package schemas | None |
| 1 | Core classifier/configuration registry | 0 |
| 2 | Embedded/remote Classification Runtime boundary | 0 |
| 3 | Artifact Service workflow-artifact primitives | 0 |
| 4 | Scope, target, and label snapshotting | 1, 3 |
| 5 | Document features and group-aware splits | 3, 4 |
| 6 | Trainer, evaluation, and safe package | 2, 3, 5 |
| 7 | Durable model-build workflow | 1-6 |
| 8 | Candidate decisions, activation, and rollback | 7 |
| 9 | Durable batch scoring | 6-8 |
| 10 | CAL scoring/queue integration | 9 |
| 11 | Operational hardening | 1-10 |

Phases 1, 2, and 3 may proceed in parallel after Phase 0. Each phase must ship
with its migrations, command/query service, API or internal contract, and tests.

## 4. Phase 0: Contracts and policy fixtures

### Goal

Define stable contracts before creating database rows or service endpoints.

### Deliverables

Create versioned Pydantic contracts for:

- Training scope and target-field snapshot.
- Binary class mapping and classification strategy.
- Label, feature, and prediction-resolution policies.
- Trainer specification.
- Training/scoring request and result envelopes.
- Package manifest, inference signature, and evaluation report.

The common training envelope includes:

```text
contract_version
training_run_id
tenant_id
matter_id
classification_model_id
configuration_id
training_scope
target_field_snapshot
classification_strategy
trainer_specification
feature_artifact
label_artifact
split_artifact
validation_policy
random_seed
output_derivation_key
```

### Contract rules

- Binary requires non-empty, disjoint positive and negative value-key sets.
- Multiclass requires a single-cardinality target and at least two selected
  mutually exclusive values.
- One-vs-rest requires at least two class keys.
- Multi-valued fields reject native multiclass.
- Single-valued one-vs-rest requires conflict/abstention policy.
- Unknown trainer kinds/parameters and incompatible input roles are rejected.

The first trainer contract is `sklearn_logistic_regression` version 1 with an
allowlisted regularization type, candidate `C` values, and maximum iterations.
It does not expose a `class_weight` option in the MVP.

### Fixtures and tests

- Contract serialization and canonical hashing.
- Strategy/target compatibility matrix.
- Gold label only; gold overriding silver; agreeing sources without duplicate
  weight; equal-authority conflict; missing label; partial one-vs-rest label.
- Package JSON schemas and valid/invalid package fixtures.
- Unknown-field and out-of-range parameter rejection.

### Exit criteria

Embedded and remote code import the same versioned contracts, and a complete
build request fixture can be validated and hashed without a database or HTTP
implementation.

## 5. Phase 1: Core classifier registry

### Goal

Create the authoritative field-scoped classifier identity and immutable
configuration model before training exists.

### Migration

Add `classification_model`:

```text
id
tenant_id
matter_id
target_metadata_definition_id
name
description
status
current_configuration_id nullable
created_at / created_by_user_id
updated_at / updated_by_user_id
row_version
```

Add `classification_model_configuration`:

```text
id
classification_model_id
revision
target_field_snapshot JSONB
classification_strategy
ordered_class_keys JSONB
binary_mapping JSONB nullable
label_policy JSONB
feature_contract JSONB
prediction_resolution_policy JSONB
trainer_specification JSONB
configuration_hash
created_at / created_by_user_id
```

### Constraints and services

- Revision and configuration hash are unique per classifier.
- Matter and target field are required and immutable.
- The command service verifies that the field belongs to the same matter and
  tenant and is active, reviewable, and Boolean or enum.
- Enum values use stable keys.
- A used configuration cannot be edited or deleted.
- `current_configuration_id` must belong to the same classifier.
- `active_model_version_id` is added with the model-version table in Phase 7.
- Add create, revise, list, read, compatibility-check, and archive services.
- Use existing matter `ADMIN` authorization and audit conventions.

### Proposed APIs

```text
POST /v1/matters/{matter_id}/classification-models
GET  /v1/matters/{matter_id}/classification-models
GET  /v1/matters/{matter_id}/classification-models/{model_id}
POST /v1/matters/{matter_id}/classification-models/{model_id}/configurations
GET  /v1/matters/{matter_id}/classification-models/{model_id}/configurations
```

Responses include tenant, matter, field ID, stable key, current name,
configured name snapshot, and schema fingerprint. A name is never accepted as
field identity.

### Tests and exit criteria

Test cross-tenant/matter/field rejection, field strategy compatibility, rename
behavior, deactivation, immutable revisions, duplicate hashes, authorization,
audit, and migration upgrades.

Exit when an administrator can create and retrieve a valid configuration and
it cannot be attached to the wrong tenant, matter, field, or value keys.

## 6. Phase 2: Classification Runtime boundary

### Goal

Establish identical embedded and remote invocation before actual training.

### Layout and configuration

```text
classification_service/
  api.py
  auth.py
  backend.py
  config.py
  main.py
  packaging.py
  registry.py
  schemas.py

app/classification_gateway.py
```

```text
CLASSIFICATION_MODE=embedded|remote
CLASSIFICATION_BASE_URL=http://classification-api:8003
CLASSIFICATION_SERVICE_TOKEN=...
CLASSIFICATION_REQUEST_TIMEOUT_SECONDS=...
CLASSIFICATION_MAX_CONCURRENCY=...
```

Initially implement authenticated capabilities and package validation:

```text
GET  /health
GET  /v1/capabilities
POST /v1/packages/validate
```

Reserve `/v1/train` and `/v1/score` contracts without returning fake models.
Add `classification-api` on port `8003` to the split Compose profile. It has no
database and is never called by browsers.

### Tests and exit criteria

Test embedded/remote parity, service-token authentication, HTTP error mapping,
timeouts, malformed responses, request limits, and package validation.

Exit when the workflow worker can use the same gateway contract in either mode
and deployment choice is invisible to Core domain services.

## 7. Phase 3: Artifact Service workflow artifacts

### Goal

Store corpus-scale classification inputs and outputs without forcing them into
collection-item artifact rows.

### Artifact model

Add an Artifact-owned workflow artifact containing tenant/client scope, opaque
Core scope kind/ID, artifact type, schema version, derivation key, blob ID,
content hash, media type, size, producer kind/run ID, metadata, and timestamp.
Artifact Service does not create foreign keys into Core.

Initial types:

```text
CLASSIFICATION_POPULATION
CLASSIFICATION_FEATURES
CLASSIFICATION_LABELS
CLASSIFICATION_SPLITS
CLASSIFICATION_MODEL_PACKAGE
CLASSIFICATION_PREDICTIONS
CLASSIFICATION_EVALUATION
CLASSIFICATION_RESULT_MANIFEST
```

Required operations are create-or-return by derivation key, authorized metadata
and content reads, derivation lookup for reconciliation, and explicit lineage.
Enforce type, media-type, schema, content-hash, and size allowlists.

Use Parquet/Arrow-compatible columnar formats for population, features, labels,
splits, and predictions; constrained archive bytes for packages; and JSON for
evaluation/result manifests.

Extend the Core Artifact gateway with embedded/remote parity. Core stores
Artifact UUIDs and hashes without cross-database foreign keys.

### Tests and exit criteria

Test migration, authorization, idempotent derivation reuse, conflicting bytes,
lineage, gateway parity, and streaming behavior.

Exit when every classification artifact type can be stored and resolved in
both modes and a retry cannot create conflicting logical output.

## 8. Phase 4: Scope, target, and label snapshotting

### Goal

Create the exact reproducible supervised-learning population from a matter or
review batch.

### Migrations

Add `classification_dataset_snapshot` with classifier/configuration identity,
purpose, scope type/ID, population/label/split/feature artifact IDs and hashes,
aggregate hash, status, counts, warnings, creator, and timestamp.

### Preview

Without persisting a snapshot, report:

- Scope membership and field-schema compatibility.
- Label counts by allowed source and authority.
- Resolved labeled, unlabeled, excluded, and conflicting counts.
- Binary or per-class support and source-weight totals.
- Family/duplicate grouping coverage.
- Blocking errors and warnings with stable reason codes.

### Materialization

1. Reauthorize the actor.
2. Freeze current matter membership or verify ready review-batch membership.
3. Pin configuration and target-field schema.
4. Resolve label sources by authority and conflict policy.
5. Exclude missing/unresolved labels.
6. Assign one source weight per resolved row without adding agreeing sources.
7. Record grouping/cohort attributes.
8. Write population and label artifacts.
9. Finalize hashes and counts in Core.

Do not infer negatives from missing values or include blinded validation/audit
labels unless their protocol explicitly permits training use.

### Proposed APIs

```text
POST /v1/matters/{matter_id}/classification-models/{model_id}/dataset-snapshots/preview
POST /v1/matters/{matter_id}/classification-models/{model_id}/dataset-snapshots
GET  /v1/matters/{matter_id}/classification-models/{model_id}/dataset-snapshots
GET  /v1/matters/{matter_id}/classification-models/{model_id}/dataset-snapshots/{snapshot_id}
```

### Tests and exit criteria

Test frozen matter membership, exact review-batch membership, wrong-matter
batch rejection, label precedence, agreeing-source deduplication, missing
labels, multiclass conflict policy, one-vs-rest observed masks, deterministic
hashes, and blinded-label exclusion.

Exit when identical inputs reproduce identical population and label artifacts,
including exact provenance and raw source weights.

## 9. Phase 5: Feature and split snapshotting

### Goal

Produce the deterministic model input matrix and group-aware folds.

### Feature builder

- Reuse existing `CHUNK_VECTOR_SET` artifacts.
- Implement one versioned chunk-to-document pooling specification.
- Pin embedding model/revision, dimension, normalization, text-source policy,
  pooling version, and numeric type.
- Preserve document IDs and deterministic row order.
- Record missing/failed features explicitly.
- Write one corpus-scale `CLASSIFICATION_FEATURES` artifact.

### Split builder

- Use family, exact-duplicate, and available near-duplicate group IDs.
- Keep each group entirely within one fold.
- Pin deterministic seed and algorithm version.
- Validate minimum labeled support per class/fold.
- Write immutable split assignments.
- Normalize source weights to mean `1.0` within each training fold while
  retaining raw values.

### Tests and exit criteria

Test deterministic pooling and bytes, exact dimensions/types, missing-feature
accounting, group leakage, repeatable splits, minimum support, and preservation
of relative source weights.

Exit when a dataset is `READY_FOR_TRAINING` only after population, labels,
features, and splits align exactly by document ID and content hashes.

## 10. Phase 6: Trainer, evaluation, and safe package

### Goal

Implement the first real runtime without DBOS orchestration.

### Trainer

Implement weighted regularized logistic regression for:

- Binary classification.
- Native multinomial classification.
- Atomic one-vs-rest classifier sets.

Use frozen inputs, group-aware cross-fit predictions, source weights only, and
a deterministic candidate-selection/tie-break policy. Do not add automatic
class balancing. Insufficient per-class support fails or warns according to the
validation policy.

### Evaluation

Produce binary/per-class confusion counts, precision, recall, false-positive
and false-negative rates, PR-AUC, optional ROC-AUC, Brier score, log loss,
calibration data, valid macro/micro aggregates, cohort metrics, denominators,
coverage, exclusions, and failures.

Keep cross-fit development evidence distinct from formal validation, random
audit, and targeted QA.

### Safe package

```text
manifest.json
signature.json
estimator.npz
preprocessing.json
calibration.npz
labels.json
environment.lock
model-card.json
checksums.json
```

- NPZ loads with `allow_pickle=False` and rejects object arrays.
- No pickle/joblib, import paths, executables, or arbitrary code.
- Manifest pins tenant, matter, field, name snapshot, schema/configuration,
  trainer, features, class order, runtime image, source revision, and file
  hashes/sizes.
- Reload exact completed bytes and reproduce probabilities/metrics within fixed
  tolerances before publication.

Complete authenticated runtime endpoints:

```text
POST /v1/train
POST /v1/score
```

They consume artifact references and return result manifests; they do not
accept large inline matrices or write Core records.

### Tests and exit criteria

Test deterministic binary/multiclass/one-vs-rest fixtures, source-weight effect,
absence of class balancing, group isolation, support failures, runtime parity,
package security, exact-byte round trip, class order, and field identity.

Exit when embedded and remote execution produce equivalent safe, reloadable
packages and evaluation results from the same fixture snapshot.

## 11. Phase 7: Durable model-build workflow

### Goal

Connect Core, DBOS, Artifact Service, and the runtime into one recoverable
build.

### Migration

Add `classification_training_run` with classifier/configuration/snapshot IDs,
workflow ID, trainer and validation specifications, seed, derivation key,
status/current stage/progress, result-manifest artifact ID, failure details,
creator, timestamps, and row version.

Add `classification_model_version` containing classifier/configuration/run
identity, field snapshot, target/feature hashes, package/evaluation references,
code/runtime identity, metric summary, and status:

```text
CANDIDATE
APPROVED
REJECTED
RETIRED
```

Add `classification_evaluation`, uniquely identifying model version,
evaluation kind, dataset snapshot, and metric-policy version. Detailed results
remain in Artifact Service.

Add `classification_model.active_model_version_id` and its restricted foreign
key now that the version table exists; it remains null until Phase 8.

Statuses:

```text
QUEUED
SNAPSHOTTING
WAITING_FOR_RUNTIME
TRAINING
PACKAGING
EVALUATING
PUBLISHING
FINALIZING
RECONCILING
COMPLETED
COMPLETED_WITH_ERRORS
FAILED
CANCELED
```

### `classification_model_build_v1`

1. Reauthorize launch.
2. Validate classifier/configuration/field compatibility.
3. Create or reuse the run by derivation key.
4. Create or verify the ready dataset snapshot.
5. Dispatch the immutable request through `ClassificationGateway`.
6. Validate returned scope, configuration, derivation key, schemas, and hashes.
7. Reconcile ambiguous artifact publication by derivation key.
8. Persist model-version and evaluation records transactionally.
9. Finish with a candidate requiring human decision.

### Proposed APIs

```text
POST /v1/matters/{matter_id}/classification-models/{model_id}/builds/preview
POST /v1/matters/{matter_id}/classification-models/{model_id}/builds
GET  /v1/matters/{matter_id}/classification-models/{model_id}/builds
GET  /v1/matters/{matter_id}/classification-models/{model_id}/builds/{run_id}
POST /v1/matters/{matter_id}/classification-models/{model_id}/builds/{run_id}/cancel
POST /v1/matters/{matter_id}/classification-models/{model_id}/builds/{run_id}/reconcile
```

Launch requires an idempotency key. The derivation key covers configuration,
dataset, trainer, validation policy, code/runtime, seed, and package schema.
Retries reuse published output; Core finalization failure cannot cause another
logical package.

### Tests and exit criteria

Test duplicate launch, DBOS retry, runtime failure, timeout after upload,
artifact mismatch, safe cancellation, finalization failure/reconciliation, and
cross-scope result rejection.

Exit when one launch yields exactly one candidate or one explicit terminal or
reconcilable failure, never duplicate logical versions.

## 12. Phase 8: Decisions, activation, and rollback

### Goal

Add the governed model lifecycle without coupling human approval to build
execution.

### Migration

Add append-only `classification_model_decision` for approval, rejection,
activation, rollback, and retirement. Record actor, reason, policy/metric
snapshot, before/after active version, timestamp, and authorization context.

### Commands and workflow

- Approve/reject candidate with required reason.
- Preview activation or rollback.
- Activate an approved compatible version.
- Roll back to an approved compatible version.
- Retire a non-active version when retention references permit it.

Implement `classification_model_promote_v1`:

1. Reauthorize actor and lock the logical classifier.
2. Validate preview token/expected row version.
3. Verify approval, field, configuration, package, and required evaluations.
4. Append the decision.
5. Atomically replace `active_model_version_id`.
6. Emit audit/domain event.

Do not write a duplicate `ACTIVE` status on the model version.

### Proposed APIs

```text
GET  /v1/matters/{matter_id}/classification-models/{model_id}/versions
GET  /v1/matters/{matter_id}/classification-models/{model_id}/versions/{version_id}
POST /v1/matters/{matter_id}/classification-models/{model_id}/versions/{version_id}/approve
POST /v1/matters/{matter_id}/classification-models/{model_id}/versions/{version_id}/reject
POST /v1/matters/{matter_id}/classification-models/{model_id}/activation/preview
POST /v1/matters/{matter_id}/classification-models/{model_id}/activation
POST /v1/matters/{matter_id}/classification-models/{model_id}/rollback/preview
POST /v1/matters/{matter_id}/classification-models/{model_id}/rollback
POST /v1/matters/{matter_id}/classification-models/{model_id}/versions/{version_id}/retire
```

### Tests and exit criteria

Test required reasons, authorization, wrong-classifier candidate, stale
preview, concurrent activation, rollback history, retirement protection, and
field deactivation.

Exit when Core can always identify the exact active version and reconstruct
every activation interval from append-only decisions.

## 13. Phase 9: Durable batch scoring

### Goal

Score an immutable population with an exact model version and keep complete
predictions outside Core.

### Migration and workflow

Add `classification_scoring_run` with model version, dataset snapshot,
configuration/feature hashes, scoring-policy hash, workflow/derivation IDs,
status/counts, prediction artifact ID, errors, and timestamps.

Implement `classification_model_score_v1`:

1. Reauthorize and freeze/verify scoring population.
2. Resolve and pin the requested version once; do not resolve `active` again.
3. Verify tenant/matter/field and current field compatibility.
4. Verify package, signature, and feature hashes.
5. Invoke runtime batch inference.
6. Persist complete predictions as Parquet.
7. Reconcile and finalize status/counts.

Prediction rows include document/content/feature identity, model/scoring IDs,
ordered probabilities, resolved output/abstention, margin/uncertainty, and
per-row success/exclusion/error code. Core does not create one diagnostic row
per prediction.

### Proposed APIs

```text
POST /v1/matters/{matter_id}/classification-models/{model_id}/scoring-runs/preview
POST /v1/matters/{matter_id}/classification-models/{model_id}/scoring-runs
GET  /v1/matters/{matter_id}/classification-models/{model_id}/scoring-runs
GET  /v1/matters/{matter_id}/classification-models/{model_id}/scoring-runs/{run_id}
POST /v1/matters/{matter_id}/classification-models/{model_id}/scoring-runs/{run_id}/cancel
```

### Tests and exit criteria

Test version pinning during concurrent activation, explicitly selected approved
historical versions, compatibility failures, every strategy output, partial
row failure, and idempotent output reuse.

Exit when a run is reproducible from its pinned model, features, policy, and
prediction artifact without consulting mutable active state.

## 14. Phase 10: CAL integration

### Goal

Use predictions to create operational CAL queues without making prediction
rows authoritative review decisions.

### Deliverables

- Link `cal_campaign` to one logical classifier.
- Link each `cal_round` to its exact model version and scoring run.
- Add versioned acquisition/selection policy.
- Read the prediction artifact and materialize only selected `cal_queue_item`
  rows.
- Record rank/score, selection reason, model/scoring/policy provenance.
- Connect the queue to the existing review-batch/workspace path.
- Keep human coding separate from probabilities.
- Make completed CAL labels eligible for a later snapshot only through the
  configured label-source policy; do not auto-retrain.

### Proposed APIs

```text
POST /v1/matters/{matter_id}/cal-campaigns/{campaign_id}/rounds/preview
POST /v1/matters/{matter_id}/cal-campaigns/{campaign_id}/rounds
GET  /v1/matters/{matter_id}/cal-campaigns/{campaign_id}/rounds
GET  /v1/matters/{matter_id}/cal-campaigns/{campaign_id}/rounds/{round_id}
POST /v1/matters/{matter_id}/cal-campaigns/{campaign_id}/rounds/{round_id}/cancel
```

### Tests and exit criteria

Test exact queue reproducibility, absence of unselected diagnostic rows in
Core, idempotency, provenance, reviewer separation, and later authoritative
label precedence.

Exit when an active model can produce an explainable CAL queue whose reviewed
results can deliberately feed a later snapshot.

## 15. Phase 11: Operational hardening

### Security

- Short-lived scoped Artifact delegation for runtime jobs.
- Runtime service-token rotation.
- Worker resource/concurrency limits.
- Archive-bomb, path-traversal, and size protection.
- No arbitrary pickle/joblib or executable uploads.
- Audit package and detailed evaluation access.

### Reliability and observability

- Failure injection at every Core/runtime/Artifact boundary.
- Reconciliation commands and support diagnostics.
- Graceful runtime shutdown and bounded requests.
- Transient/terminal error classification.
- Retention/legal-hold-aware cleanup and orphan reconciliation.
- Metrics for queue/stage duration, results, artifact size/latency, retries,
  integrity failures, exclusions, per-class support, activation, and rollback.
- Logs contain identifiers and safe codes, never evidence, labels,
  credentials, or signed URLs.

### Performance and release checks

- Streaming/batched Artifact IO.
- Vectorized scoring.
- Bounded feature loading or documented initial limits.
- Runtime concurrency/load and matter/batch snapshot scale tests.
- Core and Artifact migration upgrade tests.
- Embedded and split Compose-profile tests.

Run:

```text
pipenv run ruff check .
pipenv run pytest
pipenv run alembic check
```

Exit when operators can diagnose, reconcile, retry, cancel, activate, roll
back, and clean up supported workflows without direct DB/object-store edits.

## 16. Recommended pull-request sequence

1. Shared contracts, fixtures, and safe-package schemas.
2. Core classifier/configuration migration, services, router, and tests.
3. Runtime shell, gateway, auth, Compose, and parity tests.
4. Artifact workflow-artifact migration/API/gateway and tests.
5. Dataset preview plus population/label snapshotting.
6. Document feature pooling plus group-aware split materialization.
7. Embedded logistic trainer, evaluation, package, and fixture tests.
8. Remote train/score endpoints and parity tests.
9. DBOS build workflow, run APIs, failure injection, and reconciliation.
10. Candidate/evaluation/decision tables and approval APIs.
11. Activation/rollback workflow and concurrency tests.
12. Batch scoring workflow and prediction artifacts.
13. CAL queue integration.
14. Security, observability, retention, scale, and release hardening.

Each PR includes its migration, domain/service tests, API tests, and contract
documentation. Avoid landing orphan tables without a command service or tests.

## 17. Deferred work

- Automatic class balancing.
- Automatic retraining or activation.
- General-purpose AutoML or arbitrary algorithms.
- User-supplied executable packages.
- Online prediction serving.
- Feature store.
- MLflow integration.
- SageMaker execution backend.
- GPU training.
- Saved-search scope; create a frozen review batch instead.
- Cross-matter or cross-tenant model reuse.

## 18. Backend MVP completion criteria

An authorized matter administrator can:

1. Create a classifier bound to one tenant, matter, and metadata field.
2. Choose matter/review-batch scope and a valid strategy.
3. Preview and freeze population, labels, source weights, features, groups, and
   splits.
4. Build the same deterministic candidate in embedded or remote mode.
5. Inspect separate evaluation evidence and immutable lineage.
6. Approve and atomically activate an exact version.
7. Reproducibly batch-score an immutable population.
8. Create an explainable CAL queue.
9. Roll back to a compatible approved version.
10. Recover safely from retries, timeouts, partial failures, and stale
    decisions.

Normal operation requires no direct Core DB, Artifact DB/object-store,
DBOS-console, or runtime-container access.
