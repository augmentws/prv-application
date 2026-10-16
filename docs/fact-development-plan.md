# Fact Development and Timeline Plan

Status: slices 1 and 2 (task type, entities, fact ledger, permissions, API, CSV export) implemented on `feature/fact-backend`; slice 3 (Postgres search, facets, histogram, recall) implemented; slice 4 (single-pass extraction runs on `matter_analysis_task_run`, frozen in an analysis input manifest, job outcomes) implemented; slice 5 (ordered chunk window, duplicate consolidation, confirmed-fact suggestions, final duplicate sweep with merge suggestions, launch estimate) implemented; slice 9 backend (`fact_state` as a derived SYSTEM metadata field with backfill and change-only upserts, the per-document facts endpoint) and the pending-suggestion filter implemented; slice 6 (matter tabs and detail pages) next

## Objective

Add an agentic second-pass review that reads documents already coded responsive, together with their issue coding,
and incrementally develops **Facts**: dated, sourced statements that support one or more issues. The agent builds
context as it reviews: each document is evaluated against the facts developed so far, so evidence accumulates on
existing facts instead of producing near-duplicates. Reviewers confirm, edit, reject, and merge facts while the run
is still in progress. Confirming a fact puts it on the matter's in-app timeline immediately. There is no separate
publish step. Exports are produced from the same data.

Target scale: hundreds of thousands of input documents per run.

## Decisions captured from product discussion

| Topic | Decision |
| --- | --- |
| Owner | The person in the record who is responsible for the fact, e.g. the executive who approved the contract. It is not the reviewing attorney. It is a link to a matter entity and may be unknown. Who authored the fact record (agent run or user) is tracked separately as provenance. |
| Issues | One fact may support multiple issues. Issues come from a **Review Guidance** profile. Issue coding may be stored in any matter metadata field. A Fact Development task version pins one Review Guidance profile and one revision. |
| Entities | A per-matter entity store of people and organizations only. Subject entities (projects, agreements, products and the like) are out of scope for now. |
| Analysis | Facts can be filtered, faceted, and sorted on every fact field, and on attributes of their evidence documents. |
| Scale | Hundreds of thousands of responsive documents. |
| Human review | Reviewers work on facts while the run is creating them. |
| Output | In-app timeline only for now. Facts are exportable as CSV (and JSON). XLSX is not needed. |

## Is this an Analysis Task?

**Yes. Add it as a new `FACT_DEVELOPMENT` analysis-task type with its own code-owned workflow.** Do not implement
it as a variant of `QUESTION_ANSWERING`.

`docs/system-one-decision-engine-plan.md` says future task types must add "genuinely different workflow
behavior rather than relabeling this Jev-backed lifecycle." Fact development qualifies. It is generative rather
than typed-decision work. It is stateful across documents. Its output is a matter-level fact ledger, not per-document
coding values.

**Reused from the Analysis Task platform:**

- `matter_analysis_task` / `matter_analysis_task_version` hold the versioned, published fact-development guidance.
  The guidance covers what counts as a fact for this matter, issue-specific direction, the granularity rules, and
  how dates and owners are assigned. Every run pins the published version.
- `WorkflowRun`, `WorkflowStepRun`, `SkillRun`, and `ModelInvocation` for durability, usage, and telemetry.
- Managed skills and `WorkflowSkillBinding` for the extraction and consolidation models.
- Review batches as the frozen input scope.
- The paragraph map (`app/document_evidence.py`) for citable evidence and viewer locations.
- Model rate limiting, batching, and launch estimates.

**Not reused:**

- The Decision Specification and Jev typed-decision path.
- The Decision Specification compiler skill.
- `ReviewDecisionResult` and `ReviewBatchRunValue`. A fact is not a per-document coding value.

**Required change to the Analysis Task code:** add a third type alongside `QUESTION_ANSWERING` and
`CLASSIFICATION`.

- `FACT_DEVELOPMENT` versions carry a **Fact Development Specification**, not a Decision Specification or a
  classification model binding.
- `publish_analysis_task_version` in `app/analysis_tasks.py` already branches by task type. It gets a
  `FACT_DEVELOPMENT` branch that requires a READY, validated Fact Development Specification and a valid guidance pin.
- `TASK_WORKFLOW_KEYS` and the `ck_matter_analysis_task_type` constraint add `FACT_DEVELOPMENT`.

### Link to Review Guidance

Review Guidance is the `matter_definition` / `matter_definition_revision` collection described in
`docs/multiple-review-guidance-plan.md`. Today guidance is Markdown with no structured link to the metadata
fields that hold coding. The fact task therefore has to supply that link.

- **Pinning.** Each `FACT_DEVELOPMENT` task version pins a Review Guidance profile, an exact revision, and the
  revision's content hash. It uses explicit columns on `matter_analysis_task_version`:
  `review_guidance_id`, `review_guidance_revision_id`, `review_guidance_content_hash`. The service layer
  requires them for this type.
  - The pin never follows the guidance's moving `published_revision` pointer.
  - When the guidance publishes a new revision, the task UI shows that the pin is out of date. Adopting the new
    revision creates a new draft task version, which must be recompiled and republished.
  - Only a published guidance revision can be pinned.
- **Issue catalog compilation.** A managed skill, `compile-fact-development-specification`, reads the pinned
  guidance revision and drafts the issue catalog. For each issue it produces:
  - a stable issue key, name, and description;
  - source references into the guidance: heading, excerpt, and excerpt hash;
  - **coding mappings**: one or more `(metadata_definition_key, match)` pairs saying how this issue's coding is
    stored. The match depends on the field type: an enum value key (including a hierarchical node and its
    descendants), `true` for a boolean, or an exact value for text. The field may be any active matter metadata
    definition.
  - fact guidance for the issue: what counts as a fact here, granularity, and exclusions.
- **Specification-wide rules:** date and owner assignment rules, recall and limit settings.
- **Validation (deterministic, required before publish):**
  - every mapped definition exists, is active, and has a supported type;
  - every enum value key exists, and deactivated values are refused;
  - every source reference resolves to an excerpt of the pinned revision with a matching hash;
  - issue keys are unique.
- **Human review.** The matter admin reviews and edits the catalog and mappings, then publishes the task version
  atomically. This follows the existing compile → review → publish flow.
