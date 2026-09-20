# Matter Definition assessment workflow plan

## Objective

Build a durable workflow that evaluates how well an immutable Matter Definition fits the documents in a
matter. The workflow will:

1. Read a pinned Matter Definition revision and generate controlled keyword, semantic, or hybrid searches.
2. Execute those searches against one pinned physical search-index generation.
3. Materialize a frozen diagnostic review batch from the selected documents.
4. Analyze every document under the pinned Matter Definition and store a structured `SUMMARY` artifact with
   source-linked citations.
5. Build a batch-scoped topic taxonomy and compare the observed document patterns with the instructions.
6. Produce consolidated, evidence-backed clarification questions for a supervising reviewer.

The workflow supports direct launch from the UI and launch proposed by the existing Matter Definition Setup
agent. It must not change or publish a Matter Definition automatically.

## Product model

### Agent

An agent is an interactive, user-facing reasoning component. An agent has:

- a system prompt;
- an allowlist of tools;
- an allowlist of available managed skills;
- conversation history;
- approval-aware tool execution.

The existing Matter Definition Setup agent remains an agent because it converses with a user, chooses tools,
requests approval for mutations, and may propose starting an assessment.

No new autonomous agents are required for this workflow.

### Managed skill

A managed skill is a bounded, versioned model task. A skill has:

- stable instructions;
- an input contract;
- a structured output schema;
- default model policy and limits;
- required capabilities and required tools, if any;
- prompt-cache boundary hints;
- evaluation fixtures;
- `DRAFT`, `PUBLISHED`, or `RETIRED` status.

Skills do not launch themselves, maintain conversations, grant tools, or create arbitrary workflows. When an
agent uses a skill, the selected skill version contributes a separate instruction layer to the assembled model
request. A workflow may also execute a managed skill directly without launching an agent.

The assessment requires three managed skills:

| Workflow role | Managed skill key | Purpose |
| --- | --- | --- |
| `retrieval_planner` | `matter_definition_retrieval_plan` | Convert the Matter Definition into a controlled query and sampling plan. |
| `document_analysis` | `matter_definition_document_analysis` | Produce a cited factual summary, responsiveness analysis, and clarification candidates for one document. |
| `assessment_synthesis` | `matter_definition_assessment_synthesis` | Aggregate document results into a taxonomy, fit report, and consolidated questions. |

### Code-owned workflow

`matter_definition_assessment_v1` is a code-owned DBOS workflow. Code owns:

- step order and fan-out;
- input and output contracts;
- authorization and tenant boundaries;
- retry, cancellation, and partial-failure behavior;
- artifact creation rules;
- the tools and capabilities available to each step.

The executable graph is not stored as administrator-authored data. Arbitrary database-defined graphs and a
visual workflow designer are out of scope.

### Direction of control

The supported direction is:

```text
User or interactive agent
        ↓
Authorized Core command
        ↓
Code-owned durable workflow
        ↓
Managed skills
        ↓
Model invocations
```

An agent may propose starting the workflow through an approval-required tool. The tool executes the same Core
command used by the UI. The workflow does not launch an interactive agent. After completion, the interactive
agent may read the results in a later turn and propose a Matter Definition revision through the existing
approval boundary.

## Prompt assembly

Prompt layers must remain distinct and be assembled deterministically:

```text
1. Platform security instructions
2. Agent instructions, when an interactive agent selected the skill
3. Selected managed-skill instructions
4. Stable workflow context, including the pinned Matter Definition
5. Dynamic document or task input
```

Skill instructions must not override platform security instructions. Required skill tools must be a subset of
the tools allowed by the initiating agent or workflow. The runtime rejects incompatible bindings before
execution.

Only the selected skill is added to a request. Assigning a skill to an agent makes it available; it does not
append every available skill to every prompt.

## Workflow specifications and bindings

Add a code-owned `WORKFLOW_SPECS` registry. The assessment specification declares:

