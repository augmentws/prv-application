# First Pass Review User Experience Plan

Status: proposed

Related plans:

- `docs/first-pass-review-program-automation-plan.md`
- `docs/matter-navigation-plan.md`
- `docs/matter-definition-assessment-plan.md`
- `docs/analysis-result-comparison-artifact-implementation-plan.md`
- `docs/statistical-classification-backend-implementation-plan.md`
- `docs/analysis-task-batch-sets-plan.md`
- `docs/classification-coding-projection-implementation-plan.md`

## 1. Product outcome

Present each first-pass review as an independent, durable matter workspace created from a reusable Review Workflow
Template. The guided flow starts with Matter Definition work, then creates and presents the required Analysis Tasks,
batches, comparisons, classifiers, and reports as the senior attorney advances through training-data development,
classifier review, collection-wide application, and no-code quality control.

The interface should answer four questions immediately:

1. Where is the review now?
2. What has the system learned or completed?
3. What decision or work is required from the user?
4. What will happen after that action?

The workspace is not a ten-step wizard and does not conceal the existing domain records. A first-pass review may run
for days, revisit an earlier phase, or wait for human review. The top-level experience therefore persists status,
decisions, progress, and accepted limitations while linking to the authoritative Matter Definition, task, batch,
comparison, reviewer-coding, classifier, scoring, and report views.

The primary presentation rule is:

> Show the legal-review decision first, the evidence supporting it second, and workflow mechanics only when the
> user asks for them.

## 2. Intended users

### 2.1 Senior review attorney

The senior attorney owns the substantive review strategy. The interface must help this user:

- refine the definition and issue boundaries;
- understand model agreement without treating agreement as truth;
- decide when an issue has enough evidence to train;
- choose how machine labels become a training set;
- assess per-issue classifier quality;
- authorize collection-wide work; and
- accept or remediate quality-control findings.

This user needs concise findings, consequences, representative documents, and defensible records. Internal workflow
identifiers and infrastructure state should remain available but secondary.

### 2.2 Review attorney

A review attorney may be assigned disagreement or QC batches. This user needs a focused document-review experience,
the current issue guidance, progress through the assigned batch, and a clear return path to the first-pass review.
They should not receive controls for classifier activation or program-wide policy unless separately authorized.

### 2.3 Review manager

The review manager monitors throughput, failures, usage, coverage, and assignments. This user needs operational
visibility without being invited to make legal decisions beyond their permissions.

### 2.4 System administrator

The administrator diagnoses configuration, provider, job, and artifact failures. Technical details should appear in
an expandable diagnostics area and link to existing job and audit views.

## 3. Experience principles

### 3.1 One review, many authoritative records

The First Pass Review workspace is the coordinating view. It summarizes and links to child records instead of
copying their editors or hiding them behind a proprietary report.

### 3.2 Five user-facing phases

Present the lifecycle in legal-review language:

1. Define the review.
2. Develop the training set.
3. Train and evaluate.
4. Review the collection.
5. Quality control and complete.

The backend may retain more detailed stages. Users should see those details in phase activity or diagnostics, not as
a twenty-step primary navigation control.

### 3.3 Decisions are first-class work

A waiting decision is not a generic paused state. It has a named question, an explanation, alternatives, measured
consequences, supporting evidence, and an authorized resolution.

### 3.4 Issue-level readiness before aggregate metrics

An acceptable average can conceal an unusable category. Primary reports show each issue's support, agreement,
quality, limitations, and next action. Aggregate measures appear as context.

### 3.5 Recommendations remain proposals

The agent may synthesize findings and recommend a next step. Deterministic services calculate counts, eligibility,
sampling, and state. The user authorizes legal and publication decisions through structured actions.

### 3.6 Progressive disclosure

The default view favors conclusions and next actions. Users can expand methodology, provenance, parameters, model
identity, hashes, costs, and job diagnostics without leaving the workspace.

### 3.7 Historical states remain inspectable

When refinement or remediation creates a new version, prior decisions, batches, reports, and model results remain
available in a round and version history. The interface never rewrites an earlier result to look current.

### 3.8 Reusable workflow templates

The workspace shell is reusable. A Review Workflow Template defines the user-facing phases, required artifacts,
agent proposals, human decisions, transition rules, defaults, and completion criteria. **First Pass Review** is one
template; **Second Pass Review** and later workflows use the same shell with their own phase content.

Each time a user chooses a template, the system creates an independent review program in the current matter. A
matter may therefore have several first-pass programs using different Matter Definitions, guidance, destination
fields, Q&A profiles, and classifiers. The program pins the template version used to create it, so later template
changes do not silently alter an active review.

### 3.9 Template-controlled batching

Each workflow template declares how every automated task population is batched. First Pass Review uses task-specific
Analysis Task Batch Sets for all automated Q&A work. The only ad hoc Review Batches it creates are Matter Definition
assessment batches and comparison-derived difference or disagreement batches for human review.

Future review-program templates use the same `AD_HOC_BATCH`, `PARTITIONED_SET`, and `WHOLE_MATTER` platform modes,
but must declare the permitted mode for every stage. The reusable workspace displays that policy and does not infer
batching from the program name.

## 4. Navigation and routes

Add **Review programs** to the existing **Review** group in the matter menu. Do not introduce a second persistent
sidebar. The list includes first-pass, second-pass, and future template-backed workflows.

Recommended routes:

```text
/app/clients/:clientId/matters/:matterId/review-programs
/app/clients/:clientId/matters/:matterId/review-programs/new
/app/clients/:clientId/matters/:matterId/review-programs/:programId
/app/clients/:clientId/matters/:matterId/review-programs/:programId/history
/app/clients/:clientId/matters/:matterId/review-programs/:programId/reports/:reportKey
```

The list route shows all template-backed reviews for the matter. The program route is the persistent workspace. Child
artifacts retain their existing routes. Every child page opened from the workspace should include a contextual link
back to the originating review program when a program reference is present.

Add `review-programs` to `MatterSection`, `MATTER_NAV`, `matterHref`, route parsing, breadcrumbs, permission filtering,
and the Search and Review matter menu. Use one route builder rather than constructing URLs inside components.

## 5. Review programs list

The list page establishes whether the matter has no programs, one or more active programs, or historical completed
programs. Multiple first-pass programs are normal and must not be collapsed into one matter-level status.

### 5.1 Empty state

Explain the workflow in one paragraph and show a single primary action:

> Start a guided review program from a reusable workflow template. Each program keeps its own guidance, fields,
> generated tasks, decisions, and results.

Primary action: **New review program**.

Do not require the user to understand program versions, model artifacts, or batch sets before starting.