- **Coding source at run time.** A document belongs to an issue when any of the issue's mappings matches its
  coding. There are two sources, and the match for each document is snapshotted when the run is prepared:
  - **Current matter coding** (`document_metadata_current`), the default.
  - **One reviewer's batch coding** (`ReviewBatchReviewerCoding`), pinned at its current revision when the run is
    prepared, as reviewer-coding promotion does. This covers coding that hasn't been promoted to the matter yet.
    Later edits by that reviewer don't change an in-flight run.

## Who

| Actor | Role in this feature |
| --- | --- |
| Task author (`analysis_task.manage`) | Creates the fact-development task, pins a published Review Guidance revision, reviews the compiled issue catalog and coding mappings, and publishes the task version. |
| Run operator (`analysis_task.run`) | Launches and cancels fact-development runs. |
| Fact reviewer (`fact.review`, `entity.manage`) | Reviews facts while they are created: confirm, edit, reject, merge, change issues, mark key facts. Confirms, edits, merges and unmerges entities. |
| Specification compiler | Managed skill (`compile-fact-development-specification`). Drafts the issue catalog and coding mappings from the pinned guidance revision. Its output is a draft that is validated deterministically and reviewed by a human. |
| Fact extraction agent | Managed skill (`fact-extraction`). Runs once per document with recalled context and proposes fact operations. It never writes facts directly. |
| Fact consolidation agent | Managed skill (`fact-consolidation`). Decides whether near-duplicate candidates are the same fact. Invoked only by the consolidator. |
| Consolidator (code) | Deterministic step that validates proposed operations, resolves entities, applies them to the fact ledger under a lock, and projects to search. |
| System | Search projection of facts, progress accounting, run reports. |
| Case team (`fact.view`, `fact.export`) | Read facts and the timeline, and export. A read-only grant is enough to read. |

## What

### Fact shape

| Field | Notes |
| --- | --- |
| Summary | One or two sentences stating the fact. |
| Supporting material | One or more evidence records. Each has the document, an exact quote, a paragraph ID, character offsets, a source-artifact hash, and a stance (`SUPPORTS` or `CONTRADICTS`). |
| Date | `event_date_start`, an optional `event_date_end`, `date_precision` (`DAY`, `MONTH`, `QUARTER`, `YEAR`, `RANGE`, or `UNKNOWN`), and `date_basis` (`STATED_IN_TEXT`, `DOCUMENT_DATE`, or `INFERRED`). The event date is distinct from the date the document was sent. |
| Owner | Optional reference to a `matter_entity` row. Unknown is allowed. Displayed and filtered as that row's canonical entity. |
| Participants | `matter_entity` row references, each with a role: `ACTOR` or `RECIPIENT`. Displayed and filtered as canonical entities. |
| Issues | One or more issue keys from the pinned task version's issue catalog. |
| Status | `WORKING` (agent-created and unreviewed), `CONFIRMED`, `REJECTED`, or `MERGED`. |
| Flags | `key_fact`, `has_contradiction`, `undated`. |

### Evidence integrity (code-enforced)

- A quote must be an exact substring of the cited paragraph's text in the pinned source artifact. Character
  offsets are computed by code, not taken from the model.
- Paragraph IDs must exist in the document's paragraph map (`paragraph-v1`). Reuse the citation validation in
  `app/document_evidence.py`.
- Operations that reference a `fact_id` the agent was not shown are rejected.
- Proposed issues outside the document's coded issues are rejected, but recorded as a QC signal.
- Rejected operations are kept with their validation errors. They are never silently dropped.

### Entity store (per matter)

The store holds **people and organizations only**: the parties who can own or take part in a fact. Projects,
agreements, products and other named things stay in the fact summary and quotes, where search can find them.

| Type | Examples |
| --- | --- |
| `PERSON` | executives, employees, counsel |
| `ORGANIZATION` | companies, subsidiaries, law firms, agencies, counterparties |

- `entity_type` is a code-owned enum so more types can be added later without reshaping the store.
- **One table, canonical rows and variants.** Each `matter_entity` row is either a **canonical entity**
  (`parent_id` is null) or a **variant** of one (`parent_id` set). A variant records a name and/or one email address
  under which the entity was seen. Aliases and multiple email addresses are just variant rows, so there are no
  separate alias or email tables. Example:

  ```text
  id  parent_id  display_name   email
  1   —          Jane Smith     jsmith@acme.com        ← canonical
  2   1          Jane Smith     jane.smith@acme.com
  3   1          J. Smith       —
  4   1          Jane           jane@gmail.com
  ```

- **Rules (enforced by the entity command service, not triggers):**
  - One level only: a `parent_id` must point to a canonical row. Merging an entity also moves its variants to the
    new parent, so chains never form.
  - Each email belongs to one row in the matter: a partial unique index on `(matter_id, normalized_email)` where
    an email is present. This also makes email lookup a single indexed query.
  - Type, title, organization, custodian link and status live on canonical rows. Variants inherit them.
  - Status (canonical rows): `PROPOSED` (agent-created), `CONFIRMED`, or `REJECTED`.
- **Merge and unmerge.** Merging B into A sets `parent_id = A` on B and its variants. Unmerging clears it. Facts
  link to the specific row that matched (e.g. the `jane@gmail.com` variant), so merges and unmerges carry facts with
  them without rewriting fact records, and the matching name or address stays visible as provenance. Rejecting a
  proposed entity clears its fact links through events.
- **A person's organization and title.** An optional `organization_id` (a canonical `ORGANIZATION`) and a free-text
  `title` on a canonical `PERSON`, so the UI can show "Jane Smith, General Counsel (Acme Corp)".
- **Resolution order:** an exact email match, then an exact normalized name match on any row, then fuzzy
  candidates decided by the model. When the canonical entity is known but the name or email is new, a variant row is
  added. Anything still ambiguous creates a new `PROPOSED` canonical entity for a reviewer.
- **Relationships with time validity** (`EMPLOYED_BY`, `COUNSEL_FOR`, `SUBSIDIARY_OF`, `PARTY_TO`, with
  `valid_from` and `valid_to`) come in a later slice. They aren't needed for the timeline. They are the
  foundation for privilege work later ("was X acting as counsel to Y on this date").
- Facts link to entities in two ways: one **owner** and **participants** with roles. Both can be filtered and
  faceted.

### Filtering and faceting ("slice and dice")

Every fact field can be filtered, faceted, and sorted. The Facts list, the timeline, and CSV export share
one request model. A filtered export contains exactly the filtered facts.

