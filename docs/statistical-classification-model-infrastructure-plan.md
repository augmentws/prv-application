# Statistical Classification Model Product and Platform Plan

Status: Proposed MVP implementation plan

Related document: `docs/jev-cal-review-program-workflow.md`

Backend execution companion:
`docs/statistical-classification-backend-implementation-plan.md`

## 1. Objective

Add the product and platform capabilities needed to configure, build, validate,
review, approve, promote, and run statistical classification models for the
Jev/CAL review program.

The MVP will use:

- Core and PostgreSQL for model identity, lifecycle, authorization, approvals,
  audit history, and active-model selection.
- DBOS for durable orchestration and recovery.
- A stateless model trainer worker for training, evaluation, calibration,
  packaging, and batch scoring.
- Artifact Service for immutable datasets, model packages, predictions, and
  evaluation reports.

This deliberately avoids introducing a separate MLOps control plane for the
MVP. MLflow and managed training services such as SageMaker remain optional
adapters that can be added without changing the domain model or lifecycle.

## 2. Scope and guiding decisions

### 2.1 MVP scope

The first implementation supports:

- Matter-wide or existing review-batch training scopes, frozen at build time.
- A selected Boolean or enum metadata field as the prediction target.
- Binary, native multiclass, and one-vs-rest classifier-set strategies.
- Deterministic, document-level embedding features.
- A regularized logistic-regression candidate model.
- Group-aware cross-validation and out-of-fold predictions.
- One probability-calibration method selected during implementation.
- CPU-based batch training and batch scoring.
- Manual approval and promotion.
- Immutable, safely loadable model packages.
- CAL as the first consumer of the generic classification subsystem.

### 2.2 Architectural decisions

1. Core is the system of record for lifecycle and governance.
   Experiment trackers and compute services are never authoritative for model
   status, approval, or the active model.
2. Artifact Service owns large and immutable data products. Core stores their
   artifact identifiers and semantic relationships, without cross-database
   foreign keys.
3. The trainer worker is stateless and does not write directly to Core. It
   consumes an immutable job specification and returns a result manifest.
4. DBOS owns orchestration, retries, reconciliation, and durable workflow
   state. Human approval starts a separate promotion workflow; a build workflow
   must not wait indefinitely for a person.
5. Model construction is separate from CAL policy. The package produces
   probabilities and diagnostics; Core applies queue-selection, stopping, and
   review-program policy.
6. Model content is immutable. Lifecycle transitions are mutable only through
   audited Core operations.
7. The MVP does not require MLflow, SageMaker, OpenSearch, a feature store,
   AutoML, or online model serving.
8. The primary product surface is the matter's review program, not a generic
   MLOps console. Users work in terms of readiness, candidates, validation,
   CAL rounds, and publication; internal runs and artifacts remain available
   as supporting lineage.
9. Training scope and label source are independent choices. A matter or review
   batch defines the eligible document universe; versioned label policy decides
   which documents in that universe have authoritative training labels.
10. A one-vs-rest build is one atomic model version containing multiple binary
    components. Approval, activation, scoring, and rollback apply to the bundle,
    not independently to child classifiers.

## 3. System boundaries

### 3.1 Core

Core owns:

- Logical classifier definitions and their matter/task scope.
- Dataset-snapshot records and semantic lineage.
- Training- and scoring-run records.
- Model-version identity and lifecycle state.
- Metric summaries needed for approval decisions.
- Approval, rejection, promotion, retirement, and rollback actions.
- The active model pointer for each classifier.
- Authorization, tenant/matter isolation, and audit events.
- CAL campaign/round linkage and materialized CAL queue items.

Core must not store full feature matrices, full prediction tables, serialized
estimators, or large evaluation reports.

### 3.2 Artifact Service

Artifact Service owns the bytes and storage metadata for:

- Frozen document-population manifests.
- Feature matrices and feature specifications.
- Label, source-training-weight, cohort, and split manifests.
- Out-of-fold and holdout predictions.
- Model packages.
- Evaluation reports, plots, and machine-readable metrics.
- Corpus-scale scoring outputs.

The Artifact Service must preserve content hashes, size, media type, artifact
schema version, derivation key, and lineage. Its embedded and remote modes must
expose equivalent behavior through the existing artifact gateway boundary.

### 3.3 Model trainer worker

The model trainer worker owns computation only:

- Validate the immutable input specification.
- Load approved inputs from Artifact Service.
- Train and compare allowed candidates.
- Produce group-aware out-of-fold predictions.
- Fit probability calibration.
- Evaluate the deserialized final package.
- Build and validate a safe model package.
- Upload outputs to Artifact Service.
- Return a result manifest to the DBOS workflow.

The worker does not approve models, choose the active model, create CAL queue
items, publish review results, or update Core database rows directly.

### 3.4 OpenSearch

OpenSearch is optional and rebuildable. The MVP does not require indexing every
prediction. If later used for discovery or visualization, it remains a
projection of authoritative Core and Artifact Service data.

## 4. Terminology and domain model

Use generic classification names so CAL is the first consumer rather than the
only possible consumer.

### 4.1 `classification_model`

A logical classifier within one tenant and matter, for one prediction target.
It stores stable configuration such as:

- `id`, `tenant_id`, and `matter_id`.
- Name and description.
- Required `target_metadata_definition_id` foreign key.
- Target field stable key and display-name snapshot.
- Pinned target type, cardinality, selected value keys, and schema fingerprint.
- Classification strategy: `BINARY`, `MULTICLASS`, or `ONE_VS_REST`.
- Ordered selected class keys and explicit binary positive/negative mappings
  where applicable.
- Label-source, missing-label, and conflict policies.
- Allowed feature-specification family.
- `active_model_version_id`, nullable.
- Creation/update metadata.

There is at most one active model version per logical classifier. Promotion
updates the pointer transactionally and records an audit event.

Tenant, matter, and target metadata field together form the model's domain
identity. Core must verify that the referenced metadata definition belongs to
the same matter and tenant when the logical classifier is created and whenever
a build, promotion, or scoring run is authorized. A model cannot be reassigned
to another field, matter, or tenant, even when names and schemas happen to
match.

The metadata-definition ID and stable key are authoritative identity. The
display name is important user and audit context, but it is renameable in the
existing metadata domain. Therefore Core and every immutable model version
record the display name used when the target was configured/trained while the
UI may also show the field's current display name. A rename does not detach or
silently retarget a model.

Multiple logical classifiers may target the same field when their strategy or
class mapping represents a genuinely different task. Their identities and
active-model pointers remain separate; field ID alone is not a model primary
key.

#### Identity row versus immutable configuration

Do not make historical model interpretation depend on mutable JSON columns on
the `classification_model` row. Use:

- `classification_model` for stable identity, matter/field ownership, user
  name/description, lifecycle, current configuration pointer, and active model
  version pointer.
- `classification_model_configuration` for immutable, numbered configuration
  revisions containing strategy, class mappings, label policies, feature
  contract, and prediction-resolution policy.

Every dataset snapshot and training run pins one configuration revision. Name
and description may be edited on the logical model, but a material prediction
contract change creates a new configuration revision. A revision already used
by a training run is never edited. Core determines whether a revision is
compatible with the same logical classifier or represents a sufficiently
different task that requires a new logical classifier.

Suggested `classification_model` fields are:

```text
id
tenant_id
matter_id
target_metadata_definition_id
name
description
status
current_configuration_id
active_model_version_id
created_at
created_by_user_id
updated_at
updated_by_user_id
row_version
```

Suggested `classification_model_configuration` fields are:

```text
id
classification_model_id
revision
target_field_snapshot
classification_strategy
ordered_class_keys
binary_mapping
label_policy
feature_contract
prediction_resolution_policy
configuration_hash
created_at
created_by_user_id
```

#### Ordered class keys and binary mappings

Use stable metadata enum keys or canonical Boolean tokens, never renameable
display labels, as class identity. `ordered_class_keys` defines the permanent
output index order for the configuration, evaluation artifacts, prediction
files, and model package. The order must be canonicalized once and stored; a
worker must not reorder classes based on observed frequency.

Strategy-specific interpretation is:

- `BINARY`: `binary_mapping` contains non-empty, disjoint
  `positive_value_keys` and `negative_value_keys`. A value not explicitly in
  either set is out of scope/unlabeled according to policy; "everything else"
  must never become negative implicitly.
