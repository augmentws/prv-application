# Scripts

This directory contains maintenance commands, data-import utilities, local
preprocessing tools, and developer harnesses. Run commands from the repository
root so that `.env`, the application packages, and generated contracts resolve
consistently. Python commands must run through Pipenv:

```bash
pipenv run python -m scripts.<module> --help
```

Review a command's scope before running it against shared data. Commands that
change a database, OpenSearch, the Core API, local source files, or an external
provider are called out below. None of these commands starts the DBOS workflow
worker.

## Command index

| Command | Purpose | Changes state? |
| --- | --- | --- |
| `scripts.backfill_matter_document_custodians` | Copy pre-migration custodian relationships from Artifact into Core | Core database |
| `scripts.batch_coding_import` | Import CSV values into a reviewer's canonical batch coding and immutable revision history | Core API |
| `scripts.cleanup_search_indexes` | Find or remove obsolete physical search indexes and generation rows | Dry-run by default; OpenSearch and Core database with `--execute` |
| `scripts.export_openapi` | Regenerate the web application's OpenAPI contract | `web/openapi.json` |
| `scripts.extract_eml_attachments` | Extract attachments from a local EML corpus and write an inventory | Local filesystem |
| `scripts.extract_inventory_text` | Create searchable text sidecars from an attachment inventory | Local filesystem |
| `scripts.assessment_model_baseline` | Export read-only assessment model, schema, retry, timing, and usage diagnostics | Reads Core; local JSON report only |
| `scripts.build_assessment_replay_corpus` | Freeze a content-free, stratified assessment replay corpus | Reads Core and Artifact; local JSON manifest only |
| `scripts.focused_assessment_repair_test` | Plan or execute read-only focused repairs for failed assessment model outputs | Reads Core, Artifact, and provider results; optional model calls; local JSON report only |
| `scripts.import_client` | Preprocess supported datasets and upload them through the Core API | Dry-run available; Core and Artifact APIs otherwise |
| `scripts.replay_assessment_synthesis` | Compile and inspect a deterministic staged-synthesis manifest without model calls | No, unless `--output` writes a local manifest |
| `scripts.run_assessment_replay` | Compare a candidate model against a frozen document-analysis corpus | Reads Core and Artifact; model calls and local JSON report only |
| `scripts.run_evidence_extraction_conformance` | Exercise the evidence-first extraction contract over one frozen source window | Reads Core and Artifact; one model request and a local JSON report only |
| `scripts.run_evidence_document_conformance` | Shadow one frozen document through extraction, deterministic aggregation, criterion adjudication, and decision | Reads Core and Artifact; bounded model requests and a local JSON report only |
| `scripts.run_evidence_conformance_cohort` | Repeat selected evidence-first shadow documents and summarize stability | Reads Core and Artifact; bounded model requests and local JSON reports only |
| `scripts.run_evidence_shadow` | Plan or explicitly execute artifact-backed evidence extraction for selected assessment documents | Dry-run reads Core and Artifact; `--execute` writes isolated workflow/artifact telemetry and makes bounded model calls |
| `scripts.run_evidence_adjudication_shadow` | Plan or explicitly adjudicate one durable evidence-observations artifact | Dry-run reads Core and Artifact; `--execute` writes isolated workflow/artifact telemetry and makes bounded model calls only for non-empty criteria |
| `scripts.run_evidence_narrative_shadow` | Plan or explicitly add optional prose to a completed evidence-first compatibility result | Dry-run reads Core and Artifact; `--execute` writes an isolated narrative artifact and makes at most one bounded model call |
| `scripts.build_evidence_outcome_manifest` | Reconcile all evidence-first document stages for one assessment | Dry-run reads Core workflow state; `--publish` writes one immutable `ANALYSIS_OUTCOME_MANIFEST` artifact without model calls |
| `scripts.trec_matter_publish` | Publish a TREC coded set plus a deterministic uncoded sample to a matter | Manifest only with `--dry-run`; Core API otherwise |
| `scripts.trec_qrels_import` | Import assessed TREC Qrels as the authenticated user's review-batch coding | Core API |
| `scripts.voyage_embedding_harness` | Prepare and inspect Voyage embedding batch files or operate a Voyage batch | Local filesystem and, for provider commands, Voyage API |

## Database and search maintenance

### Backfill matter-document custodians