| Group | Filters and facets |
| --- | --- |
| Content | Keyword search over the summary and evidence quotes (semantic search deferred) |
| Issues | Issue key (multi-select, OR within the facet), task, task version |
| Entities | Owner; participant, optionally restricted to a role; entity type (person or organization) |
| Dates | Event date range; date histogram (year, quarter, month); precision; basis; undated |
| Review state | Status; key fact; has contradiction; has a suggested revision; confirmed by; confirmed at |
| Provenance | Author type (agent or human); created by run; created and updated time |
| Evidence | Evidence count range; document count range; stance |
| Evidence documents | Custodian, and any document field held in Core (email from/to, sent date, family, file extension, control number, coding fields). Read from Core tables at query time. |

**Default order: by event date, oldest first**, everywhere facts are listed: the Facts list, the timeline, an
entity's facts, a document's Facts tab, and exports.
- The sort key is `event_date_start`, so a month-precision fact ("March 2021") sorts at the start of that month.
- Ties are broken by date precision (coarser first), then creation time, then fact ID, so the order is stable.
- Undated facts come last.
- Users can still sort by any other column. The default is restored when filters are cleared.

Facet behavior follows matter search: selections are ORed within a facet and ANDed across facets, facet counts
exclude their own selection, and counts are fact counts. Filter requests are a controlled request model compiled
server-side. Callers never send raw OpenSearch DSL. Saved fact views reuse the saved-search sharing model in a
later slice.

### Human review while the agent runs (concurrency rules)

- Every fact has a `lock_version`, and human edits use optimistic concurrency.
- **Confirmed facts are human-owned.** The agent may add evidence to them. Changes to summary, date, owner, or
  issues are stored as a *suggested revision* for the reviewer and never applied automatically.
- **Rejected facts are tombstones.** They are included in recall marked "rejected." The consolidator also blocks
  re-creation when a new fact matches a rejected one above the duplicate threshold.
- Merges keep the absorbed fact as `MERGED` with `merged_into_fact_id`, move its evidence, and can be undone
  through an event.

### Timeline (confirmation is publication)

- **The timeline shows `CONFIRMED` facts, live.** A fact appears as soon as a reviewer confirms it. It disappears
  if it is later rejected or merged, and edits to a confirmed fact show immediately.
- `WORKING` (agent-created, unreviewed) facts never appear on the timeline. Reviewer confirmation is the only human
  gate between agent output and the timeline.
- There is no publish step, publication record, or snapshot in v1. Every fact's revision and event history is kept,
  so numbered snapshots ("the chronology as of Oct 1") can be added later without reworking the data model.
- Reviewers can switch the timeline to include working facts, clearly marked as unconfirmed, to see what is coming.

### Export

- **CSV** of facts, one row per fact: ID, summary, date fields, owner, participants, issues, status,
  evidence count, confirmed by and confirmed at. A second CSV of evidence has one row per quote with control number, document title, document date and
  the quote.
  Both are streamed by the standard library `csv` module, so no new dependency is needed.
- **JSON** with the complete structure, for downstream tools.
- Exports honor the active filter request (including status), so "confirmed facts only" is the timeline export.
- Every export is stamped "as of <timestamp>" in its file name and a header row, so a circulated chronology records
  when it was taken.
- XLSX is out of scope.

## Where

### Backend (Core)

| Area | Location |
| --- | --- |
| Models | `app/models.py`, new classes listed under the data model below |
| Migration | `alembic/versions/<rev>_fact_development.py`, including widening `ck_matter_analysis_task_type` |
| Task type registration | `app/analysis_tasks.py`: `TASK_WORKFLOW_KEYS["FACT_DEVELOPMENT"] = "fact_development_v1"` and type-aware publish validation |
| Fact Development Specification | `app/fact_specifications.py` (new): Pydantic schema for the issue catalog, coding mappings and rules, plus deterministic validation against the pinned guidance revision and active metadata definitions |
| Specification compilation | `app/fact_specification_compilation.py` (new), following `app/analysis_task_compilation.py`: compiles the pinned guidance revision into a draft specification |
| Issue membership | `app/fact_issue_membership.py` (new): evaluates coding mappings against `document_metadata_current` or a pinned reviewer-coding revision (`app/reviewer_coding.py`), for every supported metadata type |
| Fact ledger command service | `app/facts.py` (new): the only write path for agent and human mutations. It appends events, updates projections, writes the audit record, and enqueues the search upsert in one transaction, mirroring the metadata command service. |
| Entities | `app/matter_entities.py` (new): the entity command service (create, add variant, edit, confirm, reject, merge, unmerge) and resolution |
| Run orchestration | `app/fact_development.py` (new): launch, prepare, wave planning, progress, completion, run report |
| Consolidator | `app/fact_consolidation.py` (new) |
| Evidence validation | `app/fact_evidence.py` (new), built on `app/document_evidence.py` |
| DBOS workflows | `app/workflows/fact_development.py` (new), following `app/workflows/analysis_task_batch.py` |
| Dispatcher | `app/workflows/dispatcher.py`: `enqueue_fact_development`, `cancel_fact_development` |
| Workflow spec | `app/workflow_specs.py`: `FACT_DEVELOPMENT_SPEC` with `fact_extraction` and `fact_consolidation` roles, and `FACT_SPECIFICATION_COMPILATION_SPEC` with a `fact_specification_compiler` role |
| Managed skills | `app/standard_skills.py`: bootstrapped `compile-fact-development-specification`, `fact-extraction` and `fact-consolidation` skills and their schemas |
| Fact search and recall | `app/fact_queries.py` (shared filter, facets, histogram) and `app/fact_recall.py` (new) |
| Exports | `app/fact_exports.py`: produced within the request and stored in Artifact (originally a `MATTER_FACT_EXPORT` job; replaced in migration `0098`) |
| Job execution | `app/job_executions.py`: `FactDevelopmentAnalysisTaskExecutionAdapter`, so runs appear in Jobs with the common progress, cancel and failure fields |
| Permissions | `app/rbac_manifest.py`: new permission keys and system-role assignments (see Authorization) |
| Routers | `app/routers/facts.py`, `app/routers/fact_runs.py`, `app/routers/matter_entities.py` (new), registered in `app/main.py` |
| Schemas | `app/schemas.py` |
| Tests | `tests/test_facts.py`, `tests/test_fact_evidence.py`, `tests/test_fact_consolidation.py`, `tests/test_fact_development.py`, `tests/test_fact_exports.py`, `tests/test_matter_entities.py` |

### Data model (all Core-owned)

