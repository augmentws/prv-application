# Multiple Matter Definition review guidance plan

## Status

Implemented on 2026-09-29.

The implementation preserves the specialized Matter Definition assessment workflow while adding independently
versioned Review Guidance profiles, profile-bound chats and assessments, clone provenance, archive/restore behavior,
collection-based APIs, compatibility routes for the migrated `general_review` profile, and a collection workspace in
the Matter Definition tab. The assessment progress and provider-batching reliability work was completed separately
before this feature was enabled.

## Objective

A matter needs different reviewer guidance at different stages of review. Typical examples include:

- First Pass Review;
- Second Pass Review;
- Privilege Review; and
- General Counsel Review.

Replace the current one-Matter-Definition-per-matter assumption with a collection of independently versioned review
guidance profiles. Each profile must retain the existing specialized Matter Definition editing, assessment,
document-analysis, synthesis, and refinement workflows.

The collection and version-management experience should resemble Analysis Tasks, but Review Guidance must not be
implemented as a Question Answering task and must not be moved onto the JEV execution backend.

## Product terminology

The recommended product name for an individual item is **Review Guidance**. The existing **Matter Definition** page
becomes the place where the matter's Review Guidance profiles are managed.

This distinction reflects the product model:

- the matter is the shared parent context;
- a Review Guidance profile contains the instructions for one review purpose or stage; and
- a Question Answering Analysis Task is a separate executable task that generates and runs a Decision Specification
  through JEV.

The database may retain the existing `matter_definition` names during the first implementation to reduce migration
risk. Product and API terminology should nevertheless make the collection semantics clear.

## Design principles

### Review Guidance is a first-class domain object

Privilege Review and First Pass Review are records, not hard-coded task types. A matter may require other guidance
profiles, so the application must not limit the collection to a fixed enum.

### Every profile is independently governed

Each profile has its own:

- stable key, name, and description;
- active or archived lifecycle;
- current revision;
- published revision;
- assessment history;
- refinement questions and resulting drafts; and
- frozen workflow provenance.

Publishing or editing one profile must not change another profile.

### A revision is complete and self-contained

The first release should not implement inheritance between guidance profiles. Each published revision is the full
guidance used by a reviewer or workflow. This avoids hidden composition and guarantees that a historical run can be
reproduced from one immutable revision.

Users can reuse guidance through an explicit clone action. Shared fragments or dependency composition can be
considered later if actual duplication becomes unmanageable.

### Runs pin exact inputs

Every assessment and review batch must identify the exact guidance profile, immutable revision, content hash,
workflow bindings, and search-index generation used for the run. Later edits or archival must not change historical
results.

### Specialized workflows remain specialized

Review Guidance continues to use the Matter Definition workflow:

```text
Guidance revision
        ↓
Diagnostic retrieval plan
        ↓
Frozen review batch
        ↓
Document analysis
        ↓
Assessment synthesis and clarification questions
        ↓
Optional refined guidance draft
```

The only concept shared with Analysis Tasks is the management pattern: a named collection, independent version
histories, drafts, publication, and explicit selection for execution.

## Target domain model

```text
Matter
├── Review Guidance: First Pass Review
│   ├── Revision 1 — retired
│   ├── Revision 2 — published
│   └── Revision 3 — draft
│       └── Assessment runs
├── Review Guidance: Privilege Review
│   └── Revision 1 — published
│       └── Assessment runs
├── Review Guidance: Second Pass Review
│   └── Revision 1 — published
└── Review Guidance: General Counsel Review
    └── Revision 1 — draft
```

### Review Guidance profile

Generalize the existing `MatterDefinition` record with:

- `id`;
- `matter_id`;
- `key`, unique within the matter;
- `name`;
- optional `description`;
- `status`: `ACTIVE` or `ARCHIVED`;
- `current_revision`;
- optional `published_revision`;
- creator and timestamps.

Replace the current unique constraint on `matter_id` with a unique constraint on `(matter_id, key)`.

An optional `is_default` field may be used temporarily while legacy singular routes are supported. New workflows
must not silently choose a default when more than one active profile exists.

