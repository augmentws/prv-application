# Duplicate and near-duplicate detection plan

## Objective

Detect exact duplicates, textual near duplicates, contained variants, and email threads in a way that is
computed once, reused across matters, and defensible in review. The design splits the work into two
layers:

1. **Collection layer.** When documents are ingested into a collection, compute everything that depends
   only on the documents themselves: hashes, canonical text, fingerprints, verified pair scores, family
   links, and the email thread graph.
2. **Matter layer.** When documents are promoted into a matter, build duplicate groups from the
   collection-level pairs whose documents are both in the matter, using the matter's thresholds, its own
   representatives, and its reviewer overrides.

Neither layer deletes or modifies documents. Duplicate results suppress, batch, or propagate coding only
through explicit actions, such as promotion culling or a bulk tag job with duplicate expansion.

### Data model

- **Collection:** the database-backed set of all ingested documents.
- **Matter:** a selection of collection documents promoted by facets such as date range, custodian, and
  file type.

The guiding rule: anything intrinsic to the documents belongs to the collection. Anything that depends on
matter membership or on a review decision belongs to the matter.

### Non-goals

- **Semantic similarity.** Embedding similarity finds related documents, not duplicates, and its scores
  are hard to explain to opposing counsel. A "conceptually similar" feature may reuse the existing
  embeddings, but it is separate from this design and never contributes to duplicate groups.
- **Automatic suppression.** The system produces classifications. Suppression from review or production
  is a user decision.

## Approach and prior art

MinHash signatures, LSH candidate generation, and exact verification remain the standard production
approach for textual near-duplicate detection. The approach originates with Broder's resemblance and
containment work (1997) and is the method used to deduplicate recent large-scale text corpora (The Pile,
RefinedWeb, Dolma, FineWeb, RedPajama). Review platforms describe textual near-duplicate features in the
same terms: a similarity percentage against a principal document, with email threading as a separate
feature.

This plan adopts that core and adds:

- **LSH Ensemble** (Zhu et al., 2016) for containment candidates. Standard MinHash LSH estimates Jaccard
  similarity and cannot find a short document contained in a much longer one.
- **High-frequency shingle suppression** so boilerplate does not inflate similarity.
- **Metadata-based email threading**, which near-duplicate text matching cannot replace.
- **Reviewer-facing diffs** and a **validated threshold**.

Optional refinements, deferred unless profiling shows a need:

| Refinement | Benefit | When to adopt |
| --- | --- | --- |
| One-permutation hashing with densification (or C-MinHash) | One hash pass instead of 128 | Fingerprinting dominates ingest time |
| b-bit MinHash | Smaller signatures | Signature storage becomes significant |
| Weighted MinHash (ICWS, ProbMinHash) | Accounts for shingle frequency | Validation shows set semantics misgroup documents |
| Suffix-array exact-substring detection (Lee et al., 2021) | Finds long shared passages | Boilerplate or quoted-history detection needs more precision |

## Collection layer

### Duplicate generations

All collection-level results belong to a **duplicate generation**, following the same pattern as search
index generations. A generation pins:

- the algorithm version and full configuration (canonicalization rules, tokenizer, shingle size,
  signature size, seeds, LSH parameters, pair storage floors, bucket cap);
- the boilerplate shingle list (see below);
- the email-hash field list and normalization rules.

