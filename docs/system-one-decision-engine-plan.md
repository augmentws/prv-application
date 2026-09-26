# System One Decision Engine Plan

Status: proposed

## Goal

Turn a matter's reviewed guidance into versioned, executable analysis tasks, then use a System One provider such
as TypeSafe Jev to perform fast typed decisions over documents.

The generalized unit is a `MatterAnalysisTask`. Each task has its own human-authored definition and generated
Decision Specification. They are one publication unit and share one immutable task-version number. Initial task
types cover matter-issue first-pass review, privilege review, topic generation, and bounded data-exploration
tasks. A generative model compiles a task definition into a structured draft specification. A matter
administrator reviews, edits, tests, and publishes the complete task version before it can be executed.

Jev results are provisional review decisions. They do not silently update matter metadata. Code-owned rules and
primitive-aware uncertainty thresholds decide whether a result is presented as a suggestion, sent to a
second-pass model, or requires human review.

## Architectural decisions

1. **The task definition and Decision Specification are governed together.** They are fields of one immutable,
   matter-scoped task version and publish atomically. A specification can never execute against a different
   definition version.
2. **The compiler is a managed skill.** A structured generative-model skill converts selected definition
   content into a draft specification. Editing a definition creates a new draft task version and makes its
   specification missing or stale. Regeneration never overwrites a published version.
3. **The first pass is a workflow, not an agent conversation.** A DBOS workflow owns document fan-out, retries,
   progress, and completion. Its Jev calls run as managed skill runs so the existing usage and invocation ledger
   remains authoritative.
4. **`SystemOneDecisionEngine` is separate from the generative executor.** The existing structured model executor
   continues to produce summaries and synthesis. The decision engine accepts state plus typed questions and
   returns typed answers, probability distributions, confidence where the primitive supports it, and usage.
5. **Jev is the first adapter, not the domain abstraction.** Provider-neutral request and response types prevent
   decision specifications, workflows, and stored results from depending on the TypeSafe SDK.
6. **Actual document text is the primary first-pass input.** A generated summary is not substituted for the
   source document. Long documents use the existing paragraph map and an explicit window/aggregation policy.
7. **Composition remains in code.** The model answers atomic questions. A bounded, declarative policy language
   combines the answers. Never execute model-generated Python, JavaScript, SQL, or free-form expressions.
8. **Evidence is required for an actionable recommendation.** Positive or boundary results must resolve to
   source paragraph IDs. A classification without validated evidence remains advisory and is routed for review.
9. **Batch results are isolated by default.** Derived coding values use the existing `WORKFLOW` review-batch run
   and isolated run-value model. Publishing values to matter metadata remains a separate authorized action.
10. **Every run is reproducible.** It pins the atomic task version, dependency task versions, state-builder and
    paragraph-map versions, provider, resolved model version, question hashes, policy, thresholds, and document
    source hashes.
11. **The new task model is introduced beside the current implementation.** Existing Matter Definition,
    assessment, topic, summary, and batch workflows remain operational until each use case reaches parity and is
    cut over. There must never be two independently writable sources of truth.
12. **Uncertainty semantics are explicit.** Full probability distributions live in immutable Decision Results.
    Scalar confidence projected beside a coding value records whether it is provider confidence, selected-answer
    probability, or a derived probability. The system never fabricates Jev confidence for Noul answers.

## Product terminology

- **Matter Analysis Task**: a stable matter-scoped analysis purpose such as issue review, privilege review, topic
  generation, or a named exploration.
- **Task Version**: the atomic immutable publication containing the definition, generated Decision Specification,
  input/output contracts, policies, and provenance.
- **Task Definition**: the human-reviewed Markdown guidance describing what the task must determine or produce.
- **Decision Specification**: the reviewed structured document containing typed questions, evidence requirements,
  aggregation rules, field mappings, and decision policy.
- **Compiler**: the generative-model skill that drafts a Decision Specification from a Task Definition.
- **Decision Engine**: the provider-neutral typed decision interface.
- **First-pass Review**: the durable workflow that applies a published Decision Specification to a batch.
- **Decision Result**: the immutable raw typed answers, derived recommendation, evidence, and provenance for one
  document in one run.

## Target lifecycle