### 5.2 Review table

Show:

- name;
- workflow template and pinned template version;
- overall status;
- current phase;
- current action or blocker;
- Matter Definition name and version used;
- destination field or fields when selected;
- active and excluded issue counts;
- accepted classifier, when available;
- last activity; and
- owner.

Primary row action is **Open review**. Secondary actions are **View history**, **Duplicate setup**, and **Archive**
when allowed. Destructive or terminal actions do not appear as casual row icons.

### 5.3 Status language

Map internal states to a compact user vocabulary:

| User-facing status | Meaning |
| --- | --- |
| Not started | Setup exists but execution has not begun. |
| In progress | Automated work or assigned review is active. |
| Needs decision | A senior reviewer must choose how to proceed. |
| Needs review | A document-review batch requires human coding. |
| Attention | A failure, stale configuration, or unresolved document requires intervention. |
| Complete | Completion criteria and QC decision are recorded. |
| Stopped | The user ended the review before completion. |

Use the detailed internal status only in diagnostics and audit views.

## 6. Guided creation and just-in-time setup

Creating a review program is intentionally short. The template guides the user through substantive configuration as
the information becomes relevant; it does not ask the user to assemble every Analysis Task, batch, classifier, and
QC policy before the Matter Definition has been refined.

### 6.1 Choose a workflow template

The template catalog initially presents **First Pass Review** and later presents **Second Pass Review** and other
approved templates. Each card explains:

- the workflow's purpose;
- the user-facing phases;
- the records it will create;
- required permissions;
- typical review responsibilities; and
- the published template version.

Selecting **First Pass Review** creates a program from that immutable template version.

### 6.2 Name and assign the program

Collect only:

- program name;
- owner;
- optional description; and
- optional reviewer or manager assignments.

Default the name from the matter and template, but require names to be distinguishable when the matter already has
another first-pass program.

### 6.3 Start with the Matter Definition

The first substantive screen asks the user to:

- create a new Matter Definition draft;
- fork an existing definition as a new program-specific starting point; or
- adopt an existing draft or published revision.

Forking copies the starting guidance but creates an independent lineage. Later changes in one review program do not
change another program's accepted definition. The new program is saved immediately and opens in the **Define the
review** phase.

Primary action: **Begin with this Matter Definition**.

### 6.4 Configure each phase when reached

After the definition is assessed, refined, and accepted, the template advances to coding and Q&A setup. At that
point—not during initial creation—the system guides the user through:

1. Selecting the destination field or creating the required field through the controlled metadata flow.
2. Mapping accepted issue criteria to stable destination values.
3. Generating all required Q&A Analysis Tasks from the accepted definition and template.
4. Selecting or confirming the two template-supported model profiles.
5. Reviewing questions, mappings, evidence requirements, warnings, and expected outputs.
6. Publishing the generated tasks after user approval.

The same pattern continues through the workflow:

- the retrieval stage proposes and creates task-specific batch sets;
- the comparison stage creates its comparison artifacts;
- the training stage creates label sets, classifier configuration, and builds;
- the collection stage creates scoring and Q&A coverage runs; and
- the QC stage creates the sampling manifest, task-specific batch set, runs, and report.

At every stage, the workspace explains what will be created, shows the proposed configuration, permits authorized
edits, and records approval before creating or launching the child work.

The First Pass Review template uses this guided creation map:

| Phase reached | System prepares | Batch mode | User reviews or decides |
| --- | --- | --- | --- |
| Define the review | Matter Definition assessment plan, diagnostic batch, assessment run, questions, and proposed guidance refinement | Ad hoc assessment batch | Starting definition, assessment findings, refinements, and accepted definition version |
| Develop the training set | Destination mappings, shared Q&A Decision Specification, required Q&A Analysis Task, two execution profiles, retrieval plan, discovery batch sets, and comparison | Task-specific partitioned sets; ad hoc difference batches only | Field and values, generated questions, model profiles, retrieval scope, issue sufficiency, and label policy |
| Train and evaluate | Resolved training-label artifact, classifier configuration, dataset preview, build, evaluation, and improvement retrieval plan | Task-specific sets for any supplemental Q&A | Label source, weak categories, additional review, classifier acceptance, and activation |
| Review the collection | Whole-collection Classification Analysis Task, scoring run, Q&A coverage manifest, Analysis Task Batch Set, and final comparison | Whole matter for classifier; task-specific sets for Q&A coverage; ad hoc difference review | Collection scope, prediction policy, uncovered classifier positives, Q&A coverage, and final disagreements |
| Quality control and complete | No-code sampling manifest, QC Analysis Task Batch Set, blind dual-Q&A runs, QC report, and completion artifact | Task-specific partitioned set; ad hoc difference review if needed | Sample design, QC findings, remediation, accepted limitations, and completion |

### 6.5 Template defaults and advanced settings

Template defaults cover resource limits, family handling, Q&A coverage, classifier policy, and QC sampling. Show
these settings when their stage is reached. Keep advanced numeric settings collapsed and explain limits in documents,
estimated reviewer effort, and estimated model usage.

### 6.6 Isolation among programs

Every program displays its own definition, guidance, fields, generated tasks, batches, model profiles, classifiers,
reports, and decisions. Different programs may use different destination fields. Programs may also analyze the same
field because intermediate outputs remain isolated; if they could later publish competing values, show a warning and
require a separate source-aware publication decision.

## 7. Persistent review workspace

### 7.1 Page header

Use the existing matter shell and `ResourcePageHeader` pattern. Show:

- breadcrumb: `Review programs / Review name`;
- review name;
- user-facing status badge;
- compact current phase;
- **View history** and **More actions** controls; and
- a primary action only when the current user can perform the next required action.

Do not replace the title with a collection-wide action. Launch actions belong in a decision card with context.

### 7.2 Phase rail

Render the five phases as a horizontal rail on desktop and a compact select or disclosure on small screens:

```text
Define       Training set       Train       Collection       QC
Complete     Complete           Current     Not started      Not started
```

Each phase may be `NOT_STARTED`, `ACTIVE`, `WAITING`, `ATTENTION`, `COMPLETE`, or `REOPENED`. Selecting a phase
scrolls or routes to its summary; it does not mutate state.

Show loop rounds beneath the relevant phase, for example `Discovery round 3` or `Classifier candidate 2`, without
turning each round into a primary navigation step.

### 7.3 Default dashboard layout