```text
matter_analysis_task_version  + review_guidance_id?, review_guidance_revision_id?, review_guidance_content_hash?
                              (required by the service for FACT_DEVELOPMENT; the issue catalog lives in the
                              type-specific specification JSON)

matter_entity              id, matter_id, parent_id? (null = canonical), entity_type (PERSON|ORGANIZATION),
                           display_name, normalized_name, email?, normalized_email?,
                           title?, organization_id?, custodian_id?, description?,        -- canonical rows only
                           status (PROPOSED|CONFIRMED|REJECTED),                         -- canonical rows only
                           created_by_user_id? | created_by_run_id? (exactly one), timestamps
                           UNIQUE (matter_id, normalized_email) WHERE normalized_email IS NOT NULL
                           INDEX (matter_id, normalized_name), INDEX (parent_id)
matter_entity_event        id, entity_id, event_type, payload, actor_user_id?, run_id?, created_at   -- append-only
matter_entity_relationship (later slice) from_entity_id, to_entity_id, relationship_type, valid_from?, valid_to?,
                           evidence refs, status

matter_analysis_task_run   (existing, generalized) run_kind (CLASSIFICATION|FACT_DEVELOPMENT), scope REVIEW_BATCH,
                           configuration_snapshot (task version, issue-catalog hash, coding source and pinned
                           reviewer-coding revision, chunk size, skill version, model), report, status, progress;
                           classification-only columns are required only when run_kind = CLASSIFICATION
analysis_input_manifest    (generic platform) one per run, owner MATTER_ANALYSIS_TASK_RUN; the Artifact-owned Parquet
                           holds one chronological row per batch document: status (READY|MISSING|EXCLUDED), issue
                           keys, File Date, email headers, fingerprint, pinned source text artifact and hash
matter_analysis_task_run_chunk  run_id, chunk_number, ordinal_start, item_count, fingerprints (finished, JSON),
                           status, processed_count, failed_count, counts   -- execution state only, one row per chunk

Input manifest retention: the run's `ANALYSIS_INPUT_MANIFEST` is kept for the life of the run so its population, pinned text and outcomes stay reproducible; unlike fact exports, no expiry is applied. Workers verify the manifest's owner, scope, contract key and version, and resolver before reading rows, and a cancel during freezing publishes no manifest.

Reuse mirrors embeddings: a document's extraction fingerprint hashes the document, its pinned source text hash, the
paragraph-map version, the issue-catalog hash and its coded issues (not the skill version or model). It is computed
when the manifest is frozen, so documents finished by earlier runs of the task are marked before any text is read.
Finished documents record it with their facts, so relaunching after a cancel continues instead of duplicating facts.

Per-document results are job outcomes (Artifact-owned Parquet fragments plus a final manifest), not Core rows.
Operations are validated and applied inline; invalid ones are recorded in the document's outcome payload.
(`matter_fact_run`, `matter_fact_run_document`, and `matter_fact_operation` from the first draft were dropped in
favour of the unified job framework and job outcomes.)

matter_fact                id, matter_id, status, current_revision_id, merged_into_fact_id?, key_fact,
                           has_contradiction, evidence_count, document_count, origin_run_id?, lock_version, timestamps
matter_fact_revision       id, fact_id, revision, summary, event_date_start, event_date_end, date_precision,
                           date_basis, owner_entity_id?, author_type (AGENT|HUMAN), author_user_id?, skill_run_id?,
                           reason, created_at                                   -- immutable
matter_fact_issue          fact_id, matter_analysis_task_id, issue_key           -- current projection; issue keys are
                                                                                 stable across task versions
matter_fact_entity         fact_id, entity_id, role (PARTICIPANT_ACTOR|PARTICIPANT_RECIPIENT)
                                                                                 -- current projection; owner is on the revision;
                                                                                 entity_id is the matched row (canonical or variant)
matter_fact_evidence       id, fact_id, matter_document_id, source_artifact_id, source_content_hash,
                           paragraph_map_version, paragraph_id, char_start, char_end, quote, quote_hash,
                           stance, status (ACTIVE|REMOVED), added_by (run|user), created_at
matter_fact_event          id, fact_id, event_type, payload, actor_type, actor_user_id?, run_id?, skill_run_id?,
                           created_at                                            -- append-only ledger
```

`matter_fact` also carries `confirmed_by_user_id` and `confirmed_at`, which are set and cleared through events.

Issue keys are compiled from the guidance and are stable across task versions. When a recompiled catalog renames
or removes a key, the new task version must record an explicit key mapping (`renamed_from`), or the affected facts
are flagged for reviewer reassignment. Keys are never silently re-pointed.

`matter_fact_event` is authoritative. The `matter_fact*` current tables are projections maintained by the command
service in the same transaction, following the `metadata_event` / `document_metadata_current` pattern. Projections
are never updated directly.

### Search and recall (Postgres only for the MVP)

Facts are **not** projected into OpenSearch for the MVP. The ledger tables answer every query directly; fact volume
(thousands to tens of thousands per matter) is well within what indexed SQL handles. An OpenSearch projection can be
added later as a rebuildable copy without changing the ledger or the API.

- **One filter model** (`app/fact_queries.py`) is shared by the fact list, search, facets, the date histogram, and
  CSV export, so all of them always agree.
- **Keyword search** matches summaries and active evidence quotes (case-insensitive substring). Semantic search in
  the UI is deferred.
- **Facets** are SQL counts of distinct facts per value. Each facet's counts exclude that facet's own selection.
- **Date histogram** buckets confirmed facts by year, quarter, or month, plus an undated count.
- **Evidence-document attributes come from Core only:** custodians from `matter_document_custodian`, and any document
  field from `document_metadata_current` (control number, email from/to/cc/bcc/subject, sent and received dates,
  family, file extension, and matter coding fields). A fact matches when any of its active quotes comes from a
  matching document. No Artifact or OpenSearch calls.
- **Recall for the agent** (`recall_facts`): SQL selects candidates (same issues, optional participant overlap, a
  date window, excluding merged facts, rejected facts flagged as tombstones); summary embeddings are compared in
  code. Embeddings are computed on first use through `embedding_gateway` and cached on the fact with the model and a
  hash of the summary, so editing a summary never waits on the embedding model and a stale embedding is recomputed.

### API

