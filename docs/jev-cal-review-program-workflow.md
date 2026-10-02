# Jev and CAL Review Program Workflow

## Document status

**Status:** Proposed internal workflow design  
**Audience:** Product, review operations, engineering, and quality teams  
**Publication status:** Internal only; revise after implementation and operational refinement before incorporating it
into published product documentation.

This document describes the intended review-manager workflow for iteratively refining a TypeSafe Jev first-pass
review with a Continuous Active Learning (CAL) challenger, human quality review, adjudication, validation, and
controlled publication of final matter tags.

It is a target design, not a description of functionality that is fully implemented today. In particular, batch-run
values are currently isolated from matter metadata, and the explicit resolution and publication workflows described
below remain to be built.

## Objective

A review manager should be able to run the following program:

1. Run Jev machine review on a controlled pilot batch.
2. Train a CAL-style challenger from Jev decisions and existing human judgments.
3. Send likely errors, uncertain results, and a random audit sample to human reviewers.
4. Resolve differences and use the resulting human judgments to retrain the challenger.
5. Diagnose error causes, refine the Jev guidance or related system component, and rerun Jev.
6. Validate the refined version on unseen documents.
7. Run the approved version over a frozen full-corpus batch.
8. Perform production QA and publish a resolved set of final tags only after explicit approval.

The process separates three concerns:

- **Machine review:** Jev applies a frozen task version and produces complete, auditable decision results.
- **Quality analysis:** CAL identifies decisions that deserve inspection and learns from human corrections.
- **Publication:** an explicit resolution workflow converts approved results into authoritative matter metadata and
  its OpenSearch projection.

## Mental model

The system should treat the complete activity as a **Review Program**, not as one review-batch run.

```text
Configure program
      |
      v
Jev pilot ---> CAL challenger ---> Human QA ---> Adjudication
   ^                                                |
   |                                                v
   +---- new guidance version <---- Analyze error causes
                              |
                              v
                       Holdout validation
                              |
                              v
                      Full-corpus Jev run
                              |
                              v
                       Production QA/CAL
                              |
                              v
                      Final resolved run
                              |
                              v
                    Publish tags to matter
                              |
                              v
                 Project searchable tags to index
```

Steps in the refinement loop operate on a development batch. The full corpus is not processed until a task version
passes a separate validation gate.

## Core terminology

### Review program

The coordinating record for one review objective, such as responsiveness or privilege. It identifies the task,
target coding fields, development and validation strategy, quality thresholds, and publication policy.

### Review iteration

One immutable attempt within a program. An iteration pins a task version, Jev run, CAL campaign, human judgments,
adjudication results, and evaluation snapshot.

### Jev decision

The complete immutable `ReviewDecisionResult`, including typed answers, probabilities, confidence semantics,
evidence, routes, coverage, provider identity, and task-version provenance.

### Batch-run value

A typed value produced by one human or workflow run for one document and field. A batch-run value is isolated from
matter metadata. Competing runs can hold different values without overwriting one another.

### Silver label

A machine-produced label used to bootstrap quality analysis. Jev mapped decisions are silver labels: useful for
training a challenger, but not independent proof that the decision is correct.

### Gold label

A human-reviewed or adjudicated judgment. Gold labels take precedence over silver labels when training the
challenger or evaluating Jev.

### Final tag

An authoritative matter-level value produced through the `MetadataEvent` ledger and resolved into
`DocumentMetadataCurrent`. A final tag is not the same as a batch-run value.

### Indexed tag

The rebuildable OpenSearch projection of an active, searchable final matter tag. Only resolved current values are
projected as `metadata.<definition_key>`.

## Relationship between batches, labels, final tags, and the index

The existing architecture has three distinct data paths:

```text
Batch membership
ReviewBatchDocument ----------------------> OpenSearch batch_ids[]

Batch coding
ReviewBatchRunValue ----------------------> isolated in Core only
        |
        | explicit resolution and publication (proposed)
        v
Matter MetadataEvent
        |
        v
DocumentMetadataCurrent
        |
        v
SearchProjectionOperation
        |
        v
OpenSearch metadata.<definition_key>
```

Completing a human, agent, Jev, or CAL-related batch run must not automatically change final tags. Publication is a
separate authorized operation with an impact preview and durable provenance.

## Program configuration

The review manager configures:

- the analysis task and target coding fields;
- the initial published task version;
- a development or pilot batch;
- a sealed random validation batch or validation sampling policy;
- the human QA budget;
- required precision, recall, coverage, and confidence thresholds;
- mandatory-review routes, including failures, partial results, and specified uncertainty bands;
- cohort checks, such as document type, custodian, source, language, or date range;
- blind second-review and adjudication requirements;
- the policy for full-corpus execution and final publication.

Suggested program states are:

```text
DRAFT
PILOT_RUNNING
QA_RUNNING
ADJUDICATING
REFINEMENT_REQUIRED
VALIDATION_READY
VALIDATING
FULL_RUN_READY
FULL_RUN_RUNNING
FINAL_QA
READY_TO_PUBLISH
COMPLETE
FAILED
CANCELED
```

The development batch should contain representative documents, known positives where available, relevant edge
cases, and difficult document types. The validation sample should remain sealed and unseen during guidance
refinement.

## Iterative pilot workflow

### 1. Run Jev on the development batch

The manager launches Jev using one published `MatterAnalysisTaskVersion`. The run freezes the task definition,
Decision Specification, field mappings, provider/model policy, state-builder version, paragraph-map version, and
document population.

The run produces:

- immutable Decision Results;
- mapped isolated batch-run values;
- complete Choice, Score, or Noul probability information;
- explicit confidence semantics;
- evidence references and routes;
- partial, failed, skipped, and missing-text outcomes;
- document and cohort coverage metrics.

These outputs remain candidate decisions. They are not matter metadata and do not update the search index.

### 2. Train the CAL challenger

The initial challenger uses:

- document text or deterministic document-level embeddings as features;
- mapped Jev decisions as down-weighted silver labels;
- human gold labels accumulated in earlier rounds;
- no Jev decision, confidence, or probability as an input feature.

The challenger should not be presented as an independent accuracy measurement while it is trained only from Jev
labels. At that stage, it measures learnability, consistency, and anomalous decisions.

Initial QA scoring should use cross-fitted or out-of-fold predictions. Scoring every source label with a model trained
directly on that same label can conceal suspicious decisions through overfitting.

Potential risk signals include:

- hard disagreement between Jev and the challenger;
- large probability or margin divergence;
- inconsistent decisions among similar documents;
- low Jev confidence or a policy-boundary result;
- missing or weak evidence;
- partial, failed, or skipped Jev results;
- out-of-distribution documents;
- elevated disagreement within a predefined cohort.

Each model version must record its feature snapshot, embedding configuration, algorithm and parameters, training
label IDs and hash, source-label weights, metrics, and model artifact.

### 3. Build the human QA queue

The QA queue has separate lanes with separate reporting semantics.

#### Targeted QA

Targeted QA selects documents most likely to contain a Jev error or operational defect. It efficiently discovers
problems, but its observed error rate is selection-biased and must not be presented as the corpus-wide Jev error
rate.

#### Random audit

Random audit selects documents independently from a recorded sampling frame. It supports unbiased prevalence and
quality estimates, provided that the draw seed, frame size, strata, and inclusion probability are preserved.

The random sample size should be driven by the required confidence bound, not a fixed percentage of every review.

#### Mandatory review

Mandatory review includes failures, partial results, missing required evidence, unsupported document types, and any
task-specific route that cannot be accepted without a human judgment.

#### QC review

QC supplies blind second review and adjudication samples used to measure reviewer agreement and identify unstable
gold labels.

### 4. Record human judgments

For each selected document and field, a reviewer can:

- accept the Jev value;
- replace the Jev value;
- mark the decision ambiguous;
- route it for specialist review;
- identify an evidence or extraction failure;
- identify a guidance or policy problem.

The human judgment is stored separately and linked to the original Jev result. It never rewrites the immutable Jev
answer.

Label authority is resolved in this order:

```text
adjudicated human
    > confirmed human QA
        > existing human reference
            > Jev silver label
```

Random audit labels should remain excluded from active training until the associated evaluation snapshot is locked
if they are being used as an unseen measurement set.

### 5. Resolve disagreements

The manager receives a comparison of Jev, challenger, and human judgments.

