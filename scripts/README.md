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
| `scripts.import_client` | Preprocess supported datasets and upload them through the Core API | Dry-run available; Core and Artifact APIs otherwise |
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