- `MULTICLASS`: `ordered_class_keys` is the complete selected mutually
  exclusive class set. A usable row resolves to exactly one key.
- `ONE_VS_REST`: each ordered key identifies one child binary component. A
  multi-valued row may be positive for more than one component.

For example:

```json
{
  "classification_strategy": "BINARY",
  "ordered_class_keys": ["not_responsive", "responsive"],
  "binary_mapping": {
    "positive_value_keys": ["responsive"],
    "negative_value_keys": ["not_responsive"]
  }
}
```

#### Label policy

`label_policy` is a versioned structured object, not free-form text. It defines
which authoritative records may supply a training label and how to resolve
them. It includes:

- Allowed label source kinds, such as adjudicated review, a pinned human review
  batch run, accepted Jev output, or current asserted matter metadata.
- Authority/precedence order when more than one allowed source exists.
- Whether a source is gold, adjudicated, or silver and its approved sample
  weight policy.
- Maximum-age or as-of rules when applicable.
- `missing_label_action`: initially `EXCLUDE`; optionally `FAIL_SNAPSHOT` for a
  protocol requiring complete coding.
- `source_conflict_action`: `REQUIRE_ADJUDICATION`, `EXCLUDE`, or
  `FAIL_SNAPSHOT` after authority precedence has been applied.
- `target_value_conflict_action` for an invalid label shape, such as multiple
  selected values in a multiclass task.

An absent field value is always unlabeled unless an explicit reviewed
"none/negative" value is part of the target mapping. The selected matter or
batch scope must not turn missing values into negatives.

The configuration stores source policy, while the immutable label snapshot
records the exact event IDs, batch-run IDs, values, provenance, resolutions,
and weights actually used.

Source authority and training weight are independent concepts. Label
resolution happens first:

1. Gather allowed label claims for the document and target field.
2. Apply authority/precedence rules to select one authoritative label state.
3. Apply conflict policy when equally authoritative claims disagree.
4. Exclude unresolved or missing labels.
5. Assign the selected source's base training weight to the resolved row.

Do not create duplicate rows or add weights when the same document has several
agreeing sources. The highest-authority resolved source supplies one label and
one base source weight. A later gold/adjudicated label supersedes a silver
label; it does not coexist with it in the same snapshot.

Treat the source weight as relative influence on the training objective, not as
a probability that the label is correct. For a weighted loss:

```text
loss = sum(training_weight_i * loss_i) / sum(training_weight_i)
```

For example, a silver source weight of `0.25` gives each resolved silver row
one quarter of the loss influence of a weight-`1.0` gold row. It does not mean
the silver label is "25% correct."

For the MVP, store only:

```text
source_weight
normalized_training_weight
```

`normalized_training_weight` is the source weight normalized within each
training fold to mean `1.0`, so the overall magnitude does not silently change
the relationship between the data loss and regularization. Record both values
for reproduction and cap permitted source-weight ratios through policy.

Class-balancing weights are not part of the MVP contract. Class distribution
and per-class support remain required diagnostics, but balancing should be
added only later through an explicit trainer-policy revision if validation
shows it is needed. Sampling/design weights used for population estimates or
random-audit metrics belong to evaluation and are not training weights.

For multiclass, the resolved row has one weight. For a fully adjudicated
multi-label/one-vs-rest row, the same source weight may apply across its known
label vector. If annotation is only partial, store a per-class observed mask
and optional per-class source weights: an unobserved class is excluded from
that component's loss rather than treated as a negative.

#### Prediction conflict policy

Training-label conflicts and prediction conflicts are separate. Store
inference behavior in `prediction_resolution_policy`:

- Binary: positive threshold plus optional abstention band.
- Native multiclass: normally highest probability plus any minimum-confidence
  abstention rule.
- One-vs-rest for a multi-valued field: per-class thresholds; zero or many
  positives are valid.
- One-vs-rest for a single-valued field: explicit handling for zero classes or
  multiple classes over threshold, such as `ABSTAIN`, `HIGHEST_PROBABILITY`, or
  a policy-blocked result.

CAL acquisition may consume raw probabilities and uncertainty independently of
this resolution policy. Prediction resolution still must be versioned so
evaluation and any later operational interpretation are reproducible.

#### Feature contract

`feature_contract` identifies the allowed family of inputs, not one generated
feature artifact. It contains:

- Stable family key and contract version, such as
  `DOCUMENT_EMBEDDING_POOLED_V1`.
- Required artifact roles and schema versions.
- Required input dimension/data type.
- Allowed or exact embedding model/revision policy.
- Pooling, normalization, text-source, and missing-feature policies.

Each training run pins exact feature artifacts and a concrete
feature-specification hash compatible with this contract. Each model version
then pins that exact hash. This permits new snapshots using the same compatible
feature family without allowing a model trained on one embedding space to
score another.

#### Active model pointer

`active_model_version_id` is nullable until an approved version is activated
and after the model is fully retired. It must reference a version belonging to
the same logical classifier and compatible configuration lineage.

The pointer is the authoritative answer to "which version is active." Avoid a
second independently mutable `ACTIVE` flag on model versions. Promotion and
rollback lock the logical model row, validate the candidate, append a decision
record, and atomically replace this pointer. Historical activation intervals
come from append-only decision/activation events.

#### Creation and update metadata

`created_at`/`created_by_user_id` record the origin of the logical model.
`updated_at`/`updated_by_user_id` apply only to permitted mutable identity
fields and pointer/lifecycle changes. `row_version` supports optimistic
concurrency for UI previews and decisions.

Configuration revisions, model versions, decisions, and activation events each
carry their own immutable creation metadata. Do not rely on the logical
model's `updated_at` timestamp to reconstruct who changed configuration or
activated a model.

For `ONE_VS_REST`, the logical classifier and model version represent the
complete set of child binary estimators. Child component identities, class
keys, thresholds, calibration, and metrics live in the immutable package and
evaluation artifacts. They are not separately promoted.

### 4.2 `classification_dataset_snapshot`

An immutable description of the exact data used for training, validation, or
scoring. It references Artifact Service artifacts for:

- Document population and content hashes.
- Features.
- Labels and label provenance.
- Source and normalized training weights.
- Group identifiers.
- Cohort assignments.
- Train/cross-validation/holdout assignments.

It also stores the feature-specification hash, snapshot-purpose enum, creation
metadata, and an aggregate content hash.

### 4.3 `classification_training_run`

A durable request to build one candidate model version. Suggested fields:

- `id`, `classification_model_id`, and `dataset_snapshot_id`.
- DBOS workflow identifier.
- Immutable trainer specification and configuration hash.
- Validation-policy identifier and version.
- Random seed and runtime image digest.
- Derivation key.
- Status, timestamps, failure category, and sanitized failure summary.
- Result-manifest artifact ID, when available.

The derivation key is unique within the classifier and is computed from all
inputs that affect output. A retry with the same key returns or reconciles the
same result instead of training a second logical version.

### 4.4 `classification_model_version`

An immutable candidate or released version produced by a completed training
run. Suggested fields:

- `id`, `classification_model_id`, and `training_run_id`.
- Monotonic display version.
- Tenant ID, matter ID, target metadata-definition ID, stable key, and field
  display-name snapshot inherited from the logical classifier.
- Target-schema fingerprint and ordered value-key mapping used for training.
- Model package artifact ID and content hash.
- Evaluation artifact ID.
- Package schema version and inference-signature hash.
- Feature-specification hash.
- Runtime image digest and source-code revision.
- Selected summary metrics.
- Lifecycle status.

Suggested lifecycle statuses are:

- `CANDIDATE`
- `APPROVED`
- `REJECTED`
- `RETIRED`

The package and evaluation references do not change after candidate creation.
Status transitions are authorized operations with actor, reason, timestamp,
and before/after values in the audit trail. Active use is derived exclusively
from `classification_model.active_model_version_id`; it is not a second model
version status.

### 4.5 `classification_evaluation`

An immutable evaluation event for one model version against one named dataset
snapshot and one metric-definition version. It records the evaluation kind
(for example development cross-fit, sealed validation, random audit, or
targeted QA), summary metrics, cohort status, policy outcome, and detailed
evaluation artifact ID.

Separate records prevent evidence produced under different sampling and
blinding protocols from being merged into an ambiguous model score.

### 4.6 `classification_model_decision`