- workflow key and code version;
- required role keys;
- required skill input and output schema identifiers;
- allowed capabilities and maximum tool permissions per role;
- compatible workflow configuration schema.

Add database-managed `WorkflowSkillBinding` records with:

- `workflow_key`;
- `role_key`;
- `scope` (`SYSTEM` or `TENANT`);
- optional `owner_tenant_id`;
- `skill_definition_id`;
- `skill_definition_version_id`;
- `status`.

Agent Management will show which workflows use each skill and permit authorized administrators to change
bindings for future runs. A run resolves the tenant-specific binding first and falls back to the active system
binding. Missing, unpublished, retired, schema-incompatible, or over-permissioned bindings prevent launch.

Each workflow run snapshots the resolved workflow specification, code version, skill versions, prompts, output
schemas, model policies, limits, capability requirements, and tool permissions. Later binding changes cannot
alter an existing run.

## Execution records

Use execution terminology that reflects the actual runtime:

```text
WorkflowRun
├── WorkflowStepRun: retrieval
│   └── SkillRun: retrieval planning
├── WorkflowStepRun: document analysis
│   ├── SkillRun: document 1
│   ├── SkillRun: document 2
│   └── SkillRun: document N
└── WorkflowStepRun: synthesis
    └── SkillRun: assessment synthesis
```

A `SkillRun` is one logical application of a managed skill. A long-document analysis may use several provider
calls, but those calls remain `ModelInvocation` children of one document `SkillRun`; they are not subagents.

Record the following levels:

### `WorkflowRun`

- workflow key and code version;
- matter, initiating user, status, and workflow ID;
- pinned inputs and complete binding snapshot;
- progress counters, errors, and timestamps.

### `WorkflowStepRun`

- workflow run and role key;
- ordinal and optional fan-out group;
- status, progress, error, and timestamps.

### `SkillRun`

- workflow and step run IDs;
- skill definition version;
- optional parent and root skill-run IDs;
- scope type and scope ID, such as a matter document;
- input and configuration hashes;
- output artifact ID;
- status, error, and timestamps.

### `ModelInvocation`

- exactly one execution owner: an interactive `AgentRun` or a managed `SkillRun`;
- provider request ID;
- provider, model, and model-configuration hash;
- attempt and request sequence;
- request, input, output, cached-input, and cache-write token counts;
- latency, status, and error;
- timestamps.

Interactive agent conversations may reference workflow and skill runs, but batch skill runs must not require a
synthetic `AgentConversation` or `AgentTurn`.

`ModelInvocation` is the canonical per-call execution record. Existing `AgentRun` counters and the corresponding
`SkillRun`, step, and workflow counters are denormalized aggregates derived from invocation rows. Every billable
invocation must also be represented idempotently in `ExternalProviderUsage`, which remains the immutable billing
ledger. Reconciliation tests must prove that invocation totals equal their execution aggregates and billable
provider-usage totals. Cached-input and cache-write tokens must be available consistently in execution telemetry
and billing reporting rather than creating a workflow-only accounting path.

## Assessment domain record

Add a `MatterDefinitionAssessmentRun` Core record with:

- matter and initiating user;
- pinned `MatterDefinitionRevision` and content hash;
- workflow run ID;
- pinned physical `SearchIndexGeneration`;
- resulting review-batch ID;
- configuration and binding snapshot;
- requested maximum document count, with a default of 500;
- any large-run warning acknowledgment, including the acknowledging user and time;
- estimated analysis input and output tokens, estimator method and version, and the model used for estimation;
- candidate, selected, summarized, skipped, and failed counts;
- status, error, and timestamps.

Recommended statuses:

```text
QUEUED
PLANNING
RETRIEVING
BUILDING_BATCH
SUMMARIZING
SYNTHESIZING
COMPLETED
COMPLETED_WITH_ERRORS
FAILED
CANCELED
```

Add child records:

- `MatterDefinitionAssessmentQuery` for the controlled search request, criterion, rationale, quota, and result
  count;