```text
Task definition draft
        |
        v
compiler skill --> draft Decision Specification
        |                    |
        +--------------------+
                  |
                  v
       review, edit, test, diff
                  |
                  v
       publish atomic Task Version
                  |
          +-------+-------------------------+
          |                                 |
          v                                 v
   code-owned workflow               calibration run
          |
          +--> generative stage when the task requires creation or synthesis
          +--> Jev typed decisions
          |
          v
typed answers + probabilities + confidence semantics + evidence
          |
          v
deterministic policy and uncertainty gates
          |
     +----+----------------+----------------+
     |                     |                |
     v                     v                v
suggest value       second-pass model   human review
```

## Phase 1: side-by-side Matter Analysis Task domain

Do not generalize the current `matter_definition` tables in place. Add the new task model beside the existing
implementation so current Matter Definition, assessment, topic, summary, chat, and review behavior remains
available throughout migration.

### `matter_analysis_task`

- `id`, `matter_id`, stable `key`, `name`, and optional `description`;
- `task_type`: initially `ISSUE_REVIEW`, `PRIVILEGE_REVIEW`, `TOPIC_GENERATION`, `DATA_EXPLORATION`, or
  `CUSTOM_DECISION`;
- code-owned `workflow_key` selecting an allowed execution shape;
- `current_version` and nullable `published_version`;
- `status`: `ACTIVE`, `SUSPENDED`, or `ARCHIVED`;
- creator and timestamps;
- unique `(matter_id, key)`.

### `matter_analysis_task_version`

One row is the atomic definition/specification publication unit:

- `id`, `matter_analysis_task_id`, and monotonically increasing `version`;
- `status`: `DRAFT`, `PUBLISHED`, or `RETIRED`;
- `compilation_status`: `NOT_GENERATED`, `STALE`, `GENERATING`, `READY`, or `FAILED`;
- `definition_markdown` and definition content hash;
- nullable canonical `decision_specification` JSON and its content hash;
- state/input contract, output contract, evidence policy, and routing policy snapshots;
- compiler SkillDefinitionVersion and optional compiler SkillRun provenance;
- compiler provider/model/configuration snapshot and validation report;
- creator, creation time, publisher, and publication time.

Editing definition text creates a new draft task version. It never mutates a published version. The new draft's
specification begins as `NOT_GENERATED` or `STALE`; publication is prohibited until compilation and deterministic
validation produce `READY`. Definition and specification always publish under the same task-version number.

### Optional version dependencies

Add `matter_analysis_task_version_dependency` when one task requires another task's published context. The
dependency pins the exact upstream task-version ID and content hash. It must not follow an upstream task's moving
`published_version` pointer during execution.

This supports, for example, a privilege task that needs matter-specific party names or a data-exploration task
that builds on an approved topic taxonomy without merging the tasks' independent publication lifecycles.

### Code-owned execution shapes

The task type identifies the product purpose while `workflow_key` selects a code-owned workflow. Initial shapes
are:

| Task type | Generative stage | System One stage |
| --- | --- | --- |
| `ISSUE_REVIEW` | Compile guidance; optional uncertain-case explanation | First-pass issue decisions |
| `PRIVILEGE_REVIEW` | Compile guidance; optional uncertain-case explanation | Atomic privilege decisions |
| `TOPIC_GENERATION` | Generate candidate taxonomy and labels | Evaluate candidates and assign documents |
| `DATA_EXPLORATION` | Formulate questions and synthesize findings | Score/classify documents or passages |
| `CUSTOM_DECISION` | Optional code-approved stage | Bounded typed questions |

Jev does not replace generation where the output is open-ended text. Topic creation remains generative; System
One evaluates or applies the resulting bounded taxonomy. Data exploration may use generative synthesis after
typed decisions have been collected.

### New API and UI

Add matter-admin APIs under:

```text
/v1/matters/{matter_id}/analysis-tasks
```

Add an Analysis Tasks section to the matter page for creating tasks, editing the current draft definition,
viewing version history, compiling the specification, testing, and publishing the atomic task version.

## Phase 2: compatibility import and controlled deprecation

### Legacy import

