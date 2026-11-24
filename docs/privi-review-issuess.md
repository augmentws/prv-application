# Privilege Review: Findings, Issues, and Recommended Direction

## Purpose

This document records what we learned while importing the TREC Legal Track 2010 privilege data, reconstructing the
review instructions, running the current Question Answering (Q&A) decision models against the imported labels, and
examining parent/attachment families. It is intended to guide product design, model evaluation, and implementation of
a defensible privilege-review workflow.

This is a product and engineering analysis, not legal advice. Matter-specific privilege rules and final coding
decisions must remain under the control of qualified reviewers.

## Executive summary

The current generic document-level decision model is not, by itself, a good authoritative privilege reviewer.
Privilege is unusually dependent on relationships and context: who is an attorney, who represents whom, the purpose
of a communication, confidentiality and waiver, legal versus business advice, draft or work-product status, and the
relationship between an email and its attachments. The present model normally receives only one document's text and
metadata. Review guidance can tell it to consider a family, but it cannot follow that instruction when the family is
not in the input.

Our TREC-positive test demonstrates this clearly. The two model profiles marked only 13.61% and 21.56% of the 1,220
known-positive documents as privileged. Threshold tuning does not explain most of the gap. Many missed documents,
especially attachments, received strong `not_privileged` scores rather than borderline privilege scores. A
deterministic rule that promotes a positive attachment to its parent email produces only a small improvement because
the attachment itself is commonly missed.

The proper direction is a purpose-built, family-aware privilege workflow:

1. Retain `trec_priv_coding` as imported benchmark data and use the real `privilege` field for operational review.
2. Enrich the review input with family relationships and attorney/entity information.
3. Extract privilege signals and cited evidence before making a final legal classification.
4. Adjudicate at the family level, with explicit and auditable propagation rules.
5. Use one mutually exclusive output contract for `privileged`, `not_privileged`, or `needs_review` instead of three
   independent yes/no questions.
6. Treat model output as triage and decision support until a balanced evaluation establishes an acceptable operating
   point. Human coding remains authoritative.

## Source material and limitations

The analysis used the following materials supplied for TREC Legal Track 2010:

- `AssessmentGuidelines_leg_int_2010.pdf`, particularly the Topic 304 guidance on page 20.
- `LEGAL10.OVERVIEW.pdf`, particularly the privilege-review discussion on pages 24–26.
- `trecov10wn.pdf`.
- `LT10_Complaint_K_final-corrected.pdf`.
- `TRECLega2007l_HowToGuide_Version1.1.doc`, as background from an earlier Legal Track.

These sources help reconstruct the task, but they do not appear to contain the complete operational guidance, legal
instruction, training, and judgment calls available to the original assessors. The TREC labels are valuable reference
data, but they should not be treated as a complete statement of the legal standard or as infallible ground truth.
Unexpected labels need to be audited rather than explained away.

## Imported data model

The imported TREC qrels belong to the matter, not to the source collection. They were imported as reviewer codings by
the importing user so that they can participate in batch statistics and comparisons.

The two fields have intentionally different purposes:

| Field | Purpose |
| --- | --- |
| `trec_priv_coding` | Boolean reference label imported from the TREC Topic 304 qrels |
| `privilege` | Operational matter coding with `privileged`, `not_privileged`, and `needs_review` values |

The reference label must not silently become the operational decision. Keeping the fields separate lets us compare a
review process with the benchmark, investigate disagreements, and preserve the provenance of each answer.

An assessed qrel value of `0` is still part of the coded set; it is not the same as an uncoded document. Documents with
no qrel assessment form a separate population. This distinction matters when constructing evaluation or publication
sets.

## What the TREC guidance says about families

The available guidance does not support blanket-coding every member of a family as privileged. Instead:

- Each email and attachment is assessed as an individual document.
- An attachment should be evaluated with the surrounding family context.
- When an attachment is privileged or work product, the transmitting parent email is protected under the Topic 304
  instruction.
- Families were sampled together so the assessor could see that context.

The imported data agrees with this interpretation. The 1,220-document positive batch contains 941 parent emails and
279 attachments across 941 message/family units. Every document has a family identifier. Expanding those families to
all available matter documents produces 1,569 documents: 1,220 positive, 348 negative, and one without a TREC label.

Of the 941 families, 544 are singletons and 397 contain multiple documents. Among the multi-document families, 202
are all-positive and 195 contain a mixture of positive and negative documents. Across all families, 746 are
all-positive and 195 are mixed. In addition, 225 positive parent emails have at least one positive attachment.