| Jev | Challenger | Human | Disposition |
| --- | --- | --- | --- |
| Responsive | Responsive | Not reviewed | Provisionally accept |
| Not responsive | Responsive | Responsive | Correct Jev |
| Responsive | Not responsive | Responsive | Retrain challenger |
| Responsive | Not responsive | Ambiguous | Mandatory human route |
| Partial | Responsive | Responsive | Investigate coverage defect |
| Responsive | Responsive | Not responsive | Investigate shared systematic miss |

The adjudication process should create or update a consolidated human reference result for the development batch.
That reference is the durable gold set used to compare later Jev versions.

### 6. Classify the error cause

Not every disagreement warrants a guidance edit. Resolution should record a controlled reason, such as:

- `GUIDANCE_GAP`
- `GUIDANCE_AMBIGUITY`
- `DECISION_POLICY_THRESHOLD`
- `EVIDENCE_FAILURE`
- `TEXT_EXTRACTION_FAILURE`
- `JEV_PROVIDER_INCONSISTENCY`
- `CAL_MODEL_ERROR`
- `HUMAN_DISAGREEMENT`
- `DOCUMENT_REQUIRES_SPECIALIST`

The reason determines the remediation:

| Cause | Primary response |
| --- | --- |
| Guidance gap | Revise the Task Definition |
| Guidance ambiguity | Add or clarify rules and examples |
| Threshold error | Revise the Decision Policy |
| Evidence failure | Improve evidence localization |
| Text failure | Correct processing or state construction |
| Provider inconsistency | Test stability, retry, or routing policy |
| CAL error | Add gold label and retrain the challenger |
| Human disagreement | Blind review and adjudication |
| Legitimate ambiguity | Preserve a mandatory human-review route |

CAL or another analysis component may summarize correction patterns and propose guidance changes. It must not edit,
publish, or activate guidance automatically.

### 7. Create and publish a refined task version

A guidance, question, mapping, aggregation, or policy change creates a new draft `MatterAnalysisTaskVersion`. The
definition and Decision Specification are reviewed, compiled, validated, diffed, and explicitly published together.

The previous task version and all of its results remain immutable.

### 8. Rerun the development batch

The new published task version is run against the same development batch. The fixed human reference values allow
direct version comparison.

The manager should see:

- errors corrected by the new version;
- new regressions;
- precision, recall, and false-negative changes against human gold;
- evidence and input-coverage changes;
- provider failure and partial-result changes;
- results by predefined cohort;
- cost and latency changes;
- remaining CAL-targeted correction yield.

Each iteration retains:

```text
Task version N
    +-- Jev run N
    +-- CAL model versions N.1, N.2, ...
    +-- Human QA judgments
    +-- Adjudicated reference values
    +-- Frozen evaluation snapshot
    +-- Manager disposition
```

When a new Jev version runs, its silver labels replace the prior version's silver labels in the active challenger.
Human gold labels carry forward. Historical silver labels remain available for audit but do not describe the current
Jev behavior.

## Validation gate

When the manager believes refinement is complete, the candidate task version is run against the sealed validation
batch.

The validation set must not have influenced guidance or threshold selection. Suggested promotion requirements
include:

- the configured precision and recall targets are met;
- the required one-sided confidence bound is met;
- no unacceptable cohort regression is present;
- partial and provider-failure rates remain below their limits;
- required evidence coverage remains above its limit;
- reviewer agreement and QC requirements are met;
- no unresolved systematic error cluster remains.

If the candidate fails validation, the validation documents may be moved into development after their results are
examined, but a new sealed validation sample should be drawn for the next promotion attempt.

Passing validation allows the manager to mark the task version `FULL_RUN_READY`. It does not publish any tags.

## Full-corpus execution

The manager launches the approved task version against a frozen full-corpus `ReviewBatch`. The execution snapshot
must include:

- task definition and Decision Specification hashes;
- provider, resolved model, and policy configuration;
- question and field mappings;
- state-builder and paragraph-map versions;
- source-content hashes;
- document population and search-generation provenance;
- mandatory-review and publication policies.

The full-corpus Jev run produces isolated Decision Results and batch-run values. Completion still does not update
matter metadata or OpenSearch final tags.

## Production QA

Production QA runs after the full-corpus Jev pass and before publication.

The system should:

- train a challenger from the current full-corpus Jev silver labels;
- carry forward applicable pilot and validation human gold labels;
- review high-risk disagreements;
- review every mandatory failure, partial result, and unsupported route;
- draw a statistically designed random audit sample;
- estimate the residual false-negative rate and remaining relevant population;
- check for novel cohorts or distribution shifts not represented in development.