Provide an idempotent importer that creates an `ISSUE_REVIEW` task version from the current published Matter
Definition. Record the legacy MatterDefinitionRevision ID, content hash, and import time in the new version's
provenance. Do not alter or delete the legacy record.

Existing topic configuration may later seed a `TOPIC_GENERATION` task, but topic migration occurs only after the
issue-review path has reached parity.

### Authority phases

1. **Legacy authoritative:** the existing implementation remains writable. Imported task versions are shadow
   configuration used only for development, playground evaluation, and comparison.
2. **Shadow execution:** new workflows run on evaluation batches and create isolated results. They do not change
   legacy outputs or matter metadata.
3. **Per-task cutover:** after parity and calibration, a matter explicitly selects the new task as authoritative
   for that use case.
4. **Compatibility view:** existing Matter Definition endpoints and UI resolve through the authoritative
   `ISSUE_REVIEW` task while retaining their external contract where practical.
5. **Deprecation:** legacy writes are disabled before old storage is removed. Historical assessments,
   conversations, summaries, and run provenance remain readable.

Never dual-write independently editable legacy and new definitions. At every phase, exactly one model is the
write authority; the other is imported, projected, or read-only. Keep a per-matter rollback switch until the new
path has completed an agreed observation period.

## Phase 3: Decision Specification contract

The Decision Specification is canonical JSON embedded in and governed by `matter_analysis_task_version`. It does
not have an independently publishable version or moving pointer.

### Canonical specification shape

```json
{
  "schema_version": "review-decision-specification-v1",
  "questions": {
    "responsiveness.issue_1_nonrenewal": {
      "type": "noul",
      "instructions": {
        "question": "Does `document.text` report an actual or threatened policy cancellation or non-renewal?",
        "focus": "Require an event, reported experience, or concrete threat."
      },
      "criteria": {
        "true": "The document reports a cancellation, non-renewal, or notice that coverage will end.",
        "false": "The document discusses insurance generally without reporting such an event."
      },
      "source_refs": [
        {
          "task_version_id": "uuid",
          "heading": "Issue 1",
          "excerpt_hash": "sha256"
        }
      ],
      "aggregation": {"operator": "ANY_WINDOW"},
      "evidence": {"required": true, "minimum_exists_probability": 0.7},
      "field_mapping": {
        "metadata_definition_key": "responsiveness",
        "value": "responsive",
        "uncertainty": {
          "kind": "DERIVED_PROBABILITY",
          "source": "MAPPED_BOOLEAN_PROBABILITY"
        }
      }
    }
  },
  "decision_policy": {
    "recommendations": {},
    "routes": {},
    "version": "decision-policy-v1"
  },
  "state_contract": {
    "builder_version": "document-review-state-v1",
    "required_paths": ["document.id", "document.metadata", "document.paragraphs"]
  }
}
```

The JSON is authoritative. The UI renders it as a readable question catalog with an advanced JSON view and a
canonical export. A generated Markdown rendering may be attached for convenience, but it is not the source of
truth and does not require a new Artifact type. The enclosing task version is the only publication boundary.

## Phase 4: compiler managed skill

Add and bootstrap a system-managed skill such as `compile-analysis-task-decision-specification`.

### Compiler input

- the exact draft Task Definition text and hash;
- task type, workflow key, and any pinned upstream task-version dependencies;
- available coding-field definitions and enum values;
- specification JSON Schema and question-type constraints;
- prior published task version when revising;
- optional reviewed examples and known boundary cases.

### Compiler output

- complete canonical specification;
- a source mapping for every question;
- human-readable rationale for adding each question;
- explicit omissions or ambiguities;
- suggested, but not automatically accepted, thresholds and routing rules;
- warnings about deterministic facts that should be computed in code.

### Deterministic validation

Reject or warn on:

- duplicate or unstable question keys;
- unsupported primitive types;
- malformed Choice, Score, or Noul criteria;
- a Choice without `other`, `unclear`, or `insufficient_evidence` where the options are not exhaustive;
- source references whose task version, heading, or quoted excerpt cannot be validated;
- field mappings to missing, inactive, incompatible, or non-snapshotted metadata definitions;
- embedded runtime values such as today's date instead of a state path;
- unknown aggregation operators or policy operations;
- unbounded question or option counts;
- broad questions that combine several independently testable conditions. This last check is a warning because
  atomicity requires judgment.