Use a responsive two-column layout on wide screens and a single column on narrow screens:

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ Review programs / Acme First Pass             Needs decision            │
│ Define  ✓   Training set  ✓   Train  ●   Collection  ○   QC  ○         │
├───────────────────────────────────────────┬─────────────────────────────┤
│ CURRENT DECISION                          │ REVIEW SUMMARY              │
│ Executive Knowledge needs more examples   │ 4 of 5 issues ready         │
│ Finding, recommendation, consequences     │ 3,240 documents reviewed    │
│ [Review plan] [Ignore issue] [Stop]        │ 2 open limitations          │
├───────────────────────────────────────────┴─────────────────────────────┤
│ ISSUE READINESS                                                         │
│ Issue              Support       Agreement      Quality       Next      │
│ Pricing            Ready         High           Ready         —         │
│ Executive Knowledge Needs data   Moderate       Not trained   Retrieve  │
├───────────────────────────────────────────┬─────────────────────────────┤
│ PHASE FINDINGS                            │ RECENT ACTIVITY             │
│ Concise current-phase report              │ Runs, reviews, decisions    │
└───────────────────────────────────────────┴─────────────────────────────┘
```

Order dashboard content as follows:

1. Current decision, review assignment, or blocking failure.
2. Issue readiness.
3. Current-phase findings and recommended next step.
4. Coverage and usage summary.
5. Recent activity and child records.

If no action is required, replace the decision card with **Work in progress** or **Next scheduled step**. Do not
show disabled decision buttons while a child job is still running.

### 7.4 Batch presentation

Present a task-specific Analysis Task Batch Set as one program work item. Show its frozen population, pinned task and
version, managed-child count, allocation policy, keep-together rules, aggregate progress, and retry state. Keep the
child Review Batches collapsed unless the user opens allocation or failure details.

Use explicit badges in history and child links:

- **Task-specific set** for `PARTITIONED_SET` work;
- **Assessment · Ad hoc** for Matter Definition assessment batches; and
- **Differences review · Ad hoc** for comparison-derived human-review batches.

Do not present discovery, supplemental Q&A, classifier-positive coverage, or QC as ordinary ad hoc batches. The
workspace should make an unexpected ad hoc task run visible as a policy violation rather than treating it as another
successful program child.

## 8. Reusable decision card

Every human gate uses one consistent decision-card pattern.

### 8.1 Required content

The card contains:

- a question phrased in legal-review language;
- why the decision is required now;
- measured findings;
- the agent's recommendation and rationale, when available;
- alternatives with consequences;
- documents, estimated reviewer effort, and estimated model usage affected;
- accepted limitations created by each choice;
- supporting report and representative-document links; and
- stale-state or authorization warnings.

### 8.2 Action behavior

Selecting an alternative opens a confirmation panel containing the exact scope and consequence. Require a reason
for issue exclusion, partial coverage, classifier acceptance with weak categories, QC acceptance with unresolved
items, stopping, and reopening.

The confirmation request includes a preview hash or row version. If the supporting data changes, reject the stale
submission, refresh the preview, and explain what changed.

After resolution, replace the active controls with a durable decision summary containing actor, time, reason, and
the version of the evidence reviewed.

### 8.3 Recommendation treatment

Use **Recommended** only when a versioned policy produces a recommendation from current data. Explain the basis in
one or two sentences. Do not use color alone, preselect the option, or imply that the recommendation substitutes for
legal judgment.

## 9. Phase 1 Define the review

### 9.1 Definition summary

Show:

- Matter Definition title and version used;
- responsiveness and issue criteria;
- issue-to-metadata mappings;
- known exclusions and assumptions;
- assessment rounds completed; and
- open, answered, and dismissed questions.

Use **Version used for this review** in the primary interface. Reserve `content hash` and `revision ID` for the
provenance disclosure.

### 9.2 Assessment findings

Group findings into:

- criteria that appear workable;
- criteria needing clarification;
- recurring near misses;
- likely vocabulary or scope gaps; and
- accepted limitations.

Each finding links to representative documents and the full assessment. The agent's proposed refinement appears as
a redline or structured before-and-after comparison, not as an unexplained replacement.

### 9.3 Definition decision

The primary question is **Is the Matter Definition ready for first-pass review?**

Choices:

- Accept this version.
- Refine the definition.
- Stop the review.

Acceptance freezes the version used by the next stage. Refinement creates a new version and clearly identifies
which prior assessment results remain applicable.

### 9.4 Q&A instruction review

Present the generated questions by issue, including destination value, evidence requirement, and Matter Definition
source. Warnings for unmapped criteria, duplicate destinations, unsupported schemas, or model-profile incompatibility
must block publication.

Primary action: **Publish Q&A instructions**.

## 10. Phase 2 Develop the training set

### 10.1 Retrieval-plan preview

Show the plan by issue rather than as a raw search payload:

- what the system is looking for;
- why those documents are useful;
- proposed queries or semantic examples;
- positive, hard-negative, control, family, and cohort quotas;
- documents already reviewed and excluded from reselection; and
- estimated batch size and dual-model usage.

Allow authorized users to edit controlled queries, filters, date ranges, cohorts, quotas, target child size, and
keep-together rules. Preview the frozen population and child allocation before the **Create task-specific batch set
and run Q&A** action becomes available.

### 10.2 Dual-Q&A progress

Identify the task-specific batch set, frozen population, number of managed child batches, and allocation warnings.
Then display both profiles symmetrically:

| Q&A reviewer | Status | Documents complete | Failed or partial | Usage |
| --- | --- | ---: | ---: | ---: |
| Reviewer A model name | Running | 640 of 1,000 | 2 | estimate |
| Reviewer B model name | Complete | 1,000 of 1,000 | 0 | actual |

Do not call one model primary or ground truth. Provide retry links for eligible failures through the existing job
controls.

### 10.3 Agreement report

Lead with issue-level and document-set agreement:

- exact complete-set agreement;
- partial agreement;
- conflicting issue assignments;
- model-A-only and model-B-only selections;
- incomplete and failed results; and
- cohort concentrations.

Use **agreement** or **consistency**, not **accuracy**, unless the comparison uses an independently adjudicated
reference source.

### 10.4 Issue readiness table

The primary table contains:

| Column | Meaning |
| --- | --- |
| Issue | User-facing issue name. |
| Status | Ready, needs examples, models disagree, excluded, or blocked. |
| Complete documents | Documents eligible for the displayed policy. |
| Positive support | Positive documents and distinct validation groups. |
| Negative support | Negative documents and distinct validation groups. |
| Model agreement | Complete-set and per-issue consistency summary. |
| Training feasibility | Result from the authoritative dataset preview. |
| Next action | Inspect, retrieve more, review disagreement, or resolve issue. |

Expandable details show family counts, cohort distribution, failures, source rounds, and each label-policy preview.
Do not reduce readiness to one positive-document count.

### 10.5 Insufficient-issue decision

Ask **How should we handle this issue?**

- **Retrieve more examples**: show the proposed targeted plan and expected cost.
- **Refine the issue definition**: show which stages will need to rerun.
- **Exclude from this classifier**: show the downstream impact and require a reason.
- **Stop the review**.

The primary issue remains in Q&A reporting even if the user excludes it from the classifier.

### 10.6 Training-label decision

Present the four choices using consequences rather than letters alone:

| Choice | Explanation | Preview |
| --- | --- | --- |
| Broader training set | Use an issue when either model selected it, but only when both runs completed successfully. | Eligible documents, added positives, feasibility, and false-positive risk. |
| High-agreement training set | Use documents only when both models assigned the same complete issue set. | Eligible documents, excluded disagreements, and classes losing feasibility. |
| Overlapping values | For multi-value fields, keep each value selected by both models even when their complete value sets differ. Single-value disagreements remain excluded. | Retained shared values, dropped model-only values, and categories losing feasibility. |
| Review disagreements | Use exact agreements and have reviewers assign the final issue set for disagreements. | Review volume, estimated effort, feasibility, and authority mix. |

Every decision control must expose its full consequence in a hover and keyboard-focus tooltip. The card must state
that agreement measures consistency rather than correctness. If **Review disagreements** is
selected, create an ad hoc difference Review Batch, show that batch type in its provenance details, and link to the
existing batch review workspace. Do not run the generated Q&A task ad hoc on that batch; any automated rerun uses a
new task-specific batch set sourced from the difference manifest.

The finalization gate repeats the selected policy and shows its computed outcome before the labels are frozen: one
eligible/excluded document total per coding field and one retained positive-document count per coding label. These
counts must reflect overlap removal and completed human decisions rather than the earlier union-based discovery
support totals.

## 11. Phase 3 Train and evaluate

### 11.1 Training progress

Show a compact sequence:

```text
Training set finalized → Features prepared → Validation created → Model trained → Evaluation ready
```

Each step reports status, document coverage, exclusions, and failures. Detailed runtime logs remain in Jobs.

### 11.2 Classifier summary

Lead with a plain-language outcome:

> Three issues are ready. One issue needs more positive examples. One issue finds most positives but produces too
> many false positives.

Then show an issue-quality table:

- readiness status;
- support and validation groups;
- precision and recall;
- false-positive and false-negative counts;
- fold stability;
- calibration or threshold warning;
- cohort warning;
- proportion of human-adjudicated labels; and
- recommended next step.

Label development metrics as development estimates. Separate human or sealed evaluation results visually and in
language.

### 11.3 Weak-category explanation

Translate internal reason codes:

| Internal result | User-facing explanation |
| --- | --- |
| `BLOCKED_SUPPORT` | Not enough independent examples to evaluate this issue. |
| `WEAK_SUPPORT` | Results are based on a small or concentrated sample. |
| `WEAK_PRECISION` | Too many documents are being assigned this issue incorrectly. |
| `WEAK_RECALL` | The classifier is missing too many known examples. |
| `UNSTABLE_FOLDS` | Performance changes substantially across validation groups. |
| `CALIBRATION_RISK` | Confidence scores do not reliably match observed outcomes. |
| `COHORT_REGRESSION` | Results are weaker for a particular custodian, period, source, or other cohort. |

### 11.4 Improvement decision

For each selected weak issue, show a proposed retrieval strategy tied to the weakness:

- likely positives for weak recall or support;
- hard negatives for weak precision;
- missing families or cohorts;
- uncertain documents when authorized; and
- random controls.

The user may accept, edit, or decline the plan. New candidates appear in a version comparison that separates fixed
evaluation results from metrics calculated on changed populations.

### 11.5 Classifier acceptance

Ask **Is this classifier ready for collection-wide review?**

Choices:

- Improve selected issues.
- Accept with documented limitations.
- Reconfigure and retrain.
- Reject and stop.

Activation and acceptance are related but distinct. If activation is necessary for scoring, show it as a separate
authorized step and state which active-model pointer will change.

## 12. Phase 4 Review the collection

### 12.1 Collection-wide launch preview

Before launch, show:

- collection population and version;
- feature-ready, excluded, and expected failure counts;
- classifier version and selected thresholds;
- predicted runtime and resource use;
- output isolation policy; and
- the commitment to Q&A-review classifier-positive documents.

Primary action: **Run classifier across the collection**. State clearly that this does not publish issue coding into
authoritative matter metadata.

### 12.2 Scoring summary

Show coverage first:

- documents scored successfully;
- feature exclusions and failures;
- abstentions or mandatory-review routes;
- documents assigned at least one issue; and
- issue counts and confidence bands.

Prediction charts should link to the corresponding document cohort or derived batch.

### 12.3 Existing Q&A comparison

Separate training documents, adjudicated documents, held-out documents, earlier discovery documents, and documents
without Q&A review. Report alignment as descriptive unless a human reference is present.

### 12.4 Q&A coverage decision

Present the number of classifier-positive documents still missing one or both Q&A profiles, broken down by issue,
confidence, cohort, and profile.

Choices:

- Run both Q&A reviewers on every uncovered classifier-positive document.
- Use a narrower documented coverage policy.
- Stop before completion.

The default is full dual-Q&A coverage. A narrowed policy requires a reason and becomes an accepted limitation.

### 12.5 Coverage execution

Show one parent progress view for the Analysis Task Batch Set:

- total and completed child batches;
- documents covered by both profiles;
- profile-specific failures;
- retryable child batches;
- usage; and
- remaining coverage.

Users may open a child batch or job, but ordinary monitoring should not require visiting every child record.

### 12.6 Final alignment

Show three-source combinations among Q&A profile A, Q&A profile B, and the classifier. Make human-adjudicated results
a distinct higher-authority source where present.

Primary cohorts include:

- all three agree;
- Q&A models agree and classifier differs;
- classifier agrees with one Q&A model;
- classifier only;
- Q&A only;
- Q&A models disagree; and
- incomplete or failed coverage.

Offer reproducible cohort and disagreement batches. Do not silently resolve these results into final matter coding.

## 13. Phase 5 Quality control and complete

### 13.1 No-code QC setup

Explain that QC examines documents with no selected issue while keeping uncertain, failed, partial, stale, and
mandatory-review documents outside the ordinary no-code population.

Show separate sampling lanes:

- probability-based random audit;
- near-threshold negatives;
- uncertain or abstained documents as a separate review lane;
- underrepresented cohort controls; and
- semantic near misses.

Only the probability sample supports collection-level estimates. Targeted lanes report discovery yield and must not
be combined into an unweighted error rate.

### 13.2 Blind QC progress

Show the QC task-specific batch set, managed child progress, two Q&A profiles, and completion state without revealing
classifier scores or selection reasons in the Q&A task. The user interface may display the stratum to an authorized
manager after execution; it must not leak into the model prompt.

### 13.3 QC report

Lead with:

- whether the random audit found issue-positive documents;
- the supported miss-rate estimate and interval, when the design permits it;
- targeted discovery yield by lane;
- issues and cohorts involved;
- Q&A agreement on QC findings;
- failures and unresolved documents; and
- the recommended remediation.

Every finding links to evidence, the QC batch set, and the applicable managed child batch. Sampling methodology and
limitations appear in an expandable section and in the exportable report.

### 13.4 Completion decision

Ask **Is first-pass review complete?**

Choices:

- Accept QC and complete.
- Manually review QC positives or disagreements.
- Retrieve more examples and retrain.
- Revise the Matter Definition or Q&A guidance.
- Stop without completion.

If remediation reopens an earlier phase, show which results remain historical and which stages will rerun.

## 14. Completion report

The completed workspace becomes read-only except for authorized reopen, export, archive, and downstream publication
actions. The completion report contains:

- Matter Definition and Q&A instructions used;
- issue categories included and excluded;
- discovery and review rounds;
- selected training-label policy and authority composition;
- accepted classifier and documented limitations;
- whole-collection and Q&A coverage;
- final source-alignment findings;
- no-code QC design and results;
- unresolved documents;
- every consequential user decision; and
- links to all authoritative child records.

Provide a print-friendly view and exportable artifact. Completion does not itself publish coding to matter metadata.
If a downstream publication workflow exists, show it as a separate action with its own preview and authorization.

## 15. Agent experience

### 15.1 Placement

Do not make a general chat transcript the center of the workspace. Present agent output in three forms:

1. A concise **Review analyst finding** in the current decision or phase summary.
2. A structured proposed refinement, retrieval plan, diagnosis, or remediation plan.
3. An optional **Ask about this result** panel scoped to the current report and sources.

### 15.2 Required grounding

Agent findings link to the relevant Matter Definition excerpts, metrics, issue-support rows, reports, and
representative documents. Separate measured facts from recommendations.

Example:

> **Finding:** Executive Knowledge has 14 positive documents, but 11 belong to one family. The current dataset cannot
> create reliable group-aware validation folds.
>
> **Suggested next step:** Retrieve communications from the remaining custodians using the proposed executive-role
> and approval terminology.

### 15.3 Structured actions

The agent may populate a draft plan or explanation, but state-changing operations use structured controls. A chat
message alone cannot publish instructions, ignore an issue, accept a classifier, start whole-collection scoring, or
accept QC.

### 15.4 Conversation persistence

If scoped conversations are supported, retain them by review and phase. A response should cite the program version
and report state it used. When that state becomes stale, show the response as historical rather than current advice.

## 16. Notifications and assignments

Use the shared notification backbone rather than adding a Review Program inbox, toast queue, or task-specific
messaging system.

The complete message flow is:

```text
child task changes authoritative state
    -> durable program event wakes the coordinator
    -> coordinator advances the Review Program
    -> coordinator opens a decision, assignment, or blocking attention state when needed
    -> shared notification service resolves recipients
    -> global bell and notification center show one typed item
