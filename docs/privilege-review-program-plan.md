# Privilege Review Program Plan

## Purpose

Add a second Review Program type for privilege review. Unlike Issue Review, which uses Q&A coding and TAR to learn
and apply an issue boundary, Privilege Review operates against an already responsive population and must account for
every in-scope document before production.

Automation may prioritize work, assemble context, extract evidence, recommend a disposition, and perform quality
control. It must not silently remove documents from the review denominator. Each responsive document must complete
the configured LLM, human, or combined review route and receive a terminal operational disposition.

## Implementation status (2026-11-21)

Phase IA-1 and the standalone Phase 1 foundation are implemented. A matter administrator can manually start Identity
Analysis from the Entities workspace. The durable job freezes the current matter-document population, reads structured
sender/To/CC/BCC metadata in bounded batches, aggregates addresses and domains, publishes an immutable JSON artifact,
and reports compact coverage and progress in Core. The Artifact service has a batch participant-metadata endpoint so
remote deployments do not issue one HTTP request per document.

Unresolved participant values are normalized with Unicode normalization, whitespace collapsing, and case-insensitive
lowercase keys inside the analysis artifact. The source metadata is preserved unchanged. The result separately reports
the number of repeated unresolved observations and the number of unique normalized unresolved values, retaining source
variants for audit and later alias resolution.

The first IA-2/IA-3 slice is also implemented. An operator can run an artifact-backed, bounded review of aggregate
domains for credible organizations. The model returns categorized recommendations with confidence, rationale, and
source signals. It does not read subjects or document text, infer representation, or make privilege decisions. Each
recommendation creates an agent-authored `PROPOSED` organization and canonical domain mapping in the existing entity
review queue. Confirming the entity makes the signal operational; rejecting it removes the proposed domain mapping.
The complete recommendation set remains in the immutable artifact rather than being copied into Core tables.

The first matter-specific representation relationship is also implemented. Authorized operators can record a
confirmed `OUTSIDE_COUNSEL_FOR` relationship from a confirmed `LAW_FIRM` organization to a confirmed person or
organization. Relationships are optional privilege-review signals, not program prerequisites or privilege decisions.
The storage and API contract are generic and registry-validated so later relationship types do not require replacing
the relationship model. Entity merges move active relationships to the surviving canonical entity, while entity
rejection rejects affected relationships.

General person/organization resolution, agent-recommended representation relationships, and a reusable standalone
published identity snapshot in IA-4 remain open. Privilege Review creation now performs the narrower automatic
per-program identity freeze described below.

The first Phase 4 foundation slice is implemented. `PRIVILEGE_REVIEW` is a distinct Review Program template. Creating
one from a non-empty Review Batch freezes that batch membership as an immutable review-population artifact and
automatically freezes the matter's current confirmed entities, domains, organization categories, and relationships as
a separate optional identity-context artifact. The program pins the real operational coding field, review route, and
family-assignment policy in its first immutable version. Core stores only artifact references, hashes, counts, and
configuration; it does not create a document row per program. The workspace reports the frozen denominator and
identity counts and clearly states that no document analysis has started.

Family-packet preparation, family-level cited evidence extraction, and isolated family-level adjudication are
executable. The adjudication phase consumes one immutable observations artifact per family, creates one `privileged`,
`not_privileged`, or `needs_review` recommendation for each successful family evidence result, applies that isolated
recommendation to the family's in-scope documents, preserves evidence failures as explicit exceptions, and stores its
values on an isolated Review Batch run. It never writes the operational privilege field.
The program then stops at an explicit human-review gate. Human review, controlled promotion, exception resolution,
QC, and production clearance remain open.

## Product boundary

### Issue Review

- Finds responsive or issue-bearing material.
- Uses reviewed examples to train and improve a classifier.
- Can use TAR stabilization and model quality as completion signals.
- Treats an individual document as the normal unit of analysis.

### Privilege Review

- Starts with a responsive or production-candidate population.
- Reviews every in-scope document by LLM, human, or both under a configured review policy.
- Treats the family group plus participant context as the evidence and adjudication unit while retaining document-level
  review outcomes.
- Uses classifiers, similarity, and queries only for prioritization and supplemental safety checks.
- Completes only when every in-scope document is cleared, withheld, redacted, or placed in an explicitly blocking
  exception state.

## Core concepts

### Review population

The documents that must receive a privilege disposition. The initial implementation should allow a responsive coding,
saved search, selected batches, a production-candidate set, or an explicit document list to define this population.
The population must be versioned so additions and removals are visible.

### Context population

Family members and other records used to understand an in-scope document but not automatically added to production or
required to receive an operational privilege code. A responsive attachment may, for example, use a non-responsive
parent email as context.

### Operational disposition

One mutually exclusive result in the real matter privilege field:

- `privileged`
- `not_privileged`
- `needs_review`

Missing text, processing failure, incomplete required review, and unresolved exceptions are workflow states, not
negative privilege decisions.

### Identity signal

A structured observation about a participant or organization. An email domain, known law firm, confirmed attorney,
or possible third party is evidence available to the reviewer; it is never a privilege determination by itself.

## Program stages

### 1. Define the responsive population

Freeze or version the in-scope population and report document, family, parent, attachment, missing-text, and newly
added counts. Every document enters the program as pending.

### 2. Select identity context

Select a completed matter Identity Analysis snapshot. Warn when it is missing, stale, or has unresolved high-frequency
participants, but do not make Identity Analysis inseparable from the program. Identity Analysis is a reusable matter
capability that can be run manually at any time.

### 3. Prepare family context

Resolve parent, attachment, and sibling relationships. Build bounded review packets that keep review scope distinct
from context-only documents. Broken relationships and missing source metadata become visible exceptions.

### 4. Extract privilege evidence

Use document content, metadata, confirmed and suggested identity signals, matter guidance, and family context to
extract cited observations such as:

- request for or provision of legal advice;
- legal versus business purpose;
- litigation or work-product purpose;
- draft status;
- confidentiality;
- third-party disclosure or possible waiver;
- family facts that change the meaning of an attachment;
- countervailing evidence and missing information.

This stage produces evidence, not an operational coding.

### 5. Recommend a family-aware disposition

Return one recommendation: privileged, not privileged, or needs review. Include the asserted basis, decisive evidence,
countervailing facts, confidence, supplied context, and any deterministic family rule. Replace three independent
yes/no questions with this single-choice contract.

### 6. Complete the configured review route

Support:

- human-only review;
- LLM pre-review followed by human review of every document;
- hybrid review, where matter policy specifies which LLM outcomes require human confirmation.

Families should normally be assigned together. Human decisions write the operational field without overwriting model
recommendations or benchmark/reference data.

### 7. Resolve exceptions

Provide actionable queues for mixed families, possible waiver, conflicting decisions, low confidence, unknown
participants, missing text, broken family context, and execution failures. No blocking exception may be represented as
completed negative review.

### 8. Perform quality control

Use random and stratified samples of cleared negatives, oversample attorney communications and mixed families, check
parent/attachment consistency, and measure reviewer disagreement. Quality control validates completed coverage; it
does not define which responsive documents receive review.

### 9. Clear production

Documents coded not privileged become eligible for production. Privileged documents are withheld or treated under
matter policy. Redaction-required, needs-review, failed, missing-text, and pending documents block production until
resolved. Preserve reviewer, guidance, identity-snapshot, family-context, evidence, and propagation provenance.

## Identity Analysis: first implementation slice

Identity Analysis is manually triggered against the entire matter and is independent of any Review Program run.

### Phase IA-1: metadata inventory

Read only structured participant metadata—sender, To, CC, and BCC. Do not read document bodies, subjects, attachment
names, or extracted text. Deterministically:

1. Normalize valid email addresses.
2. Preserve observed display-name variants when available.
3. Aggregate sender and recipient counts by identity.
4. Aggregate unique identities and communication counts by domain.
5. Report unresolved participant observations, unique normalized unresolved values, and documents without usable
   participant metadata.
6. Store the complete inventory as a versioned workflow artifact and only compact counts/top values on the Core job
   row.

This phase does not create or update operational entity records.

### Phase IA-2: role recommendations

Send the aggregated metadata inventory—not document text and not one request per document—to a bounded LLM step. The
model may recommend organization mappings and matter roles such as client, internal legal, potential outside counsel,
law firm, consultant, auditor, insurer, ordinary third party, or unknown. Each recommendation must include confidence,
metadata evidence, an explanation, and alternatives.

Review domains as organization evidence. For each domain, the recommendation contract may propose a canonical
organization name, parent-domain or subdomain mapping, and an organization category such as `LAW_FIRM`, `CORPORATION`,
`GOVERNMENT`, `ACCOUNTING_FIRM`, `CONSULTING_FIRM`, `INSURER`, `EMAIL_PROVIDER`, `OTHER`, or `UNKNOWN`. Consumer email
providers must not become organizations merely because their domains occur frequently. A proposed law-firm category is
an identity signal, not a privilege conclusion.

Keep intrinsic organization category separate from matter-specific role. For example, an organization may be a
`LAW_FIRM`, while `OUTSIDE_COUNSEL_FOR` records whom that firm represents in this matter. The model may recommend both,
but neither becomes operational until confirmed.

The model must not return `is_privileged` or classify a document.

### Phase IA-3: operator confirmation