The compiler may propose changes, but only a matter administrator can publish the complete task version.

## Phase 5: review, editing, and publication UI

Add an Analysis Tasks panel to the matter page. Each task opens a Definition and Decision Guidance workspace.

### Draft generation

- Create or edit the Task Definition in the current draft task version.
- Generate or regenerate that version's Decision Specification.
- Display compiler progress and errors through the existing workflow/skill-run UI.
- Preserve all earlier draft and published task versions.

### Structured editor

- Group questions by definition section and purpose.
- Edit question instructions, criteria, aggregation, evidence policy, and field mapping.
- Show primitive-specific controls for Choice, Score, and Noul.
- Display definition citations and open the exact immutable task version.
- Show validation errors inline.
- Provide an advanced JSON view without making raw JSON the normal editing experience.

### Review and publish

- Diff both the definition and specification against the current published task version.
- Require all source references and field mappings to validate.
- Require an explicit acknowledgement when publishing an uncalibrated specification.
- Record publisher and publication time.
- Never mutate a version used by an existing run.

## Phase 6: provider-neutral decision engine and Jev adapter

Introduce a new module, separate from `app/model_execution.py`, with neutral types similar to:

```python
class SystemOneDecisionEngine(Protocol):
    async def evaluate(self, request: DecisionRequest) -> DecisionEnvelope: ...
```

`DecisionRequest` contains:

- structured state;
- a dictionary of typed question specifications;
- provider/model key and settings;
- limits, timeout, and retry policy;
- run and tracing identifiers;
- idempotency/cache identity.

`DecisionEnvelope` contains:

- normalized answers keyed by stable question key;
- Choice probabilities and confidence;
- Score value, legend, probabilities, and confidence;
- Noul probability, without inventing a separate confidence value;
- provider request ID, resolved model, token usage, latency, and attempts;
- raw provider metadata only where needed for support and auditing.

### Probability and confidence contract

Preserve the provider's semantics instead of collapsing every answer to one generic confidence number:

| Primitive | Persisted provider output | Scalar projected beside a mapped value |
| --- | --- | --- |
| Choice | selected option, probability for every option, provider confidence | provider confidence by default |
| Score | numeric score, legend, probability for every level, provider confidence | provider confidence by default |
| Noul | probability that the answer is yes | no provider confidence; optionally the probability of the mapped Boolean |

Every projected scalar has a `confidence_kind`:

- `PROVIDER_CONFIDENCE` for Choice or Score confidence returned by Jev;
- `SELECTED_PROBABILITY` for the selected option's provider probability;
- `DERIVED_PROBABILITY` for a documented transformation, such as mapping a Noul probability to the probability
  of the selected Boolean;
- `NONE` when no meaningful uncertainty scalar applies.

For a Noul with `p_yes`, mapping to a Boolean may project `p_yes` for `true` and `1 - p_yes` for `false`, but that
number must be labeled `DERIVED_PROBABILITY`, not Jev confidence. Routing rules should normally operate directly
on `p_yes` and an explicit uncertainty band around 0.5. The complete provider answer always remains in the
Decision Result even when a compact value is projected.

Implement `TypeSafeJevDecisionEngine` using the official SDK. Add configuration for API key, optional endpoint,
model alias, timeout, concurrency, requests per minute, token limits, and 429-aware backoff. Secrets remain in
environment configuration and are never copied into run snapshots.

Register decision engines in a code-owned registry. A specification selects an allowed engine/model policy; a
tenant cannot configure arbitrary endpoints or executable adapters.

### Telemetry

Reuse `model_invocation` with `skill_run_id` ownership. Add an invocation-kind discriminator only if required to
distinguish generative and decision calls in reporting. Do not introduce a second token or cost ledger. Aggregate
usage through SkillRun, WorkflowStepRun, and WorkflowRun as the existing managed workflow system does.

Development filesystem traces should use the existing trace facility and redact API keys and authorization
headers.

## Phase 7: single-document playground and calibration

Before corpus execution, provide a test surface where a user can select representative documents and run a draft
or published task version without creating coding values.

For each question show:

- typed answer;
- full probability distribution where available;
- provider confidence where supported, with its semantic label;
- Noul yes probability and mapped-value probability without presenting either as provider confidence;
- derived route and the exact rule that produced it;
- supporting paragraphs and evidence-existence score;
- specification source citation;
- provider/model version and latency.

Add a calibration run over a frozen batch with optional human-reviewed values as the reference set. Report:

- Noul precision, recall, false-positive/false-negative counts, and threshold curves;
- Choice confusion matrix and per-option accuracy;
- Score ordinal error and distribution;
- evidence-presence and paragraph-agreement measures;
- missing-text, partial-coverage, invalid-result, and provider-failure counts;
- results by document type or other selected cohort to expose inconsistent performance.

Threshold changes create a new draft task version. Do not allow automatic publication merely because a metric
target was met.

## Phase 8: durable first-pass batch workflow

Add a code-owned workflow such as `first_pass_document_review_v1` with managed-skill bindings for decision
evaluation and evidence localization.

### Launch

The user selects:

- a ready review batch;
- a published MatterAnalysisTaskVersion;
- an output name;
- isolated output policy, which is the default;
- optional second-pass behavior for uncertain documents.

Creation produces:

- one `WorkflowRun`;
- one linked `ReviewBatchRun` with `run_type=WORKFLOW`;
- frozen task-version, model, field-schema, and batch snapshots;
- one `ReviewBatchRunDocument` per batch member;
- workflow steps for preparation, decision evaluation, evidence localization, projection, and completion.

### Per-document execution

1. Load the preferred faithful source text from Artifact Service.
2. Build deterministic state from document metadata and the versioned paragraph map.
3. If the document fits the provider input limit, evaluate all compatible questions together.
4. Otherwise, evaluate bounded paragraph windows and apply the question's reviewed aggregation policy.
5. Locate and validate supporting paragraph IDs for actionable positive or boundary answers.
6. Apply the bounded decision-policy interpreter.
7. Store the immutable Decision Result.
8. Project mapped suggestions into isolated `ReviewBatchRunValue` rows.
9. Mark the document complete, skipped, or failed without hiding partial coverage.

### Long-document aggregation

Supported operators must be code-owned and explicit, for example:

- `ANY_WINDOW` for presence questions;
- `ALL_WINDOWS` for universal conditions;
- `MAX_PROBABILITY` for strongest-evidence selection;
- `HIGHEST_CONFIDENCE_CHOICE` for a reviewed use case;
- `INSUFFICIENT_IF_PARTIAL` where full coverage is mandatory.

Do not silently average unrelated Choice or Score distributions. Questions without a safe aggregation rule must be
routed to a second-pass model or human review when the state exceeds the provider limit.

### Idempotency and reuse

The reusable result key includes:

- tenant and matter;
- document and source-content hash;
- task-version definition and specification content hashes;
- state-builder and paragraph-map versions;
- provider and resolved model version;
- question-set and policy hashes.

Reuse copies or references the prior immutable result into the new run while preserving both runs' provenance. A
policy-only change may reuse raw answers but must recompute and store the derived recommendation under the new
policy version. A question or source-text change requires a new provider evaluation.

## Phase 9: result storage and evidence contract

Add a Core-owned `review_decision_result` keyed by review-batch run and matter document. It stores:

- status and coverage;
- MatterAnalysisTaskVersion ID plus definition and specification content hashes;
- document source artifact ID/hash and paragraph-map version;
- normalized typed answers JSON preserving complete Choice/Score probability distributions and confidence, and
  Noul yes probabilities without a fabricated confidence;
- derived recommendations and routes JSON;
- validated evidence paragraph references;
- raw-answer hash and policy-evaluation hash;
- evaluation and evidence SkillRun IDs;
- error code/message and timestamps.

Core owns this record because it is a review decision and workflow result. Artifact Service continues to own the
source text, paragraph/chunk artifacts, summaries, and vectors. Do not create an Artifact row for each question or
answer.

Mapped coding suggestions continue to use `ReviewBatchRunValue`. Extend it with:

- nullable `confidence_kind` constrained to `PROVIDER_CONFIDENCE`, `SELECTED_PROBABILITY`,
  `DERIVED_PROBABILITY`, or `NONE`;
