# First Pass Review Program Automation Plan

Status: proposed

Related plans:

- `docs/matter-definition-assessment-plan.md`
- `docs/system-one-decision-engine-plan.md`
- `docs/analysis-task-batch-sets-plan.md`
- `docs/analysis-result-comparison-artifact-implementation-plan.md`
- `docs/statistical-classification-backend-implementation-plan.md`
- `docs/classification-coding-projection-implementation-plan.md`
- `docs/jev-cal-review-program-workflow.md`

## 1. Outcome

Create a reusable Review Workflow Template framework and a durable First Pass Review template that coordinates Matter
Definition refinement, dual-model Q&A review, label sufficiency analysis, targeted retrieval, human disagreement
review, classifier training and improvement, whole-collection scoring, Q&A verification of classifier-positive
documents, and no-code quality control.

The program is an orchestration layer over existing domain workflows. Matter Definition assessments, Q&A runs,
review batches, coding comparisons, classification builds, scoring runs, reviewer coding, and job outcomes remain the
authoritative child records. The program records their relationships, evaluates completion policies, presents human
decisions, and starts the next authorized step.

The program does not remain blocked inside one model call or one indefinitely waiting workflow. It advances through
short, idempotent transitions. When a user decision is required, it records a durable action request, changes to
`WAITING_FOR_USER`, and resumes from the recorded state after the decision.

A matter may contain multiple independent first-pass review programs. Each program pins its own template version,
Matter Definition lineage, guidance, destination field, generated Analysis Tasks, model profiles, batches, label
sets, classifier versions, reports, and decisions. Creating one program never reconfigures another.

First pass is complete only when:

- the Matter Definition and Q&A specification are published;
- every active issue category is either training-ready or explicitly excluded by the user;
- a classifier version is accepted and scored across a frozen whole-collection population;
- classifier-positive documents have the configured Q&A coverage;
- final Q&A and classifier alignment has been reported;
- a no-code QC task-specific batch set has been reviewed by both Q&A models;
- QC findings have been accepted or remediated; and
- the program has a frozen completion report linking every source batch, run, model, decision, and unresolved item.

## 2. End-to-end program

```text
Matter Definition draft
    -> assessment and refinement loop
    -> published Matter Definition revision
    -> Q&A task compiled from definition
    -> label-discovery retrieval plan and frozen task-specific batch set
    -> Q&A run with model profile A
    -> Q&A run with model profile B
    -> alignment and per-issue training-sufficiency analysis
        -> targeted retrieval and dual-Q&A loop when support is insufficient
        -> user decision when an issue remains insufficient
    -> training-label policy decision
        A. union of both model code sets
        B. exact-agreement documents only
        C. exact agreements plus human-adjudicated disagreements
    -> immutable training label set
    -> classifier build and quality summary
        -> targeted improvement and retraining loop for weak categories
    -> user approves and activates a classifier version
    -> whole-collection classifier scoring
    -> comparison with existing Q&A coding
    -> dual-Q&A coverage of classifier-positive documents without Q&A results
    -> final source-alignment report
    -> no-code QC task-specific batch set
    -> blind dual-Q&A QC run and linked report
        -> remediation loop if required
    -> first-pass completion report
```

## 3. Locked design decisions

| Topic | Decision |
| --- | --- |
| Coordinating concept | Add a reusable Review Workflow Template framework. First Pass Review is one template, and each use creates an independent matter-bound program. |
| Guided construction | A program starts with Matter Definition work. It creates and presents each required Analysis Task, batch, comparison, classifier, and report as the user reaches that phase; setup does not require the user to assemble the workflow in advance. |
| Multiple programs | A matter may have multiple first-pass programs with different definitions, guidance, destination fields, model profiles, or policies. One nonterminal run is allowed per program, not per matter. |
| Template versioning | Programs pin an immutable workflow-template version. Template changes do not alter existing programs; migration is explicit and compatibility-checked. |
| Batching | Every automated Analysis Task population created by First Pass Review uses task-specific batching through an Analysis Task Batch Set with `PARTITIONED_SET` scope. Matter Definition assessment batches and comparison-derived difference or disagreement batches are ordinary `AD_HOC_BATCH` review batches. |
| Messaging | Child tasks report durable domain events to the Review Program coordinator. The coordinator uses the shared notification platform for human-facing attention only. Notification rows never drive workflow state. |
| Child authority | Existing batches, task versions, runs, artifacts, classifier versions, and reviewer revisions remain authoritative. |
| Q&A models | One shared Q&A specification is executed through two immutable model profiles against the same frozen documents. |
| Initial target | Each first-pass program initially supports one multi-value issue-coding metadata field and a one-vs-rest classifier. Other programs in the same matter may select different fields and guidance. |
| Assessment data | Matter Definition assessment runs remain diagnostic and blinded. They are not automatically used as classifier training labels. |
| Training sufficiency | Use the actual classification dataset and split feasibility rules, not a standalone raw-count threshold. |
| Label policy | The user chooses union, exact agreement only, or agreement plus human adjudication after seeing a preview of each option. |
| Model agreement | Agreement is a consistency signal, not proof of accuracy. Q&A outputs remain machine-generated silver labels unless reviewed by a human. |
| Human gates | Matter Definition publication, persistent issue insufficiency, training-label policy, classifier acceptance, whole-collection launch, and QC acceptance require explicit user action. |
| Retrieval loops | Loops have document, round, token, and low-yield stopping policies. The system never searches indefinitely. |
| Model output | Classifier scoring remains isolated until an explicit downstream coding-resolution or publication action. |
| Population scope | Every batch and scoring run freezes an explicit population. Later stages reference those snapshots rather than reconstructing membership. |
| Large results | Document-level comparisons, training labels, predictions, and final reports use immutable artifacts. PostgreSQL remains the control plane. |
| Completion | First-pass completion records the accepted state; it does not silently publish model or Q&A values into authoritative matter metadata. |

## 4. Existing platform boundary

### 4.1 Capabilities to reuse

The program should compose these existing capabilities:

- Matter Definition revisions, assessment retrieval planning, diagnostic batches, synthesis, clarification
  questions, and guidance refinement.
- Versioned Matter Analysis Tasks and provider-neutral Decision Specifications.
- Q&A batch execution, isolated `ReviewBatchRunValue` coding, exact evidence, retry, cancellation, and usage
  accounting.
- Review batches, derived batches, disagreement batches, and Analysis Task batch sets.
- Batch comparison for two or more runs, including exact, partial, disagreement, incomplete, per-class, and source
  coverage results.
- Reviewer coding revisions for manual disagreement resolution.
- Classification target distribution, dataset preview, label authority, group-aware splits, class exclusions,
  build, evaluation, model approval, activation, scoring, and prediction artifacts.
- Classifier projection and derived batch creation from completed scoring artifacts.
- Analysis Input Manifests and Job Outcome Manifests for scalable frozen inputs and document results.
- Unified job execution, workflow, skill, model invocation, audit, authorization, and search projection services.

### 4.2 Missing orchestration capabilities

The principal gaps are:

- a reusable, versioned workflow-template contract and registry;
- a durable parent state machine connecting the child workflows;
- a durable program-event adapter that converts child terminal transitions into idempotent coordinator messages;
- a template-stage batching contract and an immutable manifest population source for task-specific batch sets whose
  populations cannot be represented by one supported search query;
- one logical Q&A task version with two model-specific execution profiles;
- a training-oriented retrieval planner that targets issue support deficits;
- a cross-round issue-support report based on actual classifier feasibility;
- a durable training-label resolution artifact implementing union, agreement, and adjudication policies;
- a user-facing retraining loop and comparable model-version summaries;
- creation of Q&A batch sets from classifier-positive documents that lack Q&A coverage;
- a final three-source Q&A and classifier comparison report;
- a statistically interpretable no-code QC sampler and linked report; and
- a frozen first-pass completion record.

### 4.3 Automation ownership

The program should deliberately divide work among the agent, deterministic application code, and the reviewer:

| Owner | Responsibilities |
| --- | --- |
| Agent and managed skills | Synthesize assessment findings, draft clarification questions, compile a Q&A-task proposal, propose retrieval queries and quotas, explain model alignment, diagnose weak categories, and recommend remediation. Outputs are structured proposals with source references. |
| Deterministic services and workflows | Validate proposals, freeze inputs, execute searches, create batches, dispatch child jobs, calculate counts and comparisons, resolve the selected label policy, test dataset feasibility, train and score classifiers, sample QC populations, enforce budgets, and advance the state machine. |
| Senior reviewer | Publish definitions and tasks, answer substantive questions, decide whether an issue may be ignored, choose the label policy, adjudicate when required, accept a classifier, authorize collection-wide work, and accept or remediate QC findings. |

The agent may recommend the next step, but it cannot manufacture a user decision or replace deterministic counts,
eligibility rules, sampling logic, label resolution, or workflow state. This makes the process explainable and
replayable while still using the agent where legal judgment and synthesis add the most value.

## 5. Program terminology