Add an Identity Analysis results workspace that allows an authorized operator to confirm, reject, edit, merge, or
split suggestions and record matter-specific representation relationships. Domain recommendations enter the existing
Proposed Entities queue with a suggested category and canonical domain. Confirming the proposed entity makes that
signal operational; rejecting it removes its proposed domain mapping. Human corrections survive later analysis runs.

### Phase IA-4: versioned consumption

Publish an immutable identity snapshot. Privilege Review and other downstream analysis records the snapshot version it
used. A later Identity Analysis run creates a new version; it does not mutate historical review inputs. Operators can
selectively reassess documents affected by a corrected identity or relationship.

## Identity data boundaries

The initial implementation is matter-scoped and manual.

- Source-derived addresses and domains are observations.
- Organization and person mappings are suggestions until confirmed.
- Representation and legal roles are matter-specific.
- Suggested signals remain distinguishable from confirmed signals in every downstream prompt.
- Counsel presence does not establish privilege.
- Absence of known counsel does not establish non-privilege.
- Tenant and matter boundaries prevent representation knowledge from leaking across clients.

Teams, chat, directory, and other participant sources can later implement the same identity-observation contract
without changing the privilege reasoning contract.

## Storage approach

To limit Core table growth:

- Use one small Core job row per Identity Analysis run for lifecycle, coverage, compact summary, and artifact reference.
- Store the complete identity/domain inventory and later model recommendations in Artifact storage.
- Reuse the existing `matter_entity` directory for confirmed/proposed operational entities rather than creating a
  parallel directory.
- Add an organization-category field to canonical organization entities and a small confirmed domain-to-organization
  mapping table. Do not store rejected or unreviewed domain recommendations in those operational tables.
- Add only the relational representation/role data needed for operational querying and human correction when the
  recommendation workflow is implemented.

## Implementation phases

### Phase 1: standalone Identity Analysis foundation

- Matter-scoped manual trigger.
- Durable job lifecycle and progress.
- Metadata-only participant scan.
- Artifact-backed identity/domain inventory.
- Recent-run status and compact result summary in the Entities workspace.
- Tests proving document text and subject are never read.

### Phase 2: organization and role recommendations

- Aggregated-inventory LLM contract for categorized organization-domain proposals. Matter roles remain future work.
- Strict schema with no privilege disposition.
- Proposed organizations, people, aliases, domain mappings, organization categories, and matter roles.
- Domain review that can recommend `LAW_FIRM` while keeping `OUTSIDE_COUNSEL_FOR` as a separate matter relationship.
- Token, request, cost, and failure reporting.

### Phase 3: identity review workspace

- Review domain recommendations and source metadata. Accept/reject is implemented for the law-firm slice; editing,
  merge/split, and representation controls remain future work.
- Confirm, reject, edit, merge, and split.
- Matter-specific representation relationships and effective dates. Manual `OUTSIDE_COUNSEL_FOR` is implemented;
  model recommendations and additional relationship types remain future work.
- Immutable published identity snapshots.

### Phase 4: Privilege Review program foundation

- Add `PRIVILEGE_REVIEW` as a program type with its own state machine. The template and creation flow are implemented;
  evidence execution remains intentionally disabled until the next phase is implemented.
- Version the review and context populations. Review Batch population and confirmed identity-context artifacts are
  implemented. Creation accepts an existing Review Batch or atomically creates a READY `ALL_MATTER` batch named after
  the Review Program; saved-search/coding populations remain open.
- Pin matter guidance, identity snapshot, operational field, review policy, and family policy. Operational field,
  identity context, review route, and family-assignment policy are implemented. The operator explicitly selects the
  eligible matter coding field used for privilege review; guidance pinning remains open.
- Prepare an immutable `PRIVILEGE_FAMILY_CONTEXT` artifact from the frozen population. This is implemented as an
  explicit operator action: it normalizes parent/attachment/sibling relationships into review units, separates
  context-only members from the review denominator, preserves documents with missing relationships, records broken
  relationship exceptions, and publishes a new immutable Review Program version.
- Provide document/family coverage and exception accounting. The UI reports review units, multi-document families,
  context-only members, and relationship exceptions from the pinned family-context artifact.
- Run the Matter Definition workflow after family preparation. Template v2 reuses the established create/clone/adopt,
  assessment, refinement, reassessment, and explicit acceptance stages. Accepted guidance and assessment provenance
  are frozen into a new program version before privilege evidence can begin. Existing v1 Privilege Review programs
  upgrade through a new immutable program version when setup starts.

### Phase 5: evidence and adjudication

- The evidence stage cannot accidentally enter the Issue Review Q&A/classifier workflow. Its first explicit operator
  action now publishes immutable family review packets; model execution remains a separate gate.
- Family review-packet contract is implemented. It pins the accepted Matter Definition, family and identity-context
  artifacts, exact member metadata and text-source references; keeps context-only members outside the review
  denominator; deterministically splits families above 25 members while repeating the root as context; and reports
  missing-text, missing-input, and relationship exceptions before model spend.