```text
POST   /v1/matters/{matter_id}/fact-runs                     launch (estimate + confirmation)
GET    /v1/matters/{matter_id}/fact-runs[/{run_id}]          status, progress, report
POST   /v1/matters/{matter_id}/fact-runs/{run_id}/cancel

POST   /v1/matters/{matter_id}/facts/search                  controlled filter request (all fields), sort, paging
POST   /v1/matters/{matter_id}/facts/facets                  self-excluding facet counts for one requested facet
POST   /v1/matters/{matter_id}/facts/date-histogram          timeline density for the same filter request
GET    /v1/matters/{matter_id}/facts?updated_since=…         live-review polling feed
GET    /v1/matters/{matter_id}/facts/{fact_id}               revisions, evidence, events, suggested revisions
PATCH  /v1/matters/{matter_id}/facts/{fact_id}               edit -> new revision (requires lock_version)
POST   /v1/matters/{matter_id}/facts/{fact_id}/confirm|reject|merge|evidence
POST   /v1/matters/{matter_id}/fact-exports                  write both CSV files for the filter request now; returns the completed export
GET    /v1/matters/{matter_id}/fact-exports[/{export_id}[/download]]   recent exports; download streams through Core

GET    /v1/matters/{matter_id}/entities                      canonical entities: search by type, status, name, email
GET    /v1/matters/{matter_id}/entities/{entity_id}          canonical entity with its variants and fact counts
POST   /v1/matters/{matter_id}/entities                      create; sub-routes: variants, confirm, reject, merge, unmerge
GET    /v1/matters/{matter_id}/documents/{document_id}/facts facts citing one document (slice 9)
```

The timeline uses `facts/search` and `facts/date-histogram` with a `CONFIRMED` status filter. No separate timeline
endpoint is needed.

**Export within the request (decided; replaces the original export job).** Fact volumes are small enough that a
background job is not needed, so an export is produced by the request itself:
- Core reads every matching fact from one `REPEATABLE READ` snapshot, writes a facts file and an evidence file to
  temporary files, and stores both in the Artifact Service as immutable `FACT_EXPORT` artifacts.
- Core then records a completed `matter_fact_export` row (filter, CSV policy, counts, file references, expiry) and a
  `fact_export.created` audit event in one transaction. A failed request records `fact_export.failed` and leaves no
  export. Each download records `fact_export.downloaded`.
- The idempotency key returns the first export for a repeated request; an advisory lock stops a double submit
  from writing the files twice.
- Download always streams through Core after fresh authorization and provenance checks.
- No per-fact rows are written to Core for an export, and fact exports do not appear in Jobs.
- Document (search-result) export stays a `MATTER_SEARCH_EXPORT` job: it can cover millions of documents and pins a
  search-index generation.

### Authorization

New permission keys go in `app/rbac_manifest.py`. Every endpoint checks them in the backend, with tests.

| Permission | Allows |
| --- | --- |
| `fact.view` | Read facts, the timeline and fact detail. **Also requires `document.view`**, because facts contain document quotes. |
| `fact.review` | Confirm, edit, reject, merge and unmerge facts, add or remove evidence, accept suggested revisions. |
| `fact.export` | Create and download fact exports. **Also requires `document.view`**, mirroring `document.export`. |
| `entity.view` | Read entities. |
| `entity.manage` | Create, edit, confirm, reject, merge and unmerge entities and variants. |

- Task authoring uses the existing `analysis_task.manage`, and launching or cancelling runs uses
  `analysis_task.run`.
- Proposed system-role assignments:
  - `MATTER_ADMINISTRATOR`: all five, by adding `"fact."` and `"entity."` to its permission-prefix list. Tenant and
    client administrators inherit them automatically because their sets derive from all permission keys.
  - `REVIEWER`: `fact.view`, `fact.review`, `entity.view`, `entity.manage`.
  - `ANALYSIS_OPERATOR`: `fact.view`, `entity.view`.
  - `READ_ONLY`: `fact.view`, `entity.view`.
  - **Only administrators can export (decided):** `fact.export` is held only by Root, Tenant, and Client
    Administrators (through their all-permissions sets) and Matter Administrators (through the `"fact."` prefix).
    Reviewer, Analysis Operator, Read-only and the other roles never receive it. This matches how
    `document.export` is assigned today.
- Mutations record audit entries. After the API lands, regenerate the OpenAPI spec and the Orval client.

### Frontend

#### Navigation

Facts and entities are separate objects from documents, so they **do not appear in the Search & Review
workspace**. Both are reached from the main matter tab menu (`components/views/matter-view.tsx`, `?tab=`). The new
tabs sit after Matter definition, and the existing tabs keep their current order on `main`:

```text
Overview · Metadata · Matter definition · Facts · Entities · Analysis · Batches · Classification · Jobs · Token usage · Search index
```

The tabs are shown only to users with `fact.view` (Facts) or `entity.view` (Entities). The backend still enforces
every permission.

- **Facts tab** (`?tab=facts`) has sub-tabs, following the existing Metadata → Definitions/Groups pattern
  (`&facts=`):
  - **List** (default): the working set for live review.
  - **Timeline**: a chronological reading view of confirmed facts.
- **Entities tab** (`?tab=entities`) has sub-tabs **Directory** (default) and **Proposed** (the queue of
  agent-proposed entities awaiting review).
- **Detail pages** are matter sub-routes, following the existing `topic-jobs/[jobId]` pattern. Each has a back link
  to its tab:
  - `web/src/app/(workspace)/app/clients/[clientId]/matters/[matterId]/facts/[factId]/page.tsx`
  - `web/src/app/(workspace)/app/clients/[clientId]/matters/[matterId]/entities/[entityId]/page.tsx`
- Filter, sort and sub-tab state live in the URL so views can be shared and bookmarked.
- Lists and the timeline use compact density. Detail-page forms use comfortable density.

#### Surfaces