A collection has exactly one active generation. Changing the algorithm or rebuilding the boilerplate list
creates a new generation, which is built alongside the active one. Matters continue to use the generation
they reference until they are explicitly moved to the new one (see
[Moving a matter to a new generation](#moving-a-matter-to-a-new-generation)).

### Per-document processing at ingest

Each ingested document is processed once per generation:

1. **Hashes.** Native SHA-256. For email, an email hash over normalized from, to, cc, bcc, subject, sent
   date, body text, and the ordered list of attachment hashes. The same message collected from different
   custodians is rarely byte-identical, so the native hash alone is not enough for email.
2. **Canonical text.** Take the preferred-text artifact, using the same preferred-text fallback as
   search. Canonicalize conservatively: normalize line endings, apply Unicode NFC, collapse trailing
   whitespace, and do nothing that removes substantive content. Store the canonical-text hash for
   `TEXT_EXACT` matching.
3. **Eligibility.**

   | Status | Rule | Treatment |
   | --- | --- | --- |
   | `NO_TEXT` | Empty preferred text (image-only, failed extraction) | Exact native or email matching only |
   | `TOO_SHORT` | Fewer shingles than the configured minimum (default 20 after boilerplate removal) | Exact matching only |
   | `STRUCTURED` | Spreadsheets and other formats whose extraction order is unstable | Exact matching only in phase 1 |
   | `ELIGIBLE` | Everything else | Near-duplicate fingerprinting |

   Ineligible documents are never silently dropped. Their status is visible in collection and matter
   reports.
4. **Shingles.** Tokenize with word tokens for space-delimited scripts and character n-grams for Chinese,
   Japanese, and Korean text. Generate five-word shingles (character 5-grams for CJK text), then remove
   shingles on the generation's boilerplate list.
5. **Signature.** Compute a deterministic 128-value MinHash signature with the generation's fixed seeds.
6. **Family and thread metadata.** Record parent and attachment links, plus `Message-ID`, `In-Reply-To`,
   `References`, `Conversation-Index`, `Conversation-Topic`, normalized subject, participants, and sent
   date.

Shingle sets and signatures are written as batch-level Parquet artifacts in Artifact Service, not as
database rows.

### Boilerplate list

Boilerplate shingles are the shingles that occur in more than a configured share of the collection's
documents (for example 1%). Examples include confidentiality footers, signatures, disclaimers, and form
templates.

Document frequency changes as data is ingested, and a changed list changes every signature. The list is
therefore **frozen per generation**. It is computed when a generation is built, stored as an artifact,
and applied unchanged to later ingests. A collection report tracks how far current document frequencies
have drifted from the frozen list, so an administrator can decide when a rebuild is worth it.

### Pair generation

Pair generation runs incrementally for each ingested batch against the whole collection.

**Near duplicates (Jaccard).** The collection keeps a persistent MinHash LSH index of 16 bands of 8 rows
in Core (one row per document per band, about 10 million rows for 639,050 documents). New documents are
inserted into the index, and every document sharing a bucket with them becomes a candidate. The S-curve
midpoint of about 0.71 gives high recall for the default matter threshold of 0.85.

**Containment.** A persistent LSH Ensemble index, partitioned by shingle-set size, finds pairs where a
smaller document is largely contained in a larger one, such as an early reply contained in a long thread.

**Hot buckets.** Buckets larger than the configured cap usually indicate remaining boilerplate or a
template family. They are recorded in the collection report instead of being expanded into all pairs.

**Verification.** For every candidate, recompute exact similarity from the stored shingle sets:

- **Jaccard** `|A ∩ B| / |A ∪ B|`;
- **Containment** `|A ∩ B| / |A|`, where `A` is the smaller document.

**Storage floors.** Store verified pairs at or above a low floor (default Jaccard `0.70`, containment
`0.80`), not at the matter threshold. Each matter then applies its own stricter threshold without
recomputation. A matter threshold cannot be set below the generation's floor.

**Exact matches.** Exact duplicates are stored as hash groups, not as pairs: documents sharing a native
hash, an email hash, or a canonical-text hash form one collection hash group per match type. This avoids
storing a quadratic number of pairs for large exact-duplicate sets.

### Thread graph

Build the email thread graph at the collection level:

1. Link messages by `Message-ID`, `In-Reply-To`, and `References`.
2. Use `Conversation-Index` and `Conversation-Topic` where available.
3. As a fallback, link by normalized subject (reply and forward prefixes removed) combined with
   participants and dates.
4. For emails with missing or broken headers, use verified containment pairs as a last resort. Containment
   never overrides a metadata-based link.

The collection stores thread membership and parent-child links. Inclusive-email flags are **not** stored
here, because they depend on which messages a matter contains.

### Text changes

If a document's preferred text changes after ingest (for example re-OCR), the collection recomputes its
canonical text, eligibility, shingles, signature, and pairs, and records a new document text version.
Matters that contain the document are notified. Their existing groups are not changed automatically (see
[Stability](#stability)).

## Matter layer

### Matter duplicate settings

Each matter pins:

- the collection duplicate generation it uses;
- Jaccard threshold (default `0.85`) and containment threshold (default `0.90`), both at or above the
  generation's floors;
- deduplication scope: **global** or **per custodian**;
- whether `TEXT_EXACT` groups are shown alongside stronger exact classes.

### Building groups

Matter grouping is a set operation over collection results, with no text processing:

1. Select collection hash groups and verified pairs where **both documents are in the matter**, and, for
   per-custodian scope, share a custodian.
2. Apply the matter's thresholds to the pairs.
3. Apply exact-class precedence: `NATIVE_EXACT` > `EMAIL_EXACT` > `TEXT_EXACT`. A document receives only
   its strongest exact classification. Weaker exact matches between members of the same stronger group
   are not recorded as separate groups.
4. Run representative-based clustering on the remaining near-duplicate and containment pairs.

**Representative-based clustering** avoids the chaining problem of connected components, where A matches
B and B matches C but A and C are not similar:

1. Order the matter's eligible documents deterministically: longest canonical text first, then earliest
   date, then lowest document ID.
2. Take the next unassigned document as a new representative.
3. Add every unassigned document whose verified score **against the representative** meets the matter
   threshold.
4. Repeat until every document is assigned or left as a singleton.

The representative is always a document in the matter. Each membership stores its score against the
representative.

`TEXT_EXACT` groups carry a warning in the UI. Identical extracted text does not prove identical content:
spreadsheet formulas, tracked changes, comments, embedded images, and hidden rows or sheets can differ,
and OCR text for the same scan can vary between runs. `TEXT_EXACT` is never treated as automatically
suppressible.

Result classes:

- exact native or email duplicates;
- exact text duplicates;
- near duplicates;
- contained variants;
- email threads, with inclusive-email flags.

### Inclusive emails

For each collection thread with at least one message in the matter, compute inclusive emails **within the
matter**: messages whose content, including attachments, is not fully contained in any later message that
is also in the matter. If the final reply of a thread was not promoted, an earlier message becomes the
inclusive email for that matter.

### Families

Groups are computed per document, but every group records family context:

- An attachment matched to another attachment with a different parent is labelled
  `ATTACHMENT_DIFFERENT_FAMILY`. Suppressing or propagating coding to it must respect family integrity for
  production.
- A family-level summary identifies families whose parent and every attachment are duplicates of another
  family in the matter, which is the safe unit for family-level deduplication.

### When grouping runs

Grouping runs automatically after each promotion or removal, and on demand. Because it only reads stored
hashes and pairs, it is fast enough to run on every membership change.

### Stability

Reviewer coding may already have been propagated through existing groups, so incremental grouping keeps
groups stable:

- **Promotion.** New documents are compared with existing representatives first and attached to existing
  groups where they meet the threshold. New groups are formed only from documents that match no existing
  representative. Existing members are never moved between groups automatically.
- **Removal of a member.** Its membership is ended and recorded. The group keeps its ID.
- **Removal of a representative.** The group keeps its ID, and a new representative is chosen from the
  remaining members by the deterministic ordering. The change is recorded as a group event.
- **Collection text change.** The affected group is flagged for review. Its membership is not changed
  until a reviewer accepts the new scores or a regroup is run.

A **full regroup** is available only as an explicit user action. It creates a new matter result version
and does not change the version that existing coding references.

### Moving a matter to a new generation

When a collection builds a new duplicate generation, each matter stays on its pinned generation until a
user moves it. Moving runs a full regroup against the new generation and shows a comparison report
(groups split, merged, and unchanged) before the new result version becomes active.

### Reviewer overrides

Reviewers with the right permission can:

- remove a document from a group ("not a duplicate");
- change a group's representative;
- merge two groups.

Overrides are stored per matter with the actor, time, and reason. They are applied on top of computed
results and survive incremental grouping and generation moves. Two matters can hold different overrides
for the same documents.

## Promotion culling

Because exact-duplicate data exists before promotion, promotion can offer an optional culling step:
promote one copy per `NATIVE_EXACT` or `EMAIL_EXACT` group, globally or per custodian. The promotion
records the culling criteria, the counts, and the unpromoted documents, so the reduction can be defended.

Culled documents stay in the collection. The promoted copy records the custodians of the culled copies in
a duplicate-custodians field, subject to the access rules below.

Near-duplicate and `TEXT_EXACT` groups are not offered for promotion culling.

## Access boundaries

A collection can feed matters with different teams, ethical walls, or protective orders. A matter must
never reveal documents outside it. Every matter-facing result is limited to the matter's documents:

- groups, pairs, thread views, and counts include only promoted documents;
- diffs are only computed between two documents in the matter;
- a matter never names or counts an unpromoted duplicate, except the duplicate-custodians field when the
  matter's settings allow it.

Collection-level results (all pairs, hot buckets, boilerplate list, drift report) are visible only to
users with collection-level access.

## Explanations

Every persisted pair stores:

- match type and score;
- the generation that produced it;
- shingle counts for both documents and the intersection size.

Every matter membership also stores the matter threshold that admitted it.

The review UI shows an aligned, highlighted diff between a document and its representative, computed on
demand from canonical text. A score alone is not enough for a reviewer to accept a grouping.

## Validation

Before the default thresholds are adopted, sample pairs from score bands just above and just below each
threshold, have reviewers judge them, and report precision and estimated recall per band. Store the
validation set with the generation, so every threshold rests on recorded human judgments.

## Persistence

### Core, collection layer (authoritative)

| Table | Contents |
| --- | --- |
| `collection_duplicate_generation` | Collection, status, algorithm version, full configuration, boilerplate artifact, pair floors, timestamps |
| `collection_duplicate_batch` | Ingest work batches for fingerprinting, candidate generation, and verification; status and retry state |
| `collection_duplicate_document` | Generation, document, text version, native hash, email hash, canonical-text hash, eligibility, shingle count |
| `collection_duplicate_hash_group` | Generation, match type, hash, member documents |
| `collection_duplicate_lsh_band` | Generation, document, band number, bucket hash |
| `collection_duplicate_pair` | Generation, document pair, Jaccard, containment, shingle counts, intersection size |
| `collection_email_thread` | Thread ID, member messages, parent-child links, link method |

### Core, matter layer (authoritative)

| Table | Contents |
| --- | --- |
| `matter_duplicate_settings` | Pinned generation, thresholds, deduplication scope, display options |
| `matter_duplicate_result_version` | Result version, generation, settings snapshot, created by, status |
| `matter_duplicate_group` | Group ID, result version, match class, representative |
| `matter_duplicate_group_member` | Group, document, match type, score against representative, admitting threshold, start and end |
| `matter_duplicate_group_event` | Representative changes, member removals, flags raised by text changes |
| `matter_email_inclusive` | Result version, thread, document, inclusive flag |
| `matter_duplicate_override` | Reviewer overrides with actor, time, and reason |

### Artifact Service

- batch-level Parquet artifacts for shingle sets and MinHash signatures;
- the boilerplate shingle list for each generation;
- collection reports (ineligible documents, hot buckets, boilerplate drift);
- matter comparison reports for generation moves.

### OpenSearch (projection only)

Each matter index receives filterable fields only: group ID, representative flag, match type, score,
thread ID, and inclusive-email flag. It is updated after Core commits and is never authoritative.

## Scale

At 639,050 documents, comparing every pair would require about 204 billion comparisons. LSH reduces this
to candidate pairs. The collection's initial build fits on one appropriately sized worker, and ingest
after that is incremental. Batching exists for resumability and retries, not for distributed execution.
Matter grouping only reads stored results and is inexpensive.

## Delivery phases

1. **Collection exact duplicates.** Generations, hashes, canonical text, eligibility, hash groups,
   promotion culling.
2. **Matter exact groups.** Matter settings, grouping from hash groups, precedence, access boundaries,
   OpenSearch projection.
3. **Collection near duplicates.** Tokenization, boilerplate list, MinHash, persistent LSH index,
   verification, pair storage.
4. **Matter near-duplicate groups.** Representative clustering, stability rules, pair explanations,
   diff view.
5. **Email threading.** Collection thread graph, LSH Ensemble containment fallback, matter inclusive
   emails.
6. **Review integration.** Reviewer overrides, family-level summary, duplicate expansion in the bulk tag
   job.
7. **Generations and validation.** New-generation builds, matter generation moves with comparison
   reports, boilerplate drift report, threshold validation workflow.

## Open questions

- Is there one collection per client, or can a matter draw from several collections? If several, pairs
  are only generated within a collection unless collections share a generation.
- Does promotion ever change a document's text, for example matter-specific re-OCR? If so, the matter
  needs its own fingerprints for those documents.
- Should the duplicate-custodians field be allowed by default, given the access boundary rules?
- What are the default boilerplate cutoff and pair storage floors, and should they be configurable per
  collection?
- Should `STRUCTURED` documents get a dedicated cell-level comparison in a later phase?
- Which roles may create overrides, and do overrides require a second approver?