Use this once for data created before migration `0007`, when Artifact already
contains collection-item custodian relationships that Core matter documents do
not yet contain:

```bash
pipenv run python -m scripts.backfill_matter_document_custodians
pipenv run python -m scripts.backfill_matter_document_custodians --batch-size 500
```

The command reads the Artifact database and adds missing
`MatterDocumentCustodian` rows to the Core database in committed batches. It is
idempotent: existing relationships are retained. Unknown custodians and
client-mismatched relationships are counted and skipped. Both database
connection configurations must be available through `.env`. There is no
dry-run mode.

### Clean obsolete search indexes

Inspect what the cleanup policy would remove:

```bash
pipenv run python -m scripts.cleanup_search_indexes
pipenv run python -m scripts.cleanup_search_indexes \
  --matter-id 3e1be7f2-c84d-4cca-bfa5-ebae033b0d08
```

The default is a read-only dry run. A matter ID may be repeated to limit the
plan to several matters. Without it, the command examines every matter with an
active index generation.

Apply the displayed policy only after reviewing the plan:

```bash
pipenv run python -m scripts.cleanup_search_indexes \
  --matter-id 3e1be7f2-c84d-4cca-bfa5-ebae033b0d08 \
  --execute
```

`--execute` deletes obsolete physical OpenSearch indexes and prunes their stale
Core generation records. The active index and every alias target are always
preserved. The Core database and OpenSearch configuration must be available
through `.env`.

## API imports

### Import batch coding for a reviewer

This command authenticates as the specified reviewer and writes coding through
the public Core API. It never writes directly to the database.

Start with validation only:

```bash
export PVR_BATCH_IMPORT_EMAIL="reviewer@example.com"
export PVR_BATCH_IMPORT_PASSWORD="local development password"

pipenv run python -m scripts.batch_coding_import \
  --source ./reviewer-coding.csv \
  --matter-id 3e1be7f2-c84d-4cca-bfa5-ebae033b0d08 \
  --batch-id 9be866d8-a6ee-4db8-8f64-cbf8798db340 \
  --dry-run
```

Remove `--dry-run` to update the authenticated reviewer's canonical batch
coding. Each changed row appends an immutable `IMPORT` revision while updating
the current projection. The CSV must contain `matter_document_id` and
may contain `_review_status` (`COMPLETED` or `SKIPPED`). Other headers identify
batch coding fields by stable key or unique display name. Empty cells mean no
value. Use JSON arrays for `MULTIPLE` fields; `--multi-value-delimiter` can
enable a delimiter for legacy files. Enum labels are resolved to their stable
keys.

Useful controls:

- `--continue-on-error` records row failures and continues. Correct the failures
  and rerun the command. Each normalized row has a deterministic request key, so
  replaying identical data does not append a duplicate revision; changed data
  appends a new revision and becomes current.
- `--base-url` and `--openapi` override the API and contract locations.
- `--access-token-env` uses an existing access token. Supplying email and the
  password environment variable as well allows automatic reauthentication.

The command validates the entire CSV and confirms that every document belongs
to the batch before it changes reviewer coding. The detailed CSV contract is also in the
[Review batches guide](../web/content/docs/review-batches.mdx).

### Import a dataset into a collection

The dataset importer supports these adapters:

| Adapter | Source |
| --- | --- |
| `athome4-loadfile` | TREC Athome4 CSV load file and its referenced messages |
| `enron-csv` | Enron multiline `file,message` CSV |
| `emc2` | EMC-2 MBOX/ZIP dataset directory |
| `jeb-bush-inventory` | Jeb Bush EML attachment inventory and text sidecars |

Inspect a source without uploading it:

```bash
pipenv run python -m scripts.import_client jeb-bush-inventory \
  --source /data/JBush/inventory.csv \
  --limit 100 \
  --dry-run
```

For an upload, configure `PVR_IMPORT_TENANT_ID`,
`PVR_IMPORT_TENANT_SLUG`, `PVR_IMPORT_CLIENT_ID`, `PVR_IMPORT_EMAIL`, and
`PVR_IMPORT_PASSWORD`, then omit `--dry-run`. `PVR_IMPORT_ACCESS_TOKEN` can be
used instead of the initial login. `--workers` controls bounded concurrent
uploads, `--continue-on-error` retains processing after item failures, and
`--no-attachments` disables generic email attachment expansion or Jeb Bush
text-sidecar loading.