An append-only record for approval, rejection, promotion, rollback, and
retirement. It includes the actor, decision, reason, policy/version evaluated,
metric snapshot, and authorization context.

This record prevents lifecycle history from being lost when the current model
pointer changes.

### 4.7 `classification_scoring_run`

A durable batch-scoring request that references:

- Model version.
- Scoring dataset snapshot.
- Scoring-policy hash.
- DBOS workflow identifier and derivation key.
- Prediction-set artifact ID.
- Status, counts, timestamps, and sanitized failures.

Uniqueness on model version, snapshot, and scoring-policy hash prevents
duplicate logical scoring runs.

### 4.8 CAL integration records

The workflow draft's proposed `cal_model_version` should not duplicate the
generic model registry. Instead:

- `cal_campaign` references a `classification_model`.
- Each `cal_round` references the model version and scoring run used for that
  round.
- A CAL-specific association may store acquisition-policy configuration, but
  it must not contain another serialized model or separate approval state.
- `cal_queue_item` contains only the operationally selected documents and the
  prediction facts needed to explain their selection.

Promotion here means authorizing a statistical model for program use. It is
distinct from publishing review tags or other matter results.

## 5. Immutable dataset construction

### 5.1 Training scope and document population

Every build selects exactly one training scope:

- `MATTER`: every document that belongs to the matter when the snapshot is
  created.
- `REVIEW_BATCH`: the permanent frozen membership of one ready review batch in
  the same matter.

Selecting the matter does not create an indefinitely changing training set.
The snapshot builder freezes the current matter membership at build time. A
review batch already has frozen membership, but the snapshot still records its
batch ID, membership version/hash, and provenance.

The selected scope is the eligible universe, not necessarily the final set of
supervised training rows. Feature availability, label authority, target
validity, blinding rules, and exclusions determine which eligible documents
become labeled training rows. Eligible unlabeled documents remain available
for later scoring and CAL selection; they must never be silently treated as
negative examples.

The snapshot builder freezes:

- Scope kind and matter or review-batch identifier.
- Exact document identifiers.
- Source item/version identifiers.
- Content and extraction hashes.
- Eligibility/exclusion reason for every considered document.
- Family, duplicate, and near-duplicate grouping identifiers when available.
- Cohort attributes required for evaluation.

The population artifact must make later reproduction possible even if matter
membership, batch metadata, content, or coding subsequently changes.

### 5.2 Target field and classification strategy

The target specification pins:

- Tenant ID, matter ID, metadata-definition ID, stable key, display name at
  configuration time, type, cardinality, and schema fingerprint.
- Ordered active enum value keys or Boolean values included in the task.
- Classification strategy.
- Label source and authority policy.
- Missing, conflicting, superseded, and out-of-scope value handling.
- For binary tasks, the explicit positive and negative value sets.
- For one-vs-rest tasks, the class keys that receive child classifiers.
- For single-cardinality one-vs-rest output, the versioned threshold,
  conflict, and abstention/resolution policy.

Supported strategies are:

- `BINARY`: one explicit positive class versus one explicit negative class or
  negative value set.
- `MULTICLASS`: one native model over mutually exclusive classes. Every usable
  training row has exactly one selected class, and class probabilities form a
  single distribution.
- `ONE_VS_REST`: one binary estimator per selected class. Probabilities are
  independent and need not sum to one. This strategy supports multi-valued
  fields and may also be deliberately selected for a single-valued field.

Native multiclass is valid only for a single-cardinality target whose selected
values are mutually exclusive. A multi-valued enum requires `ONE_VS_REST`.
Boolean fields normally use `BINARY`.

Default strategy guidance is:

- Use `BINARY` for a two-state decision such as responsive/not responsive.
- Use `MULTICLASS` when the field represents exactly one of several mutually
  exclusive outcomes and a single probability distribution matches the domain.
- Use `ONE_VS_REST` when multiple values may be simultaneously correct, or
  when product policy specifically requires per-class thresholds and
  independent abstention decisions.

Do not default a single-valued enum to one-vs-rest merely because there are
multiple class values. One-vs-rest can produce no class above threshold or
several classes above threshold, so a single-valued target then requires an
explicit resolution/abstention policy. Native multiclass avoids that ambiguity
and models competition among mutually exclusive classes. Conversely, native
multiclass is semantically wrong for a genuinely multi-valued field.

Target configuration is immutable for a logical classifier. A material change
to field type/cardinality, selected classes, binary mapping, or classification
strategy creates a new logical classifier or an explicit incompatible
configuration revision; it must not silently alter an active model.

Field lifecycle compatibility rules are:

- Display-name or enum-label changes retain identity because IDs/stable value
  keys are unchanged; the UI shows both the current label and trained snapshot
  when they differ.
- Field deactivation preserves historical models but blocks new builds,
  promotion, and scoring for operational use.
- Stable-key, type, or cardinality incompatibility blocks use. The current
  metadata rules already prevent most such mutation, but model checks remain
  defense in depth.
- Deactivation or removal of a selected enum value requires an explicit
  compatibility decision and normally blocks new activation.

### 5.3 Feature specification

The initial feature set should reuse deterministic document embeddings derived
from existing chunk/vector artifacts. The specification pins:

- Embedding provider/model and revision.
- Vector dimension and numeric type.
- Text source and preprocessing version.
- Chunk-to-document pooling algorithm and version.
- Normalization rules.
- Missing/failed-feature handling.

The canonical serialized specification is hashed. Training and scoring reject
features whose hash does not match the model package.

### 5.4 Labels and weights

The label snapshot records for every labeled document:

- Binary label, single multiclass key, or one-vs-rest label vector according
  to the pinned target specification.
- Label source: gold, adjudicated, silver, or another explicitly allowed type.
- Source review event/version.
- Label timestamp and authority.
- Source weight and normalized training weight.
- Inclusion/exclusion reason.

Gold and adjudicated labels take precedence over silver labels. A later
authoritative label supersedes rather than silently mutates the old snapshot.
Random-audit, sealed-validation, and other blinded labels must remain excluded
from training until their protocol explicitly unlocks them.

Absence of a current metadata value does not prove a negative label. It is
`UNLABELED` unless the selected label protocol contains an explicit reviewed
negative/none decision. For multiclass, zero or multiple selected class values
are invalid or unlabeled according to policy. For one-vs-rest, a negative for
one class must be supported by an authoritative reviewed value; incomplete
coding must not manufacture negative examples.

### 5.5 Split construction

Splits must be group-aware: family members, duplicates, and identified
near-duplicates remain in the same fold. The split artifact records group IDs,
fold assignments, seed, algorithm version, and any stratification/cohort rules.

The worker consumes a frozen split artifact; it must not silently create a new
split during training.

## 6. Trainer worker contract

### 6.1 Input job specification

The versioned input schema contains at least:

```text
training_run_id
training_scope_type
training_scope_id
target_specification
classification_strategy
trainer_specification
feature_artifact_id
label_artifact_id
split_artifact_id
feature_specification_hash
validation_policy
random_seed
output_derivation_key
```

`training_scope_id` is the matter ID for `MATTER` scope or the review-batch ID
for `REVIEW_BATCH` scope. `target_specification` contains the pinned metadata
schema fingerprint, ordered class keys, label/missing-value policy, and any
binary or one-vs-rest mappings. `trainer_specification` is a discriminated,
versioned object containing a registered trainer kind and only the parameters
allowed by that trainer's schema.

The job should also carry tenant/matter scope, input content hashes, allowed
output artifact types, contract version, and a short-lived scoped credential
or job identity. Arbitrary source code, import paths, command lines, or
executable hyperparameter expressions are prohibited.

### 6.2 Result manifest

The versioned result schema contains at least:

```text
status
model_artifact_id
model_content_hash
prediction_artifact_id
evaluation_artifact_id
metrics
cohort_metrics
package_schema_version
runtime_image_digest
warnings
```

It also includes the input derivation key, input hashes, classification
strategy, ordered class/component manifest, selected trainer configuration,
source revision, row counts, per-class counts, exclusion/failure counts, and
output artifact hashes.

If outputs already exist for the same derivation key and their hashes validate,
the worker returns the existing result manifest. This makes workflow retries
safe across ambiguous timeouts.

### 6.3 Stable implementation interfaces

Introduce narrow interfaces so local MVP behavior can later be replaced:

- `ClassificationTrainer`
- `ClassificationExecutionBackend`
- `ModelPackageLoader`
- `ExperimentTracker`