- `MatterDefinitionAssessmentCandidate` for document selection provenance, per-query ranks, scores, retrieval
  reasons, and selected state;
- `MatterDefinitionAssessmentQuestion` for a consolidated question, rationale, priority, evidence documents,
  answer, and resolution state.

Any immutable Matter Definition revision may be assessed. The UI defaults to the current revision and clearly
labels whether it is published. This permits evaluation of a draft before publication.

## Launch paths

### Direct UI launch

`POST /v1/matters/{matter_id}/definition-assessments` calls a shared Core command that:

1. reauthorizes the initiating matter administrator;
2. validates and pins the selected Matter Definition revision;
3. validates the active search generation;
4. resolves and validates workflow skill bindings;
5. creates the assessment and workflow records;
6. enqueues the DBOS workflow in the same transaction.

### Agent-proposed launch

Add an approval-required tool such as `matter_definition.start_assessment`. Approval does not bypass
authorization. At execution time the tool reauthorizes the approving user and calls the same Core command as
the UI.

## Assessment size and initial usage estimate

The requested maximum document count defaults to 500. A server-configured large-run warning threshold defaults
to 1,000 documents. A request above the warning threshold is not rejected: the UI or initiating agent must show
the requested count and require the user to acknowledge the warning explicitly before the shared Core command
starts the workflow. The acknowledgment is included in the immutable configuration snapshot. The initial
version does not impose a hard document-count, token, or currency ceiling beyond ordinary API and data-integrity
limits.

Estimate analysis usage after candidate selection from the exact source text that the document-analysis skill
will receive. Count the stable prompt prefix, Matter Definition, document text, map/reduce overhead for long
documents, and expected structured output. Use the tokenizer for the selected analysis model when one is
available. For OpenAI-compatible tokenization, make `tiktoken` a direct dependency rather than relying on its
current transitive installation. For a model without a local tokenizer, use a versioned conservative
character-based estimator and label the result as approximate.

Embedding provider usage is currently recorded at embedding batch and job scope, not per document. The stored
chunk artifacts provide per-document text and character spans but no token count. Embedding token totals may be
used to calibrate a coarse matter-level fallback, but they are not the primary estimate because embedding and
analysis models may tokenize the same text differently and embedding requests may combine several documents.

Persist the estimate, estimator version, model, selected-document count, and source-text hashes so the estimate
can be explained and compared with actual `ModelInvocation` usage. Display the estimate once the assessment
batch has been materialized; it is informational in the initial version and does not pause or reject the run.

Future cost-control work may add tenant-specific token or currency budgets, price-aware estimates, preflight
approval thresholds, budget reservation, and enforcement during fan-out. Those controls are deliberately out
of scope for the first assessment workflow.

## Phase 1: retrieval planning

The retrieval-planning skill reads the complete pinned Matter Definition and returns a structured plan. The
plan contains:

- stable criterion keys and exact display labels;
- relevant Matter Definition excerpts;
- keyword, semantic, or hybrid `MatterSearchRequest` values;
- filters, result quotas, and rationale;
- sampling guidance for high-confidence, borderline, disagreement, and control documents.

The skill may return only the application's controlled search request shape. It may never return or execute raw
OpenSearch DSL.

Execute every query against the same pinned physical search index, not the mutable alias. Record query text,
mode, filters, rank, score, best passage, and physical-generation provenance.

Merge candidates deterministically:

- apply minimum per-criterion quotas;
- combine incomparable keyword and semantic scores through deterministic rank fusion rather than raw-score
  comparison;
- deduplicate by `matter_document_id` while retaining all retrieval reasons;
- include configurable borderline and keyword/semantic-disagreement candidates;
- include a configurable random control sample from outside the retrieved population;
- stop at the configured maximum document count.

## Phase 2: frozen review batch

Extend `ReviewBatch.selection_type` with `DEFINITION_ASSESSMENT`. Its immutable selection definition snapshots:

- assessment run ID;
- pinned Matter Definition revision and hash;
- physical search generation;
- complete query plan;
- merge and sampling algorithm version;
- selected candidate provenance.

Materialize the selected document IDs into `ReviewBatchDocument` using their deterministic selection order.
The batch remains a permanent Core snapshot. PostgreSQL remains authoritative and the existing `batch_ids`
search projection is reused.

The Core model check constraint and API `ReviewBatchSelectionType` must both add `DEFINITION_ASSESSMENT`.

## Phase 3: document analysis

Create one domain-visible `ReviewBatchRun` with `run_type = WORKFLOW`, `purpose = ASSESSMENT`, and
`result_policy = ISOLATED`. The run references its `WorkflowRun`; it has no actor user or agent-definition
version, while `initiated_by_user_id` continues to identify the user who launched or approved the assessment.
The review-run actor check must enforce exactly one valid `HUMAN`, `AGENT`, or `WORKFLOW` ownership branch.

Execute the documents as managed `SkillRun` records rather than interactive agent runs. Add `QUEUED` and
`FAILED` document-run states and `COMPLETED_WITH_ERRORS` where a domain-visible run needs to expose partial
failure. Plan bounded DBOS child groups and process documents concurrently subject to provider limits.

### Text source and paragraph map

Generalize the existing preferred text selection used for embeddings. Use the active normalized text when
available, then faithful extracted text, OCR text, or supported native text.

Before inference:

- segment the exact source into deterministic paragraph identifiers such as `¶1`;
- preserve source artifact ID and hash;
- preserve character offsets and viewer locations for every paragraph;
- version the paragraph-segmentation algorithm;
- place identifiers into the model-visible document text.

For long documents, analyze all chunks in bounded map calls and merge them into one document result. Record
coverage and any truncation explicitly. Never silently analyze only the first configured byte range.

### Cache-aware request layout

Every document in one analysis run uses the same stable prefix:

```text
Platform security instructions        ┐
Document Analysis skill version       │
Pinned Matter Definition revision     ├─ stable cacheable prefix
Structured output schema              │
Stable model configuration            ┘
──────────────── cache boundary ─────────────────
Document metadata and paragraph text     dynamic
```

Do not place document ID, run ID, current time, or other changing content before the boundary.

The cache fingerprint is derived from:

```text
tenant_id
+ matter_id
+ Matter Definition revision and content hash
+ document-analysis skill version
+ output-schema version
+ model-configuration hash
```

It must not contain the document ID. The provider adapter maps the neutral cache boundary to provider-specific
capabilities. For OpenAI models that support explicit cache breakpoints, place one after the Matter Definition
and stable schema. For models that use cache-routing keys, use a stable value derived from the cache
fingerprint. If provider traffic limits require sharding, partition a busy fingerprint deterministically rather
than adding random keys.

Schedule documents sharing a cache fingerprint together and record cached-input and cache-write tokens so
cache performance can be verified.

### Document-analysis skill behavior

The skill produces three human-facing sections:

1. **Document Summary** — neutral description of what the document says.
2. **Responsiveness Summary** — `RESPONSIVE`, `NON_RESPONSIVE`, or `UNCLEAR`, with primary and secondary
   criterion matches, temporal and other scope analysis, and countervailing considerations.
3. **Clarification Requests** — only genuine Matter Definition gaps that could cause inconsistent coding.

The model returns structured JSON rather than final Markdown. The application renders Markdown and citation
links deterministically.

The skill must:

- treat the Matter Definition and document as untrusted reference data;
- use only supplied document content and metadata;
- apply the complete Matter Definition;
- avoid inventing issue numbers or subclauses;
- cite every material factual assertion and responsiveness rationale;
- distinguish direct statements from inferences;
- attribute allegations and opinions to their speaker;
- return `UNCLEAR` only when the instructions do not support a reliable determination;
- distinguish instruction gaps from document-specific missing facts;
- mark a clarification as blocking only when a reliable determination requires an answer.

