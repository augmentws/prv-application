# Dataset Import Client

This package preprocesses local datasets and uploads them through the Priv-View Core API. It never writes directly to the Core database, Artifact database, or object store.

## Design

- `api.py` resolves methods and paths from `web/openapi.json` by FastAPI `operationId`. A missing or renamed operation fails immediately instead of silently calling a stale hard-coded route.
- `importer.py` is the dataset-neutral base importer. It handles authentication state, tenant storage, collection reuse, custodian reuse/creation, optional source-container storage, iteration, standard MBOX/ZIP enumeration, basic email metadata, attachment extraction, parent/family links, idempotent item uploads, and reporting.
- `adapters/` owns only source discovery and dataset-specific mapping or parsing that cannot be generalized.

Current adapters:

- `athome4-loadfile` streams the TREC Athome4 CSV load file and uploads each referenced flattened Jeb Bush email without changing its bytes. It stores the two document IDs plus the 2015/2016 labels, important-topic values, and facets as imported JSON metadata. Semicolon-delimited judgments become arrays, and the adapter recognizes Athome4's nonstandard `Sent` email header.
- `enron-csv` reads the full Enron multiline `file,message` CSV format and derives the custodian key from the first maildir path segment. `top.csv` is the head-100 sample of the same `enron.csv` format. The base importer parses each emitted email and its attachments.
- `edrm-enron-v2` reads the extracted, de-duplicated EDRM Enron v2 text corpus used by the TREC Legal Track. It preserves each filename stem as the exact TREC document identifier, maps numbered attachment documents back to their containing email family, and derives custodians from the package directory names. Qrels are deliberately not stored on collection items; they are matter-scoped review judgments and are imported separately.
- `emc2` discovers each custodian MBOX and the DOJ ZIP, maps their custodians, and classifies known chat/transcript filenames. The base importer performs all MBOX message, attachment, and ZIP-member iteration. The published EMC-2 `crisis_team_evaluation.txt` attachment has one malformed base64 character; the adapter applies a hash-verified repair and records it in the item's raw metadata.
- `jeb-bush-inventory` streams the Jeb Bush `inventory.csv`. It uploads each EML as an EMAIL item and each existing, non-empty `<attachment-path>.txt` extraction sidecar as a child FILE item in the same family. It does not upload the original binary attachments or standalone image/OCR sidecars. Source attachment names, MIME types, sizes, and hashes remain in the text item's raw metadata. The adapter deliberately disables generic MIME attachment expansion so those binaries are not uploaded a second time. If an inventory EML is missing from disk, the adapter warns, skips that EML and its attachment rows, and continues with the next EML. If `skip.csv` exists beside `inventory.csv`, each non-empty first-column value is treated as an exact relative path to omit. A matching EML also omits its attachment rows; a matching attachment path (with or without its `.txt` suffix) omits only that text sidecar. Optional `path`, `file`, or `filename` headers are ignored. Every matched EML or attachment prints one `Skipped by skip.csv: <path>` message to stderr.
- `industrydocuments` streams UCSF Industry Documents pipe-delimited load files, including samples saved with a GNU `head` banner. Its `--source` may be one CSV or a directory, in which case every immediate `.csv` file is processed in natural filename order. It supports collections such as the San Francisco Walgreens and Insys litigation datasets that use the same 71-column schema. Each row's OCR text becomes the searchable item content, and the remaining populated columns are stored as imported JSON metadata. The load files do not include native files or custodians, so the adapter marks that provenance explicitly and assigns one collection-level unknown custodian. Rows without OCR text are retained as metadata-incomplete items with empty content. The former `sf-walgreens-loadfile` CLI name remains available as a compatibility alias.

Malformed email headers do not stop an import. Before upload, the importer replaces database-invalid NUL characters, bounds oversized sender, subject, and recipient values, and omits an oversized message ID rather than preserving a misleading partial identifier. Each normalization prints a `Warning <source_item_id>: ...` line and is recorded in the item's `raw_metadata.email_metadata_warnings`. The native EML is uploaded unchanged and remains the authoritative source for the original header bytes.

## Inspect without uploading