Initial implementations:

- Scikit-learn logistic-regression trainer.
- DBOS/local worker execution backend.
- Private View model-package loader.
- No-op experiment tracker.

Future optional implementations:

- SageMaker training/scoring execution backend.
- MLflow experiment tracker or managed SageMaker MLflow tracker.

External trackers receive non-sensitive run metadata and artifact references,
not authoritative lifecycle state or raw evidence content.

### 6.4 Trainer registry and model-specific parameters

Support different model families through a code-owned trainer registry rather
than one unvalidated parameters dictionary. Each registered trainer declares:

- Stable trainer kind and contract version.
- Supported classification strategies.
- Required feature/input artifact roles and schema versions.
- A strict Pydantic parameter schema with defaults and bounds.
- Output/package schema versions it can produce.
- Runtime/resource capabilities such as CPU/GPU support.

For example:

```json
{
  "kind": "sklearn_logistic_regression",
  "contract_version": 1,
  "parameters": {
    "regularization": "l2",
    "c_values": [0.1, 1.0, 10.0],
    "maximum_iterations": 1000
  }
}
```

Future kinds may require different parameters or additional input artifact
roles while retaining the same outer training-job and result-manifest
envelopes. Unknown kinds, parameters, artifact roles, and incompatible
strategy/input combinations fail contract validation before workflow dispatch.
The canonical validated trainer specification is stored and hashed by Core.

The runtime capability endpoint reports installed trainer kinds and contract
versions for deployment diagnostics. User-selectable choices still come from
Core product policy; the browser must not treat whatever happens to be
installed on a worker as automatically authorized.

## 7. Training and evaluation process

For each build, the worker performs these deterministic stages:

1. Validate job schema, authorization scope, artifact hashes, feature
   compatibility, label validity, and split completeness.
2. Load the frozen features, labels, weights, groups, cohorts, and folds.
3. Train the allowlisted candidate configurations across the frozen folds. A
   one-vs-rest build trains all required binary components under one run.
4. Produce out-of-fold probabilities for every labeled training document and
   retain ordered class/component semantics.
5. Compute the versioned selection metrics and choose a candidate using a
   deterministic tie-break rule.
6. Fit the chosen base estimator on the permitted development population.
7. Fit probability calibration using the selected, documented protocol.
8. Export the estimator, preprocessing, calibration, labels, and signature.
9. Reload the exact exported package and evaluate that deserialized package.
10. Upload the package, predictions, and evaluation artifacts.
11. Return the result manifest to the DBOS workflow.

### 7.1 Metric sets

At minimum, evaluation should report:

- Confusion matrix at each policy-relevant threshold.
- Precision, recall, false-negative rate, and false-positive rate.
- PR-AUC; ROC-AUC may be reported but is not sufficient by itself.
- Brier score and log loss for probability quality.
- Calibration table/curve data.
- Coverage and feature/scoring failure counts.
- The same required metrics by approved cohort.
- Confidence intervals where the sampling design supports them.

For multiclass and one-vs-rest tasks, report per-class metrics and support,
macro and micro aggregates, and strategy-appropriate calibration. A native
multiclass confusion matrix uses one selected class per row. One-vs-rest uses
one binary confusion matrix per component and separately reports multi-label or
single-cardinality conflict/abstention behavior. Aggregate metrics must never
hide a class that fails a minimum-support or safety constraint.

Metric names, formulas, averaging rules, threshold rules, and schema are
versioned. Development cross-validation, sealed validation, random audit, and
targeted QA results remain visibly separate; they must not be blended into one
unqualified score.

### 7.2 Candidate-selection policy

The MVP uses a code-owned, versioned selection policy. It should specify:

- The primary metric and any safety constraints.
- Minimum sample/count requirements.
- Permitted model and hyperparameter grid.
- Deterministic tie-breaking.
- Required cohort checks.
- Conditions that force a failed build or a warning.

Passing automated checks creates a `CANDIDATE`; it never activates the model.

## 8. Model packaging

Packaging is a first-class, durable stage rather than an incidental call to a
serialization library.

### 8.1 Establish package identity

Compute a deterministic package derivation key from:

- Dataset and input artifact hashes.
- Feature-specification hash.
- Trainer specification and selection-policy version.
- Source-code revision and runtime image digest.
- Random seed.
- Package schema version.

### 8.2 Verify preconditions

Before export, verify that training completed, expected folds and rows are
present, candidate selection succeeded, no prohibited labels were used, and
the fitted model is compatible with the declared inference signature.

### 8.3 Export a safe representation

Do not use general-purpose pickle/joblib packages for promoted models. For the
initial logistic-regression implementation, store only constrained data needed
for inference, such as:

- Coefficients and intercepts in NPZ or another non-executable numeric format;
  NPZ loading must use `allow_pickle=False` and reject object arrays.
- Preprocessing and feature rules in validated JSON.
- Calibration parameters in JSON/NPZ.
- Classification strategy, ordered class keys, binary mappings, and component
  identities in JSON.

The loader supports explicit package schema versions and never imports or
executes code named by the package.

### 8.4 Define the inference contract

`signature.json` specifies:

- Input feature shape, dimension, type, normalization, and missing-value rules.
- Output class order and probability semantics.
- Whether probabilities form one normalized multiclass distribution or are
  independent one-vs-rest probabilities.
- Any single-cardinality conflict/abstention resolution policy.
- Batch-size/shape constraints.
- Package and signature schema versions.

### 8.5 Build the package manifest

The package should contain a layout similar to:

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

`manifest.json` records package identity; tenant, matter, target field ID,
stable key, field-name snapshot, and target-schema fingerprint; training run;
input hashes; feature and trainer specifications; source revision; runtime
image digest; creation time; and every file's media type, size, and hash.

`model-card.json` contains non-sensitive intended use, exclusions, training and
evaluation summaries, limitations, and approval-relevant warnings. It must not
embed raw evidence text or reviewer notes.

### 8.6 Validate package safety and integrity

Before upload:

- Validate every document against a versioned schema.
- Reject absolute paths, path traversal, unexpected files, oversized entries,
  duplicate names, and unsupported formats.
- Verify every recorded size and hash.
- Scan structured metadata for secrets and prohibited evidence content.
- Confirm the archive contains no executable files or code-loading directives.

### 8.7 Round-trip and evaluate exact bytes

Reload the exact completed package through `ModelPackageLoader` and verify:

- Probabilities match the pre-export result within a fixed tolerance.
- Class ordering and positive-class semantics are correct.
- Repeated loads and predictions are deterministic.
- Invalid dimensions and feature hashes are rejected.
- Evaluation metrics recomputed from the package match the report.

Upload those exact bytes; do not reconstruct or reserialize the model after
evaluation.

### 8.8 Finalize only after artifact persistence

Core creates the immutable model version only after the package, evaluation,
and prediction artifacts are durably stored and their hashes are verified.

Useful internal packaging states are `EXPORTING`, `VALIDATING`, `EVALUATING`,
`PUBLISHING`, `FINALIZING`, `FAILED`, and `RECONCILING`.

## 9. DBOS workflows

### 9.1 `classification_model_build_v1`

1. Reauthorize the launch against current tenant, matter, and role state.
2. Create or reuse a training run by derivation key.
3. Freeze or verify the dataset snapshot and all input hashes.
4. Dispatch the immutable job to the trainer worker.
5. Poll or receive completion without holding a database transaction.
6. Validate the returned result schema, scope, derivation key, and hashes.
7. Reconcile ambiguous artifact uploads by derivation key.
8. Create the immutable `CANDIDATE` model version and summary evaluation rows.
9. Write audit events and finish at `AWAITING_APPROVAL`/candidate review.

The workflow must not promote a model or wait for a human decision.

### 9.2 `classification_model_promote_v1`

1. Reauthorize the actor at execution time.
2. Lock the logical classifier and candidate version.
3. Confirm the candidate is unchanged, artifacts remain valid, and all
   required checks/decisions are present.
4. Append the approval/promotion decision with its reason.
5. Retire the previous active version if one exists.
6. Atomically update the active-model pointer to the approved candidate.
7. Optionally launch a separate initial scoring run.
8. Emit audit and domain events.

Rollback uses the same controlled mechanism and points to a previously approved
compatible version; it is not a direct database edit.

### 9.3 `classification_model_score_v1`