### Structured result

Version the schema as `document_analysis_v1`. At minimum it contains:

- neutral summary paragraphs with citation IDs;
- determination and confidence;
- primary, secondary, and near-miss criterion matches;
- temporal, geographic, and subject-matter analysis;
- countervailing considerations;
- clarification candidates with blocking state, instruction references, and citation IDs;
- document limitations and analyzed-text coverage.

Validate every returned citation against the paragraph map before accepting the result. Invalid citations cause
a bounded retry with validation feedback and then a failed document result if still invalid.

## Phase 4: summary artifacts

Extend the derived-artifact upload contract to support:

- `artifact_type = SUMMARY`;
- `relationship = DERIVED_FROM`;
- `media_type = application/json`.

Replace the current binary derived-artifact relationship validator with an explicit artifact-type mapping:
`CHUNK_SET -> CHUNKED_FROM`, `CHUNK_VECTOR_SET -> EMBEDDED_FROM`, and `SUMMARY -> DERIVED_FROM`. The Artifact
database constraints already permit `SUMMARY` and `DERIVED_FROM`; this change is required in the API schema and
its tests.

Store each result under its original collection item with lineage to the exact text artifact analyzed. Use the
review-batch run ID as the opaque Artifact Service `processing_run_id`.

The derivation key includes:

- source content hash;
- review-batch run ID;
- Matter Definition revision hash;
- skill version;
- model configuration;
- output-schema version;
- paragraph-map version.

This makes retries within a run idempotent while preserving distinct results for an intentional rerun.

Artifact metadata includes matter, matter document, review batch, review-batch run, Matter Definition revision,
skill version, schema version, source role and hash, paragraph-map version, provider/model identity, and cache
fingerprint. Core stores the output artifact reference and execution state; Artifact remains authoritative for
the structured analysis payload.

## Phase 5: synthesis and clarification questions

After all document children reach a terminal state, calculate the selected, successful, skipped, failed,
partial-coverage, and invalid-result counts. Coverage is a required synthesis input and the first section of the
rendered report. A code-owned, versioned policy supplies the minimum successful-document count and coverage
ratio. Below that threshold the workflow produces an explicit insufficient-coverage result without substantive
fit conclusions; above it, partial failures produce `COMPLETED_WITH_ERRORS` and a prominent limitation.

The synthesis skill reads the coverage envelope and validated structured results and selectively reopens source
evidence when required. It returns:

- a batch-level narrative;
- recurring document subjects;
- instructions clearly represented in the batch;
- recurring near-misses and exclusions;
- instructions that appear ambiguous, conflicting, too broad, or too narrow;
- observed subjects not clearly addressed by the Matter Definition;
- consolidated clarification questions with counts and representative documents.

Do not interrupt the user for every document-level clarification candidate. Deduplicate and prioritize across
the batch. Each final question links to representative documents and exact evidence paragraphs.

User answers are stored on assessment-question records. They may be supplied to the existing Matter Definition
Setup agent in a later conversation, but they do not mutate the Matter Definition automatically.

## Phase 6: batch-scoped topic taxonomy

Add versioned Core records:

- `BatchTopicTaxonomy`;
- `BatchTopic`;
- `BatchTopicAssignment`.

Each taxonomy belongs to one review batch and source synthesis run. Only one taxonomy version is active for a
batch. Topic assignments include batch, taxonomy, document, topic, confidence, and evidence references.
Re-running synthesis creates a new version and preserves prior versions.

Do not publish batch topics into a matter-wide metadata enum. The same document may belong to several batches
and have a different topic assignment in each.

This system coexists with the existing `MatterTopicJob` pipeline. Matter topics are approved, matter-wide
metadata values visible everywhere in the matter; assessment batch topics are diagnostic labels visible only in
the selected review-batch taxonomy. A document may have both, and the UI labels and filters them separately.
The existing `MatterTopicBatch` is a processing shard rather than a review batch; use
`MatterTopicProcessingBatch` in new code and documentation where compatibility permits.