The mixed families are decisive evidence that the benchmark did not merely copy one code to every family member.
Family expansion is necessary to supply context, but family membership alone is not a positive privilege rule.

## Evaluation cohort

The principal diagnostic cohort was:

- Matter: `76c06d05-7887-40c6-8364-17a91a32a60b`
- Batch: `4e634a97-99c8-4ed4-9490-ca2f52c44def` (`TrecPriv:Yes`)
- Population: 1,220 documents, all with `trec_priv_coding = true`
- JEV run: `9ccf60a1-d692-48e1-a407-a9abde723d41`
- OpenAI run: `a42c3ff6-e6f9-4357-a893-84f158e04109`
- Task version: `af4f384a-5cef-4b0c-aa09-917560a8c94f` (`Priv Review One`)

Because every document in this cohort is TREC-positive, the experiment measures positive recall and the nature of
false negatives. It cannot measure precision or the false-positive rate. A model that labels every document
privileged would score perfectly on this cohort and still be useless. Deployment decisions therefore require a
balanced, representative cohort containing TREC-positive and TREC-negative families.

## Observed model results

### Materialized document codings

| Outcome | JEV | OpenAI Decisions |
| --- | ---: | ---: |
| `privileged` | 166 (13.61%) | 263 (21.56%) |
| `not_privileged` | 603 | 769 |
| `needs_review` | 26 | 24 |
| No materialized value | 424 | 162 |

The small difference between the population and the displayed outcome totals is attributable to failed or otherwise
non-materialized document execution. It should be reported explicitly in operational views rather than merged into
the no-value category.

The profiles agreed on `privileged` for 144 documents. Taking the union of either profile's positive decision finds
285 documents, or 23.36% of the known-positive cohort. They agreed on `not_privileged` for 559 documents. Among the
279 TREC-positive attachments, both profiles called 150 `not_privileged`.

### What “no value” means

No value is not equivalent to `not_privileged`, and in this run it usually was not a provider or processing failure.
The task asks three independent yes/no questions:

- `privilege.privileged`
- `privilege.not_privileged`
- `privilege.needs_review`

Each answer must meet a 0.70 materialization threshold. Every completed no-value document had all three scores below
that threshold. The policy therefore abstained even though the model had returned scores.

The JEV maximum score for no-value documents had a median of 0.61, a 75th percentile of 0.65, and a 90th percentile
of 0.68. The OpenAI maximum score had a median of approximately 0.475, a 75th percentile of 0.60, and a 90th
percentile of 0.67.

Lowering the threshold is not a clean solution because the independent questions can all become true:

| Threshold | JEV any answer | JEV conflicting answers | OpenAI any answer | OpenAI conflicting answers |
| --- | ---: | ---: | ---: | ---: |
| 0.70 | 795 | 32 | 1,056 | 30 |
| 0.65 | 916 | 53 | 1,081 | 42 |
| 0.60 | 1,050 | 101 | 1,102 | 45 |
| 0.50 | 1,213 | 408 | 1,130 | 68 |

Even an argmax counterfactual, which forces the highest of the three answers to win, would produce only 284 JEV and
329 OpenAI privilege decisions. Most errors are therefore not caused by a threshold that is slightly too high.

The contract is structurally mismatched to the field. A mutually exclusive enum should be returned as one choice,
with a confidence value and an explicit route to `needs_review`. Three independent NOUL questions manufacture
conflicts and ambiguous abstentions.

## Family context is missing from model input

The current document evaluator builds state from the selected document's content, metadata, paragraphs, and matter
context. It does not include the parent, children, sibling attachments, or a family-level representation. The relevant
implementation is in `app/analysis_task_playground.py`, while the permitted decision paths are defined in
`app/decision_specifications.py`.

This creates a direct contradiction: the review guidance can instruct the model to consider family context, but the
runtime does not provide that context. An attachment is often presented as an isolated contract, spreadsheet,
question list, or draft with no indication of who sent it, to whom, or for what purpose.

### Effect of post-hoc parent propagation

Applying the Topic 304 child-to-parent rule after the current decisions recovers only a small number of additional
positive documents:

| Profile | Direct document recall | Additional parents | Recall after propagation |
| --- | ---: | ---: | ---: |
| JEV | 13.61% | 28 | 15.90% |
| OpenAI Decisions | 21.56% | 27 | 23.77% |

At the family/message level, the profiles find any positive member in only 15.83% and 24.76% of positive families,
respectively. Propagation cannot help when the attachment that should trigger it was itself classified as
non-privileged.