See the [dataset import guide](import_client/README.md) for adapter-specific
behavior, normalization rules, idempotency, credentials, and full examples.

## Local corpus preprocessing

### Extract EML attachments

```bash
pipenv run python -m scripts.extract_eml_attachments /data/JBush/eml
pipenv run python -m scripts.extract_eml_attachments /data/JBush/eml \
  --inventory /data/JBush/inventory.csv
```

The command recursively reads `.eml` files. For each message with attachments,
it creates a sibling directory named after the EML without its `.eml` suffix,
writes sanitized attachment files there, and marks that directory so later
runs do not mistake extracted EML attachments for source messages. It writes a
UTF-8 CSV inventory containing message and attachment hashes, paths, MIME
types, statuses, and errors. The default inventory is
`<directory>/eml_attachment_inventory.csv`.

Existing identical files are reused. Filename collisions with different bytes
receive a distinct destination rather than being silently overwritten. The
generated `rtf-body.rtf` MIME duplicate is intentionally ignored. Any recorded
error produces a nonzero exit status.

### Extract searchable text sidecars

Run this after creating an EML attachment inventory:

```bash
pipenv run python -m scripts.extract_inventory_text \
  /data/JBush/inventory.csv \
  --workers 4
```

The command reads attachment rows and writes UTF-8 sidecars beside their source
files as `<original-name>.txt`. It also writes a CSV report, defaulting to
`<inventory-stem>_text_extraction.csv`. Existing sidecars are reported and
retained unless `--overwrite` is supplied. An interrupted run retains a
`.partial` report.

Options include:

- `--root PATH` resolves inventory-relative paths against a different root.
- `--report PATH` selects the report location.
- `--include-eml` also extracts EML message bodies.
- `--limit N` bounds a test run.
- `--workers N` controls local extraction concurrency.
- `--timeout SECONDS` limits each external extraction step.
- `--ocr`, `--ocr-language`, and `--ocr-max-pages` enable bounded Tesseract OCR
  for PDFs without embedded text.
- `--tika-jar PATH` enables Apache Tika as an optional fallback.
- `--overwrite` replaces existing sidecars.

The script handles plain text, HTML, and EML with the Python standard library.
Depending on the source types, install `pdftotext` (Poppler), macOS `textutil`,
LibreOffice `soffice`, Tesseract/`pdftoppm`, or Java plus the supplied Tika JAR.
Missing tools are reported; affected files receive an appropriate report
status.

## Developer tools

### Export an assessment model baseline

Export aggregate execution diagnostics without model calls or database writes:

```bash
pipenv run python -m scripts.assessment_model_baseline \
  --assessment-id 4a1622f2-7859-474c-a9db-0b9dbe548f47 \
  --output /tmp/assessment-model-baseline.json
```

Add `--include-documents` to include document and SkillRun identifiers with per-document request, repair, validation,
timing, and token counters. The report never includes document text or model output. Historical runs created before
structured validation telemetry was added will show empty validation categories; their request-sequence and usage
totals remain available.

### Build and run an assessment replay corpus

Freeze a representative corpus from a completed assessment:

```bash
pipenv run python -m scripts.build_assessment_replay_corpus \
  --assessment-id 4a1622f2-7859-474c-a9db-0b9dbe548f47 \
  --target-size 24 \
  --output /tmp/assessment-replay-corpus.json
```

Selection is deterministic and retains final failures, historical failed attempts, and replayed documents, then
samples repair-heavy, multi-request, slow, and stable control cases. The manifest contains protected object identifiers, source and baseline artifact hashes,
compact result signatures, and historical usage. It contains neither source text nor complete model output. The corpus
hash covers the entire frozen contract and every case.

Run the exact frozen direct/map-reduce contract against one candidate model:

```bash
pipenv run python -m scripts.run_assessment_replay \
  --corpus /tmp/assessment-replay-corpus.json \
  --extraction-model google:gemini-3.5-flash-lite \
  --adjudication-model google:gemini-3.7-flash \
  --output /tmp/assessment-replay-gemini-flash-lite.json
```