Project batch topics into OpenSearch as nested, batch-qualified records:

```json
{
  "batch_topics": [
    {
      "batch_id": "...",
      "taxonomy_id": "...",
      "topic_key": "..."
    }
  ]
}
```

Batch-topic filtering and aggregation must constrain `batch_id`, `taxonomy_id`, and `topic_key` within the same
nested object so an overlapping document cannot leak another batch's topic values.

## API surface

Assessment endpoints:

```text
POST /v1/matters/{matter_id}/definition-assessments
GET  /v1/matters/{matter_id}/definition-assessments
GET  /v1/matters/{matter_id}/definition-assessments/{assessment_id}
POST /v1/matters/{matter_id}/definition-assessments/{assessment_id}/cancel
GET  /v1/matters/{matter_id}/definition-assessments/{assessment_id}/queries
GET  /v1/matters/{matter_id}/definition-assessments/{assessment_id}/questions
PUT  /v1/matters/{matter_id}/definition-assessments/{assessment_id}/questions/{question_id}
```

Document-analysis endpoint:

```text
GET /v1/matters/{matter_id}/review-batches/{batch_id}/runs/{run_id}/documents/{document_id}/analysis
```

Skill and workflow administration endpoints will manage skill definitions, skill versions, publication, and
workflow-role bindings. They will not permit arbitrary workflow graph creation.

## UI

### Matter Definition

- Add **Assess against corpus**.
- Select an immutable definition revision, maximum document count, and control-sample size; default the maximum
  document count to 500.
- Warn when the requested maximum exceeds 1,000 documents and require an explicit **Continue with N documents**
  acknowledgment without preventing the user from proceeding.
- Show whether the selected revision is draft or published.
- Show progress through planning, retrieval, batch construction, summarization, and synthesis.
- Show the estimated analysis tokens, estimator method, and selected-document count after batch materialization.
- Show the generated query plan and retrieval reasons.
- Link to the generated batch and final questions.

### Batch review

- Show the rendered Document Summary, Responsiveness Summary, and Clarification Requests beside the source
  document.
- Resolve citation links to exact source paragraphs.
- Show analysis status and explicit missing-text, partial-coverage, or failure states.
- Load only the active taxonomy for the selected batch.
- Filter and facet using batch-qualified topic assignments.

### Agent and skill management

- Separate **Agents**, **Skills**, and code-owned **Workflow bindings**.
- Show skill versions, schemas, model policies, cache configuration, evaluations, and publication state.
- Show which agents and workflows may use a skill.
- Show workflow execution trees, artifacts, failures, token usage, and cache metrics.
- Binding changes apply only to future workflow runs.

## Authorization and safety

- Matter `ADMIN` is required to launch or cancel an assessment and answer assessment questions.
- An agent-proposed launch requires explicit approval and execution-time reauthorization.
- Workflow children inherit the initiating user, tenant, client, and matter scope; they cannot expand it.
- Skill requirements never grant tools implicitly.
- Documents and Matter Definitions are treated as untrusted model input.
- Outputs remain isolated from matter metadata until a separate, explicit publish operation is designed.
- Artifact reads and summary access use the original tenant and client boundaries.
- Cancellation stops future work without deleting completed immutable artifacts.

## Delivery sequence

Implementation status as of September 20, 2026: delivery items 1 through 9 are complete. The assessment launch
pins the Matter Definition revision, physical search generation, and managed-skill bindings; controlled search
results are merged deterministically into a permanent `DEFINITION_ASSESSMENT` batch; and bounded DBOS document
children produce one accounted `SkillRun` and one cited JSON `SUMMARY` artifact per successful document. Long
documents use the versioned map/reduce evidence plan without silently truncating source text. Synthesis,
consolidated questions, and batch taxonomy versioning begin with item 10.