1. Freeze or verify the scoring population and feature snapshot.
2. Resolve and pin the requested model version; never resolve `active` again
   after the run starts.
3. Verify package hash, schema, signature, tenant/matter/target-field identity,
   current field compatibility, and feature compatibility.
4. Run batch inference and capture per-row failures.
5. Upload the complete prediction set and scoring summary to Artifact Service.
6. Reconcile and finalize the scoring-run record.
7. Invoke CAL queue materialization under a separate, versioned acquisition
   policy when requested.

### 9.4 Workflow idempotency and recovery

- Every externally visible step has a stable idempotency/derivation key.
- Artifact uploads are content-addressed or otherwise idempotently reconciled.
- Core finalization transactions check expected prior state.
- Worker crashes and DBOS retries cannot create two logical model versions.
- Timeouts after upload but before Core commit enter reconciliation rather than
  retraining.
- Cancellation stops future dispatch where possible but never deletes existing
  audit or immutable artifacts.

## 10. Prediction storage and CAL queue materialization

The complete prediction set belongs in Artifact Service, preferably a
columnar format such as Parquet. Each row should include:

- Document/source identifier and feature-content hash.
- Model version and scoring-run identifier.
- Positive/negative probability and decision margin.
- Uncertainty and out-of-distribution diagnostics, when implemented.
- Acquisition score/rank when produced.
- Success, exclusion, or error status with a sanitized code.

Core must not create one database row for every diagnostic prediction. It
materializes only documents selected for operational CAL review as
`cal_queue_item` records. Each queue item records the scoring run, model
version, acquisition-policy version, rank/score, selection reason, and status.

These predictions are model diagnostics and selection inputs, not
`ReviewBatchRunValue` coding candidates. Human review remains the source of
review decisions.

## 11. Product and UI plan

The UI is a first-class part of the MVP. A user should be able to complete the
review-program lifecycle without knowing DBOS workflow names, Artifact Service
IDs, model package formats, or ML terminology beyond what is necessary to make
an informed decision.

### 11.1 Product vocabulary

Prefer review-program language in the UI:

| Platform term | Default UI term |
| --- | --- |
| `classification_model` | Classifier |
| `classification_model_version` | Model version or candidate |
| `classification_dataset_snapshot` | Training data snapshot |
| `classification_training_run` | Model build |
| `classification_scoring_run` | Scoring run |
| acquisition policy | CAL selection strategy |
| promote | Make active |
| retire | Remove from active use |
| derivation key | Reproducibility details |

Do not expose `Jev`, `CAL`, confidence interval, calibration, or sampling terms
without short contextual explanations and links to the relevant methodology.
The UI must distinguish:

- Building a model from approving a candidate.
- Making a model active from publishing review coding.
- Development evidence from formal validation evidence.
- Model-predicted probability from a human review decision.

### 11.2 Information architecture and routes

Add `Review program` as a matter-level tab alongside the existing Matter
definition, Analysis tasks, Batches, Jobs, and Search index tabs.

The initial entry route is:

```text
/app/clients/{clientId}/matters/{matterId}?tab=review-program
```

The matter tab renders a focused `ReviewProgramPanel`; it must not add all model
queries and mutations directly to the already-large `MatterView` component.
Use stable deep links for program, run, and model-version details so refresh,
sharing, breadcrumbs, and browser navigation work:

```text
/app/clients/{clientId}/matters/{matterId}/review-programs/{programId}
/app/clients/{clientId}/matters/{matterId}/review-programs/{programId}/builds/{runId}
/app/clients/{clientId}/matters/{matterId}/review-programs/{programId}/models/{versionId}
/app/clients/{clientId}/matters/{matterId}/review-programs/{programId}/cal-rounds/{roundId}
```

Within a program, provide these sections:

1. **Overview** — readiness, active model, current phase, important warnings,
   next action, and recent activity.
2. **Training data** — source populations, label mix, weights, exclusions,
   grouping coverage, feature readiness, and immutable snapshots.
3. **Models** — build history, candidates, active/retired versions, comparison,
   approval, promotion, and rollback.
4. **Validation** — development, sealed validation, random audit, and targeted
   QA evidence kept in separate panels.
5. **CAL rounds** — scoring/selection runs, queue size, review progress, label
   additions, and the model/version used by each round.
6. **Publication** — publication readiness and history when that workflow is
   implemented. It must not be implied by model promotion.
7. **Activity** — an append-only, user-readable timeline of material actions
   and failures.

Every program, build, model-version, validation, and CAL detail header shows
the target field's current display name and stable key. If the current name
differs from the immutable trained/configured name snapshot, show the rename
explicitly rather than making historical records appear to target a different
field.

If the MVP permits only one review program per matter, the tab may open it
directly. The route and API should nevertheless retain a program ID so adding a
second target later does not require breaking URLs.

### 11.3 Roles and visible capabilities

The server returns explicit capabilities for the current user and resource,
such as:

```text
can_create_program
can_build_model
can_cancel_build
can_review_candidate
can_promote_model
can_rollback_model
can_launch_scoring
can_manage_cal_round
can_view_detailed_lineage
```

The UI uses capabilities to hide irrelevant controls and explain disabled
ones, but the API always enforces authorization independently.

Suggested product roles are:

- **Reviewer:** works CAL queue items and sees the program context needed to
  understand the assignment.
- **Review lead:** monitors progress, reviews errors, launches allowed rounds,
  and assesses candidate evidence.
- **Matter administrator:** configures the program and authorizes promotion,
  rollback, and publication-related actions.
- **Auditor/support operator:** sees lineage and activity without gaining
  decision authority.

The final permission mapping remains an open product decision. UI tests must
cover both visible actions and deep-link access for each capability set.

### 11.4 Empty state and program setup

When no review program exists, the tab shows a plain-language explanation and
a `Set up review program` action. The setup flow is a guided wizard, not a raw
classifier form.

#### Step 1: Purpose and target

Collect:

- Program name and optional description.
- Review objective.
- Target Boolean or enum metadata field.
- Classification strategy: binary, multiclass, or one-vs-rest classifier set.
- Selected class values.
- For binary classification, explicit positive and negative value mappings.

Validate that the target field is active and reviewable. Boolean fields default
to binary. Single-cardinality enums permit multiclass or one-vs-rest.
Multi-valued enums require one-vs-rest. Explain the probability and decision
semantics of the selected strategy and that changing the target or strategy
later creates a new classifier identity rather than mutating existing lineage.

#### Step 2: Label policy

Show available label sources with counts and authority:

- Gold/adjudicated human labels.
- Accepted Jev labels if policy permits them.
- Existing asserted matter coding if policy permits it.
- Explicitly excluded sealed/random-audit labels.

The user chooses only among server-approved policy options. Default weights and
precedence come from versioned policy; advanced numeric weights should not be a
free-form MVP control.

#### Step 3: Eligible population and grouping

Choose exactly one scope:

- Entire matter as of snapshot creation.
- One existing ready review batch in the same matter.

Show the eligible population, major exclusions, family/duplicate grouping
coverage, and warnings. Explain that the scope is frozen and that unlabeled
documents are excluded from supervised training rather than treated as
negative. More complex saved-search scopes are outside the initial contract;
the user can create a review batch first when a curated subset is required.

#### Step 4: Feature readiness

Show whether compatible document embeddings exist and their coverage. If not,
provide an authorized `Generate embeddings` action or link to the matter Jobs
tab. Users should not select Artifact Service objects or enter model IDs.

#### Step 5: Review and create

Present the complete configuration, scope source, classification strategy,
estimated eligible/labeled counts by class, blocking issues, and warnings.
Creation uses an idempotency key and returns the new program summary.

The wizard is resumable as a server-side draft if setup is expected to span
sessions. If draft persistence is deferred, state that explicitly and warn
before dismissing unsaved work.

### 11.5 Program overview

The overview answers four questions without requiring drill-down:

1. Is the program ready to build, validate, run CAL, or publish?
2. Which model is active, and when/why was it activated?
3. What is happening now?
4. What should the user do next?

Show:

- A stage/readiness strip: `Setup`, `Initial review`, `Model`, `Validation`,
  `CAL`, and `Publication`.
- One primary next-action card generated from server-provided readiness state.
- Active-model card with version, activation date/actor, training snapshot,
  and key policy metrics.
- In-progress work with durable stage, progress, start time, and cancel/retry
  actions where allowed.
