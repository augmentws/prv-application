# Review Program Demo and Replay Plan

Status: proposed

First-cut implementation:

- The Review Program workspace exposes **Present run** when the selected run has recorded stage attempts.
- Replay is URL-addressable with `view=replay`, `run`, `stage`, and `attempt` parameters.
- The initial UI reads existing stage attempts and actions, presents recorded choices and rationale, and provides
  read-only stage/attempt navigation.
- Each Definition Assessment attempt loads its retained questions and recorded answers or dispositions from the
  assessment record. When the assessment produced a refined Matter Definition revision, replay links to an exact
  historical before-and-after diff using the assessed and refined revision IDs. The comparison opens in a new tab
  so closing it leaves the presenter on the same replay stage and attempt.
- Q&A task setup replay distinguishes the coordinator stage attempt from task-version revisions. It shows both the
  task originally published during Stage 5 and any later version repinned into the program, including their coding
  fields, and clearly marks the final task version actually used by downstream review.
- The initial UI does not yet create immutable `review_program_stage_snapshot` records or backfill historical child
  references. Those remain Phase 1 of the durable replay projection.

Related plans:

- `docs/first-pass-review-program-ux-plan.md`
- `docs/first-pass-review-program-automation-plan.md`
- `docs/analysis-task-batch-sets-plan.md`

## 1. Product outcome

Allow an authorized user to complete a long-running Review Program once and then present that run from beginning to
end without rerunning retrieval, Q&A, training, scoring, or QC. The presenter can revisit every stage, round, failed
attempt, supporting report, and human decision in the state in which it originally occurred.

The completed run remains immutable. Demo mode is a read-only projection of durable Review Program records, not a
second workflow engine and not a way to reopen resolved actions.

The user-facing name should be **Present run** or **Run replay**. The interface may use a **Demo replay** badge, but
should not imply that the underlying matter data or results are synthetic.

The experience must answer these questions at every stage:

1. What was the program trying to accomplish here?
2. What inputs, evidence, and results were available at that time?
3. What decision was presented to the user?
4. Which choice was recorded, by whom, and why?
5. What did that decision cause the program to do next?

## 2. Scope

### 2.1 Initial release

The initial release supports:

- replaying a completed, stopped, or failed Review Program run;
- replaying completed portions of an active run without affecting the live stage;
- moving forward and backward through stages;
- selecting a particular attempt or round when a stage ran more than once;
- showing the choices originally offered and clearly identifying the recorded choice;
- showing the evidence and metrics that supported the decision at that time;
- linking to the exact Matter Definition revision, task version, batch, model, scoring run, report, and job records;
- deep-linking to a particular replay stage and attempt; and
- presenting incomplete historical data honestly when an old run lacks a replay field or artifact.

### 2.2 Explicitly out of scope for the initial release

The initial release does not:

- resolve or reopen a production action;
- change a Review Program run, child record, coding, classifier, or Matter Definition;
- rerun an agent, task, classifier, or durable workflow;
- pretend that an alternate choice would have produced the recorded downstream results;
- create fake job progress; or
- bypass the normal authorization rules for linked records.

### 2.3 Later extension: disposable decision scenarios

A later release may add **Try another choice**. This creates a disposable demonstration scenario that records only
the presenter’s hypothetical selections. It does not mutate the source run. Unless a separate sandbox execution is
explicitly launched, the UI must stop at the first point where an alternate decision would require new analysis and
say that downstream results are unavailable for that hypothetical path.

## 3. Experience design

### 3.1 Entry points

Show **Present run**:

- on a completed run summary;
- in run history for completed, stopped, and failed runs; and
- in the active workspace after at least one stage has a replayable snapshot.

The live workspace remains the default for an active run. Entering replay never changes the program’s current run or
stage.

Recommended URL state:

```text
/app/clients/:clientId/matters/:matterId/review-programs
  ?program=:programId
  &run=:runId
  &view=replay
  &stage=:stageKey
  &attempt=:attemptId
```

The URL makes a presentation position bookmarkable and allows browser Back and Forward navigation. No persistent
demo-session record is required for read-only replay.

### 3.2 Replay shell

The replay shell contains:

- a fixed **Demo replay · read only** banner;
- program name, source run, run status, start time, completion time, and template version;
- a five-phase rail matching the normal Review Program workspace;
- Previous and Next controls;
- a stage picker for direct navigation;
- an attempt or round picker when a stage has multiple attempts;
- a progress label such as `Stage 7 of 18`; and
- **Return to live workspace**.

The stage rail uses historical status rather than the live run’s current-stage styling. Completed, failed, canceled,
and skipped attempts must remain distinguishable.