### Guidance revision

The current `MatterDefinitionRevision` structure can remain substantially unchanged. Revision numbers are scoped to
their parent guidance profile. Preserve:

- immutable Markdown content;
- source kind and source provenance;
- source artifact and filename;
- based-on revision;
- creating user and agent or skill provenance; and
- creation timestamp.

### Assessments

`MatterDefinitionAssessmentRun` already pins `matter_definition_revision_id`. Keep that immutable foreign key and
expose the parent guidance identity in API responses.

For efficient filtering and clearer invariants, either:

1. join through the revision when listing assessments for a guidance profile; or
2. add a denormalized `matter_definition_id` foreign key and enforce that the selected revision belongs to it.

The first option is the preferred initial implementation because the revision is already authoritative.

The refined revision must belong to the same guidance profile as the source revision. Enforce this in the domain
service and cover it with a database-backed test.

### Conversations

Matter Definition Setup conversations currently operate at matter scope and assume one definition. Add an optional
guidance-profile foreign key to applicable conversations, or snapshot the selected guidance ID in a strongly
validated conversation scope record.

A conversation that edits existing guidance must be bound to one profile. A create-guidance conversation may begin
without a profile and acquire the new profile ID as part of an approved creation action.

## API design

Introduce collection-based routes:

```text
GET    /v1/matters/{matter_id}/guidance
POST   /v1/matters/{matter_id}/guidance

GET    /v1/matters/{matter_id}/guidance/{guidance_id}
PATCH  /v1/matters/{matter_id}/guidance/{guidance_id}
POST   /v1/matters/{matter_id}/guidance/{guidance_id}/archive
POST   /v1/matters/{matter_id}/guidance/{guidance_id}/restore
POST   /v1/matters/{matter_id}/guidance/{guidance_id}/clone

GET    /v1/matters/{matter_id}/guidance/{guidance_id}/revisions
POST   /v1/matters/{matter_id}/guidance/{guidance_id}/revisions
POST   /v1/matters/{matter_id}/guidance/{guidance_id}/revisions/{revision}/publish

GET    /v1/matters/{matter_id}/guidance/{guidance_id}/assessments
POST   /v1/matters/{matter_id}/guidance/{guidance_id}/assessments
```

The create operation accepts:

- key;
- name;
- description;
- initial Markdown; or
- an optional source guidance revision to clone.

The clone operation creates a new profile with revision 1. It records the source guidance ID, source revision ID,
and source content hash in provenance. It does not create a live dependency between the profiles.

### Compatibility routes

Temporarily retain the current singular routes:

```text
/v1/matters/{matter_id}/definition
/v1/matters/{matter_id}/definition/revisions
```

During migration they resolve only to the migrated default profile. If a matter has multiple profiles and no
unambiguous default, mutation through a singular route must fail instead of selecting an arbitrary profile.

Remove singular-route usage from the frontend and agent tools before deleting the compatibility layer.

## Workflow changes

### Assessment launch

Assessment launch must require an explicit guidance profile and revision. The workflow input snapshot should include:

```json
{
  "guidance_id": "uuid",
  "guidance_key": "privilege_review",
  "guidance_revision_id": "uuid",
  "revision": 3,
  "content_hash": "sha256"
}
```

The workflow continues to pin its resolved skill bindings, models, configuration, physical search-index generation,
and definition content hash.

### Assessment execution

Retrieval planning, candidate selection, provider batching, document analysis, and synthesis continue to use the
selected immutable revision. Workflow and DBOS IDs may continue to use the assessment UUID because it is globally
unique.

Cache identities should include the guidance profile ID as well as the content hash and revision ID. The content
hash remains the semantic input identity; the profile ID prevents accidental cross-profile provenance ambiguity.

### Refinement

Guidance refinement must:

1. load the source revision pinned by the assessment;
2. resolve the source revision's parent profile;
3. apply the answered clarification decisions;
4. append a new draft to that same profile; and
5. preserve all unrelated content.

It must never append to the currently selected UI profile or a matter-level default.