Targeted QA reports correction yield and error discovery. Random audit supplies the defensible quality estimate.
Jev/challenger agreement alone is a consistency measure, not an accuracy measure.

If production QA identifies a systemic defect, publication is paused. The manager may create another task version
and rerun a defensibly defined affected scope or the entire corpus. Isolated errors may be adjudicated individually.

## Final resolved run

Before publication, the system creates one consolidated resolved candidate run. For every document and field, the
resolution order is:

```text
human adjudication, if present
    else accepted Jev value
        else unresolved or mandatory human review
```

Every resolved value retains provenance to the originating Jev run value, immutable Decision Result, CAL campaign
and model version, human QA judgment, adjudication, and task version.

Unresolved, failed, partial, conflicted, or mandatory-review documents are not silently converted into final tags.

## Controlled publication

The review manager receives an impact preview before any matter metadata changes. The preview includes:

- affected documents and fields;
- values to add, set, remove, or clear;
- conflicts with existing matter metadata;
- unresolved documents excluded from publication;
- counts of Jev-only, human-confirmed, and human-corrected values;
- confidence, evidence, and coverage breakdowns;
- the number of search documents to reindex.

After explicit matter-administrator approval, the publication workflow:

1. Appends matter `MetadataEvent` rows through the existing command service.
2. Preserves source-run, Decision Result, QA, adjudication, confidence-kind, and publication provenance.
3. Resolves each affected field into `DocumentMetadataCurrent`.
4. Enqueues durable `DOCUMENT_UPSERT` search projection operations.
5. Projects active searchable resolved values into OpenSearch as `metadata.<definition_key>`.
6. Retains the source Jev, CAL, human, and resolved runs unchanged.

Example:

```text
Jev run value:       responsive
CAL prediction:      not_responsive
Human QA judgment:   not_responsive
Final metadata:      not_responsive
OpenSearch:          metadata.responsiveness = "not_responsive"
```

If the human accepts Jev:

```text
Jev run value:       responsive
Human QA judgment:   confirmed
Final metadata:      responsive
OpenSearch:          metadata.responsiveness = "responsive"
```

The original Jev decision remains available in both cases.

## Manager-facing metrics

The program dashboard should distinguish the following categories.

### Machine consistency

- Jev/challenger agreement;
- disagreement rate and probability divergence;
- similar-document inconsistency;
- out-of-distribution and cohort warnings.

These are diagnostics, not human-backed accuracy estimates.

### Targeted QA effectiveness

- human correction rate among CAL-ranked documents;
- errors found per document reviewed;
- cumulative corrections by round;
- error causes and affected cohorts;
- marginal correction yield.

These metrics are selection-biased and describe QA efficiency.

### Random-audit quality

- precision and recall estimates;
- false-positive and false-negative estimates;
- abstention, failure, and partial-result coverage;
- estimated remaining relevant documents;
- confidence intervals and stopping-gate status.

These are the primary defensible quality measurements.

### Version comparison

- fixed-gold accuracy changes;
- corrected errors and regressions;
- threshold and route changes;
- evidence-coverage changes;
- cohort-level performance changes;
- cost and latency changes.

## Proposed coordinating records

Most document, task, batch, run, and result data should reuse existing records. The workflow needs a small number of
new coordinating concepts.

### `review_program`

Stores the task, target fields, development and validation policies, quality thresholds, publication policy, status,
and manager.

### `review_iteration`

Links one program iteration to a task version, development or validation batch, Jev run, CAL campaign, human
reference run, evaluation snapshot, and manager disposition.

### `cal_campaign`, `cal_round`, and `cal_model_version`

Record the source run, label-authority rules, feature snapshot, ranking policy, model versions, training-label hashes,
queue rounds, and gain metrics.

### `cal_queue_item`

Records the selected document, queue lane, score, rank, model version, lease, sampling provenance, and completion
state.

### `review_adjudication`

Records the document and field, competing source values, final gold value, reason code, adjudicator, and provenance.

### `review_publication`

Records the resolved run, frozen impact preview, approval, execution progress, failures, resulting metadata events,
and search projection operations.

## Safety and governance requirements