This is exactly what occurred at scale. JEV called 165 TREC-positive attachments `not_privileged`; OpenAI called 190
of them `not_privileged`. For JEV, the privilege score on these attachment false negatives had a median of 0.13, a
90th percentile of 0.29, and a maximum of 0.65. For OpenAI, the corresponding values were 0.01, 0.09, and 0.55.
These are substantive model disagreements, not near-threshold misses.

## Representative family failure

One examined family illustrates the problem:

- Parent matter document: `7633ce61-d2c7-4529-8e12-b1540ec5c2f7`
- Subject: `FW: Questions We Need Dominion To Answer`
- Attachment: `eb380d33-55ff-49dd-94eb-16efe45dec7f`

The attachment is a list of questions concerning litigation and discovery. In isolation, both profiles strongly
favored `not_privileged`:

| Profile | Privileged | Not privileged | Needs review | Materialized result |
| --- | ---: | ---: | ---: | --- |
| JEV | 0.16 | 0.78 | 0.27 | `not_privileged` |
| OpenAI | 0.01 | 0.98 | 0.02 | `not_privileged` |

The parent email contains materially different context: an outside-counsel address sends the questions to an Enron
employee and asks the employee to review them and identify additional questions for the third party. That is a strong
potential attorney-client/work-product signal that is absent from the attachment text.

Even the parent was not cleanly classified as privileged. JEV returned 0.66 privileged, 0.35 not privileged, and
0.72 needs review, materializing `needs_review`. OpenAI returned 0.37, 0.62, and 0.64, producing no value. The example
therefore shows both problems: attachment context is missing, and a generic model may still require better attorney
identity, purpose, and legal-standard reasoning after context is supplied.

Another inspected TREC-positive attachment appeared to be a standalone contract definition whose parent was broadly
circulated, including to an external standards group. Both models confidently favored non-privileged. Examples like
this may represent missing historical instructions, a subtle privilege theory, or label noise. They should be queued
for legal audit rather than used to force the model to imitate an unexplained label.

## Why privilege is a poor fit for a generic document classifier

Privilege is not simply a topic detectable from words. A robust decision may depend on:

- Attorney, client, employee, consultant, and third-party identities and roles.
- Which organization an attorney represented at the relevant time.
- Whether legal advice was sought or provided, rather than ordinary business advice.
- Confidentiality, distribution, forwarding, and possible waiver.
- Litigation anticipation and work-product purpose.
- Draft status and the purpose for which a draft was prepared.
- Parent/attachment and sibling relationships.
- Matter-specific legal guidance and exceptions.

Embeddings and ordinary text classification can still help prioritize documents with similar language. They do not
reliably encode these relationships or establish the legal purpose of a communication. The current results support
using Q&A decisions for triage, issue spotting, prioritization, and routing to review—not silently treating them as
authoritative privilege coding.

## Recommended target workflow

### 1. Build a family-aware review packet

The packet should remain bounded, but it must give the model the facts a reviewer would use:

- The selected document's text and metadata.
- Parent sender, recipients, CC/BCC, subject, date, and a bounded parent body or summary.
- Child and sibling attachment names, types, and bounded summaries.
- Stable family identifiers and document roles.
- Known attorney/entity roles, with provenance and effective dates where available.
- Any missing-text or extraction-quality indicators.

For a child attachment, parent context is usually the highest-value addition. For a parent email, attachment summaries
and first-pass signals are more useful than concatenating every attachment in full. Very large families should be
processed in stages rather than placed in one unbounded prompt.

### 2. Separate evidence extraction from adjudication

The first stage should extract structured, cited observations rather than jump directly to a legal label. Example
signals include:

- Attorney/client participants and the evidence supporting each role.
- Request for or provision of legal advice.
- Business-purpose indicators.
- Litigation or work-product indicators.
- Draft-document indicators.
- Third-party disclosure and possible waiver indicators.
- Confidentiality indicators.
- Family facts that alter the apparent purpose of an attachment.

Every signal should cite source text or metadata. Missing evidence should remain missing; the model must not invent an
attorney relationship from an unfamiliar email domain.

### 3. Adjudicate the family

A second, focused adjudicator should receive the structured evidence and the matter's privilege guidance. It should
return one mutually exclusive document disposition:

- `privileged`
- `not_privileged`
- `needs_review`

The output should also contain the claimed privilege basis, decisive evidence, countervailing facts, confidence, and
an operator-readable reason. Family propagation must be deterministic, explicit, and recorded—for example, a
privileged attachment protecting a transmitting parent under the applicable guidance. There must be no blanket
positive propagation to all family members.

### 4. Keep a human in the authoritative loop

The system should prioritize likely positives and ambiguous families, show the evidence and family together, and let
the reviewer make or confirm the operational `privilege` coding. Reviewers must be able to disagree with both the
model and `trec_priv_coding` without overwriting either source.