1. Add managed skill definitions, versions, publication, and administration.
2. Add the code-owned workflow registry and database-managed workflow-skill bindings.
3. Define generic structured-model request/result contracts, deterministic instruction assembly, model and
   cache adapters, usage-limit enforcement, structured-output validation, telemetry hooks, and an error
   taxonomy; then extract the executor from the interactive agent runtime without changing agent behavior.
4. Add workflow, step, skill-run, and model-invocation records with provider and cache telemetry, connect both
   interactive agent and managed-skill calls, and reconcile them with `ExternalProviderUsage`.
5. Add deterministic prompt assembly, output-schema validation, and cache-boundary adapters.
6. Add paragraph segmentation, citation validation, and deterministic Markdown rendering.
7. Add assessment domain models, APIs, and the retrieval-planning skill.
8. Add controlled query execution, deterministic candidate merging, and assessment-batch materialization.
9. Add durable document-analysis fan-out and `SUMMARY` artifact persistence.
10. Add synthesis, consolidated clarification questions, and batch taxonomy versioning.
11. Add batch-topic search projection and scoped filtering/faceting.
12. Add the Matter Definition, batch-review, skill-management, and workflow-observability UI.

## Acceptance tests

### Prompt and skill execution

- The stable prompt prefix is byte-identical for documents sharing a cache fingerprint.
- Document-specific values occur only after the cache boundary.
- The selected skill version and output schema are immutable within a run.
- A skill cannot use a tool not allowed by the workflow or initiating agent.
- Invalid structured output and invalid citation IDs are rejected and retried within limits.
- Provider cache-read and cache-write tokens are recorded idempotently.
- Every invocation has exactly one agent-run or skill-run owner.
- Invocation totals reconcile with execution aggregates and the immutable provider-usage ledger.

### Retrieval and batches

- Omitting the maximum document count uses 500.
- A maximum above the server warning threshold requires a recorded user acknowledgment but is not rejected.
- Every query uses the pinned physical search generation.
- The planner cannot submit raw OpenSearch DSL.
- Candidate merging is deterministic and honors per-criterion quotas.
- A document matched by several queries appears once with all retrieval reasons.
- The batch membership and provenance remain unchanged after search-index replacement.
- The usage estimate is reproducible from the estimator version, model, source hashes, and workflow configuration.

### Document analysis

- Every accepted factual assertion and criterion rationale has valid source citations.
- Brief responsive mentions are not lost in long documents.
- Long-document map/reduce reports complete or explicitly partial coverage.
- Missing usable text produces an explicit skipped state.
- A child failure does not discard successful document artifacts.
- Retrying a completed child reuses its artifact.
- Hostile document content cannot alter instructions or invoke tools.
- Synthesis always displays selected, successful, skipped, failed, and partial-coverage counts.
- A run below the configured coverage threshold emits no substantive fit conclusions.

### Taxonomy and questions

- Consolidated questions preserve representative document and paragraph evidence.
- Questions already resolved by the Matter Definition are excluded.
- A non-blocking clarification may accompany a confident determination.
- Two overlapping batches with different taxonomies never expose each other's values.
- A full search-index rebuild reproduces authoritative batch-topic assignments.

### Workflow behavior

- UI and agent-approved launch paths call the same Core command.
- Approval does not bypass execution-time authorization.
- Binding changes affect only future runs.
- Cancellation cascades to pending children and retains completed artifacts.
- Provider usage and cache metrics aggregate correctly from invocation to skill, step, and workflow.
- The example `examples/Case Background.md` produces eight recognizable issue criteria under a deterministic
  test model without relying on exact production-model wording.

## Explicit non-goals

- Arbitrary administrator-authored workflow graphs.
- A visual workflow designer.
- Automatic publication of Matter Definition revisions.
- Automatic publication of assessment conclusions into matter-wide coding.
- Treating batch topics as a matter-wide taxonomy.
- Treating each document or provider call as an autonomous subagent.