- A model run, CAL round, or human review completion never publishes tags implicitly.
- Guidance and Decision Specification changes always create a new version.
- CAL may propose but never automatically publish guidance changes.
- Jev answers, human judgments, and adjudications remain distinct records.
- Human gold takes precedence over machine silver for evaluation and active training.
- Random-audit and targeted-QA metrics are never conflated.
- Confidence semantics remain explicit; a derived probability is not provider confidence.
- Failures, partial coverage, and missing evidence remain visible and are never treated as negative labels by
  default.
- Full-corpus execution pins the exact task and processing configuration.
- Final publication requires an impact preview and explicit matter-administrator approval.
- OpenSearch remains a rebuildable projection; the Core metadata ledger remains authoritative.

## Recommended delivery sequence

1. Add the review-program and iteration coordination records and manager-facing state machine.
2. Implement a pilot Jev run over a frozen development batch.
3. Add source-label extraction, cross-fitted challenger training, and immutable model versions.
4. Add targeted, random, mandatory, and QC queue lanes with leases.
5. Add append-only human QA judgments, error causes, and adjudication.
6. Add fixed-gold comparison and immutable evaluation snapshots.
7. Add the sealed validation gate and promotion controls.
8. Add full-corpus execution and production QA.
9. Add the final resolved-run workflow.
10. Add publication preview, approval, metadata-ledger writes, and search projection.
11. Refine the workflow through operational use before adapting it for published product documentation.

## Current implementation boundary

The following foundations already exist:

- frozen review batches and permanent memberships;
- isolated human, agent, and workflow review runs;
- typed isolated run values;
- versioned analysis tasks and Decision Specifications;
- a TypeSafe Jev adapter and immutable Decision Result ledger;
- append-only matter metadata events and a current-value projection;
- durable OpenSearch document projection operations.
- typed materialization of policy-approved Jev recommendations into isolated batch-run values;
- explicit selection of one completed run as the batch's searchable coding source;
- batch-scoped OpenSearch coding filters and facets that do not alter final matter metadata.

The following remain proposed work:

- program and iteration coordination;
- corpus-scale Jev first-pass orchestration;
- the CAL challenger, queue, model-version, and stopping subsystems;
- consolidated human reference and adjudication workflows;
- validation promotion gates;
- resolved candidate runs;
- explicit batch-result acceptance and publication into matter metadata;
- final-tag provenance from Decision Result through publication;
- manager-facing workflow and quality dashboards.

## Open review issues

The following issues were identified during review of this first draft. They remain open design questions and should
be resolved before the affected workflow is implemented.

### Publication preview freshness and execution-time authorization

**Priority:** P1

Publication approval must be bound to the metadata and field configuration shown in the impact preview. Matter
metadata, enum options, definition status, or the approving user's authorization may change between preview and
execution. The design should preserve the preview's relevant source-event IDs or projection and schema hashes,
reauthorize the approver at execution time, and return stale items for a new preview rather than overwriting or
clearing changes the approver did not see.

### Idempotent and resumable publication

**Priority:** P1

The durable publication workflow needs explicit retry semantics. A retry after an ambiguous commit must not append
duplicate `SET` events or repeat `CLEAR` and `ADD` replacement sequences. The design should define deterministic,
uniqueness-enforced publication-item keys, such as publication, document, field, operation, and value ordinal, and
persist per-item progress so a partially completed publication can resume safely.

### Jev value acceptance policy

**Priority:** P1

The meaning of an "accepted Jev value" must be defined before resolved candidate runs are implemented. The program
should freeze an explicit eligibility predicate covering result status, mandatory-review routes, confidence,
evidence and coverage requirements, applicable thresholds, field-schema validity, and the production-QA gate. The
workflow and metrics should distinguish values accepted by program policy from values confirmed by a human.

### Repeated validation and confidence control

**Priority:** P2

When a failed validation sample is examined and used for refinement, later candidates become adaptive. Repeatedly
applying the same nominal confidence gate until a candidate passes would inflate the false-promotion probability.
The validation design should predefine an appropriate sequential-testing or alpha-spending policy, limit promotion
attempts, or reserve an untouched final confirmation sample.

### Design-weighted random-audit estimates

**Priority:** P2

Random-audit metrics must account for their sampling design. When strata or unequal inclusion probabilities are
used, raw sample proportions may bias precision, recall, false-negative, and remaining-relevant-population estimates.
The design should specify weighted estimators and confidence intervals that account for inclusion probability,
finite-population sampling, and unresolved or missing judgments.