| Term | Meaning |
| --- | --- |
| Review Workflow Template | A reusable, matter-independent definition of phases, prerequisites, generated artifacts, agent skills, human gates, transition rules, defaults, and completion criteria. |
| Template version | An immutable published version of a Review Workflow Template. A program pins one version. |
| Program | One matter-bound instance of a template with its own Matter Definition lineage, guidance, fields, generated child records, decisions, and history. |
| Program version | An immutable configuration checkpoint. Early versions may configure assessment only; later versions add the accepted definition, Q&A, training, scoring, coverage, and QC decisions. |
| Program run | One audited start-to-completion execution that begins from a published version and may adopt a newer version only at an explicit configuration checkpoint. |
| Program event | A durable, machine-facing message that records a child or program transition and wakes the coordinator. It is not shown in the user notification inbox. |
| User notification | A typed, human-facing projection created through the shared notification platform when a program event requires attention or communicates a meaningful completion. |
| Task-specific batch set | An Analysis Task Batch Set pinned to one Analysis Task and task version. Its managed child Review Batches use `PARTITIONED_SET`, cover one frozen population exactly, and do not overlap within that set. |
| Ad hoc review batch | An ordinary overlapping Review Batch using `AD_HOC_BATCH`. First Pass Review limits this to Matter Definition assessment and difference or disagreement review. |
| Discovery round | One retrieval batch followed by two Q&A runs and a support analysis. |
| Training round | One immutable label snapshot and classifier build attempt. |
| Q&A profile | A pinned provider, model, model policy, and skill binding used to execute one shared Q&A specification. |
| Resolved training label set | The immutable result of applying union, agreement, or adjudication policy to Q&A outputs. |
| Active issue | An issue value the program is expected to train and evaluate. |
| Ignored issue | An issue deliberately excluded from the classifier track by a recorded user decision. It remains in Matter Definition unless separately edited. |
| Classifier-positive document | A successfully scored, non-abstained document with at least one issue selected under the pinned prediction-resolution policy. |
| Q&A-covered document | A document with successful terminal results from every Q&A profile required by the program version. |
| No-code population | Documents with no selected issue from the accepted classifier result and no selected issue from any successful required Q&A run, after exclusions are accounted for. |

## 6. Program data model

### 6.1 `review_workflow_template`

The reusable template identity:

- tenant scope or system scope;
- key, name, description, and workflow type;
- current draft and published version IDs;
- status: `DRAFT`, `ACTIVE`, `DEPRECATED`, or `ARCHIVED`;
- owner and visibility policy; and
- lock version.

The initial system template key is `FIRST_PASS_REVIEW`. A later Second Pass Review template uses the same framework
without sharing matter-specific program state.

### 6.2 `review_workflow_template_version`

The immutable template contract contains:

- ordered user-facing phases and internal stage keys;
- stage prerequisites and completion conditions;
- child artifact types generated at each stage;
- required execution scope and batching policy for each stage, including whether the stage creates a task-specific
  `PARTITIONED_SET`, uses `WHOLE_MATTER`, or permits an ordinary `AD_HOC_BATCH`;
- agent and managed-skill bindings for proposals and synthesis;
- deterministic service commands used to validate and execute work;
- required previews, user decisions, and allowed transitions;
- retry, loop, resource-limit, program-event, user-notification, and reopening policies;
- default Q&A profiles, classifier policy, and QC policy;
- UI presentation metadata and plain-language status mappings;
- template schema version and content hash; and
- compatibility metadata for explicit migration.

Templates define the flow and defaults. They do not contain a matter's definition, issue guidance, destination
field, document population, Analysis Task IDs, model results, or decisions.

### 6.3 `review_program`

The stable program identity:

- tenant and matter IDs;
- pinned Review Workflow Template and template-version IDs;
- key, name, and description;
- working Matter Definition lineage and target metadata definition ID when selected;
- current and published program versions;
- current active run ID;
- status: `DRAFT`, `ACTIVE`, `SUSPENDED`, or `ARCHIVED`;
- program owner; and
- lock version.

Allow multiple programs in one matter, including multiple programs created from the same First Pass Review template.
Only one nonterminal run should be allowed per program unless that template explicitly supports parallel independent
tracks. No uniqueness constraint may limit a matter to one program or one program per template.

### 6.4 `review_program_version`

An immutable published configuration containing:

- configuration-completeness stage and predecessor version ID;
- pinned workflow-template version ID and applicable template-policy hashes;
- Matter Definition ID, published revision ID, and content hash;
- issue-coding target snapshot and active issue keys;
- Q&A task ID, task version ID, specification hash, and two Q&A profiles;
- retrieval, deduplication, family, quota, and control-sample policies;
- discovery round and resource limits;
- classification model and configuration templates;
- class-support and split-feasibility policy;
- model-quality thresholds;
- whole-collection scoring and prediction-resolution policy;
- Q&A coverage policy for classifier positives;
- final comparison policy;
- no-code QC sampling, reporting, and stopping policy;
- required human gates; and
- source and configuration hashes.

Fields that belong to a later phase may be absent from an early checkpoint. Publishing validates every populated
reference plus all fields required to execute through the checkpoint's next human gate. Once Q&A setup is complete,
publishing also validates that every field and issue value belongs to the matter, is active, and can be represented
by the Q&A and classifier contracts.

### 6.5 `review_program_run`

The run stores:

- program ID, starting program-version ID, and current program-version ID;
- ordered program-version transition records with actor, reason, source stage, and compatibility result;
- overall status: `QUEUED`, `RUNNING`, `WAITING_FOR_USER`, `COMPLETED`, `COMPLETED_WITH_ERRORS`, `FAILED`, or
  `CANCELED`;
- current stage and stage ordinal;
- current discovery and training round numbers;
- workflow and job execution references;
- pending action request ID;
- compact progress and report summaries;
- frozen initial configuration hash;
- failure and retry information; and
- started, completed, and canceled timestamps.

### 6.6 `review_program_stage_attempt`

Each transition attempt records:

- run, stage, ordinal, and attempt;
- status and idempotency key;
- input snapshot and policy hashes;
- child job references;
- result or report artifact references;
- compact counts;
- failure details; and
- timestamps.

A completed stage attempt is immutable. A retry creates a new attempt or resumes a child job according to that
child's existing retry contract.

### 6.7 `review_program_child`

A generic link to an authoritative child record:

- parent run and stage attempt;
- purpose such as ad hoc assessment batch, discovery batch set, Q&A A, Q&A B, ad hoc difference or disagreement
  batch, label resolution, classifier build, scoring, coverage batch set, comparison, or QC batch set;
- child job type and ID;
- optional matter task, task version, batch, model, model version, or artifact IDs;
- status projection; and
- deterministic derivation key.

The link never replaces the child record and does not copy its document results.

### 6.8 `review_program_action`

A durable human gate:

- run and stage;
- action type;
- title and explanation;
- immutable decision context and preview hash;
- allowed choices;
- status: `OPEN`, `RESOLVED`, `SUPERSEDED`, or `CANCELED`;
- resolution payload, reason, actor, authorization context, and timestamp; and
- stale-state guards.

Only one blocking action is open for a run. Resolving an action reauthorizes the user and invokes the same command
used by a direct UI action.

### 6.9 `review_program_event`

A durable machine-facing event record contains:

- tenant, matter, program, program-version, run, and stage-attempt IDs;
- event type and schema version;
- source type and source ID, normally a child domain record or `WorkflowRun`;
- bounded transition data such as terminal status, outcome counts, and failure classification;
- producer-supplied idempotency key;
- status: `PENDING`, `PROCESSING`, `PROCESSED`, or `FAILED`;
- processing attempt, failure, and next-retry information; and
- occurred, created, and processed timestamps.

The unique program/event idempotency key prevents workflow retries and reconciliation from advancing the program
twice. The event contains references and compact routing data, never document evidence or unbounded child output.

### 6.10 `first_pass_issue_support_snapshot`

Store compact per-issue counts and feasibility results for one discovery state:

- active or ignored state;
- model A positives, model B positives, union positives, agreement positives, and disagreements;
- complete, incomplete, failed, and not-evaluated document counts;
- distinct family and duplicate-group counts;
- estimated negatives and negative groups;
- training eligibility under each label-policy option;
- blocking class-exclusion reason codes; and
- source batch and run references.

Large document-level details remain in comparison and label artifacts.

### 6.11 `first_pass_training_label_set`

The control-plane record for an immutable artifact containing one row per document and target value decision:

- label-policy choice and version;
- source batch, run, reviewer coding, and comparison references;
- population and label content hashes;
- row-level authority (`SILVER` or `ADJUDICATED`);
- resolution status and reason;
- selected issue keys;
- provenance record IDs; and
- counts by issue, authority, and resolution state.

Add a classifier label source capable of reading this artifact without creating one PostgreSQL row per label. The
dataset snapshot freezes the exact artifact and content hash.

### 6.12 `review_program_completion_report`

The generic completed record references an immutable, template-typed report artifact. For First Pass Review it
contains:

- final Matter Definition and Q&A specification;
- all discovery, coverage, and QC batch sets plus ad hoc assessment and difference-review batches;
- all Q&A runs and model profiles;
- the accepted training label policy and artifact;
- classifier version, evaluation, approval, activation, and scoring provenance;
- final comparison and no-code QC results;
- ignored issues and unresolved documents;
- every user gate and decision; and
- completion actor, reason, and timestamp.

## 7. Program execution model

### 7.1 Event-driven coordinator

Use short coordinator transitions rather than a single long-running workflow waiting for users:

1. Receive or reconcile one durable `review_program_event` from a child or user action.
2. Lock the event and program run.
3. Verify the expected stage, authoritative child state, and configuration hashes.
4. Finalize the previous stage summary.
5. Determine the next automatic stage or human gate.
6. Create the next child job, action request, and any user-facing notification with stable idempotency keys.
7. Mark the event processed and commit the transition atomically.

Child completion, action resolution, and an administrative reconcile command all publish or recover the same typed
program event and call the same transition service. A periodic reconciler may repair a missing event or dispatch from
authoritative child state but must not infer or fabricate user decisions.

### 7.2 Overall stages

Recommended stage keys:

```text
MATTER_DEFINITION
DEFINITION_ASSESSMENT
DEFINITION_REFINEMENT
QA_TASK_SETUP
DISCOVERY_RETRIEVAL
DISCOVERY_QA
DISCOVERY_ANALYSIS
ISSUE_SUPPORT_DECISION
TRAINING_LABEL_DECISION
DISAGREEMENT_REVIEW
TRAINING_LABEL_FINALIZATION
CLASSIFIER_TRAINING
CLASSIFIER_REVIEW
CLASSIFIER_IMPROVEMENT
CLASSIFIER_ACCEPTANCE
WHOLE_COLLECTION_SCORING
QA_COVERAGE_ANALYSIS
QA_COVERAGE_EXECUTION
FINAL_ALIGNMENT
NO_CODE_QC
QC_DECISION
FINALIZING
COMPLETED
```

The overall run status is separate from the stage. A run at `CLASSIFIER_REVIEW` may be `WAITING_FOR_USER`; a child
failure at `DISCOVERY_QA` may be `FAILED` while retaining completed child runs for retry.

### 7.3 Loop limits

The program version freezes:

- maximum Matter Definition assessment rounds;
- maximum discovery rounds;
- maximum classifier improvement rounds;
- maximum retrieved documents and families per issue and round;
- total Q&A document and model-call budgets;
- minimum novel-document yield for continuing retrieval;
- maximum consecutive zero-support rounds; and
- QC remediation limits.

Reaching a limit produces a human decision with the measured shortfall. It never silently ignores an issue or marks
the program complete.

### 7.4 Configuration checkpoints

A run may intentionally adopt a newer immutable program version at a human gate, because Matter Definition
refinement, Q&A publication, issue exclusion, and accepted QC remediation can materially change later work. The
transition must:

1. identify the last valid completed stage;
2. compare old and new configuration hashes;
3. mark downstream stages and previews stale without changing their historical records;
4. record the actor, reason, and compatibility result; and
5. resume from the earliest affected stage.

Every stage attempt and child record remains pinned to the program version under which it ran. A run never silently
adopts a newly published Matter Definition, task version, model profile, schema, or policy.

### 7.5 Guided artifact construction

The user creates a program from a template and begins with Matter Definition work. The initial setup does not require
an existing Q&A task, discovery batch set, classifier, or QC configuration. When the run reaches a stage, the
coordinator:

1. reads the pinned template-stage contract;
2. verifies the prior stage and current program configuration;
3. invokes the configured agent or managed skill to draft the next matter-specific proposal;
4. validates the proposal through deterministic application services;
5. presents the proposal, warnings, consequences, and editable fields to the user;
6. records the user's approval or requested refinement; and
7. creates and pins the authoritative child record only after the required gate is satisfied.

For First Pass Review, this sequence creates the Matter Definition lineage first, then the destination mappings and
Q&A task, followed by retrieval batches, dual Q&A runs, comparisons, resolved labels, classifier records, scoring
runs, coverage batch sets, and QC artifacts. Generated records are reusable through their existing domain APIs but
remain linked to the program that caused their creation.

### 7.6 Program isolation and overlapping fields

Every generated child link, artifact, run, and decision carries the originating program ID and version. Support,
coverage, completion, and provenance queries operate within that scope unless a user explicitly imports an eligible
external source.

Different programs may use different Matter Definitions, guidance, issue fields, or values. They may also evaluate
the same destination field because Q&A and classifier outputs remain isolated during analysis. If two programs could
publish competing values to the same authoritative metadata field, the system warns at setup and blocks automatic
publication. A later coding-resolution or publication workflow must identify the source program and preview the
conflict policy explicitly.

### 7.7 Template batching contract

Every template stage declares its execution scope and permitted batch materialization. The generic coordinator
rejects a child launch whose scope does not match the pinned template version and Analysis Task version.

The First Pass Review template uses:

| Work | Scope | Batch behavior |
| --- | --- | --- |
| Matter Definition assessment | `AD_HOC_BATCH` | Create one ordinary diagnostic assessment batch. It may overlap any other batch and is never training-eligible by default. |
| Initial and supplemental discovery Q&A | `PARTITIONED_SET` | Create one task-specific Analysis Task Batch Set from the frozen retrieval population and run the pinned Q&A task across its managed children. |
| Difference or disagreement review | `AD_HOC_BATCH` | Create an ordinary comparison-derived batch for human review. It may overlap its source sets and retains both source-run references. |
| Classifier training | No Review Batch execution | Consume the immutable resolved-label artifact and dataset snapshot. |
| Whole-collection classifier scoring | `WHOLE_MATTER` | Use the whole-matter input manifest rather than creating an `ALL_MATTER` Review Batch solely for execution. |
| Q&A coverage of classifier positives | `PARTITIONED_SET` | Create a task-specific set from the frozen classifier-positive coverage manifest. |
| No-code QC Q&A | `PARTITIONED_SET` | Create a task-specific set from the frozen, stratified QC selection manifest. |

No other workflow-created Review Batch may be run as `AD_HOC_BATCH`. A template for a future review program must
declare its own stage policy, but it uses the same three platform modes and validation contract.

Some first-pass populations are arbitrary document sets rather than one keyword search or an existing source batch.
Extend Analysis Task Batch Set population sources with an immutable `INPUT_MANIFEST` source that pins:

- Artifact-owned ordered document IDs and source-content references;
- manifest type and schema version;
- program, stage, and source report IDs;
- selection and sampling provenance;
- population count and content hash; and
- authorization scope.

The batch-set materializer verifies the artifact and deterministically creates managed `TASK_BATCH_PARTITION`
children. This avoids creating an ordinary staging Review Batch merely to feed a task-specific set. Existing
`SEARCH_QUERY`, `SAVED_SEARCH`, `SOURCE_BATCH`, and `ALL_MATTER` sources remain available when they naturally express
the stage population.

### 7.8 Task, workflow, and user messaging

Use one event-to-notification pipeline while preserving the notification platform's authority boundary:

```text
authoritative child transition
    -> durable review_program_event
    -> idempotent Review Program coordinator transition
    -> optional review_program_action
    -> optional typed user notification
    -> global inbox and safe deep link to the Review Program
```

The child task does not send prose to the workflow or user. It commits its authoritative state and a typed event in
the same Core transaction when possible. The event handler reloads the child record before acting, so the event is a
wake-up and routing mechanism rather than a second source of truth.

Program-managed child `WorkflowRun` records must declare a generic parent-managed notification policy and identify
their parent program run. Terminal child transitions still emit program events, but they do not publish the existing
generic `job.completed` notification for every child. This prevents one batch set from flooding the initiating user
with child-completion messages. Non-program jobs retain the existing notification behavior.

The coordinator publishes a human notification only when the program has meaningful attention state:

| Program occurrence | User notification | Default audience | Primary route |
| --- | --- | --- | --- |
| Human decision becomes open | `review_program.action_required` | Users selected by the action endpoint's exact permission or assignment policy | Open the program at the current decision |
| Difference-review batch is assigned | `review_program.review_assigned` | Assigned reviewer or review group | Open the assigned batch with program return context |
| Child failure blocks the phase after automatic retry policy | `review_program.attention_required` | Program owner and configured operational audience | Open the blocking failure card |
| Resource or loop limit requires a decision | `review_program.action_required` | Authorized program decision makers | Open the limit decision |
| Program completes | `review_program.completed` | Program owner and explicitly configured recipients | Open the completion report |
| Program stops or is canceled | `review_program.stopped` | Program owner and initiator when different | Open program history |

Routine child completion, queued or running progress, successful retry, reconciliation, per-document result, and
every managed child-batch completion remain in the program activity feed and job views. They do not create user
notifications.

Notification types are code-owned and schema-versioned in the shared registry. They use matter scope, bounded
template data, allowlisted Review Program routes, durable recipient resolution, and stable idempotency keys. They
contain no evidence excerpts, document text, model reasoning, raw task payloads, or arbitrary URLs.