| Surface | Location and content |
| --- | --- |
| Facts → List | `components/facts/facts-list-panel.tsx`. A filter bar covering every field group from the slice-and-dice table, with one `FacetedFilter` dropdown per facet and self-excluding counts. Keyword, semantic and hybrid search over summaries and quotes. A compact, column-configurable `DataTable` (date, summary, owner, issues, status, evidence count, updated). Bulk confirm and reject. **Export CSV** of the current filter. A banner while a run is active. Live updates by TanStack Query polling with `updated_since`. |
| Facts → Timeline | `components/facts/facts-timeline-panel.tsx`. The existing `DateHistogram` across the top. Facts grouped by year then month as cards (precision-aware date, summary, owner, issue chips). Optional lanes by issue. A separate undated section. Same filter bar as the list. Shows confirmed facts live, with an "include unconfirmed" toggle that marks working facts distinctly. **Export CSV** of the current filter. |
| Fact detail | `facts/[factId]/page.tsx`. Editable summary, date (precision and basis), owner, participants and issues, with `lock_version` conflict handling. An evidence list (control number, document title, document date, quote) in which each quote opens the existing `DocumentViewerDialog` at the cited paragraph, with a secondary "Open in Search & Review" link (`?document=&paragraph=`). Agent-suggested revisions (accept or dismiss), revision and event history, merge candidates, and confirm, reject and merge actions. |
| Entities → Directory and Proposed | `components/entities/entities-panel.tsx`. A compact table of canonical people and organizations (name, title, organization, email count, fact count, status) with type and status filters, and merge selection. |
| Entity detail | `entities/[entityId]/page.tsx`. The canonical name, title, organization, custodian link and status, with edit, confirm and reject. A **variants** list (each name or email seen) with add, move to another entity, and split off as its own entity (unmerge). Below that, the facts the entity owns or participates in as a mini-timeline that links to the fact list pre-filtered to the entity. |
| Overview tab | Added tiles for working facts, confirmed facts and proposed entities, each linking to its tab. |
| Task authoring | Existing **Analysis** tab, adding the `FACT_DEVELOPMENT` type: a Review Guidance profile and revision picker, compile and regenerate, an issue catalog editor (issues, guidance excerpts, coding mappings with field and value pickers), a stale-pin indicator, and publish. Launch a run from the published task by picking a review batch, with the issue-match counts and cost estimate. |
| Run progress | Existing **Jobs** tab, through the job execution adapter: fact-development runs (progress, cancel, the run report with QC signals). Fact exports are not jobs; their recent downloads are listed in the Facts tab. |
| Help and docs | `HelpLink` topics `facts` and `entities` on the new tabs. `web/content/docs/facts-and-timeline.mdx` and `web/content/docs/entities.mdx`, glossary entries, `meta.json`. |

In the slices before slice 9, Search & Review, batch review and their document detail panels are unchanged. Facts
are not browsed or managed from the search UI. Slice 9 adds read access from documents to their facts, described
next.

#### Document integration (slice 9)

**1. Project one field, `fact_state`, into the document search index.**

*Implemented as a derived `SYSTEM` ENUM metadata definition (`fact_state`, display name "Facts", values `confirmed` and
`unconfirmed`) in the default profile (version 5) and a Facts system group, like `email_domain` and `file_date`, so it
uses the normal metadata filter and facet path rather than a special built-in field. Older matters get it from
`ensure_fact_state_metadata` when their first fact is created or on a full rebuild.*

The index records only *whether* a document has facts. Everything else about a document's facts is read from Core
in the Facts tab of the coding panel.

| Field | Type | Values |
| --- | --- | --- |
| `fact_state` | keyword | `CONFIRMED` if any active evidence on the document belongs to a confirmed fact. Otherwise `UNCONFIRMED` if it belongs only to working facts. Absent if no fact cites the document. |

- **Search & Review gets a "Has facts" facet:** Confirmed, Unconfirmed, None. "None" uses the existing
  missing-value filter.
- Rejected and merged facts don't count.
- **Core stays authoritative.** The value is derived from `matter_fact_evidence` plus fact status. Full index rebuilds
  recompute it from Core, the same way `batch_ids` is derived from `review_batch_document`.
- **Schema change:** add `fact_state` to `IN_PLACE_ADDITIVE_ROOT_FIELDS` in `app/search/schema.py`, with a bounded
  backfill like the existing batch fields. Existing matter indexes update in place without a confirmed reindex.
- **Write path:** when evidence is added or removed, or a fact is confirmed, rejected or merged, the fact command
  service recomputes `fact_state` for the affected documents in the same transaction.
  - It enqueues a durable document upsert **only for documents whose state actually changed**. Most agent
    activity therefore causes no reindexing: a document already marked `UNCONFIRMED` stays so as more working facts
    cite it.
  - Upserts are coalesced per document. Reviewer-driven changes use user priority, and agent consolidation uses
    agent-batch priority.
- The "find the supporting documents for a fact" need is met by the fact detail page's evidence list, not by the
  index.
- Search and facet requests stay inside the controlled `MatterSearchRequest`. Callers never send raw DSL.

**2. Facts in the document coding panel.**

- Add a **Facts** tab beside *Coding · Metadata · History* in the document details panel of both Search & Review
  (`components/review-workspace.tsx`) and batch review (`components/batch-review-workspace.tsx`).
- For the open document it lists each citing fact: date, summary, owner, issue chips, status badge
  (confirmed or working), and the quote(s) taken from this document.
- Clicking a quote highlights the passage in the open document. The review page already supports
  `?paragraph=`.
- Clicking the fact opens the fact detail page in the matter's Facts tab.
- **Read-only at first.** Edits stay on the fact detail page, so there is one place where facts change. Planned
  follow-ons: "add selected text as evidence to a fact" and "create a fact from the selection".
- **Data source:** a new Core endpoint, `GET /v1/matters/{matter_id}/documents/{document_id}/facts`, served from
  Core, not the search index. The panel is therefore correct immediately after a change, before the projection
  catches up.

## How the workflow runs (`fact_development_v1`)

```text
launch ── prepare ── plan waves ─┬─ wave 1: extract (parallel) ── consolidate (locked) ── project
                                 ├─ wave 2: extract (parallel) ── consolidate (locked) ── project
                                 ├─ ...
                                 └─ final: duplicate sweep ── run report ── complete
```

1. **Launch.** The user selects a published `FACT_DEVELOPMENT` task version (which pins its Review Guidance
   revision), a ready review batch, and the coding source: current matter coding (the default), or one reviewer's
   batch coding pinned at its current revision. The API shows how many documents match each issue and returns a cost and volume
   estimate. Launching requires confirmation. Launch creates `WorkflowRun` and a `FACT_DEVELOPMENT` `matter_analysis_task_run` and enqueues the
   workflow.
2. **Prepare.** Freeze the documents. Evaluate each issue's coding mappings against the chosen coding source and
   snapshot the issues each document matches. Skip documents that match no issue. Group documents by family.
   Order them chronologically by document date. Assign **waves**: contiguous chronological windows sized for
   throughput. Earlier facts therefore exist before later documents are read.
3. **Extract, in parallel within a wave.** Each document runs as a DBOS child workflow on a rate-limited queue. The
   input contains:
   - the paragraph-mapped text, using `render_model_document`;
   - document metadata (date, from, to, cc, custodian);
   - the document's matched issues with their fact guidance from the pinned specification;
   - known people and organizations matching its email addresses, and names or aliases found in its text;
   - **recalled facts**: top-k per issue from the facts index as of the wave snapshot, including confirmed and
     rejected markers.

   The output is a list of operations with cited paragraphs and quotes. Operations are validated and applied
   inline; invalid ones are recorded in the document's job outcome. The guidance and issue catalog form a cache-friendly prefix (`prompt_caching`).