The runner verifies the corpus, source, baseline artifact, guidance, skill, schema, prompt settings, limits, splitter,
and pipeline hashes before making model calls. It never writes assessment, workflow, SkillRun, invocation, or artifact
state. Use `--max-cases 1` or repeat `--case-id` for a bounded smoke test. Complete candidate output is excluded by
default; `--include-outputs` should be used only for a suitably protected local report.
Use `--max-output-tokens 40000` to test the newly raised document-analysis budget against a corpus frozen under the
former 20,000-token limit; the override is recorded in the report and does not mutate the assessment or skill version.

Verify all protected references and hashes without calling a model:

```bash
pipenv run python -m scripts.run_assessment_replay \
  --corpus /tmp/assessment-replay-corpus.json \
  --verify-only \
  --output /tmp/assessment-replay-verification.json
```

Exercise the evidence-first observation contract against one selected source window:

```bash
pipenv run python -m scripts.run_evidence_extraction_conformance \
  --corpus /tmp/assessment-replay-corpus.json \
  --case-id 1bd2cea7-9b40-4155-9486-0247be7f799e \
  --window-ordinal 2 \
  --model google:gemini-3.5-flash-lite \
  --output /tmp/evidence-extraction-conformance.json
```

The command verifies the frozen source and baseline artifact, calls the model exactly once with no automatic output
retry, and never changes assessment state. Local item-aware normalization preserves valid observations and reports
invalid optional items individually. Source text and observation facts are excluded from the report by default; add
`--include-observations` only for a suitably protected local report. Running the command sends the selected protected
source window and review guidance to the named external model provider, so obtain the required authorization first.
The provider-facing V2 contract uses one required primary citation and four nullable additional scalar citation slots;
this makes admitted paragraph IDs and the five-citation ceiling enforceable even when a provider removes array
`maxItems` constraints.

Build production-shaped plans for explicit documents from an existing assessment without calling a model or writing
database state:

```bash
pipenv run python -m scripts.run_evidence_shadow \
  --actor-user-id <root-or-tenant-admin-user-id> \
  --assessment-id 4a1622f2-7859-474c-a9db-0b9dbe548f47 \
  --document-id 1bd2cea7-9b40-4155-9486-0247be7f799e \
  --output /tmp/evidence-shadow-plan.json
```

The dry run is the default. The explicit actor must be an active root administrator or administrator of the matter's
owning tenant. It verifies assessment-batch membership, resolves the isolated managed extraction binding,
loads each immutable preferred text source, and reports source/plan hashes and unit counts without including source
text. Use `--max-documents N` instead of `--document-id` for the first N batch documents; the launcher enforces a
hard 25-document ceiling.

Execution is deliberately separate and disabled by default. After explicitly setting
`DEFINITION_ASSESSMENT_EVIDENCE_SHADOW_ENABLED=true`, add `--execute`. This creates or resumes an isolated shadow
workflow, persists plans and unit fragments, records SkillRun/ModelInvocation usage, and writes compacted observation
artifacts. It does not modify assessment results, document outcomes, synthesis, reviewer coding, or matter coding.
Retryable model failures are persisted and automatically advance only that extraction unit up to the bounded attempt
ceiling, including after restart; completed units are reused. Non-retryable, exhausted, and canceled units remain
explicit partial outcomes. `--retry-failed` is the operator action that advances an otherwise terminal failed or
canceled extraction unit. Executing sends the selected protected document text and Review Guidance to the configured
external model provider and therefore requires the corresponding authorization.

After an extraction shadow has produced an `ANALYSIS_OBSERVATIONS` artifact, build the separate criterion-adjudication
plan without rereading source text or calling a model:

```bash
pipenv run python -m scripts.run_evidence_adjudication_shadow \
  --actor-user-id <root-or-tenant-admin-user-id> \
  --assessment-id 4a1622f2-7859-474c-a9db-0b9dbe548f47 \
  --observations-artifact-id <analysis-observations-artifact-id> \
  --adjudication-model google:gemini-3.7-flash \
  --output /tmp/evidence-adjudication-plan.json
```

Dry-run remains the default. The launcher proves that the observations, extraction plan, frozen Review Guidance,
criterion catalog, matter, and assessment all belong together. It reports only identities and counts. It creates one
bounded packet per criterion (sharded at 32 observations); complete empty criteria become deterministic `NO_MATCH`
without a provider request, while incomplete evidence makes them `INSUFFICIENT`.

