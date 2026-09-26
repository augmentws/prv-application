# Technical debt register

This document records known architectural and product-quality gaps that are intentionally deferred. Each item
describes the current compromise, the intended end state, and a concrete completion condition.

## Matter Definition assessments

### Align document summaries with the reusable summary contract

**Current state:** The document-analysis result contains cited summary paragraphs, but it does not produce a short
document title or enforce a caller-selected summary word budget. The model receives source and document identifiers
but not the useful descriptive metadata anticipated by `docs/doc-summary.txt`, such as filename, document type,
document date, email subject, or sender. Because the Matter Definition is supplied in the same request, the boundary
between a topic-neutral document summary and Matter Definition-focused responsiveness analysis is also implicit.

The assessment's paragraph map and map/reduce coverage envelope are more complete than the legacy template's single
`truncated` flag and should remain the source of truth for omitted or partially analyzed text.

**Risk:** Summaries may vary substantially in length, omit useful document identity and context, or overemphasize
facts relevant to the current Matter Definition. That reduces scanability and makes a `SUMMARY` artifact less useful
for reuse outside the assessment that created it.

**Desired state:** Keep the document summary topic-neutral and keep Matter Definition application in the separate
responsiveness fields. Add a concise generated title, a deterministic summary-length policy, and authorized
descriptive metadata to the structured input and output. Preserve paragraph citations and explicit coverage data.

**Completion criteria:**

- The structured result includes a subject-focused title of no more than 10 words.
- The caller supplies a versioned summary word budget, and the output is validated against it.
- The model receives an allowlisted metadata snapshot including available filename, type, date, email subject, and
  sender fields without exposing unrelated or unauthorized metadata.
- Summary instructions explicitly require a topic-neutral account of the document; responsiveness analysis remains
  conditioned on the pinned Matter Definition.
- The rendered analysis shows the title and accurately discloses partial coverage or omitted ranges.
- Schema-version, prompt, renderer, artifact, and fixture tests cover short, long, metadata-poor, and partially
  analyzed documents.

### Persist assessment synthesis as an immutable artifact

**Current state:** Each document analysis is stored as an immutable `SUMMARY` artifact derived from the source
document. The batch-level synthesis is stored as mutable JSON in
`matter_definition_assessment_run.synthesis_result`; its refinement questions and topic taxonomy are also projected
into application tables. Regenerating synthesis replaces the assessment's current JSON result rather than creating
a separately addressable artifact version.

**Risk:** The provenance chain stops at the document-analysis artifacts. It is harder to reproduce, compare, retain,
or audit multiple synthesis attempts because the exact synthesis output is not represented as an immutable artifact
derived from the inputs that produced it.

**Desired state:** Store every successful synthesis as its own immutable artifact. Link it to the assessment and
workflow/skill run, and record its derivation from the complete set of input `SUMMARY` artifacts. Keep
`synthesis_result` and the normalized question/topic tables as read-model projections of the selected synthesis
artifact rather than the authoritative result.

**Completion criteria:**

- Every successful synthesis and synthesis regeneration creates a new immutable artifact.
- The artifact records the assessment, workflow run, skill version, model configuration, Matter Definition revision,
  coverage envelope, and exact input summary artifact IDs/hashes.
- The assessment identifies which synthesis artifact is current without deleting prior versions.
- Refinement questions and batch topics can be traced to the synthesis artifact that produced them.
- Regeneration, API, retention, and audit tests cover the versioned provenance chain.

### Define and validate `NEAR_MISS` criterion semantics

**Current state:** Document analysis permits `PRIMARY`, `SECONDARY`, and `NEAR_MISS` criterion matches, but the
managed-skill instructions do not define a sufficiently strict boundary for `NEAR_MISS`. Assessment synthesis counts
each model-labeled near-miss criterion occurrence; one document can contribute more than one occurrence.

**Risk:** Different model calls may use `NEAR_MISS` inconsistently. The resulting count can look more authoritative
than it is and may introduce weak or noisy signals into Matter Definition refinement questions.

**Desired state:** Define a near miss as a document that is materially related to a stated criterion but falls
outside at least one identifiable instruction boundary. Require the analysis to name that boundary, explain the
minimal policy choice that would include or exclude the document, and cite supporting paragraphs. Keep near misses
distinct from low-confidence matches, missing-document limitations, and general topical similarity.

**Completion criteria:**

- The document-analysis prompt and schema define the classification and require boundary evidence.
- Fixtures cover representative true near misses and common false positives.
- The UI labels the value as criterion-level occurrences and also shows the distinct-document count.
- Synthesis groups recurring occurrences before proposing questions and does not treat the raw count as a finding.

## Agent conversations

### Replace chatbot polling with server streaming

The current chatbot uses multiple polling loops for messages, runs, action requests, and conversation state. Replace
them with a durable, authenticated server-streaming path while keeping the database authoritative and preserving a
temporary polling fallback during rollout.

The architecture, delivery sequence, test plan, and acceptance criteria are documented in the
[chatbot server-streaming conversion plan](chatbot-server-streaming-plan.md). The follow-on work for progressive
assistant output is documented separately in the [chatbot token-streaming implementation plan](chatbot-token-streaming-plan.md).