- Blocking issues separated from non-blocking warnings.
- Recent material activity.

Readiness is computed in Core and returned with reason codes and remediation
links. The browser must not reproduce eligibility, approval, or validation
policy with ad hoc conditionals.

### 11.6 Training data view

The Training data section provides a decision-oriented snapshot list and a
detail drawer/page.

The current snapshot summary shows:

- Scope kind and matter/review-batch source.
- Target field, selected strategy, and included classes.
- Eligible, labeled, unlabeled, and excluded counts.
- Binary positive/negative counts or multiclass/one-vs-rest support and
  exclusions per class.
- Counts by label authority and weight policy.
- Feature coverage and failure count.
- Number of grouping units and ungrouped documents.
- Cohort coverage warnings.
- Creation time, creator, policy version, and content hash abbreviation.

The detail view includes:

- Label-source and class-distribution tables.
- Exclusion reasons.
- Fold/group distribution and leakage-check result.
- Feature specification in human-readable form.
- Lineage to underlying artifacts in an expandable technical section.

Provide `Preview new snapshot` before `Build model`. The preview is generated
server-side and shows how the population differs from the last snapshot. The
user confirms the preview; they never edit a frozen snapshot.

### 11.7 Launching a model build

`Build model` opens a review dialog or short wizard with:

- The snapshot that will be frozen/used.
- Training scope, target field, strategy, and per-class label counts.
- Grouping and feature-readiness checks.
- The code-owned algorithm family and selection-policy version.
- Expected resource class and a coarse time estimate if available.
- Blocking issues and warnings.

Advanced details may show the allowlisted hyperparameter grid and random seed,
but the MVP should not present a generic experiment editor.

Submission requires explicit confirmation and an idempotency key. On success,
navigate to the build detail rather than leaving the user on a toast-only
acknowledgment.

### 11.8 Build detail and progress

The build detail maps internal stages to understandable user-facing steps:

| Internal stage | UI label |
| --- | --- |
| `SNAPSHOTTING` | Freezing training data |
| worker dispatch/queue | Waiting for training capacity |
| training/cross-fit | Training candidate models |
| calibration | Calibrating probabilities |
| `EXPORTING`/`VALIDATING` | Packaging and checking the model |
| `EVALUATING` | Evaluating the packaged model |
| `PUBLISHING`/`FINALIZING` | Saving results |
| candidate complete | Ready for review |
| `RECONCILING` | Verifying an interrupted result |

Show a stage stepper, timestamps, progress counts when meaningful, warnings,
and sanitized error details. Poll with React Query only while the run is
active, following the existing job-panel pattern. Stop polling in terminal
states and provide explicit retry/reconcile actions only when the API says
they are safe.

Cancellation requires confirmation and explains that already-created
immutable artifacts and audit records remain. A completed build links directly
to its candidate-review page.

### 11.9 Models list and version comparison

The Models section uses a table with:

- Version and lifecycle status.
- Created date and actor.
- Training snapshot label/count summary.
- Primary development metric with metric-definition version.
- Validation status shown separately.
- Active-since date.
- Warnings/failures.
- Contextual actions allowed by capability and state.

Default ordering is active model first, then newest candidates. Filters include
status and date; do not mix build-run failure rows with completed model versions
without labeling them clearly.

`Compare` supports two or three compatible versions. The server returns
like-for-like metrics or marks a cell incomparable when dataset, metric, class,
or policy definitions differ. The UI must not silently line up unlike metrics.

Comparison includes:

- Primary and safety metrics.
- Calibration quality.
- Cohort results and warnings.
- Training data/label differences.
- Feature and runtime/package differences.
- Validation evidence by evidence type.

### 11.10 Candidate review page

This is the core decision surface, not a generic model detail page. Its order
should reflect the approval decision:

1. **Decision summary:** candidate status, recommended/blocked state, compared
   active model, and unresolved warnings.
2. **Evidence:** development metrics, confusion counts, calibration, and
   cohort results with denominators.
3. **Validation:** sealed/random/QA evidence in separate labeled sections.
4. **Training data:** population, labels, weights, grouping, exclusions, and
   comparison with the active model's snapshot.
5. **Limitations and model card:** intended use, exclusions, and known risks.
6. **Technical lineage:** package hash, feature specification, code/runtime,
   artifacts, and reproducibility metadata.
7. **Decision history:** who approved/rejected/promoted/retired and why.

Charts should supplement exact counts and accessible tables. At minimum, use:

- Precision/recall and error counts at policy-relevant thresholds.
- A reliability/calibration plot with tabular equivalent.
- Cohort comparison with sample sizes and warning states.

Avoid decorative dashboards and a single composite “model score.” Unknown,
insufficient-sample, and not-evaluated states must be visibly distinct from a
passing result.

`Approve` and `Reject` require a reason. Approval does not implicitly make the
model active unless product policy explicitly chooses a combined action.

### 11.11 Promotion, rollback, and retirement interactions

`Make active` opens a server-backed confirmation preview showing:

- Candidate version and package identity.
- Current active version that will be replaced.
- Required approvals and their current validity.
- Validation/policy checks and warnings.
- Programs/rounds affected.
- Whether initial scoring will be launched.

The mutation sends the preview token/version plus a reason. Core reauthorizes
and recomputes all safety checks when the DBOS workflow executes. A stale
preview produces a clear conflict and a refreshed preview; the UI must not
blindly retry promotion.

Rollback is presented as another controlled activation, not an “undo” button.
It identifies the target version, compatibility checks, effect on in-progress
runs, and required reason. Retirement is unavailable for versions still needed
by an active pointer or protected reference.

### 11.12 CAL rounds and reviewer handoff

The CAL section lists rounds with:

- Round number/status.
- Exact model version and scoring run.
- Eligible population and selected queue count.
- Selection-strategy version.
- Review progress and class balance of completed human decisions.
- New authoritative labels available for the next build.
- Start/completion dates and responsible lead.

`Start CAL round` first calls a preview endpoint that returns population,
feature coverage, active model, proposed queue size, exclusions, and warnings.
The user confirms the exact preview version. Scoring and queue materialization
then run durably.

When a round is ready, `Open review queue` routes into the existing review
workspace with the CAL batch/queue selected. The review workspace should add a
compact context banner showing program, round, queue progress, and why the
document was selected, without exposing a probability as if it were a coding
recommendation.

Completed review decisions remain in the normal coding/history experience and
become eligible label inputs under the configured authority policy. The CAL
screen links to the next snapshot/build preview rather than automatically
retraining.

### 11.13 Validation and publication views

The Validation section organizes evaluations by protocol, not merely by model:

- Development cross-fit.
- Sealed validation.
- Random audit.
- Targeted QA.

Each card shows model version, dataset/sampling design, evaluated counts,
confidence interval where applicable, status, date, and protocol version. The
screen must preserve blinding and unsealing permissions; unauthorized users see
the protocol state without hidden labels/results.

The Publication section remains separate. It shows publication eligibility,
the exact reviewed/tagged population, validation evidence used, preview token,
approval state, execution history, and partial-failure recovery. Until the
publication design issues in the related workflow draft are resolved, the MVP
may show this section as unavailable rather than inferring publication from an
active model.

### 11.14 Activity and support diagnostics

Provide a user-readable event timeline containing:

- Program creation/configuration revisions.
- Snapshot creation.
- Build/scoring/CAL lifecycle transitions.
- Approval, rejection, promotion, rollback, retirement, and publication
  decisions.
- Failures, cancellations, reconciliations, and retries.

Each entry includes actor/service, time, plain-language summary, reason, and a
link to the affected resource. Technical IDs, hashes, DBOS workflow IDs, and
artifact links appear in an expandable `Technical details` section for users
with the capability to view them.

Provide copyable correlation IDs and downloadable sanitized diagnostics for
support, but never raw evidence, credentials, or signed object URLs.

### 11.15 Required UI states

Every screen/component must design and test:

- Loading/skeleton state.
- Empty/not-yet-configured state.
- Permission-denied state.
- Blocking prerequisite state with remediation.
- Non-blocking warning state.
- Active progress with polling.
- Partial-success/completed-with-errors state.
- Recoverable failure with an allowed action.
- Terminal failure with support details.
- Stale preview/version conflict.
- Resource deleted, retired, or no longer available.