## Product and UI implications

Privilege review requires an explanation, not just a score. The UI should show:

- Parent, attachments, and siblings as direct document links.
- Which family context was actually supplied to the model.
- The model's disposition, confidence, privilege basis, evidence, and countervailing facts.
- Whether a value was materialized, abstained below threshold, failed, or was propagated by a rule.
- The exact reason a document needs operator attention and the action the operator can take.
- Benchmark coding, model coding, and human operational coding as distinct sources.

Family navigation is now available through
`GET /v1/matters/{matter_id}/documents/{document_id}/family` and the review workspace. A parent displays its
attachments; a child displays its parent and siblings. The lookup is cached per selected document and required no new
table or migration. This improves human review, but it does not yet add family context to Q&A model requests.

The batch comparison and statistics surfaces should report document-level and family-level results. A missing model
value must not be displayed as a negative result. Positive-only evaluations should be clearly labeled as recall-only.

## Evaluation plan

Before promoting a family-aware workflow, freeze a representative cohort of roughly 200–500 complete families. It
should include positives and negatives and be stratified by:

- Parent email versus attachment.
- Singleton versus multi-document family.
- Attorney-client, work-product, draft, and other asserted bases.
- Internal, external, and mixed recipient patterns.
- Text length, file type, missing text, and extraction quality.
- Straightforward decisions, ambiguous decisions, and suspected benchmark-label problems.

Compare at least these treatments:

1. Current single-document decision model.
2. Current decision plus deterministic child-to-parent propagation.
3. Family-aware signal extraction and adjudication.
4. Human reviewer baseline and, where appropriate, inter-reviewer agreement.

Report:

- Precision, recall, and F1 for privileged and non-privileged decisions.
- False-positive and false-negative counts, not accuracy alone.
- `needs_review` and abstention rates.
- Document-level and family-level results.
- Results by document role and asserted privilege basis.
- Calibration of confidence scores.
- Human review time saved or added.
- Provider requests, tokens, latency, and cost per document and family.
- Error categories, including missing text, missing relationships, unsupported assumptions, and legal disagreement.

The acceptance threshold should be set by the intended use. A triage tool can tolerate a different error profile than
an automated coding system. For privilege review, false negatives are particularly consequential, while excessive
false positives can make the workflow economically useless. Both must be visible.

## Implementation roadmap

### Immediate safeguards

1. Keep TREC reference labels and operational privilege coding separate.
2. Do not lower the 0.70 thresholds as a substitute for fixing the output contract.
3. Do not bulk-promote current model output as final privilege coding without human validation.
4. Clearly distinguish `not_privileged`, no value, `needs_review`, and execution failure.
5. Use the new family links during human review and disagreement analysis.

### Near-term engineering

1. Define and version a matter-scoped family review-packet contract.
2. Add attorney/entity role data with explicit provenance.
3. Add bounded parent context for attachments and bounded attachment summaries for parents.
4. Implement structured privilege-signal extraction with source citations.
5. Replace the three independent decisions with a single-choice privilege result schema.
6. Add a family adjudication and deterministic propagation stage.
7. Persist the reason, evidence, supplied context, and propagation provenance for audit.
8. Back-test the workflow on the frozen family cohort before controlled rollout.

### Longer-term product direction

1. Treat privilege as a dedicated review workflow rather than another generic classifier field.
2. Support active learning and targeted review from adjudicated disagreements.
3. Maintain a matter-specific attorney/entity directory and privilege guidance.
4. Generate auditable privilege bases and, where required, privilege-log inputs.
5. Add quality monitoring by family type, document role, data source, and model version.

## Open questions

- Can additional original assessor guidance or adjudication notes be obtained?
- How accurate and complete are the imported parent/attachment relationships?
- Which TREC-positive disagreements survive review by a qualified human using complete family context?
- What matter-specific attorney roster, representation periods, and third-party rules are available?
- Which deterministic propagation rules are legally approved for each matter?
- What false-negative and false-positive rates are acceptable for triage versus proposed coding?
- Should missing-text and extraction-quality errors be resolved at collection ingestion before a family becomes
  eligible for privilege analysis?

## Bottom line

The experiment did not show that automated assistance is useless for privilege review. It showed that a generic,
isolated-document classifier is solving the wrong abstraction. The high-value system is one that assembles the legal
and family context, extracts auditable signals, makes a bounded family-aware recommendation, exposes uncertainty, and
helps a reviewer reach a defensible decision. Until that workflow is evaluated on balanced data, the existing models
should be used as aids to review rather than as the source of truth.