```bash
pipenv run python -m scripts.import_client athome4-loadfile \
  --source /Volumes/WorkingData/trec/athome4_loadfile.csv \
  --dry-run

pipenv run python -m scripts.import_client enron-csv \
  --source ../enron/top.csv \
  --dry-run

pipenv run python -m scripts.import_client edrm-enron-v2 \
  --source /Volumes/WorkingData/Ediscovery/enron/edrmv2 \
  --dry-run \
  --limit 100

pipenv run python -m scripts.import_client emc2 \
  --source ../EMC-2 \
  --dry-run

pipenv run python -m scripts.import_client jeb-bush-inventory \
  --source /Volumes/WorkingData/JBush/inventory.csv \
  --dry-run \
  --limit 100

pipenv run python -m scripts.import_client industrydocuments \
  --source /Volumes/WorkingData/Ediscovery/San_Francisco_Walgreens_Litigation_Documents/San_Francisco_Walgreens_Litigation_Documents.csv \
  --dry-run

pipenv run python -m scripts.import_client industrydocuments \
  --source /Volumes/WorkingData/Ediscovery/San_Francisco_Walgreens_Litigation_Documents/San_Francisco_Walgreens_Litigation_Documents_1.csv \
  --dry-run
```

Dry-run output includes source-container and item counts, byte totals, record types, and custodians.

Use repeatable `--include-record-type` options to load only selected normalized
record types. For example, this scans the full Insys folder but counts or uploads
only rows mapped to `EMAIL`:

```bash
pipenv run python -m scripts.import_client industrydocuments \
  --source /Volumes/WorkingData/Ediscovery/Insys_Litigation_Documents \
  --include-record-type EMAIL \
  --dry-run
```

Choices are `EMAIL`, `FILE`, `CHAT`, `TRANSCRIPT`, and `OTHER`. Repeating the
option forms a union; for example, include both `EMAIL` and `CHAT` by passing
the option once for each type.

Use the repeatable `--exclude-mime-type` option to omit documents based on the
MIME type inferred from their original filename. Matching is case-insensitive and
supports shell wildcards. This uses the native document type rather than the
`text/plain` MIME type of OCR content emitted by a load-file adapter. For example,
the following excludes legacy Excel and macro-enabled Excel documents while
retaining PDFs and other OCR records:

```bash
pipenv run python -m scripts.import_client industrydocuments \
  --source /Volumes/WorkingData/Ediscovery/Insys_Litigation_Documents \
  --exclude-mime-type 'application/vnd.ms-excel*' \
  --dry-run
```

Repeat the option to add unrelated MIME families, such as
`--exclude-mime-type 'image/*'`. Exclusions are applied before `--limit`, and both
dry-run and upload reports include `items_excluded` and
`excluded_by_mime_type` counts.

Original source containers are not stored by default. Add
`--store-source-containers` to upload each input CSV, MBOX, or ZIP as a separate
source artifact and link imported items to it. This option can substantially
increase storage use for load files that contain large OCR fields.

Dry-run output includes `item_gb` and `total_upload_gb` in decimal gigabytes
(1 GB = 1,000,000,000 bytes). `total_upload_gb` includes source-container bytes
only when `--store-source-containers` is present. It estimates artifact payload
content and does not include metadata, multipart transfer, database, index, or
object-store overhead.

Malformed CSV input stops an import by default with the exact filename, number
of complete records, and physical line where parsing ended. If a source is known
to contain incomplete records and omitting them is acceptable, add
`--skip-malformed-records`. The loader emits a warning for every skipped record
and continues with the next CSV; it never silently repairs or includes truncated
content.

## Upload

Supply non-secret settings as arguments or their `PVR_IMPORT_*` environment equivalents. The tenant ID and slug identify the upload target and its artifact storage; they are not sent as login credentials. Put the password in an environment variable or omit it to receive an interactive prompt.