4. **Consolidate, serialized per partition.** A partition is a run plus a primary issue, protected by a
   `pg_advisory_xact_lock`, the same mechanism `app/search/service.py` uses for projection work. Operations are applied in
   document order:
   - `ADD_EVIDENCE` and `REFINE` apply directly to working facts, and become suggested revisions on confirmed facts.
   - For `CREATE`, search the facts index for candidates created after the snapshot. Above a similarity threshold
     with overlapping participants and dates, call `fact-consolidation` to choose `SAME` or `DISTINCT`. `SAME`
     becomes `ADD_EVIDENCE`.
   - Entity references are resolved as described in the entity store section. A new name or email for a known
     entity becomes a variant row. Unknown entities become `PROPOSED` canonical rows. Entity creation takes an
     advisory lock per matter and normalized name, and the unique email index blocks duplicates, so parallel
     partitions can't create the same entity twice.
   - Each application writes events, projections, and the search upsert.
5. **Final sweep.** Cluster working facts across issues by embedding. Auto-merge only agent-authored `WORKING`
   pairs above a high threshold with matching dates. Everything else becomes a merge suggestion for reviewers.
6. **Run report.** Coverage counts, failures, fact counts by issue, and **QC signals**:
   - documents coded to an issue that produced `NO_FACT` for it;
   - proposed issues outside the document's coding;
   - entities still awaiting confirmation.

### Scale controls

- Per-document failures do not fail the run. Coverage is reported exactly.
- Queue concurrency plus `app/model_rate_limits.py` limits, with bounded retries on 429 responses.
- Recall is capped by count and tokens, so prompt size stays flat as the fact store grows.
- Cancel and resume are supported. Waves and operations are idempotent per (run, document, ordinal).
- Optional thread suppression (v2): skip an email whose text is fully contained in a later message of the same
  thread, after reviewing that the later message is in scope.
- Rough order of magnitude: 100k documents × ~6k input tokens ≈ 600M input tokens per run before caching.
  The launch estimate must surface this. Thread suppression and family grouping are the main cost levers.

## Alignment with `AGENTS.md`

| `AGENTS.md` rule | How this plan complies |
| --- | --- |
| Change discipline: phase-one scope is limited, and `AGENTS.md` must be updated when an architectural or product decision changes | Fact development is a scope expansion that you have requested explicitly. Slice 1 adds an `AGENTS.md` section for Fact Development (task type, guidance pin, fact ledger, entity store, confirmation-as-publication, facts index) before any code depends on it. |
| Core does not own artifact or blob storage | Source text, paragraph maps and chunks stay in the Artifact Service. Core stores only fact records and short evidence quotes with offsets and source-artifact hashes, the same way `review_decision_result.evidence` already stores evidence in Core. |
| Ledger plus rebuildable projection; every write path uses one audited command service; no triggers; no direct projection updates | `matter_fact_event` and `matter_entity_event` are the ledgers. The fact and entity command services are the only writers, for both humans and agents. Each write appends the event, updates projections, writes an audit record, and enqueues the search upsert in one transaction. |
| Agent output needs an explicit human approval boundary (topics never publish silently; Matter Definitions are published only by admins) | Agent facts are `WORKING` and agent entities are `PROPOSED`. Neither reaches the timeline until a reviewer explicitly confirms it. Agents cannot confirm or reject. |
| Reviewer coding and evaluation runs stay isolated; reproducible consumers pin the reviewer source and revision | Reviewer coding is only *read*, pinned at a revision when the run is prepared. Nothing is written to reviewer coding, `review_batch_run_value`, `metadata_event` or `document_metadata_current`. |
| Durable operations use the unified job execution platform | Fact-development runs register a job execution adapter, so they share the common status, progress, cancellation and failure fields. Fact exports finish within the request and are not durable operations. |
| Search-result export is a job; Artifact owns export bytes; downloads stream through Core with fresh authorization | Fact exports keep Artifact-owned bytes, Core-streamed downloads, and `fact.export` plus `document.view`, but run within the request rather than as a job. |
| Search: rebuildable projection, versioned names behind aliases, no raw DSL from callers, matter-keyed advisory lock, user updates prioritized | Covered in the search projection section above. |
| Agent runtime: code-owned model and tool registries, managed skills, DBOS durability | The three skills are bootstrapped managed skills bound through `WorkflowSkillBinding`. Workflows are code-owned. No tenant-defined code. |
| Authorization: code-owned permissions, roles and scoped grants, enforced in the backend; catalog changes are audited | New `fact.*` and `entity.*` permissions added to the code-owned manifest with system-role assignments. Every endpoint checks permissions, with backend tests. UI visibility is not relied on. |
| Every persistent change has an Alembic migration, with upgrade and downgrade exercised; each implementation states whether a migration is required, naming it with the exact `pipenv run` command | One Core migration per slice that changes the schema. Each slice summary names the migration and gives `pipenv run alembic upgrade head`. The export job's Artifact type may need an Artifact migration, checked in slice 2. |
| OpenAPI is the contract; the frontend uses Orval-generated clients | Regenerate after each API slice. Generated output is never edited by hand. |
| Frontend stack and styling: TanStack Table, shadcn/ui, semantic tokens, WCAG 2.2 AA | Status colors use the existing semantic tokens (`confirmed`, `rejected`, `agent`, `human`, `conflict`). **The timeline and histogram are built from existing primitives. If a charting library is needed, I'll ask first.** |
| User documentation is part of the definition of done for every user-facing feature | Docs ship **with each user-facing slice**, not at the end: slice 1 task authoring, slice 6 the Facts and Entities tabs, slice 7 timeline and export. Help-topic links and the glossary are updated in the same slices. |
| Workspace rule: never start, restart or background a worker | I will not start the DBOS worker. Workflow tests run inline with DBOS disabled, as review batches already do. Any end-to-end run against a live worker is yours to start, and I'll give you the `pipenv run …` command. |

## Delivery slices

Each slice ends with:
- `pipenv run ruff check .`, `pipenv run pytest` and `pipenv run alembic check`;
- for migrations, an upgrade and downgrade against a temporary database, after verifying `current_database()`,
  using the virtualenv Alembic with an explicit `DATABASE_URL`;