- nullable `review_decision_result_id` linking to the complete immutable answer;
- nullable stable `question_key` identifying the mapping source.

The existing `confidence` column remains the compact scalar used by comparison and review UI, but it is not
interpretable without `confidence_kind`. The raw Decision Result remains available even when no question maps
directly to a coding field.

### Matter metadata publication contract

The current metadata ledger already accepts a scalar `confidence` on `MetadataEvent`, but it does not preserve
the scalar's semantics or expose confidence through `DocumentMetadataCurrent`. Before publishing task results:

- add `WORKFLOW` to the allowed MetadataEvent source types;
- add nullable `confidence_kind` with the same controlled values used by ReviewBatchRunValue;
- add nullable `review_decision_result_id` provenance, or an equivalently strict typed source relation;
- copy the compact scalar and its kind from the isolated run value;
- keep the complete distribution only in the immutable Decision Result;
- expose source confidence, confidence kind, and Decision Result provenance in `DocumentMetadataValueRead` by
  joining through `source_event_id`.

Do not denormalize confidence into `DocumentMetadataCurrent` merely for display. Add a projection column only if
high-volume confidence sorting/filtering becomes a demonstrated requirement. If indexed later, OpenSearch
confidence remains a rebuildable projection derived from the source MetadataEvent and Decision Result.

A human confirmation creates a separate `CONFIRM` event targeting the workflow assertion. It must not overwrite
or relabel the original model probability. A human-authored replacement value normally has no model confidence.

## Phase 10: review integration and controlled publication

In the batch review screen:

- select the first-pass run alongside human and other workflow runs;
- display recommended coding values, complete probabilities, explicitly labeled confidence/probability scalar,
  and route;
- open supporting paragraphs in the document viewer;
- compare a first-pass run with human decisions or another task version;
- filter by question outcome, uncertainty kind/band, evidence status, and human disagreement;
- show failures and partial coverage explicitly.

Any future bulk acceptance or publication to matter metadata must:

- show an impact preview;
- require matter-admin authorization;
- use the existing audited metadata command service;
- append metadata events rather than updating projections directly;
- retain the original isolated run and Decision Results.

Automatic publication is out of scope for the first release.

## Security, reliability, and operational controls

- Enforce tenant, client, matter, and batch authorization in the Core API.
- Make external-provider use explicit in configuration and documentation.
- Treat document text as untrusted state, never executable instructions.
- Use provider/model rate limiting in addition to concurrency limits.
- Honor retry headers and apply bounded exponential backoff for 429 and transient failures.
- Cap state size, question count, option count, per-run documents, requests, and tokens server-side.
- Show a launch warning and estimate for large runs; preserve the existing user-override approach where applicable.
- Continue successful document children when another child fails; surface exact coverage in the final state.
- Require minimum successful-document and evidence-coverage thresholds before presenting aggregate conclusions.
- Store provider request IDs and model versions for support without storing credentials.

## Testing strategy

### Unit tests

- specification JSON Schema and semantic validators;
- source-reference validation;
- primitive conversion and normalized answer parsing;
- bounded decision-policy interpreter;
- Noul, Choice, and Score threshold behavior;
- confidence-kind selection and preservation of complete probability distributions;
- Noul Boolean projection without fabricated provider confidence;
- long-document aggregation operators;
- cache/reuse fingerprinting;
- field-mapping compatibility;
- evidence paragraph validation.

### Adapter contract tests

- fake decision engine shared by workflow tests;
- Jev request serialization and response normalization;
- malformed response, timeout, 429, retry-header, and terminal-error handling;
- usage and ModelInvocation aggregation;
- no fabricated Noul confidence.

### Workflow tests

- DBOS restart and idempotent replay;
- partial document failure and `COMPLETED_WITH_ERRORS` coverage;
- reuse of raw answers after policy-only change;
- no reuse after question, model, source hash, or paragraph-map change;
- cancellation and restart behavior;
- isolated ReviewBatchRun values;
- publication copies confidence and confidence kind while retaining Decision Result provenance;
- human confirmation preserves the original workflow assertion and probabilities;
- explicit publish path uses audited metadata commands.

### Migration and compatibility tests