`review_program.action_required` uses the `review_program_action` as its authoritative source. Resolving the action
through the program endpoint updates the notification's resolution projection for every recipient without changing
individual read or dismissal state. Opening, reading, or dismissing the notification never resolves the action.

If notification publication for an action-required item resolves no eligible recipient, the program remains in
`WAITING_FOR_USER`, records an operational error, and follows the configured escalation policy. It must not route the
decision to an unauthorized administrator or continue automatically.

## 8. Phase 1 Matter Definition and assessment loop

### 8.1 Setup

The guided program begins by creating a new Matter Definition draft, forking an existing definition into a new
program-owned lineage, or adopting an existing draft or published revision as its starting point. Reuse is an
explicit choice; one program's later refinements never mutate another program's pinned definition history. The
definition must give each issue a stable criterion key that can later map to a stable target metadata value.

### 8.2 Assessment

For each assessment round:

1. Pin the current published Matter Definition revision.
2. Run the existing retrieval planner.
3. Materialize its diagnostic assessment batch as an ordinary `AD_HOC_BATCH` Review Batch.
4. Analyze the batch and synthesize fit findings and clarification questions.
5. Present coverage, recurring near misses, gaps, and proposed clarification questions.

Assessment batches remain diagnostic. Their labels are blinded and excluded from classifier training unless a later
explicit human-review workflow creates independent eligible labels.

### 8.3 Refinement gate

When blocking or material questions exist, the program asks the user to:

- answer or dismiss each question;
- review the generated guidance refinement;
- edit the resulting draft revision if needed; and
- publish the revision or stop the program.

Publication starts a new assessment round. The loop exits when there are no unresolved blocking questions and the
user accepts the Matter Definition for first-pass execution. Nonblocking questions may remain, but the completion
record preserves them.

### 8.4 Exit artifact

Freeze:

- Matter Definition ID, revision, content hash, and issue keys;
- assessment IDs and diagnostic batch IDs;
- answered, dismissed, and open questions;
- accepted known limitations; and
- user acceptance decision.

## 9. Phase 2 Q&A task and two model profiles

### 9.1 Generate the Q&A task

Compile one Q&A task definition from the accepted Matter Definition. The compiler must produce:

- one question and field mapping for each active issue;
- responsiveness or scope questions required by the Matter Definition;
- exact stable metadata field and value mappings;
- evidence requirements;
- deterministic aggregation and routing policy; and
- source references back to Matter Definition excerpts.

If an issue has no valid metadata value, the compiler proposes the value and parent mapping. A human must approve
the metadata change before task publication.

### 9.2 Separate specification from model execution

The Q&A Decision Specification should be shared by both models. Add immutable Q&A execution profiles that pin:

- decision provider;
- model key and resolved model identity;
- model policy and supported capabilities;
- skill definition version;
- provider adapter version; and
- profile content hash.

Each Q&A batch run pins one profile. The program validates that the two runs use the same task version, Decision
Specification hash, destination mappings, input manifest, and evidence policy. Only the model profile may differ.

An interim implementation may use two synchronized tasks if execution profiles cannot be added immediately, but it
must reject drift in definition, specification, mappings, and output schema. Execution profiles are the preferred
design because they preserve one logical task.

### 9.3 Task publication gate

The user reviews:

- generated questions and issue mappings;
- omissions and compiler warnings;
- the two selected model profiles;
- expected output fields; and
- evidence and failure policies.

The program proceeds only after the Q&A task version is published and both profiles validate against it.

## 10. Phase 3 Initial retrieval and dual Q&A

### 10.1 Training-oriented retrieval plan

Generate a label-discovery retrieval plan from the accepted Matter Definition. Unlike the diagnostic assessment
plan, this plan targets training support. For each active issue it specifies:

- keyword, semantic, and hybrid searches;
- positive-document quota;
- hard-negative and near-miss quota;
- random or broad control quota;
- family and duplicate handling;
- exclusions for documents already reviewed in the program; and
- rationale and Matter Definition source references.

The plan returns only controlled `MatterSearchRequest` shapes. Code executes searches against one pinned physical
index generation, fuses ranks deterministically, removes previously reviewed documents, keeps configured families
together, and records selection reasons.

### 10.2 Discovery task-specific batch set

Freeze the selected documents in an Analysis Task Batch Set using `PARTITIONED_SET`. Use a directly supported search
source when it exactly represents the retrieval plan; otherwise publish an `INPUT_MANIFEST` and materialize from
that manifest. The set and its managed child Review Batches record:

- Matter Definition and Q&A task provenance;
- retrieval plan and physical index generation;
- selection, deduplication, family, and sampling policy versions;
- per-issue quotas and retrieval reasons; and
- deterministic document order.

Mark the set's managed children and Q&A runs as classification-training eligible. Do not reuse the ad hoc assessment
batch or its blinded results as training labels. No ordinary discovery Review Batch is created as a staging source.

### 10.3 Dual Q&A execution

Launch profile A and profile B over the same frozen discovery batch set and its identical managed children. The
parent stage completes only when both profile run-all executions are terminal or the user resolves a non-retriable
source failure.

The run pair preserves:

- identical batch-set population, managed child membership, and task-version identity;
- exact profile and model identity;
- result, evidence, and value coverage;
- failures, partials, skips, and not-evaluated documents; and
- request, token, cost-estimate, and latency metrics.

The program does not promote either model's values to matter metadata.

## 11. Phase 4 Alignment and training-sufficiency analysis

### 11.1 Multi-source comparison

Create a durable comparison over the discovery batch set and all managed children. Report:

- exact, partial, disagreement, incomplete, and not-evaluated counts;
- pairwise and multi-source agreement;
- per-issue selection counts for both models;
- per-issue intersection, union, and disagreement counts;
- documents selected by only one model;
- confidence and failure distributions; and
- family, custodian, date, record-type, and other configured cohort breakdowns.

The comparison is descriptive. Neither Q&A model is treated as the reference unless a human source is later added.

### 11.2 Sufficiency previews

For each of the three training-label options, build a read-only classifier dataset preview using the current
classifier configuration and proposed label-resolution semantics. A category is training-ready only when the
existing classifier pipeline can produce feasible group-aware development folds.

Use these existing exclusion conditions as the source of truth:

- insufficient positive documents;
- insufficient positive groups;
- insufficient negative documents;
- insufficient negative groups; and
- infeasible fold support.

The support report must not reduce readiness to one minimum positive count. It shows raw document counts, distinct
groups, negative support, conflicts, missing labels, and the effect of each policy choice.

### 11.3 Targeted retrieval loop

When an active issue is not feasible under any acceptable label option:

1. Generate a supplemental retrieval plan only for deficient issues and required hard negatives.
2. Use existing positive examples, Matter Definition language, near misses, entities, date ranges, and cohort gaps.
3. Exclude all previously reviewed documents and already selected family or duplicate groups according to policy.
4. Preview expected new counts, allocation, and estimated Q&A usage.
5. Materialize a supplemental task-specific Analysis Task Batch Set.
6. Run both Q&A profiles.
7. Recompute comparison and support across all discovery rounds.

The planner records actual yield per query and issue so later rounds can broaden, narrow, or abandon low-yield
strategies rather than repeating them.

### 11.4 Persistent insufficiency gate

When support remains infeasible after the configured rounds or budgets, prompt the user for each deficient issue:

- **Refine and continue**: revise Matter Definition terminology, issue boundaries, or retrieval guidance, publish a
  new program version, and run another targeted round.
- **Ignore for this classifier**: exclude the issue from the classifier track while retaining it in Matter
  Definition and Q&A reporting.
- **Stop**: leave the program incomplete.

Ignoring an issue requires a reason and an impact preview. It changes the classifier's active class set but does not
delete or deactivate the matter metadata value.

## 12. Phase 5 Training-label policy and disagreement review

### 12.1 Option A Union of both Q&A models

For each document successfully and completely evaluated by both profiles:

```text
resolved issue set = model A issue set union model B issue set
```

This maximizes positive recall but may import one model's false positives. Documents with an incomplete or failed
required run are excluded rather than treating missing output as an empty set. Resulting labels have `SILVER`
authority.

### 12.2 Option B Exact agreement only

Include a document only when both required models completed successfully and their complete issue sets are equal.
The agreed set becomes the silver label. Exclude partial-set agreement, disagreements, failures, and incomplete
documents.

Exact document-set agreement is the recommended initial meaning of “use only codes where models agree.” Per-code
intersection can silently convert disputed positives into negatives and should be a separate future policy if
needed.

### 12.3 Option C Agreement plus human-adjudicated disagreements

Use exact-agreement documents as silver labels and create a derived disagreement batch for all partial and complete
disagreements. Reviewers assign final issue sets through reviewer coding. Completed human decisions have
`ADJUDICATED` authority and override machine silver labels for the same document.

The disagreement batch is an ordinary `AD_HOC_BATCH` Review Batch because it is a comparison-derived human-review
population, not an automated Analysis Task execution population. It retains the source batch-set, child-run,
comparison, and document-decision provenance. If a later action runs an Analysis Task on those documents, create a
new task-specific batch set from the difference manifest instead of executing the task ad hoc.