- Cited privilege-evidence extraction is implemented as a durable, program-scoped fan-out over frozen family groups.
  The v5 workflow maps every in-scope family member through full source coverage, then deterministically merges the
  member observations into one citation-namespaced family artifact and attaches that artifact to every in-scope
  member. Context-only members remain bounded family context and stay outside the review denominator. An incomplete
  member makes the family an explicit evidence exception; it cannot silently produce a complete adjudication input.
  The configured `PRIVILEGE_FAMILY_EVIDENCE_V1` mode is fail-closed and must load the frozen family and identity
  context. The Google Batch API remains request- and byte-bounded, with isolated provider failures falling back to the
  real-time member path without reprocessing successful work. Extraction produces observations only and never writes
  the operational privilege field.
- Single-choice isolated recommendation is implemented at the same family boundary. The adjudication workflow runs
  once for each shared family observations artifact, then records the recommendation on every in-scope member of that
  family. Its v4 transport resolves zero-evidence criteria deterministically, sends only model-required criterion
  packets through request- and byte-bounded Google provider batches, retains successful packet artifacts, and retries
  only missing or invalid provider items through the real-time path. An unrecoverable family becomes an explicit
  exception rather than failing unrelated families. The resulting values are stored on a reviewable batch and are not
  published to matter coding without human action.
- Deterministic, matter-approved family rules.
- Human review queues and promotion to the operational privilege field. The program now stops at this explicit gate;
  the authoritative review and promotion workflow remains open.

### Phase 6: QC and production clearance

- Negative QC and mixed-family sampling.
- Complete-population readiness gate.
- Withhold, redact, release, and blocking-exception reporting.
- Privilege-log preparation from approved decisions.

## Stratified backtest before another full run

Do not validate the corrected path on another unstructured random sample. Freeze a reproducible family cohort from the
completed run and compare the old and corrected decisions on exactly the same cohort. The reference coding is used only
to select and score the cohort; it must be excluded from analysis snapshots, prompts, family context, and identity
context.

Use the six prior-recommendation/reference cells as the primary strata:

1. `privileged / reference-positive`
2. `privileged / reference-negative`
3. `needs-review / reference-positive`
4. `needs-review / reference-negative`
5. `not-privileged / reference-positive`
6. `not-privileged / reference-negative`

Select 20 families per cell where available, using a fixed seed. Within each cell balance standalone and multi-member
families; within multi-member families balance homogeneous and mixed reference labels where the population permits.
Tag rather than separately sample law-firm identity signal, member role, family size, and source-length quartile so the
report can expose regressions in those slices. Include every in-scope member of a selected family, never an isolated
document. If a cell has fewer than 20 families, take the complete cell and record the shortfall instead of silently
substituting a different population.

The frozen cohort manifest must record the seed, source program/run, family keys, selected document IDs, stratum,
population size, selection probability, reference labels, and hashes of the family packets and Review Guidance. Store
the scoring labels separately from the execution input manifest.

Run extraction and adjudication with fresh v5 workflow identities. Report paired old-versus-new results at both
document and family level:

- reference-positive capture into `privileged + needs_review`;
- false-negative rate among `not_privileged` recommendations;
- privileged precision and recall;
- outcomes by standalone/multi-member, homogeneous/mixed family, member role, identity signal, and length quartile;
- full-member extraction coverage, missing/failed members, and family merge exceptions;
- requests, tokens, estimated cost, provider wait, processing time, and end-to-end time.

The safety gate for a full rerun is at least 95% reference-positive capture into `privileged + needs_review`, no silent
incomplete-family adjudication, and no material regression in privileged precision. Because this is a deliberately
stratified cohort, publish both raw stratum results and population-weighted totals; do not interpret the unweighted
aggregate as production prevalence.

## Acceptance criteria for Phase 1

1. A matter administrator can manually start Identity Analysis from the Entities workspace.
2. Only matter documents and their structured sender/recipient metadata are read.
3. The run reports total documents, documents with participant metadata, unique valid addresses, unique domains,
   unresolved participant observations, unique normalized unresolved values, and documents without usable participant
   metadata.
4. The complete deterministic inventory is stored as an immutable artifact, while Core stores only a compact summary.
5. Starting another run creates a new snapshot without changing prior results.
6. A failed scan is visible and does not publish a partial result as completed.
7. The implementation does not automatically create entities or make privilege recommendations.

## Success measures for the complete program

- Every responsive document has an auditable terminal review route.
- No failed, missing-text, needs-review, or pending document is production-cleared.
- Reviewers can see the family and the identity evidence used by automation.
- Model recommendations remain distinguishable from human operational coding.
- Privileged precision/recall, false negatives, negative-QC yield, family coverage, review time, cost, and latency are
  measurable by program version.