```bash
export PVR_IMPORT_TENANT_ID="tenant UUID"
export PVR_IMPORT_TENANT_SLUG="tenant-slug"
export PVR_IMPORT_CLIENT_ID="client UUID"
export PVR_IMPORT_EMAIL="admin@example.com"
export PVR_IMPORT_PASSWORD="local development password"

pipenv run python -m scripts.import_client athome4-loadfile \
  --source /Volumes/WorkingData/trec/athome4_loadfile.csv \
  --collection-name "TREC Athome4" \
  --workers 8 \
  --continue-on-error

pipenv run python -m scripts.import_client enron-csv \
  --source ../enron/enron.csv \
  --collection-name "Enron sample"

pipenv run python -m scripts.import_client edrm-enron-v2 \
  --source /Volumes/WorkingData/Ediscovery/enron/edrmv2 \
  --collection-name "EDRM Enron v2" \
  --workers 8 \
  --continue-on-error

pipenv run python -m scripts.import_client jeb-bush-inventory \
  --source /Volumes/WorkingData/JBush/inventory.csv \
  --collection-name "Jeb Bush Emails" \
  --workers 8 \
  --continue-on-error

pipenv run python -m scripts.import_client industrydocuments \
  --source /Volumes/WorkingData/Ediscovery/San_Francisco_Walgreens_Litigation_Documents/San_Francisco_Walgreens_Litigation_Documents.csv \
  --collection-name "San Francisco Walgreens Litigation Documents" \
  --workers 8 \
  --continue-on-error

pipenv run python -m scripts.import_client industrydocuments \
  --source /Volumes/WorkingData/Ediscovery/Insys_Litigation_Documents \
  --collection-name "Insys Litigation Documents" \
  --exclude-mime-type 'application/vnd.ms-excel*' \
  --workers 8 \
  --continue-on-error
```

The importer uses four concurrent upload workers by default; set `--workers 1` for serial operation or increase the value to raise concurrency. Work is queued with bounded backpressure, parent emails and their child files remain on the same worker in order, and the command waits for all queued uploads before returning its final report. The importer automatically logs in again and retries a request once when its access token expires. File streams are rewound before retrying, so an opted-in source-container upload is not replayed with an empty body. An existing access token may be supplied through `PVR_IMPORT_ACCESS_TOKEN` instead of initial email/password login; set `PVR_IMPORT_EMAIL` and `PVR_IMPORT_PASSWORD` as well if that token should have automatic re-login available. Useful controls include `--limit`, `--include-record-type`, `--exclude-mime-type`, `--store-source-containers`, `--skip-malformed-records`, `--continue-on-error`, and `--no-attachments`. For the Jeb Bush adapter, `--no-attachments` means that only EML items are loaded and text sidecars are skipped.

Every adapter must expose its aggregate inputs as `SourceContainer` records. `BaseImporter` knows how to iterate `MBOX` and `ZIP` containers and how to expand email attachments; an adapter supplies custodian and record-type mappings. Only a genuinely custom source shape, such as Enron's multiline CSV, implements `custom_items`. All emitted items retain their source-container key.

## Import TREC Qrels into a matter

Qrels are gold review judgments, not collection provenance. Import the EDRM
corpus into a collection, add that collection's documents to the target matter,
and then run the separate matter-scoped importer. It creates one judgment field
and one sampling-probability field per selected topic. Only assessed rows (`0`
or `1`) are imported; `-1` and `-2` rows remain unassessed and are reported as
skipped.

Inspect Topic 304 (the TREC 2010 privilege topic) without changing the matter:

```bash
pipenv run python -m scripts.trec_qrels_import \
  --source /path/to/qrel_leg_int_2010_doc_post.txt \
  --topic 304 \
  --matter-id "matter UUID" \
  --collection-id "EDRM collection UUID" \
  --dry-run
```

Import the judgments after the collection documents have been published to the
matter:

```bash
pipenv run python -m scripts.trec_qrels_import \
  --source /path/to/qrel_leg_int_2010_doc_post.txt \
  --topic 304 \
  --matter-id "matter UUID" \
  --collection-id "EDRM collection UUID"
```

The five Qrels columns are parsed as topic, iteration, TREC document ID,
relevance judgment, and assessment sampling probability. Imports are
idempotent. Missing collection documents and documents not yet present in the
matter are reported and cause a nonzero exit status; no Qrels value is written
to a collection item.