To execute, keep `DEFINITION_ASSESSMENT_EVIDENCE_SHADOW_ENABLED=true` and add `--execute`. The command persists an
adjudication plan, one durable fragment per packet attempt, and a compacted criterion-decisions artifact. It does not
modify the assessment, synthesis, document outcomes, reviewer coding, or matter coding. A rerun reuses completed
packets. Retryable failures automatically advance only the affected packet up to the bounded attempt ceiling and
resume after restart. `--execute --retry-failed` explicitly advances a terminal failed or canceled packet. The explicit
3.7 Flash override is recommended for the first validation cohort because prior conformance work showed the
adjudication role needs reliable distinction between a match and a related near miss; omitting the option uses the
configured model pinned by the managed skill.

After criterion compaction, the same execution now derives and persists `ANALYSIS_DOCUMENT_DECISION` without another
model request. The initial code-owned policy supports positive inclusion criteria: any `MATCH` makes the document
`RESPONSIVE`; absent a match, unresolved or incomplete evidence makes it `UNCLEAR`; otherwise it is
`NON_RESPONSIVE`. `NEAR_MISS` is preserved as a criterion boundary but does not itself make the document responsive.
`UNMAPPED` observations remain available in diagnostics but are not frozen criteria and cannot independently change
the document determination.
Guidance containing negative or contextual decision semantics is reported as `MODEL_FALLBACK_REQUIRED` rather than
being forced through the positive-only rule.

The final deterministic step writes `ANALYSIS_COMPATIBILITY_RESULT`. For a completed code-owned decision, its
`output` validates against the existing `document-analysis-pipeline-v4` contract. Adjudicated matches and near misses
become criterion matches, cited observation facts provide a bounded evidence digest, and extraction coverage is
translated from the immutable evaluation plan. The artifact records field-level provenance and explicitly marks
narrative as `NOT_REQUESTED`; it does not invent prose or clarification questions. If document-decider fallback is
required, the compatibility artifact is stored as `BLOCKED` with reasons and no fabricated output. All three artifacts
remain non-authoritative and cannot be projected into the assessment or coding by this command.

After the deterministic compatibility result is complete, the optional narrative stage can be planned separately:

```bash
pipenv run python -m scripts.run_evidence_narrative_shadow \
  --actor-user-id <root-or-tenant-admin-user-id> \
  --assessment-id 4a1622f2-7859-474c-a9db-0b9dbe548f47 \
  --compatibility-artifact-id <analysis-compatibility-result-artifact-id> \
  --output /tmp/evidence-narrative-plan.json
```

Dry-run reports only identities and counts. Add `--execute` to make at most one model request over observations already
admitted by criterion adjudication. The narrative model receives the frozen document decision, criterion decisions,
and only material admitted observations; its output can reference only those observation IDs. The application resolves
those references back to paragraph citations and produces a direct-schema `presented_output`. Determination,
confidence, criterion matches, coverage, limitations, and all other decision fields remain byte-for-byte equivalent to
the deterministic compatibility result.

The stage writes `ANALYSIS_NARRATIVE_PROJECTION` and never replaces the deterministic compatibility artifact. Empty
narrative lists retain the deterministic evidence digest. A model or schema failure ends the optional workflow as
`COMPLETED_WITH_ERRORS`, reports the unchanged compatibility artifact as the fallback, and can be retried with
`--execute --retry-failed`; it does not fail or re-run extraction, adjudication, or document decision. Executing sends
selected protected evidence to the configured external model provider and therefore requires corresponding
authorization. Apply artifact migration `0020_narrative_projection` before execution.

Reconcile the selected assessment population into one operator-facing outcome summary without making model calls:

```bash
pipenv run python -m scripts.build_evidence_outcome_manifest \
  --actor-user-id <root-or-tenant-admin-user-id> \
  --assessment-id 4a1622f2-7859-474c-a9db-0b9dbe548f47 \
  --output /tmp/evidence-outcome-manifest.json
```

The dry-run reports readiness and per-stage status counts plus deduplicated workflow usage. Add `--publish` to persist
the same immutable snapshot as `ANALYSIS_OUTCOME_MANIFEST`. Each document records extraction, adjudication,
code-owned decision, compatibility, and narrative state; bounded error summaries; exact artifact references; and the
stage-specific retry actions that are safe. A completed narrative is selected for synthesis, a failed narrative
selects the deterministic compatibility artifact as `READY_WITH_FALLBACK`, and a missing compatibility result is
`PENDING` or `ACTION_REQUIRED`. The builder never re-runs a stage. Apply artifact migration
`0021_analysis_outcome_manifest` before publishing.