Failures, partial results, and documents that cannot be reliably adjudicated remain excluded or unresolved; they
are not negative labels.

### 12.4 Decision preview

Before asking the user, show all three options side by side:

- eligible and excluded document counts;
- per-issue positive and negative support;
- family and duplicate-group support;
- source conflicts and unresolved documents;
- classes that would be excluded;
- expected disagreement-review volume; and
- dataset and split feasibility.

The user selects one option, records a reason, and may choose Option C even when A or B is feasible because quality
policy is a legal-review decision, not an optimization performed by the agent.

### 12.5 Training label finalization

After any required disagreement review completes, materialize one immutable `first_pass_training_label_set`.
Validate the artifact, recompute support, and run the authoritative classifier dataset preview. If the selected
policy is no longer feasible because of exclusions or incomplete human review, return to the issue-support gate.

## 13. Phase 6 Classifier training and quality summary

### 13.1 Build

Create or reuse a classifier bound to the target issue field. For a multi-value issue field, use `ONE_VS_REST`.
Freeze:

- the training label artifact and hash;
- target field and active class snapshot;
- embedding job and feature specification;
- family and duplicate grouping;
- validation policy and random seed;
- trainer specification and runtime image; and
- class exclusions accepted by the user.

Then materialize features and group-aware splits, build the model, store its package, predictions, and evaluation,
and create a candidate model version.

### 13.2 Quality summary

Summarize overall and per-class development results:

- eligible and excluded training documents;
- positive, negative, and group support;
- precision, recall, F1, PR AUC, ROC AUC when defined, false positives, and false negatives;
- macro and micro metrics;
- calibration and threshold warnings;
- fold support and variability;
- confusion information appropriate to the classification strategy;
- results by configured cohort when sufficient support exists;
- feature, runtime, and data-quality warnings; and
- Q&A source composition and human-adjudicated share.

Cross-fit development metrics must be labeled as development estimates. They are not an independent measure when
the labels themselves come from Q&A models. Human-adjudicated or sealed evaluation cohorts are shown separately.

### 13.3 Weak-category classification

Apply a versioned, matter-configurable quality policy. A category may be:

- `BLOCKED_SUPPORT`;
- `WEAK_SUPPORT`;
- `WEAK_PRECISION`;
- `WEAK_RECALL`;
- `UNSTABLE_FOLDS`;
- `CALIBRATION_RISK`;
- `COHORT_REGRESSION`; or
- `READY`.

The report gives evidence for each status and does not silently average a weak category into an acceptable overall
score.

## 14. Phase 7 Classifier improvement and retraining loop

### 14.1 Improvement gate

Ask the user to:

- accept the candidate as-is;
- gather more review data for selected weak categories;
- change training or threshold configuration and rebuild;
- ignore additional categories with an impact preview; or
- reject the candidate and stop.

Acceptance and activation remain separate existing model-governance decisions.

### 14.2 Suggested retrieval plan

For selected weak categories, generate a review-data plan that distinguishes:

- additional likely positives for low support or weak recall;
- hard negatives and near misses for weak precision;
- underrepresented families and cohorts;
- uncertain or low-margin documents when candidate-model acquisition is authorized; and
- random controls to detect selection bias.

The user may edit queries, filters, quotas, and sampling policy before execution. Code validates the controlled search
request and freezes the final plan.

### 14.3 Execute and retrain

Materialize a supplemental task-specific batch set, run both Q&A profiles across its managed children, apply the
already selected label policy, resolve any required ad hoc disagreement review, and publish a new training label
set. Build a new immutable model version without changing the prior version.

The model-version comparison should use a fixed human or sealed evaluation cohort when available. If the evaluation
population changes, show the results separately and do not present the metric delta as a direct improvement.

### 14.4 Stop conditions

The improvement loop ends when:

- the user accepts a candidate version;
- every active category meets the configured gate;
- the configured round or resource limit is reached and the user accepts the remaining limitations; or
- the user stops the program.

## 15. Phase 8 Classifier acceptance and whole-collection scoring

### 15.1 Acceptance

Use existing classifier lifecycle controls:

1. Review the candidate's immutable evaluation and compatibility snapshot.
2. Approve or reject it with a reason.
3. Preview activation against the current target-field schema and active model pointer.
4. Activate the approved version or retain it as an approved non-active version.

The First Pass Review Program pins the accepted version ID. A later activation change outside the program does not
redirect the program.

### 15.2 Whole-collection launch gate

Prompt the user with:

- frozen matter population count;
- feature-ready, missing, and expected exclusion counts;
- accepted model and target snapshot;
- scoring and prediction-resolution policy;
- estimated runtime and usage;
- whether results will remain isolated; and
- the later Q&A verification commitment for classifier positives.

On approval, launch a whole-matter Classification Analysis Task or scoring run. The run freezes one Analysis Input
Manifest and exact model version. Predictions remain in the immutable scoring artifact and do not update matter
coding automatically.

## 16. Phase 9 Classifier and existing Q&A comparison

After scoring completes, compare classifier predictions with all existing program Q&A results over their shared
documents. Separate:

- training documents;
- human-adjudicated documents;
- held-out or sealed documents;
- discovery-only documents; and
- documents scored but not yet Q&A reviewed.

Report for each issue:

- classifier and each Q&A model positive counts;
- all-three agreement;
- Q&A-pair agreement;
- classifier-only, model-A-only, model-B-only, and mixed disagreements;
- confidence or margin bands;
- abstentions, feature exclusions, failures, and incomplete Q&A results; and
- cohort breakdowns.

Training-overlap agreement is diagnostic and must not be labeled accuracy. Metrics against human adjudication may
use reference terminology when the source is genuinely adjudicated.

## 17. Phase 10 Q&A coverage of classifier-positive documents

### 17.1 Coverage manifest

Stream the whole-collection prediction artifact and build an immutable coverage manifest of documents that:

- have a successful, non-abstained classifier-positive issue set;
- were in the frozen scoring population; and
- do not have successful results from every required Q&A profile for the same task version and source content.

Group counts by issue, confidence band, cohort, and missing Q&A profile. Distinguish absent Q&A from failed or stale
Q&A.

### 17.2 User gate

Prompt the user with the number of documents and estimated dual-Q&A usage. The user may:

- run all required Q&A coverage;
- narrow by an explicitly previewed policy and accept incomplete coverage; or
- stop before first-pass completion.

The default program policy requires all classifier-positive documents to receive both Q&A profiles.

### 17.3 Batch-set materialization and execution

Materialize an Analysis Task Batch Set from the coverage manifest:

- preserve the frozen scoring population and prediction provenance;
- deduplicate documents already covered by successful current Q&A runs;
- keep families together when configured;
- partition to bounded child batches; and
- record an immutable allocation manifest.

Run both Q&A profiles on every child batch. Use the existing run-all parent workflow, retry only failed children, and
recompute coverage until every required document is covered or explicitly unresolved.

Classifier predictions do not appear in the Q&A prompt. The Q&A verification remains an independent application of
the Matter Definition.

## 18. Phase 11 Final coding alignment report

Once required Q&A coverage is complete, create a durable final comparison artifact covering every
classifier-positive document and every prior discovery or adjudication document.

The report includes:

- population and source coverage;
- per-issue counts and intersections;
- exact and partial set agreement;
- classifier-only and Q&A-only issue assignments;
- disagreements between the two Q&A profiles;
- disagreements involving human adjudication;
- confidence and margin bands;
- failures, missing text, stale source content, and unsupported documents;
- results by family and configured cohorts;
- training overlap and label-authority composition; and
- links to reproducible disagreement batches.

The program may offer manual review of final disagreements, but it does not silently choose classifier or Q&A values.
A final coding publication policy is a separate explicit workflow.

## 19. Phase 12 No-code QC batch set and report

### 19.1 Define the no-code population

Using the frozen whole-collection scoring population and completed program Q&A runs, select documents with:

- no classifier-selected issue under the accepted resolution policy;
- no issue selected by either successful Q&A profile; and
- no human-adjudicated positive issue.

Do not treat feature exclusions, classifier failures, Q&A failures, partial Q&A, stale content, or mandatory-review
routes as no-code negatives. Put them in a separate mandatory-resolution queue.

### 19.2 Sampling design and task-specific materialization

Create one immutable QC selection manifest with separately identified strata:

1. **Random no-code audit** with recorded sampling frame, seed, inclusion probability, and optional strata. This is
   the only lane used for population-level estimates.
2. **Near-threshold negatives** immediately below one or more issue thresholds.
3. **Uncertain or abstained documents** as a separate mandatory-review or exploratory stratum. They are excluded
   from the inferential random no-code lane unless an explicit, statistically documented policy defines them as
   ordinary scored negatives.
4. **Cohort controls** for custodians, dates, record types, languages, or sources underrepresented in training.
5. **Semantic near misses** retrieved against representative positive documents or Matter Definition criteria.

Targeted strata measure error-discovery yield. They must not be combined with the random lane as an unweighted
corpus error rate.