```

Program events are machine-facing and appear in Activity or diagnostics, not in the user's notification center. User
notifications are human-facing projections; reading or dismissing them never advances the workflow.

### 16.1 User notification policy

| Program state | Inbox item | Recipient | Destination |
| --- | --- | --- | --- |
| A decision is ready | Action required | Users authorized to resolve that exact decision | Current decision card |
| A differences-review batch is assigned | Review assigned | Assigned reviewer or review group | Batch review with a return link to the program |
| Automatic retry is exhausted and a failure blocks progress | Attention required | Program owner and configured operational audience | Blocking failure card |
| A usage, round, or low-yield limit requires direction | Action required | Authorized decision makers | Limit decision card |
| First-pass review completes | Completed | Program owner and configured recipients | Completion report |
| The program is stopped or canceled | Stopped | Program owner and initiator when different | Program history |

Do not notify for queued or running progress, each managed child-batch completion, a successful automatic retry,
routine reconciliation, document-level results, or a phase transition that immediately continues automatically.
Those updates remain visible in the workspace and job views.

Program-managed child jobs should not generate the ordinary per-job completion notification. Their terminal events
flow to the parent program, which publishes at most one consolidated action, attention, or completion notification.
This keeps a task-specific set with many managed children from flooding the user.

### 16.2 Notification behavior

Notifications use the existing global bell and full notification center, including its matter filter, recipient read
and dismissal state, and safe route descriptors. The Review Program workspace may show its current attention state,
but it does not maintain a separate inbox.

Assigned-review notifications state the document count, due date when configured, issue-guidance version, and batch
link. Decision notifications state the question and affected issue or population. Neither includes document text,
evidence excerpts, model reasoning, raw task payloads, or arbitrary links.

Resolving a Review Program action updates the notification's shared resolution projection for every recipient while
preserving each recipient's read and dismissal state. Opening a notification rechecks current authorization and
stale action state. If no eligible recipient can be resolved for a required action, show an operational attention
state; never send it to an unauthorized fallback user or advance the program automatically.

## 17. Loading, empty, failure, and stale states

### 17.1 Loading

Use stable skeletons for the page header, phase rail, decision area, and issue table. Do not block the entire
workspace while a secondary report loads.

### 17.2 No current action

If work is running, show the active child work, expected next transition, elapsed time, and cancel control when
authorized. If the program is waiting for a reviewer batch, show assignment and completion progress.

The administrative reconciliation control is labeled **Refresh state**. Its tooltip says that it checks pending
events and completed work and updates the program when its recorded state is behind; it does not retry work. This is
a state-repair action, not the normal way to advance a healthy run.

### 17.3 Partial failures

Distinguish:

- retryable job failure;
- document-level failures;
- incomplete Q&A profile;
- stale source or configuration;
- permission failure; and
- report or artifact availability failure.

Explain whether completed work remains valid and what retry will repeat.
Display a stage failure and its retry control on the affected stage. The program summary may say **Needs attention**,
but must not present a retriable classifier-training or child-task failure as though the entire First Pass Review failed.

### 17.4 Stale decisions

When supporting data changes, disable the old action, show the changed counts or version, and require review of the
new preview. Retain the stale preview in history for audit.

### 17.5 Empty issue or cohort results

Use explicit language such as **No positive examples found in this round**. Do not display zero as evidence that the
issue does not exist.

### 17.6 Starting another run

An active run blocks creation of another run. A failed current run remains unfinished: the normal recovery action is
**Retry**, and the API rejects an unconfirmed replacement. If a manager intentionally starts a replacement, require a
warning that identifies the failed stage and explains that the new run restarts setup, becomes current, and neither
retries nor deletes the existing run. Present replacement as a program-level administrative action, never as an action
inside the failed stage. Completed and intentionally canceled runs may start again without this warning.

## 18. Responsive behavior

The existing matter sidebar remains the only persistent navigation rail.

- At desktop widths, use a two-column dashboard with the action and issue table receiving the wider column.
- At laptop widths, keep the phase rail compact and allow wide issue tables to use column prioritization before
  horizontal scrolling.
- On mobile, render the phase rail as a select or disclosure, stack cards, and convert tables into issue summary
  cards with expandable details.
- Decision confirmations use a full-height sheet or page on small screens rather than a cramped dialog.
- The Search and Review and batch-review workspaces retain their existing full-screen layouts and use the matter
  section menu for return navigation.

## 19. Accessibility and content standards

- Use semantic headings and one page-level heading.
- Give the phase rail an accessible label and expose current/completed state in text.
- Use `aria-current` for the active phase or route.
- Never rely on color alone for readiness, status, recommendation, or failure.
- Preserve keyboard access and visible focus for tables, disclosures, menus, dialogs, and document links.
- Announce material asynchronous state changes without announcing every polling refresh.
- Keep confirmation focus within the dialog or sheet and return focus to its triggering control.
- Use readable issue names in every action; place IDs in diagnostic details.
- Use **Q&A reviewer** or the configured model name in primary copy rather than unexplained provider codes.
- Use **version used for this review** rather than **frozen** in ordinary copy.
- Use **agreement** rather than **accuracy** for machine-to-machine comparison.
- Use **documents with no issue selected** before introducing **no-code population**.

## 20. Read-model and API requirements

The frontend should not reconstruct program state from dozens of child endpoints. Add program-oriented read models
that contain compact summaries and links while preserving existing child APIs as the source of detailed views.

### 20.1 List response

Include:

- program and run identity;
- template key, template display name, and pinned template version;
- name, owner, and status;
- current phase and action summary;
- issue readiness counts;
- accepted definition and classifier display names;
- last activity; and
- allowed actions.

### 20.2 Workspace response

Include:

- header and permission data;
- pinned template identity, schema version, and presentation contract;
- the template's user-facing phase summaries;
- each active stage's declared execution scope and batching policy;
- current decision or assignment;
- issue-readiness rows;
- current-phase findings;
- coverage and usage summary;
- recent activity;
- child resource links;
- current and historical version summaries; and
- ETag or row version.

Large comparison rows, predictions, document lists, and report bodies remain paginated or artifact-backed.

### 20.3 Decision preview and resolution

Each decision preview returns:

- question and explanation;
- alternatives;
- consequences and counts;
- recommendation with policy version;
- supporting links;
- required reason flags;
- authorization result;
- preview hash; and
- expiration or stale-state guards.

Resolution submits the selected alternative, optional scoped edits, reason, and preview hash. The response returns
the durable decision record and next program state.

### 20.4 Progress updates

Use the repository's established React Query polling pattern initially. Poll only while the run or a visible child
is active, and stop polling for terminal or waiting states. A later server-event stream may replace polling without
changing the view contracts.

Polling and a future event stream refresh the active workspace; they do not create user attention. Durable program
events wake the coordinator, while the shared notification backbone wakes a person only for a decision, assignment,
blocking failure, limit, stop, or meaningful completion. The notification inbox is not a live progress feed.

### 20.5 Document cohorts

Report rows that represent a document cohort return a controlled link or cohort token that can open Search and
Review, create a derived batch, or open an existing batch. Do not send unbounded document ID arrays in ordinary
workspace responses.

## 21. Frontend component plan

Recommended components:

| Component | Responsibility |
| --- | --- |
| `review-programs-section.tsx` | Matter-level list, template filters, and empty state. |
| `review-program-create.tsx` | Template selection, program identity, assignment, and Matter Definition starting point. |
| `review-program-workspace.tsx` | Reusable workspace query, layout, permissions, and top-level state. |
| `review-program-phase-rail.tsx` | Template-driven phase navigation and phase state. |
| `review-program-current-action.tsx` | Decision, assignment, running-work, or failure card. |
| `review-program-decision-card.tsx` | Reusable alternatives, consequences, confirmation, and resolution history. |
| `review-program-agent-finding.tsx` | Grounded finding and proposed-plan presentation. |
| `review-program-progress-summary.tsx` | Coverage, child-job, usage, and review-assignment progress. |
| `review-program-history.tsx` | Template version, program version, round, decision, and remediation timeline. |
| `review-program-template-renderer.tsx` | Selects registered phase and report components for the pinned template key and schema version. |
| `first-pass/issue-readiness.tsx` | First-pass issue-level readiness table and mobile cards. |
| `first-pass/phase-summary.tsx` | First-pass phase findings, recommendation, and child links. |
| `first-pass/completion-report.tsx` | First-pass final summary and export controls. |

Prefer shared existing components for status badges, cards, dialogs, tables, query errors, skeletons, document
viewer links, batch links, and resource page headers. Extend `StatusBadge` or add a small mapping wrapper so user-facing
status labels are consistent rather than passing raw engine states throughout the feature.

## 22. Expected implementation locations

| Area | Expected location |
| --- | --- |
| Matter navigation | `web/src/components/matter/matter-sections.ts`, `web/src/lib/matter-routes.ts` |
| Routes | `web/src/app/(workspace)/app/clients/[clientId]/matters/[matterId]/review-programs/**` |
| Reusable workspace components | new `web/src/components/review-programs/**` |
| First-pass template components | new `web/src/components/review-programs/first-pass/**` |
| Generated API types | `web/src/generated/**` after OpenAPI regeneration |
| Query and mutation hooks | new `web/src/lib/review-programs.ts` or colocated hooks |
| Backend summary contracts | reusable review-program and first-pass contracts from the automation plan |
| Notification integration | existing global notification components and routes plus registered Review Program route descriptors |
| Documentation | `web/content/docs/review-programs.mdx`, first-pass content, and matter-navigation references |
| Component tests | colocated `*.test.tsx` files |
| End-to-end tests | `web/e2e/review-programs.spec.ts` and first-pass fixtures |

Follow the repository's current Next.js documentation before implementation, as required by `web/AGENTS.md`.

## 23. Authorization behavior

The API remains authoritative. The frontend uses allowed-action fields to hide unavailable primary actions and may
show disabled explanatory controls when that helps a user understand who must act.

Permission-sensitive examples:

- a reviewer can open an assigned disagreement batch but cannot change the label policy;
- a manager can monitor Q&A coverage without activating a classifier;
- a classifier approver sees the acceptance and activation decisions;
- a user lacking whole-collection authorization sees the preview and the role or permission required; and
- a viewer can inspect historical decisions but cannot reopen the review.

Reauthorize every mutation. Never infer authorization from the fact that the user can view the program.

## 24. Testing plan

### 24.1 Navigation and routing

- Review Programs appears in the matter menu only with view permission.
- List, setup, workspace, history, and report routes produce correct breadcrumbs.
- Back, refresh, bookmarks, and deep links preserve matter and review context.
- Search and Review and batch workspaces return to the correct review program.
- Mobile matter navigation exposes the new section without another rail.

### 24.2 Setup

- The user chooses a published workflow template before creating a program.
- Initial creation collects identity, assignment, and the Matter Definition starting point rather than every later
  stage setting.
- The program opens in Matter Definition work and resumes after navigation or reload.
- Target mappings, generated Q&A tasks, and model profiles appear after definition acceptance.
- Missing target mappings and incompatible profiles block Q&A task publication, not initial program creation.
- Advanced defaults appear at the applicable phase and remain optional and accessible.
- Permission problems identify the unavailable action.
- Two first-pass programs in one matter retain different definitions, guidance, fields, tasks, and results.
- The start preview states that coding is not published.

### 24.3 Workspace states

- Task-specific sets appear as one parent work item with expandable managed children.
- Assessment and difference-review batches carry distinct ad hoc badges.
- Unexpected ad hoc task execution renders as a template-policy error.
- Each overall status and phase state renders the correct label.
- Running work, waiting decisions, review assignments, failures, stopped reviews, and completed reviews have distinct
  primary presentations.
- Polling starts and stops according to active state.
- Partial report failures do not blank the whole workspace.
- Historical versions remain accessible after reopening.
- Child program events update workspace activity without creating an inbox item for every child transition.
- Parent-managed child completion notifications are suppressed while standalone jobs retain their existing behavior.

### 24.4 Decisions

- Every decision shows alternatives, consequences, links, and required reasons.
- Recommendation does not preselect or auto-submit an action.
- Stale preview hashes reject resolution and refresh the counts.
- Unauthorized decisions cannot be submitted.
- Resolved decisions display actor, time, reason, and evidence version.
- Reopening identifies invalidated downstream stages before confirmation.
- Opening a decision or assignment creates at most one typed notification for each logical attention state.
- Duplicate events, coordinator retries, and reconciliation do not duplicate the notification.
- Notification links open the exact current decision, assignment, failure, or completion state and reauthorize the
  user before showing it.
- Resolving an action updates its shared notification-resolution projection while each recipient's read and
  dismissal state remains independent.
- Notification summaries never contain evidence excerpts, document text, model reasoning, or raw task payloads.

### 24.5 Issue readiness and reports

- Readiness uses actual dataset-feasibility results rather than a UI count rule.
- Agreement is never labeled accuracy without a qualifying reference source.
- Development and independent evaluation metrics are visually distinct.
- Excluded and ignored issues remain visible with reason and impact.
- Cohort links open the correct document population.
- Large issue and cohort result sets paginate or virtualize correctly.

### 24.6 Accessibility

- Keyboard-only setup, phase navigation, decision selection, confirmation, and report inspection.
- Screen-reader labels for statuses, recommendations, phase progress, charts, and icon-only controls.
- Focus restoration after dialogs and asynchronous decisions.
- Status and readiness remain understandable without color.
- Responsive tests at phone, tablet, laptop, and wide desktop widths.

### 24.7 End-to-end scenarios

1. Straight-through review with all issues feasible and QC accepted.
2. Repeated targeted retrieval followed by issue exclusion.
3. Human disagreement review before training.
4. Weak classifier category followed by retrieval and retraining.
5. Whole-collection scoring with partial failure and retry.
6. Classifier-positive Q&A coverage across multiple child batches.
7. QC finding that reopens training and produces a new scoring round.
8. Configuration change that makes an open decision stale.
9. Reviewer, manager, approver, and viewer permission boundaries.
10. Completed review with full history and export.

## 25. Delivery phases

### Phase 0 Content and interaction prototypes

Deliverables:

- Template catalog and reusable workspace-shell prototype showing both First Pass Review and a placeholder second
  template.
- Task-specific set, ad hoc assessment, and ad hoc differences-review presentation fixtures.
- Final user-facing phase, status, readiness, and decision vocabulary.
- Low-fidelity desktop and mobile prototypes for the list, setup, workspace, label decision, classifier review, and
  QC completion.
- Representative fixtures for straight-through, weak-issue, failure, and remediation states.
- Senior-attorney walkthrough of decision wording and consequences.

Exit criteria:

- Users can identify current state and next action without explanation from the product team.
- The difference among agreement, classifier quality, and random-audit quality is understood.

### Phase 1 Navigation and read-only workspace

Deliverables:

- Review Programs matter-navigation entry and generic routes.
- Template-aware program list, reusable header and phase rail, first-pass summary cards, issue-readiness table,
  history, and child links.
- Program summary read APIs and generated client types.
- Global-notification deep links into the exact Review Program state, without a second program inbox.
- Loading, empty, failure, responsive, and permission states.

Exit criteria:

- Existing program fixtures can be inspected end to end from one workspace.
- No state-changing program action is required for the first release slice.

### Phase 2 Setup and definition decisions

Deliverables:

- Template selection, short program creation, and independent Matter Definition starting-point flow.
- Definition assessment presentation.
- Refinement comparison and definition-ready decision.
- Just-in-time destination mapping, generated Q&A Analysis Task review, profile confirmation, and publication
  decision.

Exit criteria:

- A senior attorney can create more than one independent first-pass program in a matter, begin each with its own
  Matter Definition lineage, and reach training-data development without visiting raw job records.

### Phase 3 Training-set development

Deliverables:

- Retrieval-plan and task-specific allocation preview with controlled editing.
- Dual-Q&A progress and agreement report.
- Issue-readiness table and targeted-retrieval loop.
- Insufficient-issue and training-label decisions.
- Disagreement-batch assignment and return links.

Exit criteria:

- Every issue is ready, explicitly excluded, or presented as a blocking decision with measurable consequences.

### Phase 4 Classifier review

Deliverables:

- Training progress and classifier-quality summary.
- Weak-category explanations and proposed improvement plans.
- Candidate-version comparison.
- Acceptance and activation decisions.

Exit criteria:

- A senior attorney can accept one classifier version with an issue-by-issue understanding of quality and
  limitations.

### Phase 5 Collection review

Deliverables:

- Whole-collection preview, launch, and coverage summary.
- Existing-Q&A comparison.
- Classifier-positive Q&A coverage decision and task-specific batch-set progress.
- Final three-source alignment report and cohort actions.

Exit criteria:

- Required classifier-positive documents are Q&A-covered or explicitly accepted as unresolved.

### Phase 6 QC and completion

Deliverables:

- No-code sampling-manifest and task-specific allocation preview.
- Blind dual-Q&A QC batch-set progress.
- Random-audit and targeted-lane report.
- Completion and remediation decisions.
- Completion report, print view, and export.

Exit criteria:

- The user can complete or reopen first pass with a defensible, evidence-linked QC record.

### Phase 7 Hardening

Deliverables:

- Accessibility audit and remediation.
- Scale tests for large issue sets, histories, child batches, and comparison artifacts.
- Failure injection, stale-state, concurrency, and permission testing.
- Usage instrumentation, notification tuning, and support diagnostics.
- Documentation and role-based onboarding.

Exit criteria:

- The experience is operable at target matter size and remains understandable under partial failure and multi-round
  remediation.

## 26. Product analytics

Measure whether the workspace reduces orchestration burden without encouraging unsafe automation:

- time from review creation to accepted definition;
- time waiting for each decision type;
- percentage of decisions opened from notifications;
- age of unresolved Review Program action notifications and empty-audience failures;
- parent-managed child notifications suppressed versus consolidated program notifications published;
- retrieval rounds and incremental positive yield;
- disagreement-review volume and completion time;
- weak categories improved, excluded, or accepted with limitations;
- time from classifier acceptance to complete Q&A coverage;
- QC remediation frequency;
- stale-decision frequency;
- navigation from summaries to supporting documents and reports;
- user reversals or reopenings after a decision; and
- support incidents caused by unclear status or hidden child failures.

Do not optimize for fewer human gates by itself. A shorter workflow is not better if users make decisions without
understanding their scope or consequences.

## 27. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| The workspace becomes a second implementation of every child feature | Summarize and link; keep authoritative editing in existing domain workspaces. |
| Users cannot tell task-specific campaigns from overlapping review batches | Present batch sets as parents and badge the two permitted ad hoc purposes explicitly. |
| Future templates bypass the batching rules | Require every template stage to declare its supported execution scope and surface policy violations. |
| Five phases hide important loops | Show rounds, reopened states, history, and the earliest affected stage. |
| Users interpret model agreement as accuracy | Use consistency language and reserve accuracy for adjudicated or independent reference comparisons. |
| Aggregate metrics hide a weak issue | Make issue readiness and per-category quality primary. |
| Agent recommendations appear authoritative | Separate findings from recommendations and require structured user decisions. |
| Too many decisions create fatigue | Surface only consequential gates and combine related issue decisions in a reviewable matrix when safe. |
| Technical details overwhelm attorneys | Use progressive disclosure and plain-language reason mappings. |
| Simplified copy hides consequential scope | Show affected documents, issues, versions, usage, and invalidated stages in confirmations. |
| Long-running work appears stuck | Show child progress, last update, next transition, and actionable failures. |
| Notifications become a second workflow state machine | Advance the program only from durable program events and authoritative child state; use notifications as human-facing projections. |
| Managed child jobs flood the user's inbox | Suppress generic completion notices for parent-managed jobs and publish one consolidated program-level item only when attention is meaningful. |
| A notification reaches a user who cannot act | Resolve the audience from the source action's authorization and reauthorize every open and decision. |
| QC targeted sampling is mistaken for a corpus estimate | Separate random and targeted lanes in layout, wording, and calculations. |
| Reopening makes history confusing | Preserve immutable rounds and label current versus historical results prominently. |
| A stale browser submits an obsolete decision | Require preview hashes and refresh changed evidence before resolution. |

## 28. MVP acceptance criteria

The user experience MVP is complete when:

- Review Programs is a permission-aware matter section with durable generic routes;
- the user selects a published workflow template, names the program, and starts with a Matter Definition;
- a matter can contain multiple independent first-pass programs with different guidance and destination fields;
- each program pins its template version and retains separate generated tasks, batches, classifiers, and history;
- the reusable workspace renders template-defined phases while First Pass Review supplies its specialized issue and
  report views;
- the guided flow creates and presents the required Analysis Tasks and other child artifacts when their phase is
  reached;
- every automated first-pass Q&A population appears as a task-specific batch set with managed children;
- only Matter Definition assessments and difference or disagreement review appear as ad hoc Review Batches;
- the workspace shows five understandable phases and one unambiguous current state;
- every blocking decision uses the shared decision-card pattern;
- issue readiness is visible throughout discovery, training, collection review, and QC;
- the two Q&A profiles are presented symmetrically;
- label-policy choices show actual eligibility and feasibility consequences;
- classifier reports distinguish development estimates from independent evaluation;
- collection-wide scoring and Q&A coverage require explicit previews;
- no-code QC separates probability and targeted samples;
- the agent provides grounded findings without owning state transitions;
- child records and representative documents remain directly accessible;
- durable program events carry child state into the workflow, while the shared notification backbone carries only
  typed human attention;
- program-managed child jobs do not produce per-child completion noise, and no separate Review Program inbox is
  introduced;
- historical versions, rounds, decisions, and accepted limitations remain inspectable;
- desktop, mobile, keyboard, screen-reader, stale-state, failure, and permission tests pass; and
- completion produces a readable, exportable, provenance-complete report.

## 29. Decisions to resolve before implementation

1. Whether the first release exposes only system-owned templates or permits tenant-authored workflow templates.
2. Which template fields are declarative presentation metadata and which require a registered code-backed phase
   component.
3. Which roles may create, stop, reopen, archive, duplicate, and explicitly migrate a review program.
4. Whether the agent explanation panel is included in the MVP or follows the structured phase summaries.
5. Whether each phase exposes usage budgets in dollars, model units, document counts, or a combination.
6. Whether disagreement review supports assignment and due dates in the first release.
7. Which classifier metrics appear by default and which remain in expanded technical details.
8. Whether classifier activation is required for program scoring or an approved pinned version may be invoked
   directly.
9. What confidence target and maximum sample size the no-code random-audit setup should recommend.
10. Which accepted limitations block completion and which require only recorded acknowledgement.
11. What export formats are required for the completion report.
12. Whether final coding publication will remain a separate future workspace or appear as a linked post-completion
    action.