### 3.3 Stage presentation

Every replay stage uses the same structure:

1. **Purpose** — the stage explanation from the pinned workflow template.
2. **What went in** — frozen configuration, source versions, population, and policy values.
3. **What happened** — concise result and operational status.
4. **Evidence at the time** — issue-level statistics, agreement, quality, counts, limitations, and representative
   links relevant to that stage.
5. **Decision** — the original question, all choices, their consequences, the recorded choice, actor, time, and
   reason.
6. **What happened next** — the recorded transition and child work created by the choice.
7. **Supporting records** — links to authoritative definitions, tasks, Batch Sets, Review Batches, jobs, classifiers,
   scoring results, reports, and audit history.

The original controls may be rendered to make the decision experience understandable, but they are disabled. The
recorded option uses **Selected in this run** rather than appearing as an actionable primary button.

### 3.4 Multiple attempts and rounds

Default to the final successful attempt for each stage. If no attempt succeeded, default to the last attempt.

The attempt picker shows:

- attempt or round number;
- status;
- start and completion time;
- failure or cancellation summary; and
- whether a later attempt superseded it.

Reassessments, supplemental discovery rounds, retraining, and failed/retried work must not be collapsed into a
single synthetic result. The replay should make the evolution of the program visible.

### 3.5 Presenter controls

Add a compact presenter toolbar:

- **Start presentation** — opens the first replayable stage;
- **Previous** and **Next**;
- **Show decision** — scrolls to or reveals the decision card;
- **Show details** — toggles technical provenance and job metadata;
- **Copy link to this stage**; and
- **Exit replay**.

Do not add an automatic timed slideshow in the initial release. The legal-review decisions need explanation and
should advance under presenter control.

## 4. Replay data contract

### 4.1 Existing durable sources

The current architecture already retains most source facts:

| Record | Replay use |
| --- | --- |
| `ReviewProgramRun` | Run identity, terminal status, rounds, current/final summary, and pinned versions. |
| `ReviewProgramStageAttempt` | Stage order, attempt status, input snapshot, policy hashes, result summary, and errors. |
| `ReviewProgramAction` | Original question, choices, context, recorded resolution, actor, and time. |
| `ReviewProgramVersion` | Configuration and source hashes accepted at program milestones. |
| `ReviewProgramChild` | Exact tasks, batches, jobs, models, scoring runs, and reports produced by an attempt. |
| `ReviewProgramEvent` | Durable transition and reconciliation history. |
| Completion report and artifacts | Final findings and provenance. |

The current `progress_summary` is useful for the live workspace but is not sufficient as the sole replay source. It
is mutable, represents the latest aggregate state, and may no longer describe what the user saw during an earlier
decision.

### 4.2 Immutable stage presentation snapshot

Add `review_program_stage_snapshot`, with one immutable row per stage attempt once the attempt reaches a replayable
checkpoint.

Recommended fields:

```text
id
tenant_id
matter_id
program_id
run_id
stage_attempt_id           unique
program_version_id
schema_version
stage_key
stage_ordinal
attempt
template_key
template_version
snapshot_status            READY | PARTIAL
presentation               JSON document
content_hash
frozen_at
created_at
```

The `presentation` document contains only replay-oriented summaries and references:

```text
purpose
input_summary
result_summary
metrics
limitations
action_snapshot
transition_snapshot
child_references
artifact_references
completeness_warnings
```

The snapshot references document, batch, model, and artifact identifiers; it does not duplicate document bodies,
model vectors, predictions, or complete job logs.

### 4.3 Snapshot timing

Create or finalize the snapshot deterministically when:

- a human action is resolved;
- a stage completes without a human action;
- a stage fails or is canceled; and
- a run completes or is stopped.

Snapshot creation belongs in the same transaction that records the durable transition whenever practical. Repeated
projection of the same stage attempt must produce the same content hash and must not create a second snapshot.

Agent-generated prose is not required to build a replay snapshot. Counts and state come from deterministic services;
the snapshot reuses findings already accepted or generated by the source workflow.

### 4.4 Historical-run backfill

Provide an idempotent backfill service for runs completed before stage snapshots existed. It projects snapshots from
stage attempts, actions, versions, children, events, and completion artifacts.

Backfill rules:

- never alter the source run;
- never invoke a model or start workflow work;
- mark a stage `PARTIAL` when a historical field cannot be reconstructed;
- include a human-readable completeness warning;
- continue building other stages when one child artifact is missing; and
- retain a stable content hash for the facts that are available.

Backfill may run on demand when an authorized manager chooses **Prepare replay**, but a `GET` request must never
perform writes. New and recently completed runs should be snapshotted automatically.