Run a complete read-only evidence-first shadow document after single-window conformance succeeds:

```bash
pipenv run python -m scripts.run_evidence_document_conformance \
  --corpus /tmp/assessment-replay-corpus.json \
  --case-id 1bd2cea7-9b40-4155-9486-0247be7f799e \
  --extraction-model google:gemini-3.5-flash-lite \
  --adjudication-model google:gemini-3.5-flash-lite \
  --output /tmp/evidence-document-conformance.json
```

The command runs every frozen extraction window once, deduplicates exact overlap observations, adjudicates only
criteria with observations, supplies deterministic `NO_MATCH` decisions for empty complete packets, and combines the
positive criteria in code. It never performs output repair. A dropped observation marks evidence incomplete, makes a
negative document conclusion `UNCLEAR`, and reports `COMPLETED_WITH_ADJUSTMENTS`; source coverage and evidence
completeness are reported separately. Adjudication uses five disposition-neutral nullable scalar observation-reference
slots limited to the admitted evidence packet. Every relationship except `INSUFFICIENT` requires provenance. If the
model omits all references and the packet contains exactly one admitted observation, the runner attaches that sole
reference and records a normalization adjustment; it never guesses among multiple observations. The default report
excludes source text and observation facts.
Use `--model` as a shortcut when both roles intentionally use the same model.

Repeat a fixed cohort to measure output stability rather than relying on one successful run:

```bash
pipenv run python -m scripts.run_evidence_conformance_cohort \
  --corpus /tmp/assessment-replay-corpus.json \
  --case-id 19b1194c-1ca7-4e9e-9a94-35f48f116944 \
  --case-id 1bd2cea7-9b40-4155-9486-0247be7f799e \
  --case-id 247c3be1-5d40-42d6-b868-934894f99248 \
  --case-id 18ffc4b4-2fdd-4ad0-9a89-174a4a8b7629 \
  --case-id 17883d78-19c2-4f22-8579-04a62f30db8c \
  --model google:gemini-3.5-flash-lite \
  --repeats 3 \
  --report-directory /tmp/evidence-cohort-runs \
  --output /tmp/evidence-cohort-summary.json
```

Each child report excludes source text and observation facts by default. It uses the production compatibility
assembler, validates `document-analysis-pipeline-v4`, and proves that the result can compile into the current
synthesis manifest. The cohort summary compares determination stability, baseline and criterion agreement,
material-decision differences, compatibility validity, synthesis-input compatibility, observation-count ranges,
dropped observations, requests, tokens, and latency. This command sends every selected protected document and review
guidance to the named providers once per repeat; obtain the required authorization first.

Use `--all-cases` instead of individual `--case-id` options for a one-pass full-corpus calibration. The cohort also
accepts `--extraction-model` and `--adjudication-model`; `--model` remains the shared-model shortcut. Add `--resume`
to reuse only existing child reports whose corpus hash, contract version, case ID, and role-specific model bindings
match the requested run.

Compare one completed repeat with the frozen direct baseline artifacts without calling a model or changing the
assessment:

```bash
pipenv run python -m scripts.compare_evidence_conformance \
  --corpus /tmp/assessment-replay-corpus.json \
  --report-directory /tmp/evidence-cohort-runs \
  --repeat 1 \
  --output /tmp/evidence-compatibility-backtest.json
```

The comparator verifies frozen baseline hashes, revalidates legacy content-free reports through the production
compatibility assembler, separates document-decision disagreement from criterion-level differences, and compiles the
complete direct and evidence-first populations into synthesis manifests. Its report contains identities, signatures,
counts, and differences—not source text or complete direct baseline output.

### Test focused repair of failed assessment outputs

After a definition assessment reaches a terminal status, inspect every failed document without making model calls:

```bash
pipenv run python -m scripts.focused_assessment_repair_test \
  --assessment-id b72ca17d-e307-49cb-9729-318802abebd1 \
  --output /tmp/assessment-focused-repair-plan.json
```

Add `--execute-repairs` to run focused synchronous repairs using the assessment's pinned model. The harness supplies
only the invalid JSON, exact validation errors, relevant schema fragment, referenced evidence paragraphs, and any
guidance needed to repair the determination. It validates the complete repaired analysis but never updates the
assessment, document outcomes, skill runs, or workflow state. Its only write is the requested local JSON report.