Use the QC selection manifest as the `INPUT_MANIFEST` source for one task-specific Analysis Task Batch Set. Its
managed children are the only Review Batches created for automated QC execution. Do not create an ordinary ad hoc QC
source batch.

### 19.3 Blind Q&A QC

Run both Q&A profiles across the same QC batch set and managed children without exposing classifier scores or
no-code selection reasons in the model prompt. Store the selection stratum and probabilities separately for
reporting.

### 19.4 QC report

Link the report directly to the QC batch set, managed child batches, and both profile runs. Include:

- batch and source coverage;
- issue positives found by either or both models;
- Q&A alignment;
- positive yield by QC stratum;
- weighted random-audit estimates and intervals when the sampling design supports them;
- near-threshold and cohort risk findings;
- failures and unresolved documents;
- representative evidence-linked documents; and
- recommended remediation.

### 19.5 QC decision

The user may:

- accept the QC result and complete first pass;
- create an ad hoc difference-review batch for QC positives or disagreements;
- launch targeted retrieval for affected issues;
- retrain and rescore the collection; or
- revise Matter Definition or Q&A guidance and reopen the program.

If QC triggers remediation, retain the original scoring and report. A new training or scoring round creates new
immutable records and a new final comparison.

## 20. Human decision gates

| Gate | Choices | Required preview |
| --- | --- | --- |
| Matter Definition ready | Accept, refine, stop | Assessment coverage, questions, limitations, revision diff |
| Q&A task publish | Publish, edit, stop | Questions, field mappings, model profiles, warnings |
| Persistent issue insufficiency | Refine, ignore issue, stop | Counts, group support, retrieval yield, classifier impact |
| Training-label policy | Union, agreement only, human adjudication | Eligibility, class feasibility, conflicts, review volume |
| Classifier review | Improve, accept, reconfigure, ignore issue, reject | Overall and per-class quality, support, limitations |
| Model activation | Activate, keep approved, reject | Compatibility, current pointer, evaluation snapshot |
| Whole-collection run | Run, revise policy, stop | Population, usage estimate, model and scoring policy |
| Q&A coverage | Run all, accept narrowed scope, stop | Missing coverage by issue, profile, and estimated usage |
| Final alignment | Continue to QC, review disagreements, stop | Coverage and source-comparison report |
| QC completion | Accept, manually review, retrieve, retrain, revise definition | QC report, sampling limitations, positives, unresolved work |

Every decision stores a reason and a content hash of the preview the user saw. If underlying state changes, the
action becomes stale and requires a new preview.

## 21. Program reports

### 21.1 Definition readiness report

- accepted revision and criteria;
- assessment coverage and batches;
- answered and open questions;
- recurring near misses and accepted limitations.

### 21.2 Discovery and support report

- retrieval yield by query, issue, and round;
- Q&A source coverage and alignment;
- support under union, agreement, and adjudication options;
- classifier feasibility and blocked classes.

### 21.3 Classifier report

- label policy and authority composition;
- dataset, feature, split, and model provenance;
- overall and per-class metrics;
- weak-category diagnoses;
- version comparisons and user acceptance.

### 21.4 Whole-collection report

- scoring coverage and selected issue counts;
- Q&A coverage of classifier positives;
- final source alignment and disagreement cohorts;
- excluded, failed, stale, and mandatory-review documents.

### 21.5 QC and completion report

- no-code sampling frame and strata;
- dual-Q&A QC findings;
- random-audit estimates and targeted yield;
- remediation history;
- final accepted limitations; and
- completion checklist and provenance graph.

## 22. API plan

### 22.1 Template catalog

```text
GET    /v1/review-workflow-templates
GET    /v1/review-workflow-templates/{template_key}
GET    /v1/review-workflow-templates/{template_key}/versions/{version}
```

Template creation and publication may remain system-administration APIs in the first release. Matter users consume
published template versions when creating programs.

### 22.2 Program configuration

```text
POST   /v1/matters/{matter_id}/review-programs
GET    /v1/matters/{matter_id}/review-programs
GET    /v1/matters/{matter_id}/review-programs/{program_id}
PATCH  /v1/matters/{matter_id}/review-programs/{program_id}
POST   /v1/matters/{matter_id}/review-programs/{program_id}/versions
POST   /v1/matters/{matter_id}/review-programs/{program_id}/versions/{version}/publish
```

The create request pins a published template version and collects only the identity and Matter Definition starting
point needed to enter the first guided phase. Later phase configuration is added through versioned stage proposals
and decisions.

### 22.3 Run lifecycle

```text
POST   /v1/matters/{matter_id}/review-programs/{program_id}/runs/preview
POST   /v1/matters/{matter_id}/review-programs/{program_id}/runs
GET    /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}
GET    /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/stages
POST   /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/retry
POST   /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/cancel
POST   /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/reconcile
```

### 22.4 Decisions and reports

```text
GET    /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/actions
POST   /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/actions/{action_id}/resolve
GET    /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/issue-support
GET    /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/reports
GET    /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/completion
```

The coordinator should call existing child-service commands directly rather than duplicating assessment, task,
batch, comparison, classifier, and scoring APIs internally.

Review Programs use the existing `/v1/notifications` inbox and mutation APIs. They add registered notification types
and safe Review Program route descriptors, not a second program-specific notification API.

## 23. Frontend plan

### 23.1 Program setup

A guided creation flow selects a published workflow template, names the program, assigns an owner, and creates,
forks, or adopts the starting Matter Definition draft. The workspace then begins in the definition phase.

Later configuration is requested just in time. After the definition is accepted, the flow guides the user through
the destination field and issue mappings, generates the required Q&A task and two execution profiles, and presents
them for approval. Retrieval, classification, coverage, and QC configuration appear only when their phases become
relevant. Template defaults keep each screen focused while preserving editable advanced policies.

### 23.2 Program dashboard

Show one stage timeline with:

- current status and blocking action;
- completed and active child jobs;
- frozen source versions;
- issue-support readiness;
- Q&A alignment;
- current classifier and weak categories;
- whole-collection and Q&A coverage;
- QC status; and
- model and document usage.

Do not hide child records. Every stage links to the existing Matter Definition, batch, task run, comparison,
classification, scoring, reviewer coding, or report view.

The program activity feed reads program events and domain history. Human attention appears through the existing
global notification bell and notification center, with deep links back to the applicable decision, assignment,
failure, or completion report. The workspace does not create a second inbox.

### 23.3 Decision surfaces

Use dedicated previews for:

- definition acceptance;
- issue exclusion;
- training-label policy;
- disagreement-batch review;
- classifier improvement or acceptance;
- whole-collection scoring;
- Q&A coverage; and
- QC acceptance or remediation.

Actions should explain consequences in domain terms, not expose raw workflow payloads.

## 24. Authorization and audit

Recommended permissions:

| Permission | Program capability |
| --- | --- |
| `matter_definition.manage` | Edit and publish Matter Definition revisions and answer assessment questions. |
| `analysis_task.manage` | Generate, compile, and publish the Q&A task. |
| `analysis_task.run` | Launch Q&A batches and batch sets. |
| `review_batch.manage` | Create task-specific batch sets and the permitted ad hoc assessment and difference-review batches. |
| `review_batch.review` | Complete manual disagreement and QC review. |
| `classification.manage` | Configure, build, approve, activate, and score classifiers. |
| `review_program.manage` | Create programs, publish program versions, and resolve template-defined gates. |
| `review_program.view` | Read program state, reports, and child links. |

The coordinator reauthorizes the acting user at every human decision and every child-job launch that changes
external state. Program creation does not grant continuing authority to perform later gated actions.

Notification audiences do not grant access. Action-required audiences use the same effective permission or explicit
assignment policy as the authoritative action endpoint, and every deep link rechecks current resource access.

Audit records cover:

- program and version publication;
- every action preview and resolution;
- issue ignore and accepted limitation decisions;
- label-policy selection;
- classifier acceptance and activation;
- whole-collection and Q&A coverage launch;
- QC acceptance or remediation; and
- final completion.

## 25. Failure, retry, and cancellation

### 25.1 Child failure

The parent projects child state but preserves child-specific retry semantics. Retriable failures offer retry. A
non-retriable failure creates an action explaining whether the user can change configuration, exclude affected
documents, accept partial coverage, or stop.

Child terminal transitions publish idempotent program events. The coordinator suppresses per-child generic job
notifications for parent-managed work and publishes one program attention notification only when failure remains
blocking after the configured retry policy.

### 25.2 Parent retry

Parent transitions are idempotent. Deterministic derivation keys prevent duplicate assessments, batches, Q&A runs,
label sets, builds, scoring runs, coverage sets, or reports after an ambiguous commit.

### 25.3 Cancellation

Cancellation requests cancellation of active child jobs through their existing APIs, prevents new children from
starting, and marks the program run canceled after child state is reconciled. It does not delete completed batches,
labels, model versions, predictions, reviewer coding, or reports.

### 25.4 Source changes

If Matter Definition, task, target schema, model profile, search generation, source content, or active issue values
change outside the program, the next transition performs compatibility checks. Material changes require a new
program version or a fresh child snapshot rather than mutating completed stages.