- where the slice includes UI: frontend type-check, lint, unit tests, production build, the relevant Playwright
  flows, and a check that the docs build.

1. **Task type, guidance pin, and specification.** Register `FACT_DEVELOPMENT`. Add the guidance pin columns. Add
   the specification schema and deterministic validation, the compiler skill and workflow, issue-membership
   evaluation for every metadata type, and type-aware publish. Add the `AGENTS.md` Fact Development section. (Task-authoring user docs
   ship with the authoring UI in slice 6; slice 1 is API-only.) *Acceptance:* a task pins a published guidance
   revision, compiles an issue catalog, refuses invalid mappings, and publishes. Issue membership counts for a
   batch match a hand-checked sample.
2. **Entity store and fact ledger (no extraction).** Models and migration. Permission keys and role assignments.
   The entity command service (canonical and variant rows, merge and unmerge) and the fact command service, with
   events and projections. Manual fact create, edit, confirm, reject, merge, and add evidence through the API.
   Quote validation. The `MATTER_FACT_EXPORT` job. *Acceptance:* a fact can be created by hand, confirmed, and
   exported. Every change is recorded in the event history. Merging and then unmerging an entity restores its facts'
   canonical owner and participants.
3. **Search, facets, histogram, and recall (Postgres only).** The shared filter model covering every field
   (including Core evidence-document attributes), keyword search, self-excluding facets, the date histogram, cached
   summary embeddings, and `recall_facts`. *Acceptance:* every field in the filter table can be filtered and faceted.
   Counts are fact counts. Export matches the filter exactly.
4. **Extraction.** The `fact-extraction` skill and the workflow with a single wave and no duplicate
   consolidation, tested on a small batch. *Acceptance:* every applied evidence quote is verbatim, and invalid
   operations are recorded with errors.
5. **Consolidation and ordering.**
   - **5a (done):** chunks dispatched in chronological manifest order through an ordered window (no wave
     barriers), duplicate checks with the `fact_consolidation` skill and per-issue advisory locks with a bounded
     recheck, tombstones (rejected matches block re-creation), `REFINE`, and confirmed-fact suggestions with
     accept and dismiss.
   - **5b (done):** final duplicate sweep that only suggests merges (no automatic merging, by decision);
     accepting a merge suggestion merges with both facts' lock versions and is undoable; dismissed pairs are not
     suggested again. Launch estimate that reuses the freeze's population resolution.
6. **Matter tabs and detail pages.** Task authoring in Analysis. The **Facts** tab (List sub-tab) with live
   review, facets and bulk actions. The fact detail page. The **Entities** tab (Directory and Proposed) and the
   entity detail page. The run job type in Jobs. Overview tiles.
7. **Facts → Timeline sub-tab, export UI, and user documentation.**
7b. **Provider Batch API (done).** Opt-in per run when the extraction model has a batch adapter: one provider batch per chunk, durable polling, results applied in manifest order with real-time fallback, and pending batches canceled with the run. Documents in one chunk recall only earlier chunks' facts; consolidation catches duplicates within a chunk.
8. **Evaluation** on a closed matter (the Enron sample) to measure duplicate rate, quote validity, reviewer accept
   and reject rate, entity resolution accuracy, timeline coverage of known key events, cost per 1k documents, and
   the effect of recall on duplicate rate.
9. **Document integration.**
   - Project `fact_state` into the document index: additive in-place mapping plus backfill, rebuild derivation,
     and upserts only on state change.
   - Add the "Has facts" facet to Search & Review.
   - Add the read-only **Facts** tab in both document detail panels and the per-document facts endpoint.
   - *Acceptance:* confirming the first fact on a document moves it from Unconfirmed to Confirmed after projection.
     Rejecting its last fact clears it. A full rebuild reproduces the same values from Core. The Facts tab reflects
     changes immediately.
10. **Later:** entity relationships with time validity, saved fact views, thread suppression, and creating or
    adding evidence from a text selection in the document panel.

## Tests to add (highlights)

- Specification validation: an unknown or inactive field, a deactivated enum value, an unsupported type, a stale
  guidance excerpt hash, duplicate issue keys, an unpublished guidance revision refused as a pin.
- Issue membership for each field type (enum including hierarchical descendants, boolean, text, multi-value), from
  both matter coding and a pinned reviewer-coding revision. Later reviewer edits don't change a prepared run.
- Entities:
  - email beats name;
  - a new name or email for a known entity becomes a variant;
  - the unique email index blocks duplicates under concurrency;
  - merge moves variants and never creates chains;
  - unmerge restores the facts' canonical links;
  - rejecting an entity clears its fact links.
- Facets: self-exclusion, fact-count semantics for nested entity and issue facets, histogram buckets, export
  equal to the filtered result.
- Quote validation: exact match, whitespace and Unicode edge cases, an unknown paragraph, a quote spanning
  paragraphs.
- The command service: event plus projection plus audit plus search-upsert atomicity, and rejection on a stale
  `lock_version`.
- Agent operations against confirmed and rejected facts produce suggestions or blocks and never mutate.
- Consolidator: deterministic application order, idempotent replay of a wave, merge undo.
- Default ordering is oldest first, with stable tie-breaks and undated facts last, in search, timeline and export.
- The timeline shows only confirmed facts, updates on confirm, reject and merge, and orders correctly by date
  precision, with an undated lane.
- Authorization: every endpoint refuses users without the required permission. `fact.view` and `fact.export`
  also require `document.view`. A read-only grant can read but not mutate.
- Workflow: a document failure doesn't fail the run, cancel and resume work, and progress counts are exact.

## Risks and open questions

1. **Duplicate quality is the main product risk.** Slice 8 metrics gate any scale-up beyond pilot matters.
2. **Mapping quality.** Guidance is free-form Markdown, so the compiler can mis-map an issue to a field or value.
   Human review of the catalog before publishing, plus the issue-membership counts shown at launch, are the
   safeguards.
3. **Reviewer volume.** At this scale reviewers cannot read every working fact. The default sort is chronological,
   oldest first (decided). Reviewers narrow the queue with filters and facets (status, issue, evidence count,
   contradictions) rather than relying on a priority ranking.
4. **Role assignments.** Export is decided (administrators only). Still to confirm: `fact.review` and
   `entity.manage` for Matter Administrator and Reviewer, and view-only access for Analysis Operator and Read-only.
5. **`AGENTS.md` update.** Slice 1 updates it once this plan is approved. See the alignment section.