If another draft was created after the assessed revision, the refinement operation should still create a new draft
based on the assessed revision and clearly mark the branch point. It must not silently merge concurrent edits.

### Agent tools

Update Matter Definition tools to accept or return explicit guidance identity:

- list guidance profiles;
- create guidance profile;
- read one profile and revision;
- apply a draft edit to one profile;
- publish one revision;
- start an assessment for one profile; and
- read assessment and refinement results.

All mutation tools retain the current approval boundary.

## User experience

The Matter Definition tab becomes a two-pane Review Guidance workspace similar to the useful organizational parts
of Analysis Tasks.

### Guidance list

The left pane shows:

- name;
- optional purpose description;
- current revision;
- published revision;
- draft, published, or archived state; and
- assessment activity where relevant.

It provides actions to create, clone, rename, archive, and restore guidance.

### Guidance workspace

The right pane shows the selected profile's:

- name, key, description, and status;
- revision selector;
- Markdown editor;
- live character-level revision diff;
- draft and publication controls;
- assessment controls;
- assessment history and progress; and
- refinement questions and generated drafts.

The current assessment panel becomes scoped to the selected guidance profile. Switching profiles changes both the
revision history and assessment history.

### New guidance flow

The creation dialog supports:

- start with blank guidance;
- paste or upload initial guidance; and
- clone an existing guidance profile or historical revision.

Examples such as First Pass Review, Privilege Review, Second Pass Review, and General Counsel Review may be offered
as name suggestions, not fixed types.

### Review and batch surfaces

Assessment-generated review batches display:

- guidance name;
- guidance revision;
- published or draft state at launch; and
- assessment name.

Historical links continue to open the frozen batch even if the guidance profile has since been archived.

## Assessment progress prerequisite

Before enabling more independently running guidance assessments, correct the current assessment transaction design:

- do not hold a shared `WorkflowStepRun` row lock during remote model or artifact operations;
- do not keep one transaction open across an entire provider-batch finalization;
- commit document results idempotently and incrementally;
- aggregate progress without every worker updating the same locked row;
- refresh visible counts independently of the order in which child workflow handles are awaited; and
- test concurrent real-time documents and provider-batch finalizers together.

This is a prerequisite because multiple guidance profiles increase the likelihood of concurrent assessments.

## Migration plan

### Additive schema migration

1. Add `key`, `name`, `description`, and `status` to `matter_definition`.
2. Backfill the existing singleton as:
   - key: `general_review`;
   - name: `General Review Guidance`;
   - description explaining that it is the migrated Matter Definition; and
   - status: `ACTIVE`.
3. Replace the unique constraint on `matter_id` with `(matter_id, key)`.
4. Add indexes for `(matter_id, status)` and profile lookup.
5. Preserve every existing definition ID and revision ID.

The migration must not guess that an existing definition is First Pass, Privilege, or another stage. The user can
rename the migrated profile after deployment.

### Historical records

Existing assessment, question, candidate, review-batch, skill-run, and artifact records remain linked through their
current immutable revision IDs.

Backfill guidance identity into workflow snapshots only where it can be derived unambiguously from the pinned
revision. Do not rewrite content hashes, revision IDs, or immutable output artifacts.

Existing Matter Definition Setup conversations should be associated with the migrated profile when their matter has
exactly one profile. Ambiguous conversations remain readable but require explicit profile selection before another
edit or assessment action.

### Rollout sequence

1. Deploy the additive schema and compatibility readers.
2. Backfill the existing singleton records.
3. Deploy collection APIs and domain services.
4. Update workflows and agent tools to require explicit guidance identity.
5. Deploy the collection-based UI.
6. Monitor legacy singular-route usage.
7. Remove legacy frontend and agent callers.
8. Remove compatibility routes in a later release.

## Delivery slices

### Slice 0: assessment reliability

- Remove the shared progress-row lock bottleneck.
- Add incremental progress commits.
- Add concurrency and recovery tests.

### Slice 1: domain and migration

- Generalize `MatterDefinition` to a matter-scoped collection.
- Backfill the existing definition safely.
- Add list, create, update, clone, archive, and restore services.
- Preserve compatibility reads.