Use `--max-documents` for an intentionally bounded diagnostic run. Use `--include-repaired-output` only when the
local report may safely contain document-analysis content. A running assessment is rejected by default so the test
uses the complete frozen failure set; `--allow-running` is available for read-only inspection.

### Regenerate the OpenAPI contract

```bash
pipenv run python -m scripts.export_openapi
cd web
pnpm generate:api
```

The first command imports the FastAPI application and replaces
`web/openapi.json` with its current schema. Regenerate the TypeScript client
afterward. Review and commit both generated changes with the API implementation.
This module performs its export when imported, so treat it only as a command.

### Voyage embedding batch harness

The harness has local file operations and provider operations. Prepare Voyage
request JSONL plus a manifest from source JSONL records shaped as
`{"id": ..., "text": ...}`:

```bash
pipenv run python -m scripts.voyage_embedding_harness prepare \
  source.jsonl requests.jsonl manifest.jsonl \
  --max-inputs 256
```

Resolve unordered provider output back to source IDs:

```bash
pipenv run python -m scripts.voyage_embedding_harness resolve \
  output.jsonl manifest.jsonl resolved.jsonl
```

`prepare` and `resolve` refuse to replace output files unless `--force` is
provided. They do not contact Voyage.

Provider commands require `EMBEDDING_PROVIDER=voyage_api`,
`EMBEDDING_MODE=remote`, a supported hosted `EMBEDDING_MODEL`, and
`VOYAGE_API_KEY` in `.env`:

```bash
pipenv run python -m scripts.voyage_embedding_harness submit requests.jsonl \
  --metadata purpose=diagnostic
pipenv run python -m scripts.voyage_embedding_harness status VOYAGE_BATCH_ID
pipenv run python -m scripts.voyage_embedding_harness wait \
  VOYAGE_BATCH_ID output.jsonl
pipenv run python -m scripts.voyage_embedding_harness cancel VOYAGE_BATCH_ID
```

`submit` uploads a file and creates a billable external batch. `wait` polls to
a terminal state and downloads completed or partially completed output; use
`--poll-seconds`, `--timeout-seconds`, and `--force` as needed. `cancel` changes
external provider state. These harness commands are for diagnostics and manual
operations, not a replacement for the application's durable embedding job.

## Internal modules

The following files support the commands above and are not standalone command
entry points:

| File | Responsibility |
| --- | --- |
| `scripts/__init__.py` | Marks `scripts` as an importable Python package. |
| `scripts/import_client/__init__.py` | Marks the dataset importer as a package. |
| `scripts/import_client/__main__.py` | Parses the `python -m scripts.import_client` command and selects an adapter. |
| `scripts/import_client/api.py` | Resolves Core endpoints by OpenAPI `operationId`, authenticates, and sends API requests. |
| `scripts/import_client/email_parser.py` | Parses MIME messages and normalizes email metadata. |
| `scripts/import_client/importer.py` | Implements dataset-neutral container iteration, attachment expansion, bounded uploads, idempotency, and reporting. |
| `scripts/import_client/models.py` | Defines the importer's typed source, item, custodian, and email records. |
| `scripts/import_client/adapters/__init__.py` | Exports the supported adapter classes. |
| `scripts/import_client/adapters/base.py` | Defines the adapter interface and shared MIME attachment handling. |
| `scripts/import_client/adapters/athome4_loadfile.py` | Maps TREC Athome4 load-file rows, labels, facets, and referenced messages. |
| `scripts/import_client/adapters/enron_csv.py` | Streams Enron's large multiline CSV and derives custodians from maildir paths. |
| `scripts/import_client/adapters/emc2.py` | Discovers EMC-2 MBOX/ZIP inputs and applies its hash-verified malformed attachment repair. |
| `scripts/import_client/adapters/jeb_bush_inventory.py` | Maps the Jeb Bush inventory, EML files, text sidecars, skip list, and family relationships. |

When adding a new runnable script, add it to the command index and give it a
section with its inputs, outputs, side effects, prerequisites, safe/dry-run
path, and Pipenv invocation. When adding an internal module or adapter, add it
to the internal-module table and update the dataset import guide if its behavior
is user-visible.