Use the existing `QueryError`, `TableLoading`, `StatusBadge`, toast, dialog,
table, and card patterns where appropriate. Toasts acknowledge mutations but
must not be the only place a durable outcome or failure is visible.

### 11.16 Frontend component boundaries

Suggested components/modules are:

```text
review-program-panel.tsx
review-program-overview.tsx
review-program-setup-dialog.tsx
training-data-snapshots-panel.tsx
classification-build-dialog.tsx
classification-build-detail.tsx
classification-models-panel.tsx
classification-model-review.tsx
classification-model-compare.tsx
classification-promotion-dialog.tsx
cal-rounds-panel.tsx
cal-round-launch-dialog.tsx
review-program-validation.tsx
review-program-activity.tsx
```

Keep domain query/mutation hooks in a dedicated review-program module with
stable React Query keys. Use types generated from Core's OpenAPI contract; do
not maintain parallel hand-written response types in components.

Prefer server-provided summary/read models over assembling a product view by
joining many low-level endpoints in the browser. Large detailed reports and
prediction files remain artifact-backed and are fetched only on demand.

### 11.17 API read models required by the UI

In addition to resource CRUD and workflow commands, add aggregate endpoints or
equivalent service operations for:

- Program summary, readiness, current work, active model, and capabilities.
- Setup and snapshot previews with blocking/warning reason codes.
- Build launch preview and build progress/detail.
- Models list and compatible-version comparison.
- Candidate review bundle containing evidence, lineage, limitations, decisions,
  capabilities, and current resource version.
- Promotion/rollback preview with a short-lived preview token or expected
  resource version.
- CAL round preview, progress, and reviewer-handoff link.
- Validation evidence grouped by protocol.
- Paginated activity timeline.

Classifier creation and lookup contracts include both matter ID and target
metadata-definition ID, preferably through a matter/field-scoped route or an
equivalent request that Core validates. Responses always return tenant ID,
matter ID, target field ID, stable key, current display name, configured/trained
name snapshot, and target-schema fingerprint. A name alone is never accepted
as field identity.

Commands include:

- Create/revise program.
- Create snapshot.
- Launch/cancel/reconcile/retry build where permitted.
- Approve/reject candidate.
- Promote/rollback/retire model version.
- Launch/cancel scoring or CAL round where permitted.

All duplicate-prone commands require idempotency keys. Decision-sensitive
commands require expected-version or preview tokens and return `409 Conflict`
with a structured reason when state changed. Error responses include stable
reason codes, safe user messages, remediation actions, and a correlation ID.

### 11.18 Accessibility, responsiveness, and product analytics

- All tabs, steppers, dialogs, menus, tables, and charts have keyboard and
  screen-reader equivalents.
- Do not communicate lifecycle or metric status by color alone.
- Charts include exact-value tables and meaningful descriptions.
- Wide metric tables degrade to prioritized cards or horizontal scrolling
  without hiding decisions/actions.
- Destructive or high-impact actions have focus-safe confirmation dialogs.
- Preserve route/query state for selected program section, model comparison,
  and filters.
- Record product analytics for setup abandonment, blocked launches, build
  review, approval/promotion, CAL launch, and error remediation without
  collecting evidence content or sensitive model inputs.

The UI must not imply that a development metric constitutes a statistical
validation result or that model activation publishes review outcomes.

## 12. Authorization, security, and retention

- Scope every model, snapshot, run, and decision to tenant and matter.
- Define explicit permissions for build launch, candidate review, promotion,
  rollback, and scoring launch. Promotion should be restricted to the matter
  administration role selected by product policy.
- Reauthorize sensitive actions at workflow execution, not only preview time.
- Use scoped service-to-service identity for worker artifact access.
- Do not place raw evidence, review text, credentials, or signed URLs in logs,
  DBOS status, model cards, or optional experiment trackers.
- Accept packages only from the trusted build workflow; do not support
  arbitrary executable model uploads in the MVP.
- Verify package hash and signature contract on every scoring run.
- Retain a model package and its lineage while referenced by an active model,
  scoring run, CAL round, approval decision, validation report, or legal hold.
- Audit access to packages and detailed evaluation artifacts.

## 13. Observability and operations

Capture structured metrics and logs for:

- Queue time, training time, packaging time, and scoring throughput.
- Run outcomes and failure categories.
- Artifact upload/download sizes and latency.
- Retry/reconciliation counts.
- Feature, training, and scoring exclusion counts.
- Package validation failures.
- Model version activations and rollbacks.

Logs use identifiers and sanitized error codes rather than evidence content.
Operational alerts should cover stuck workflows, repeatedly failing workers,
artifact-integrity failures, and scoring error rates above policy limits.

## 14. Delivery plan

Deliver these phases as vertical product slices. A phase is not complete when
only its tables, workflows, or worker code exist; its authorized API contract,
generated web types, user-visible states, component tests, and documentation
must ship with it. Do not defer the entire UI until the processing platform is
finished.

### Phase 0: Contracts and policy decisions

- Finalize record names, lifecycle states, metric definitions, and permission
  matrix.
- Finalize target-schema, training-scope, and binary/multiclass/one-vs-rest
  strategy contracts, including conflict and abstention semantics.
- Select the probability-calibration method and candidate-selection policy.
- Define versioned schemas for feature, label, split, job, result, evaluation,
  prediction, and package manifests.
- Define the safe logistic-regression package format and numerical tolerances.
- Decide retention defaults and maximum job/package sizes.
- Finalize the review-program information architecture, user vocabulary,
  capability matrix, primary user journeys, and low-fidelity screen flows.
- Define reason-code and readiness read models before component implementation.

Exit criteria: schemas and policy versions are reviewed, test fixtures exist,
Core/Artifact/worker ownership is unambiguous, and the principal setup, build,
candidate-review, promotion, and CAL flows have approved wireframes/state maps.

### Phase 1: Core registry and Artifact Service primitives

- Add Core migrations/models for logical classifiers, snapshots, training runs,
  model versions, evaluations, decisions, and scoring runs.
- Add Artifact Service types and lineage for corpus-scale snapshot, package,
  prediction, and evaluation artifacts.
- Extend the artifact gateway consistently for embedded and remote modes.
- Add audit events and authorization checks.
- Add the matter-level `Review program` tab, route shell, breadcrumbs, empty
  state, capability loading, and program setup wizard.
- Generate the frontend API client/types and add shared program status/readiness
  components.

Exit criteria: records and immutable artifacts can be created, resolved, and
authorized without a real trainer, and an authorized user can create and reopen
a program through the UI while unauthorized users receive the correct state.

### Phase 2: Dataset snapshot builder

- Build matter/review-batch population, target, feature, label/weight,
  group/cohort, and split artifacts.
- Implement label precedence and blinded-label exclusion.
- Implement group-aware split generation and validation.
- Add deterministic hash/derivation-key computation.
- Add the Training data section, snapshot preview/confirmation, label-source
  summaries, feature readiness, exclusions, and grouping/cohort warnings.
- Link missing embeddings to an allowed generation action or the Jobs tab.

Exit criteria: rerunning the same inputs produces the same snapshot identity;
changed content or policy produces a new identity, and users can understand and
confirm the exact population used without reading artifact manifests.

### Phase 3: Trainer worker and package loader

- Implement the versioned worker job/result contracts.
- Implement weighted, group-aware binary, multinomial, and one-vs-rest
  logistic-regression training and cross-fit predictions.
- Implement calibration, metric computation, safe export, package validation,
  and exact-byte round-trip evaluation.
- Implement idempotent artifact publication.
- Build fixture-backed metric, calibration, cohort, lineage, and model-card UI
  components against the agreed candidate-review contract.
- Validate chart accessibility and exact tabular equivalents before live build
  integration.

Exit criteria: a fixture dataset produces a reproducible package that can be
loaded and scored without scikit-learn object deserialization, and its report
can render as a complete, accessible candidate-review fixture.

### Phase 4: Build workflow

- Implement `classification_model_build_v1` in DBOS.
- Add dispatch, polling/callback, timeouts, retry, cancellation, and ambiguous
  completion reconciliation.
- Finalize candidates only after artifact verification.
- Add build preview, launch, progress/detail, cancel, and safe retry/reconcile
  APIs with stable reason codes.
- Add `Build model`, durable progress/detail, active polling, warning, partial
  success, failure, cancellation, and candidate-ready UI states.