## 5. API design

### 5.1 Read endpoints

Add:

```text
GET /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/replay
GET /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/replay/stages/{stage_attempt_id}
```

The first response is the replay manifest:

- source run identity and status;
- pinned template identity;
- replay status: `READY`, `PARTIAL`, or `UNAVAILABLE`;
- ordered stages and attempts;
- default attempt per stage;
- phase grouping;
- completeness warnings; and
- Previous/Next ordering.

The second response contains the selected immutable stage presentation. Keeping stage payloads separate avoids
loading every report and child reference before the presenter needs it.

### 5.2 Preparation endpoint

Add a manager-only, idempotent command for historical runs:

```text
POST /v1/matters/{matter_id}/review-programs/{program_id}/runs/{run_id}/replay/prepare
```

Return the replay manifest when preparation is synchronous and small. If preparation later becomes a durable job,
return a normal job reference and expose its progress through the existing jobs and notification backbone.

### 5.3 Response types

Introduce explicit contracts such as:

- `ReviewProgramReplayRead`;
- `ReviewProgramReplayStageSummaryRead`;
- `ReviewProgramReplayStageRead`;
- `ReviewProgramReplayActionRead`;
- `ReviewProgramReplayChildReferenceRead`; and
- `ReviewProgramReplayWarningRead`.

Do not expose raw table-shaped JSON and ask the frontend to infer stage meaning. The backend should normalize durable
records into a versioned replay contract.

## 6. Authorization, safety, and audit

- Viewing replay requires `review_program.read` for the matter.
- Preparing historical snapshots requires `review_program.manage`.
- Replay does not grant access to a linked document, batch, model, or job that the user could not otherwise view.
- Restricted links render as **Access required** rather than leaking names or counts.
- Replay GET endpoints are read-only and produce no events, notifications, jobs, or audit mutations.
- Snapshot preparation records an audit entry containing the source run, schema version, content hashes, warnings,
  and initiating user.
- Every replay screen displays the source run ID and a read-only indicator.
- Production action endpoints continue to reject resolved actions; replay never calls those endpoints.

## 7. Child links and historical availability

Replay must resolve children by the exact IDs recorded in `ReviewProgramChild`. It must not substitute the current
Matter Definition, latest task version, latest classifier, or newest batch.

Archived child records remain valid provenance. Add or adjust read endpoints so an authorized replay can inspect an
archived child by explicit ID even when ordinary list views exclude it. Missing MinIO artifacts or retired search
indexes make the relevant supporting link unavailable but do not make the entire replay fail.

Each child reference reports:

- type and purpose;
- stable ID;
- recorded status at the stage checkpoint;
- current availability;
- display label;
- applicable matter route; and
- optional warning when current state differs from recorded state.

## 8. Template reuse

Replay is a Review Program platform feature, not a First Pass-specific page. The pinned Review Workflow Template
provides:

- phase and stage order;
- stage names and explanations;
- presentation hints;
- important metric keys;
- expected decisions; and
- supporting child purposes.

First Pass Review is the first implementation. Second Pass Review and later programs use the same replay shell and
stage snapshot contract.

Template presentation hints must be declarative. They may choose standard cards and metric layouts, but must not
embed executable UI code or mutate transition logic.

## 9. Disposable decision scenarios

After read-only replay is stable, add an optional scenario layer.

### 9.1 Behavior

- **Try another choice** starts a scenario from a replay stage.
- The source run and snapshot IDs remain visible.
- Choices are stored separately from production actions.
- The scenario can follow template transitions only while no new analysis is required.
- At the first analysis-dependent transition, the scenario displays **New work would be required here**.
- The user may reset or discard the scenario at any time.

### 9.2 Persistence

Start with browser-session state unless users need to share scenarios. If sharing is required, add a separate
`review_program_demo_scenario` aggregate. Never encode hypothetical choices in `ReviewProgramAction`,
`ReviewProgramEvent`, or `ReviewProgramVersion`.

### 9.3 Sandbox execution

A future **Fork as sandbox run** may execute alternate choices. It must be an explicit new Review Program run with a
separate identity, visible cost warning, normal permissions, and normal durable jobs. It is not part of replay.

## 10. Delivery phases

### Phase 0 — Replay completeness inventory

For every First Pass stage, map:

- the stage attempt inputs and results currently retained;
- actions and decision context;
- child purposes and stable routes;
- metrics required for presentation;
- missing historical fields; and
- whether those fields can be backfilled.

Exit criteria:

- a stage-by-stage completeness matrix exists;
- required snapshot fields are agreed; and
- no replay screen depends solely on mutable `progress_summary` state.

