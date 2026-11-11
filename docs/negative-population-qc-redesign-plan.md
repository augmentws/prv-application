# Negative-Population QC Redesign Plan

## Status

Inventory foundation and inventory-backed procedure selection are implemented. The Review Program now prepares a
durable, immutable Parquet inventory before configuration; reports the available count for each procedure; disables
zero-count procedures; and requires the launch request to identify the procedures and record types selected by the
user. The selected execution manifest is pinned to the inventory content hash.

Separating targeted human-review, exception-retry, and source-remediation execution into independently completing
subtasks remains Phase 3. Until that phase is completed, selected reviewable procedures continue through one frozen
ad hoc QC batch while preserving separate selection-lane provenance.

## 1. Objectives

The redesigned workflow must:

- calculate available QC populations before asking the user for sample sizes;
- let the user choose which QC procedures to run;
- use the appropriate automated, human-review, retry, or remediation route for each procedure;
- avoid presenting zero-count populations as configurable options;
- preserve frozen, document-level selection provenance;
- report each QC procedure independently; and
- keep the complete QC history available for replay and audit.

## 2. Build the population inventory first

Before displaying QC configuration, run a deterministic inventory analysis over the accepted whole-collection
classifier-scoring population. This analysis does not make model calls.

For each document, collect:

- matter and document identifiers;
- record type;
- current source availability and content hash;
- classifier status for every accepted coding field;
- selected classifier values and positive-class probabilities;
- applicable classifier decision thresholds and threshold margins;
- current results from each pinned Q&A profile;
- existing human coding, if any; and
- processing-exception reasons.

### 2.1 Ordinary no-code population

A document belongs to the ordinary no-code population when it has:

- successful classifier results;
- no selected issue code under the accepted classifier coding policy;
- no positive current Q&A coding;
- no positive human coding;
- available source content; and
- no unresolved processing exception.

The accepted classifier coding policy determines whether a document has an issue code. The confidence threshold used
to select classifier-positive documents for dual-Q&A coverage must not silently replace the classifier coding policy.

### 2.2 Near-threshold population

Near-threshold documents are ordinary no-code documents for which at least one positive-class probability falls
within an explicit range below its applicable classifier decision threshold.

For example:

```text
Positive threshold: 0.85
Near-threshold band: 0.80 through less than 0.85
```

This is a defined population, not an instruction to take the highest-scoring number of documents. Store the closest
threshold margin so the available count can be recalculated when the user changes the band.

### 2.3 Record-type populations

Group ordinary no-code, near-threshold, and exception documents by record type. Report the available count for every
record type before allowing the user to configure record-type QC.

### 2.4 Processing exceptions

Count exception reasons independently, including:

- classifier failed;
- classifier abstained;
- classifier result missing;
- Q&A profile failed;
- Q&A profile result missing;
- incomplete Q&A result; and
- source unavailable.

Source-unavailable documents cannot enter automated or human review and must remain in a remediation population.

### 2.5 Population overlaps

Inventory populations may overlap. Store eligibility flags and an overlap matrix instead of forcing the inventory
into mutually exclusive buckets. The execution plan determines how overlaps are handled after the user chooses QCs.

## 3. Inventory storage

Do not create a database row for every inventory document.

### 3.1 Document-level inventory

Store the document-level inventory as an immutable, versioned Parquet analysis-input manifest in the artifact
service. Use an engine-neutral contract such as:

```text
review-program.negative-population-qc-inventory.v1
```

Each row records the document, source provenance, classifier and Q&A state, threshold data, eligibility flags,
record type, and exception reasons.

### 3.2 Database metadata

PostgreSQL stores only:

- manifest, artifact, and content-hash identifiers;
- Review Program run and stage identifiers;
- classifier scoring-run and model-version identifiers;
- accepted Q&A task version and pinned profile identifiers;
- inventory policy version and generation timestamp; and
- compact summary counts and overlaps.

Link the manifest to the Review Program as a child with purpose:

```text
NEGATIVE_POPULATION_QC_INVENTORY
```

Store the compact inventory summary in the run progress and current Review Program version for the UI and replay.

## 4. QC selection interface

After the inventory is ready, present each available QC as an optional card. Show its population definition,
available count, supported execution route, expected cost, and overlap with other available QCs.

| QC procedure | Default execution route | Purpose |
| --- | --- | --- |
| Random negative audit | Dual Q&A | Screen a representative ordinary no-code sample |
| Near-threshold review | Human review | Examine documents close to a classifier decision threshold |
| Record-type QC | Human review | Examine selected or underrepresented record types |
| Processing exceptions | Retry or human review | Resolve classifier and Q&A failures or abstentions |
| Source remediation | Remediation queue | Restore unavailable content before review |

The user may run any combination or explicitly skip optional QCs. A zero-count procedure remains visible as
unavailable with an explanation, but it does not display editable configuration.

## 5. Configure selected QCs

Only display settings for procedures selected by the user:

- **Random negative audit:** sample size.
- **Near-threshold review:** score band and human-review sample size.
- **Record-type QC:** record types and sample size for each selected type.
- **Processing exceptions:** retry automated failures, create human-review work, or accept documented limitations.
- **Source remediation:** create or export a remediation queue.