Exit criteria: failure injection at each boundary produces either one valid
candidate or a clear recoverable/terminal failure, never duplicates, and the
user can identify the current stage and permitted next action in the UI.

### Phase 5: Evaluation and candidate review

- Store summary metrics in Core and detailed reports in Artifact Service.
- Complete the Models list, like-for-like comparison, candidate-review,
  validation-evidence, model-card, and technical-lineage views.
- Add approval/rejection decisions with required reasons.
- Clearly separate development, validation, audit, and QA evidence.
- Add permission-aware decision controls, insufficient-evidence states, and
  stale-resource conflict handling.

Exit criteria: an authorized reviewer can assess lineage and evidence and issue
an audited decision without direct database or object-store access, while a
read-only user can inspect the permitted evidence without seeing action controls.

### Phase 6: Promotion and rollback

- Implement `classification_model_promote_v1` and compatible rollback.
- Enforce one active version and transactional pointer changes.
- Reauthorize at execution and emit domain/audit events.
- Add promotion and rollback preview/confirmation dialogs, active-model cards,
  stale-preview conflicts, decision history, and success/failure deep links.

Exit criteria: concurrent promotion attempts resolve deterministically and the
complete decision history remains visible; the UI always identifies the active
version and never equates activation with publication.

### Phase 7: Batch scoring and CAL integration

- Implement `classification_model_score_v1`.
- Persist complete predictions in Artifact Service.
- Add versioned CAL acquisition policy and materialize only selected queue
  items in Core.
- Link campaigns/rounds to the exact model version and scoring run.
- Update `docs/jev-cal-review-program-workflow.md` to replace a duplicate
  `cal_model_version` concept with references to this registry.
- Add the CAL rounds list/detail, launch preview, scoring/selection progress,
  queue progress, label-yield summary, and next-build call to action.
- Add the review-workspace CAL context banner and direct reviewer handoff.
- Add the Validation and Publication section shells with correct unavailable,
  blinded, awaiting-decision, and ready states.

Exit criteria: a promoted model can score an eligible matter population and
produce a reproducible, explainable CAL queue without creating per-document
diagnostic rows in Core, and a review lead can launch and monitor the round and
hand reviewers into the existing workspace entirely through the UI.

### Phase 8: Operational hardening

- Add dashboards, alerts, runbook, retention/cleanup jobs, and capacity limits.
- Test worker isolation and credential expiry.
- Add reconciliation tooling and support diagnostics.
- Validate embedded and remote Artifact Service parity.
- Complete the Activity view, correlation-ID/support affordances, responsive
  behavior, accessibility audit, product analytics, and user-facing help.
- Run role-based and browser end-to-end tests for all principal journeys.

Exit criteria: operators can diagnose, reconcile, retry, cancel, and safely
roll back supported failure modes, and users can recover from supported errors
without database, object-store, or DBOS-console access.

### Phase 9: Optional managed integrations

Only after the local contract is stable:

- Add an MLflow `ExperimentTracker` adapter for visualization/comparison.
- Add a SageMaker `ClassificationExecutionBackend` for managed compute.
- Consider managed MLflow in SageMaker if it materially reduces operations.
- Keep external tracker/compute details behind the existing program UI; expose
  only useful provider status or links to authorized support operators.

Core remains authoritative, and failure or removal of either integration must
not invalidate model packages, approvals, or scoring history.

## 15. Verification strategy

### 15.1 Unit tests

- Canonical hashing and derivation keys.
- Dataset/feature/label/split schema validation.
- Label precedence and blinded-label exclusion.
- Group leakage detection.
- Weighted metric definitions and deterministic candidate selection.
- Package schema, path, size, file allowlist, and checksum validation.
- Export/load probability parity and class-order checks.
- Lifecycle transition and authorization rules.

### 15.2 Integration tests

- Core with a fake worker and fake artifact gateway.
- Worker with real fixture artifacts and a temporary Artifact Service.
- Embedded and remote artifact-gateway contract parity.
- Migration upgrade tests for Core and Artifact Service databases.
- DBOS retry/idempotency behavior.

### 15.3 Frontend component and contract tests

- Program setup steps, validation, cancellation, and server-error recovery.
- Capability-controlled actions and direct-route permission failures.
- Readiness blockers, warnings, remediation links, and next-action selection.
- Build polling start/stop behavior and every required run state.
- Metric formatting, denominators, incomparable versions, and unknown versus
  failed evaluation states.
- Accessible chart/table parity and keyboard operation.
- Approval, rejection, promotion, rollback, and stale-preview dialogs.
- CAL launch preview, progress, and review-workspace handoff.
- OpenAPI generation drift so component types cannot silently diverge from
  Core response models.

### 15.4 End-to-end product tests

Run a small deterministic matter through:

1. Program setup in the matter UI.
2. Snapshot preview and creation.
3. Model build launch and progress.
4. Candidate review, rejection or approval, and promotion.
5. Batch scoring and CAL queue materialization.
6. Reviewer handoff to the existing review workspace.
7. A later model version, comparison, and rollback.
8. Activity/decision-history verification.

Assert exact lineage, hashes, decision history, tenant isolation, and stable
predictions as well as correct navigation, permissions, user-visible states,
and absence of misleading activation/publication language.

### 15.5 Failure-injection tests

Cover at least:

- Worker crash before and after artifact upload.
- Timeout with an upload that actually succeeded.
- Artifact Service unavailable or returning a hash mismatch.
- Core finalization transaction failure.
- Duplicate launch and concurrent promotion.
- Expired/revoked worker credential.
- Partial scoring failures.
- Incompatible feature specification or package schema.
- Stale build, promotion, rollback, and CAL preview tokens.
- Polling/network interruption followed by page refresh and durable recovery.
- A workflow that succeeds after the browser reports an ambiguous timeout.

## 16. MVP non-goals

- General-purpose AutoML.
- Arbitrary user-supplied algorithms or executable model uploads.
- Online/low-latency prediction serving.
- A full feature store.
- Automatic retraining or automatic promotion.
- Model deployment to external endpoints.
- Treating MLflow, SageMaker, or OpenSearch as the model system of record.
- Replacing the Jev/CAL validation and publication protocols.

## 17. Open decisions to resolve before implementation

1. Which calibration method and minimum calibration sample size are required?
2. What candidate-selection metric and safety constraints apply to the first
   CAL classifier?
3. Which label sources may train the first model, and what weights apply to
   accepted Jev/silver labels?
4. What grouping rules are mandatory when family, exact-duplicate, or
   near-duplicate metadata is incomplete?
5. Which cohort dimensions are mandatory for approval?
6. What role may approve and promote, and is separation of duties required?
7. What package signing mechanism, if any, is required in addition to content
   hashes and trusted Artifact Service provenance?
8. What are the retention, legal-hold, and deletion rules for snapshots,
   predictions, and retired packages?
9. Does the first release need isolated worker processes/containers, or is a
   separately deployed trusted worker sufficient?
10. At what scale threshold should SageMaker or another managed execution
    backend be evaluated?
11. Does the MVP allow one review program per matter or multiple programs for
    different target fields?
12. Are candidate approval and activation always separate actions, and is
    separation of duties required between them?
13. Which existing product roles map to reviewer, review lead, matter
    administrator, and auditor capabilities?
14. Which stages and metrics are appropriate for ordinary reviewers versus
    review leads and administrators?
15. Must program setup drafts survive across sessions in the MVP?
16. Which CAL selection explanation is useful to reviewers without biasing
    their coding decision?
17. What minimum labeled support is required for each class and evaluation
    fold, and should undersupported classes block a build or be excluded?
18. For one-vs-rest over a single-valued field, what threshold, tie/conflict,
    and abstention policy converts independent probabilities into one outcome?

These decisions are intentionally explicit. None requires adopting a separate
MLOps control plane, and all can be represented through versioned Core policy,
worker contracts, and immutable artifacts.

## 18. MVP completion criteria

The MVP is complete when an authorized operator can reproducibly build a model
from frozen matter data through the matter UI, inspect its lineage and correctly
separated evidence, approve and promote it through an audited workflow, use the
exact immutable package for batch scoring, launch an explainable CAL queue, and
hand reviewers into the existing review workspace. Principal journeys,
permission variants, accessibility, stale-state conflicts, retries, ambiguous
failures, rollback, tenant isolation, and artifact integrity must be verified.
No database access, object-store access, DBOS console, or external MLOps product
may be required for normal operation or correctness.