- create new task tables without changing existing Matter Definition behavior;
- idempotently import the current published Matter Definition into an `ISSUE_REVIEW` task version;
- preserve current draft/published revisions, assessments, conversations, and summaries;
- enforce one writable authority during legacy, shadow, cutover, and compatibility phases;
- switch a matter to the new authority and roll it back without losing either history;
- upgrade and downgrade new migrations against a temporary PostgreSQL database.

### UI tests

- create issue-review, privilege-review, topic-generation, and exploration tasks;
- edit a Task Definition and verify its Decision Specification becomes stale;
- generate, edit, validate, diff, and atomically publish a task version;
- run the single-document playground;
- launch and monitor a first-pass batch run;
- inspect complete probabilities, distinguish provider confidence from derived probability, and navigate evidence
  links;
- compare first-pass and human coding;
- permission and error states.

Live Jev tests must be opt-in and require an environment key. Default CI uses the adapter fake and recorded schema
fixtures, not billable provider calls.

## Delivery sequence

1. Write the Decision Specification JSON Schema, decision-policy DSL, provider-neutral request/response types, and
   architecture decision record.
2. Add the side-by-side MatterAnalysisTask and jointly versioned definition/specification persistence, APIs, and
   generated client types without changing current behavior.
3. Add the idempotent legacy Matter Definition importer, authority state, feature flag, and rollback path.
4. Bootstrap the compiler managed skill and add draft generation/regeneration plus deterministic validation.
5. Build the structured task-definition and specification review/edit/diff/publish UI.
6. Implement `SystemOneDecisionEngine`, its fake adapter, invocation telemetry integration, and the Jev adapter.
7. Add immutable Decision Results, explicit confidence semantics, and the single-document playground.
8. Add evidence localization, calibration runs, and threshold review.
9. Implement `ISSUE_REVIEW` and `PRIVILEGE_REVIEW` durable batch workflows in shadow mode.
10. Integrate results into batch Analysis/Coding and add comparison/filtering.
11. Cut over issue review per matter, then place the legacy Matter Definition API behind the compatibility view.
12. Extend the task framework to topic generation/assignment and bounded data-exploration workflows.
13. Add explicit publication of accepted values only after the advisory workflow is validated in practice.

Each delivery step should be independently migratable and testable. Do not begin batch-scale Jev execution before
the specification editor, provider fake, and single-document playground are usable.

## MVP acceptance criteria

- A matter administrator can create independently governed `ISSUE_REVIEW` and `PRIVILEGE_REVIEW` tasks.
- Editing a Task Definition creates a draft task version and prevents publication until its generated Decision
  Specification is current and valid.
- A generative compiler can produce a validated draft Decision Specification with traceable definition references.
- A user can edit, diff, test, and explicitly publish the definition and specification as one task version.
- A published task version can be run against one document through the Jev adapter.
- Every typed answer, complete probability distribution, supported provider confidence, derived probability kind,
  evidence reference, model version, and invocation is persisted and inspectable.
- A first-pass workflow can process a frozen batch durably and report exact success, failure, skip, and partial-
  coverage counts.
- Suggested field values remain isolated in a `WORKFLOW` ReviewBatchRun.
- A document with missing evidence, uncertain output, incomplete text, or invalid output is never silently
  auto-coded.
- Existing Matter Definition agents, assessments, summaries, batch chat, and human review continue to work.
- The existing Matter Definition can be imported and shadow-tested without changing its authority or content.

## Expansion acceptance criteria

- A `TOPIC_GENERATION` task can use a generative stage to propose a taxonomy and System One stages to evaluate
  candidates and assign documents without replacing the existing approval boundary.
- A `DATA_EXPLORATION` task can compile a bounded question set, evaluate a selected batch, and synthesize findings
  while preserving the underlying typed answers and evidence.
- Each task type uses the same atomic task-version, provenance, confidence, calibration, and isolated-result
  contracts.

## Deferred work

- Automatic publication of first-pass values.
- Learned downstream classifiers using Jev probabilities as features.
- Cross-matter or tenant-wide specification templates.
- Non-text Jev input.
- Provider selection by tenants outside the code-owned registry.
- Replacing document text with summaries as the primary decision state.
- Fully automatic compiler publication or threshold tuning.
- Physical removal of legacy Matter Definition tables before the compatibility observation period is complete.