### Phase 1 — Immutable replay projection

- Add the stage snapshot model and migration.
- Implement versioned deterministic snapshot builders.
- Snapshot new stage checkpoints.
- Implement historical backfill with partial-stage warnings.
- Add replay manifest and stage endpoints.

Exit criteria:

- a completed First Pass run produces ordered replay stages;
- repeated preparation is idempotent;
- changing live aggregate state does not change an existing snapshot; and
- no model or workflow job is invoked.

### Phase 2 — Read-only replay UI

- Add **Present run** entry points.
- Add URL-addressable replay state.
- Implement the phase rail, attempt picker, Previous/Next controls, and replay banner.
- Render the standard Purpose, Inputs, Results, Decision, Next step, and Supporting records sections.
- Add the return-to-live path.

Exit criteria:

- a presenter can traverse a completed run without waiting for work;
- refresh and browser navigation preserve the selected stage;
- no production action control is enabled; and
- archived and restricted child states are represented correctly.

### Phase 3 — First Pass presentation cards

Add stage-specific cards for:

- Matter Definition assessment and refinement;
- dual-profile Discovery Q&A progress and agreement;
- low-support issue decisions;
- disagreement resolution and final training-label counts;
- classifier quality and acceptance;
- whole-collection scoring;
- classifier-positive Q&A coverage;
- final alignment;
- Negative-population QC; and
- completion findings.

Exit criteria:

- every substantive First Pass decision shows the evidence that was available when it was made; and
- issue-level findings appear before aggregate metrics.

### Phase 4 — Presenter polish and documentation

- Add presenter toolbar and compact technical-details mode.
- Add copy-link support.
- Document preparation, partial replay, permissions, and retention behavior.
- Add a curated internal demo checklist using one completed run.

Exit criteria:

- a non-developer can conduct the complete demonstration from the UI; and
- operational identifiers are available but not required for the presentation.

### Phase 5 — Optional decision scenarios

- Add disposable alternate-choice state.
- Clearly separate recorded and hypothetical paths.
- Stop simulation when new analysis would be required.
- Evaluate whether shareable scenarios or explicit sandbox-run forks are needed.

Exit criteria:

- hypothetical choices cannot mutate or be mistaken for the source run; and
- no downstream result is presented as real unless it came from an explicitly executed sandbox run.

## 11. Testing strategy

### 11.1 Backend

- Snapshot generation is idempotent and content hashes are stable.
- Stage and attempt ordering follows the pinned template.
- Resolved choices, actor, reason, and resolution time are preserved.
- Later program activity cannot alter a frozen snapshot.
- Multiple attempts remain separately addressable.
- Backfill reports partial data instead of failing the entire replay.
- Missing artifacts and archived children do not abort replay generation.
- Cross-matter and cross-tenant identifiers are rejected.
- Replay reads cause no database writes or workflow dispatches.

### 11.2 Frontend

- Completed, failed, stopped, and partially completed runs expose the correct entry state.
- Previous, Next, stage selection, and attempt selection update the URL.
- Reloading a deep link restores the same stage.
- Recorded choices are visible and disabled.
- No mutation request is issued from replay controls.
- Partial stages show warnings without hiding available evidence.
- Restricted supporting records do not leak details.
- Mobile navigation can open the phase rail and exit replay.

### 11.3 End-to-end

Use one completed First Pass fixture containing:

- a reassessment or retry;
- a model disagreement decision;
- classifier training and acceptance;
- whole-matter scoring;
- classifier-positive Q&A coverage;
- Negative-population QC; and
- a completion report.

Verify the entire presentation without provider credentials, workers, OpenSearch, or model calls.

## 12. Rollout and observability

- Gate the initial UI with `REVIEW_PROGRAM_REPLAY_ENABLED`.
- Enable replay for internal completed runs first.
- Track replay preparation status and completeness warnings.
- Log stage snapshot failures with run and attempt IDs, without logging document content.
- Add counts for replay opens, stages viewed, partial snapshots, missing child records, and preparation failures.
- Do not count replay navigation as Review Program execution or model usage.

## 13. Recommended first implementation slice

The first useful slice should be deliberately narrow:

1. Inventory the existing completed First Pass run used for demonstrations.
2. Build deterministic replay projections for its stage attempts and resolved actions.
3. Add the replay manifest and per-stage read endpoint.
4. Add **Present run**, a read-only banner, stage navigation, and recorded-choice cards.
5. Support links to existing child records and honest partial-data warnings.

This slice solves the immediate demonstration problem without introducing hypothetical branching, fake progress, or a
parallel workflow implementation.