### Slice 2: revision and workflow scoping

- Add nested revision and publication APIs.
- Require explicit guidance identity for assessments and agent tools.
- Enforce same-profile refinement.
- Update workflow snapshots, audit records, and cache identities.

### Slice 3: collection UI

- Add the guidance list and selected-guidance workspace.
- Scope editor, diff, assessment history, and refinement UI.
- Add create and clone flows.
- Surface guidance provenance in review batches.

### Slice 4: compatibility cleanup

- Remove singular-route usage.
- Remove temporary default-profile behavior.
- Update documentation and help content.

## Authorization and auditing

Retain the existing matter-administrator authorization requirement for guidance creation, editing, publication,
archival, and assessments.

Audit events should include `matter_id`, `guidance_id`, `guidance_key`, revision number, revision ID, and relevant
workflow or assessment IDs. At minimum, record:

- guidance created, renamed, cloned, archived, and restored;
- revision created and published;
- assessment launched, retried, regenerated, canceled, and completed; and
- refined draft created from answered assessment questions.

## Testing strategy

### Domain and database tests

- Multiple active profiles can exist for one matter.
- Keys are unique only within a matter.
- Revision numbers are independent per profile.
- Publishing one profile does not retire another profile's published revision.
- Archived profiles reject new revisions and assessments but retain readable history.
- A refinement cannot create a revision under another profile.
- Clone provenance is immutable and does not create a live dependency.

### API tests

- List, create, read, update, clone, archive, restore, revise, and publish.
- Cross-matter guidance IDs return not found and never leak data.
- Assessment launch rejects a revision belonging to another profile.
- Legacy singular routes behave deterministically during migration.

### Workflow tests

- Every workflow snapshot contains guidance and revision identity.
- Retry and regeneration retain the original pinned guidance revision.
- Refinement appends to the correct profile even when another profile is selected in the browser.
- Archived guidance does not invalidate an existing or historical run.
- Concurrent assessments for different profiles do not contend on shared progress rows.

### Frontend tests

- Selecting a profile changes the visible revision and assessment history.
- Creating or cloning selects the new profile.
- Unsaved edits cannot leak across profile selection.
- Diff compares revisions only within one profile.
- Assessment launch sends the selected profile and revision.
- Historical batches show the correct guidance name and version.

### Migration tests

- A pre-migration matter with one definition becomes one `general_review` profile.
- Existing revision IDs and assessment references are unchanged.
- Existing refined-revision references remain valid.
- Migration is safe for matters with no definition.
- The migration fails clearly rather than guessing when inconsistent legacy data is detected.

## Acceptance criteria

The feature is complete when:

1. A matter can contain First Pass, Second Pass, Privilege, and General Counsel guidance simultaneously.
2. Each profile has independent draft, revision, and publication state.
3. Editing or publishing one profile does not modify another.
4. Assessment launch requires an explicit profile and revision.
5. Assessment retrieval, batching, synthesis, questions, and refinement use the selected profile's specialized
   workflow.
6. A refined draft is created under the same profile as its source assessment.
7. Historical runs display and retain the exact profile and revision used.
8. Archived profiles remain available for history but cannot start new work.
9. Generic Question Answering Analysis Tasks remain unchanged.
10. Large concurrent assessments report incremental progress without the current shared-row lock bottleneck.

## Non-goals for the first release

- Converting Review Guidance into Question Answering tasks.
- Running Review Guidance through JEV.
- A fixed enum of permitted review stages.
- Automatic inheritance or composition between guidance profiles.
- Automatically selecting a profile based on a document or batch.
- Automatically publishing an assessment-generated refinement.
- Deleting historical profiles, revisions, assessments, or artifacts.

## Open follow-up decisions

These decisions can be made during implementation without changing the core architecture:

- whether the UI should retain the page label **Matter Definition** or rename it **Review Guidance**;
- whether one profile should remain explicitly marked as the matter's default;
- whether batches created outside an assessment may optionally bind a guidance profile; and
- whether a later release should introduce shared, versioned guidance fragments.