The API validates requested sizes against the frozen inventory and returns a refreshed preview if the inventory or
configuration hash changes.

## 6. Execution model

Each selected QC becomes a visible subtask of the Negative-population QC stage.

### 6.1 Random negative audit

- Draw a reproducible simple random sample from the frozen ordinary no-code population.
- Create a task-specific Q&A Batch Set.
- Run both pinned Q&A profiles.
- Keep classifier predictions and selection reasons out of the Q&A prompt.

### 6.2 Near-threshold review

- Create a named human-review batch.
- Include the applicable threshold band in batch provenance, not as reviewer-facing evidence unless explicitly
  requested.
- Do not run the same Q&A process as a substitute for human review.

### 6.3 Record-type QC

- Create named human-review batches for selected record types.
- Preserve the record-type population counts and selection policy for reporting.

### 6.4 Processing exceptions

- Retry failures that are safe and meaningful to retry using their original task workflow.
- Place source-reviewable unresolved exceptions into a human-review batch when selected by the user.
- Keep accepted limitations separately recorded.

### 6.5 Source remediation

- Do not place source-unavailable documents into Q&A or human review.
- Create a remediation queue with missing-source reasons and artifact provenance.

Normally, each selected QC produces one named batch. Large populations may be partitioned automatically while
remaining within one named Batch Set for that QC.

## 7. Overlap policy

Apply these rules when selected QC procedures overlap:

- preserve the random sample exactly so its statistical properties are not changed;
- report when a random-sample document is also eligible for targeted human review;
- deduplicate overlapping human-review selections;
- assign a documented primary human-review batch while retaining every applicable inclusion reason; and
- backfill targeted samples when deduplication reduces their requested size.

Every execution manifest records the source inventory hash, selection policy, random seed where applicable, and all
inclusion reasons.

## 8. Reporting

Do not combine results from different QC procedures into one undifferentiated positive rate.

### 8.1 Random negative audit

Report:

- sampled, completed, unresolved, and Q&A-positive documents;
- exact agreement, partial overlap, and disagreement between pinned Q&A profiles;
- observed Q&A-positive rate and confidence interval; and
- documents requiring human adjudication.

A Q&A-positive rate is not a ground-truth accuracy or miss-rate estimate until the relevant results are human
adjudicated.

### 8.2 Near-threshold human review

Report confirmed positive yield by threshold-margin band and coding category.

### 8.3 Record-type QC

Report confirmed positive yield and unresolved work by record type.

### 8.4 Processing exceptions

Report retried, resolved, human-reviewed, accepted, and unresolved documents by exception reason.

### 8.5 Source remediation

Report repaired and still-unavailable documents by source and artifact reason.

## 9. Completion decision

When all selected QCs reach a terminal state, present a combined summary and let the user:

- accept QC and complete first pass;
- review identified positives;
- expand one or more QC samples;
- retry unresolved exceptions;
- accept documented limitations; or
- stop the program.

Skipped procedures and accepted limitations remain explicit in the completion report.

## 10. Notifications and replay

Use the existing notification backbone for inventory completion, required decisions, batch completion, failures,
remediation, and final acceptance.

Replay must show:

- the frozen inventory summary and manifest lineage;
- which QCs were offered, unavailable, selected, or skipped;
- the configuration and preview accepted for each selected QC;
- created batches, jobs, and review results; and
- the independent reports and final user decision.

## 11. Implementation phases

### Phase 1: inventory foundation

- Define the inventory Parquet contract and summary schema.
- Implement deterministic, chunked inventory generation.
- Add Review Program child linkage, progress, notifications, and replay data.
- Add empty-population, overlap, exception, and provenance tests.

### Phase 2: QC selection UI

- Replace the current four-count form with inventory-backed QC cards.
- Show available counts, definitions, overlaps, and execution routes.
- Allow the user to select, configure, or skip each procedure.

### Phase 3: execution routes

- Implement random dual-Q&A Batch Sets.
- Implement near-threshold and record-type human-review batches.
- Implement exception retry, human-review, limitation, and source-remediation routes.
- Integrate task and batch progress with the notification backbone.

### Phase 4: independent reporting

- Produce separate reports for every selected QC.
- Add human-adjudication follow-up for random-audit findings.
- Add the combined QC acceptance gate and completion report.

### Phase 5: migration and rollout

- Mark legacy combined-lane QC reports as historical.
- Do not treat legacy aggregate rates as equivalent to the redesigned reports.
- Add feature gating and rollback support.
- Update operational and user documentation.

## 12. Acceptance criteria

The redesign is complete when:

- the user sees population counts before configuring QC;
- zero-count procedures cannot be launched;
- each selected QC uses the correct execution route;
- Q&A is not used as a substitute for required human review;
- source-unavailable documents enter remediation instead of review;
- every selection is reproducible from an immutable manifest;
- reporting keeps random, targeted, exception, and remediation results separate; and
- the user can explicitly choose which QCs to run before first-pass completion.

## 13. Deferred DuckDB option

The initial inventory implementation does not require DuckDB. Keep the Parquet contracts engine-neutral and follow
the separate [Deferred DuckDB Integration Plan](duckdb-integration-plan.md) if scale or query complexity later
justifies introducing DuckDB.