## 26. Testing strategy

### 26.1 Program state machine

- Multiple programs from the same template coexist in one matter without sharing mutable configuration or child
  state.
- Each program pins a template version, and template publication does not change existing programs.
- Guided stages generate only the child artifact types permitted by the pinned template contract.
- Stage launches reject `AD_HOC_BATCH`, `PARTITIONED_SET`, or `WHOLE_MATTER` scopes that differ from the pinned
  template-stage batching policy.
- Duplicate child callbacks, notification retries, and reconciliation produce one program event, one state
  transition, and at most one logical user notification.
- Valid and invalid stage transitions.
- One blocking action at a time.
- Idempotent child creation and ambiguous-commit recovery.
- Resume after every human gate.
- Retry and cancellation at every stage.
- Stale preview and authorization rejection.
- Reconciliation after missed child-completion delivery.

### 26.2 Matter Definition and Q&A setup

- Assessment and refinement loops preserve revision provenance.
- Assessment labels remain training-ineligible.
- Issue keys map to active matter values.
- Two Q&A profiles share one specification and frozen task-specific batch set.
- Profile drift and incompatible output schemas are rejected.

### 26.3 Discovery and sufficiency

- Initial, supplemental, and improvement Q&A populations materialize only as task-specific batch sets.
- `INPUT_MANIFEST` populations verify artifact identity, document count, order, and hash before partitioning.
- Managed children cover the manifest exactly and do not overlap within their set.
- Retrieval excludes previously reviewed documents.
- Family and duplicate groups follow policy.
- Per-issue quotas and query yield are reproducible.
- Support combines multiple rounds without double counting documents or groups.
- Classifier preview reason codes drive readiness.
- Loop and low-yield limits produce a user gate.
- Ignored issues remain in Matter Definition but leave the classifier class set.

### 26.4 Label policies

- Union includes every selected issue from two complete runs.
- Agreement requires equal complete issue sets.
- Missing, failed, partial, and stale runs never become empty negative labels.
- Human adjudication overrides silver labels only for the adjudicated document and target.
- Label artifacts preserve source IDs, authority, hashes, and deterministic counts.
- Preview and finalized label-set counts reconcile.

### 26.5 Classifier loop

- Dataset, feature, split, build, evaluation, approval, activation, and scoring references are exact.
- Weak-category policy is deterministic and versioned.
- Retraining creates a new version and retains prior artifacts.
- Fixed-cohort comparisons reject incompatible populations.
- Cross-fit metrics are labeled development estimates.

### 26.6 Whole-collection and Q&A coverage

- Whole-matter scoring pins one model version and manifest.
- Classifier positives use the stored resolution policy.
- Coverage recognizes only current successful Q&A results from both required profiles.
- Coverage batch sets are exact subsets of the scoring population.
- Repeated execution does not duplicate covered documents.
- Q&A prompts do not expose classifier predictions.

### 26.7 Final comparison and QC

- Matter Definition assessments and comparison-derived difference or disagreement batches remain ad hoc.
- No other first-pass Review Batch is created or executed ad hoc.
- QC selection materializes as a task-specific batch set rather than an ad hoc staging batch.
- Training overlap, held-out, and adjudicated cohorts remain distinct.
- No-code selection excludes failures, partials, stale content, and mandatory-review routes.
- Random and targeted QC strata remain separately reportable.
- Weighted random estimates use stored inclusion probabilities.
- QC positives link to exact Q&A evidence.
- Remediation creates new immutable rounds without rewriting the prior report.

### 26.8 Frontend

- Program setup and validation.
- Stage timeline and child navigation.
- Every action preview and stale-state recovery.
- Issue support and label-policy comparison.
- Classifier quality and weak-category controls.
- Q&A coverage and usage confirmation.
- QC report and remediation choices.
- Notification deep links open the exact decision, assignment, failure, or completion state.
- Resolving an action updates every recipient's notification resolution while preserving per-user read state.
- Routine child progress and completion do not create notification noise.

## 27. Delivery phases

### Phase 0 Contracts and policy fixtures

Deliverables:

- Define the reusable Review Workflow Template and immutable template-version contracts.
- Define the First Pass Review template's ordered stages, generated artifact factories, gates, and transition rules.
- Define each stage's `AD_HOC_BATCH`, `PARTITIONED_SET`, or `WHOLE_MATTER` policy and fixtures that prove the two ad
  hoc exceptions.
- Lock program, version, run, stage, action, support, label-set, and completion-report contracts.
- Define union, exact-agreement, and adjudication semantics.
- Define classifier readiness and weak-category policies.
- Create fixtures covering rare issues, model disagreement, failures, family duplicates, weak categories,
  classifier-only positives, and no-code QC discoveries.

Exit criteria:

- Two programs instantiated from the same template remain independently scoped in the same matter.
- Expected state transitions and outputs are reviewed against the fixtures.
- Every user gate has explicit choices and consequences.

### Phase 1 Coordinator foundation

Implementation status: complete on `feature/review-program-foundation`.

Deliverables:

- Add the workflow-template registry and publish the first system-owned First Pass Review template version.
- Enforce template-stage batching policy during child creation and launch.
- Add program, version, run, stage-attempt, child-link, action, and program-event models.
- Add parent-managed notification context to common child executions and register Review Program notification types
  and routes.
- Add command, query, authorization, audit, and reconcile services.
- Add basic APIs and program dashboard shell.

Exit criteria:

- A synthetic run can advance, wait, resume, retry, cancel, and reconcile without launching real child jobs; a
  duplicate event cannot advance it twice or create duplicate inbox items.

### Phase 2 Matter Definition automation

Implementation status: complete on `feature/review-program-foundation`.

Deliverables:

- Link or create Matter Definition work.
- Start assessments, project child progress, surface questions, and resume after refinement publication.
- Add definition-ready action and frozen acceptance summary.

Exit criteria:

- The program reaches Q&A setup only from an explicitly accepted published revision.

### Phase 3 Shared Q&A task and dual execution profiles

Deliverables:

- Generate a Q&A task from Matter Definition criteria and metadata mappings.
- Add reusable model execution profiles or a drift-safe interim equivalent.
- Enable `PARTITIONED_SET` as the generated Q&A task's default execution scope and add immutable `INPUT_MANIFEST`
  population support to Analysis Task Batch Sets.
- Add publication preview and gate.
- Run both profiles over one frozen task-specific test batch set.

Exit criteria:

- Both Q&A runs differ only by their pinned execution profiles and produce comparable isolated values.

### Phase 4 Discovery, alignment, and sufficiency loop

Deliverables:

- Add training-oriented retrieval planning and task-specific batch-set materialization.
- Launch dual Q&A and durable comparisons.
- Add issue-support snapshots and dataset-feasibility previews.
- Add targeted supplemental retrieval and persistent-insufficiency actions.

Exit criteria:

- Every active issue is feasible under at least one displayed label option or has an explicit unresolved action.

### Phase 5 Training-label resolution

Deliverables:

- Add option previews.
- Implement union and exact-agreement resolution.
- Create and manage ad hoc disagreement review batches.
- Materialize immutable label artifacts with row-level authority and provenance.
- Add the classifier label-source adapter.

Exit criteria:

- A classifier dataset snapshot can consume one finalized program label set and reproduce all counts and sources.

### Phase 6 Classifier training and improvement

Deliverables:

- Launch classifier builds from program label sets.
- Add deterministic quality summaries and weak-category classifications.
- Add targeted improvement plans, supplemental dual Q&A, label refresh, retraining, and model-version comparison.
- Integrate approval and activation gates.

Exit criteria:

- The user can accept one pinned classifier version with explicit per-category limitations.

### Phase 7 Whole-collection scoring and Q&A coverage

Deliverables:

- Add whole-matter scoring preview and launch.
- Build classifier-positive Q&A coverage manifests.
- Materialize and run dual-profile Analysis Task batch sets.
- Reconcile and retry incomplete coverage.

Exit criteria:

- Every required classifier-positive document is Q&A-covered or explicitly accepted as unresolved.

### Phase 8 Final alignment and no-code QC

Deliverables:

- Add the durable final comparison artifact and report.
- Add no-code population calculation, stratified QC selection manifests, and task-specific QC batch sets.
- Run blind dual-Q&A QC across managed batch-set children.
- Add linked QC reports and remediation actions.

Exit criteria:

- The user can trace every final finding to a frozen batch, source run, model version, and evidence document.

### Phase 9 Completion and hardening

Deliverables:

- Add immutable completion reports and completion gate.
- Add operational metrics, alerts, orphan reconciliation, artifact retention, and legal-hold behavior.
- Run scale, concurrency, failure-injection, authorization, and cost tests.
- Pilot in non-publishing mode before enabling downstream coding publication integrations.

Exit criteria:

- Program completion is reproducible, auditable, scope-safe, and operationally supportable at the target matter
  size.

## 28. Operational metrics

Track:

- time spent running versus waiting for user action;
- assessment rounds and unresolved questions;
- retrieval yield by issue, query, and round;
- documents and families reviewed by both Q&A profiles;
- Q&A exact, partial, disagreement, incomplete, and failure rates;
- per-issue support under each label option;
- manual disagreement-review volume and completion rate;
- classifier build and scoring duration;
- per-category support, quality status, and version change;
- whole-collection feature and scoring coverage;
- classifier-positive Q&A coverage by profile and issue;
- final three-source agreement and disagreement;
- no-code QC random and targeted yield;
- unresolved and mandatory-review counts;
- tokens, cached tokens, requests, latency, and estimated cost by stage and model;
- retries, cancellations, stale actions, and reconciliation repairs;
- program-event publication-to-processing latency, retry depth, and failed-event age;
- action-required notification age, empty-audience failures, and idempotent duplicates avoided; and
- parent-managed child notifications suppressed versus program-level notifications published.

## 29. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Template logic becomes hard-coded into one first-pass service | Keep phases, artifact factories, gates, transitions, and presentation metadata behind a versioned template contract and registry. |
| A future program silently chooses incompatible batching | Require every template stage to declare one supported execution scope and validate it at child creation and launch. |
| Workflow task runs fall back to overlapping ad hoc batches | Permit ad hoc creation only for the named assessment and difference-review stage kinds; require task-specific sets elsewhere. |
| Arbitrary derived populations require ad hoc staging batches | Add a verified immutable `INPUT_MANIFEST` source to Analysis Task Batch Sets. |
| Notification rows become workflow messages | Drive transitions from durable program events and authoritative child state; treat notifications only as human-facing projections. |
| Parent-managed work floods the initiator with child job notifications | Mark child executions parent-managed and publish only program-level attention or completion notifications. |
| An action notification reaches the wrong person | Resolve audiences from the action endpoint's authorization or explicit assignment policy and reauthorize on open and decision. |
| Two programs contaminate each other's data | Scope every child link, artifact query, decision, and completion check by program ID and version. |
| Overlapping destination fields create publication conflicts | Keep analysis outputs isolated and require a separate source-aware publication preview. |
| The program becomes one brittle mega-workflow | Use a domain state machine with short idempotent transitions and existing child jobs. |
| Two Q&A tasks drift | Share one Decision Specification and validate profile-only differences. |
| Assessment data leaks into training | Keep ad hoc assessment runs blinded and require a distinct training-eligible discovery batch set. |
| Raw counts overstate readiness | Use actual group-aware dataset and split feasibility. |
| Union labels increase false positives | Show option-specific support and quality tradeoffs before selection. |
| Agreement labels encode shared model errors | Treat agreement as silver consistency and use human/QC gates for quality evidence. |
| Missing output becomes a negative label | Exclude incomplete and failed sources explicitly. |
| Retrieval loops never converge | Freeze round, budget, and low-yield limits and require a user decision at the boundary. |
| Rare categories destabilize training | Show class exclusions, allow targeted retrieval, or require explicit ignore decisions. |
| Adaptive retraining inflates quality claims | Use fixed or sealed evaluation cohorts and label cross-fit results as development metrics. |
| Classifier positives become authoritative automatically | Keep scoring isolated and require separate resolution or publication. |
| Q&A coverage becomes prohibitively expensive | Preview document and usage counts before launch and partition through batch sets. |
| Final comparison is called accuracy without a reference | Use agreement language unless a genuinely adjudicated source is selected. |
| No-code QC yields biased corpus estimates | Separate probability-based random audit from targeted strata and use design weights. |
| QC discoveries rewrite history | Preserve prior runs and create new immutable remediation rounds. |
| External configuration changes invalidate previews | Bind decisions to hashes and row versions and reject stale actions. |

## 30. MVP completion criteria

The MVP is complete when:

- the system publishes a reusable First Pass Review workflow-template version;
- one matter can contain multiple independent first-pass programs with different Matter Definitions, guidance, and
  destination fields;
- creating a program requires only a template, identity, owner, and Matter Definition starting point;
- the guided flow creates and presents its Q&A tasks, batches, comparisons, classifier records, and reports as their
  phases are reached;
- child terminal transitions advance the program through idempotent program events rather than notification rows;
- human decisions, blocking attention, assignments, and completion use the shared typed notification platform with
  safe Review Program deep links;
- program-managed child jobs do not emit one generic completion notification per child;
- every automated Q&A Review Batch belongs to a task-specific `PARTITIONED_SET`;
- only assessment and difference or disagreement review batches use `AD_HOC_BATCH`;
- a published program version can coordinate one accepted Matter Definition revision;
- one generated Q&A specification runs through two pinned model profiles on identical frozen task-specific batch
  sets;
- the program measures per-issue alignment and actual classifier feasibility across multiple discovery rounds;
- targeted retrieval can supplement deficient issues without re-reviewing existing documents;
- persistent insufficiency requires an explicit refine, ignore, or stop decision;
- the user can preview and choose union, exact agreement, or human-adjudicated label policy;
- the resulting label set is immutable, artifact-backed, and consumable by classifier dataset creation;
- classifier builds produce per-category quality summaries and a bounded improvement loop;
- one accepted model version can score a frozen whole-matter population;
- classifier-positive documents lacking Q&A coverage can be materialized into batch sets and run through both Q&A
  profiles;
- the program produces a final multi-source alignment report without mislabeling agreement as accuracy;
- a no-code QC task-specific batch set uses separately identified random and targeted strata;
- both Q&A profiles run blindly across the QC set's managed children and produce an evidence-linked QC report;
- QC acceptance or remediation is a durable user decision;
- completion produces an immutable provenance-complete report; and
- retry, cancellation, authorization, idempotency, stale-preview, artifact-integrity, and scale tests pass.

## 31. Explicit non-goals for the first implementation

- Automatically editing or publishing the Matter Definition.
- Automatically approving metadata values or Q&A task versions.
- Treating either Q&A model as human truth.
- Training directly from diagnostic assessment results.
- Unbounded autonomous retrieval or retraining loops.
- Automatically ignoring weak or rare issue categories.
- Publishing Q&A or classifier values to matter metadata as a side effect of program completion.
- Replacing existing Matter Definition, Analysis Task, review batch, reviewer coding, or classifier workspaces.
- Storing one program-owned PostgreSQL row for every document prediction or comparison.
- Allowing classifier predictions to influence blind Q&A prompts.
- Calling targeted QC yield a collection-wide error rate.
- Starting, restarting, or managing workflow worker processes.

## 32. Expected implementation locations

| Area | Expected location |
| --- | --- |
| Workflow-template and program models | `app/models.py`, Alembic migration |
| Template registry and stage contracts | new `app/review_workflow_templates.py` |
| Program contracts | new `app/review_program_contracts.py` and `app/first_pass_contracts.py` plus `app/schemas.py` reads |
| Generic coordinator and commands | new `app/review_programs.py` |
| First-pass stage handlers | new `app/first_pass_programs.py` |
| Program event adapter | new `app/review_program_events.py` plus common job transition integration |
| Notification integration | `app/notifications.py`, common job execution notification policy, and Review Program route registry |
| Job adapter and dispatch | `app/job_executions.py`, workflow dispatch module |
| Matter Definition integration | existing assessment and refinement services |
| Q&A profile support | `app/analysis_tasks.py`, batch and playground execution, workflow snapshots |
| Retrieval planning | new managed skill and code-owned planner under first-pass services |
| Comparisons | existing batch comparison plus durable artifact implementation |
| Label resolution | new `app/first_pass_labels.py` and classification label-source adapter |
| Classifier lifecycle | existing classification dataset, build, lifecycle, and scoring services |
| Batch sets | existing Analysis Task batch-set planner and run-all workflow plus `INPUT_MANIFEST` population source |
| QC sampling and reports | new first-pass QC service and immutable report artifacts |
| APIs | new `app/routers/review_workflow_templates.py` and `app/routers/review_programs.py` |
| Frontend | reusable Review Programs workspace with First Pass Review template presentation |
| Tests | program state, integration, artifact, classification, batch, authorization, and frontend suites |

## 33. Remaining product decisions

Resolve these during Phase 0:

1. Whether the first release supports only system-owned workflow templates or also tenant-authored templates.
2. The two initial Q&A provider and model profiles and whether tenants may replace either profile.
3. The default maximum assessment, discovery, and retraining rounds and resource budgets.
4. Per-category training support and model-quality thresholds.
5. Whether exact-agreement-only is the default training option or whether the program always requires an explicit
   choice.
6. Whether all Q&A disagreements require manual review under Option C or whether a confidence-bounded subset is
   allowed.
7. The minimum human-adjudicated or sealed-evaluation evidence required before classifier activation.
8. The classifier-positive threshold and abstention policy used to define Q&A coverage.
9. Whether Q&A coverage of classifier positives always requires both profiles or may be configured to require one.
10. The no-code random-audit confidence target, strata, and maximum QC budget.
11. Whether finding any random-audit positive blocks completion automatically or whether a measured tolerance is
    allowed.
12. The separate policy and workflow, if any, for resolving and publishing final first-pass coding into matter
    metadata.
