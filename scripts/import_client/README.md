# Dataset Import Client

This package preprocesses local datasets and uploads them through the Priv-View Core API. It never writes directly to the Core database, Artifact database, or object store.

## Design

- `api.py` resolves methods and paths from `web/openapi.json` by FastAPI `operationId`. A missing or renamed operation fails immediately instead of silently calling a stale hard-coded route.
- `importer.py` is the dataset-neutral base importer. It handles authentication state, tenant storage, collection reuse, custodian reuse/creation, source-container preservation, iteration, standard MBOX/ZIP enumeration, basic email metadata, attachment extraction, parent/family links, idempotent item uploads, and reporting.
- `adapters/` owns only source discovery and dataset-specific mapping or parsing that cannot be generalized.

Current adapters:

- `enron-csv` reads the full Enron multiline `file,message` CSV format and derives the custodian key from the first maildir path segment. `top.csv` is the head-100 sample of the same `enron.csv` format. The base importer parses each emitted email and its attachments.
- `emc2` discovers each custodian MBOX and the DOJ ZIP, maps their custodians, and classifies known chat/transcript filenames. The base importer performs all MBOX message, attachment, and ZIP-member iteration. The published EMC-2 `crisis_team_evaluation.txt` attachment has one malformed base64 character; the adapter applies a hash-verified repair and records it in the item's raw metadata.
- `jeb-bush-inventory` streams the Jeb Bush `inventory.csv`. It uploads each EML as an EMAIL item and each existing, non-empty `<attachment-path>.txt` extraction sidecar as a child FILE item in the same family. It does not upload the original binary attachments or standalone image/OCR sidecars. Source attachment names, MIME types, sizes, and hashes remain in the text item's raw metadata. The adapter deliberately disables generic MIME attachment expansion so those binaries are not uploaded a second time. If an inventory EML is missing from disk, the adapter warns, skips that EML and its attachment rows, and continues with the next EML. If `skip.csv` exists beside `inventory.csv`, each non-empty first-column value is treated as an exact relative path to omit. A matching EML also omits its attachment rows; a matching attachment path (with or without its `.txt` suffix) omits only that text sidecar. Optional `path`, `file`, or `filename` headers are ignored. Every matched EML or attachment prints one `Skipped by skip.csv: <path>` message to stderr.

## Inspect without uploading

```bash
pipenv run python -m scripts.import_client enron-csv \
  --source ../enron/top.csv \
  --dry-run

pipenv run python -m scripts.import_client emc2 \
  --source ../EMC-2 \
  --dry-run

pipenv run python -m scripts.import_client jeb-bush-inventory \
  --source /Volumes/WorkingData/JBush/inventory.csv \
  --dry-run \
  --limit 100
```

Dry-run output includes source-container and item counts, byte totals, record types, and custodians.

## Upload

Supply non-secret settings as arguments or their `PVR_IMPORT_*` environment equivalents. The tenant ID and slug identify the upload target and its artifact storage; they are not sent as login credentials. Put the password in an environment variable or omit it to receive an interactive prompt.

```bash
export PVR_IMPORT_TENANT_ID="tenant UUID"
export PVR_IMPORT_TENANT_SLUG="tenant-slug"
export PVR_IMPORT_CLIENT_ID="client UUID"
export PVR_IMPORT_EMAIL="admin@example.com"
export PVR_IMPORT_PASSWORD="local development password"

pipenv run python -m scripts.import_client enron-csv \
  --source ../enron/enron.csv \
  --collection-name "Enron sample"

pipenv run python -m scripts.import_client jeb-bush-inventory \
  --source /Volumes/WorkingData/JBush/inventory.csv \
  --collection-name "Jeb Bush Emails" \
  --workers 8 \
  --continue-on-error
```

The importer uses four concurrent upload workers by default; set `--workers 1` for serial operation or increase the value to raise concurrency. Work is queued with bounded backpressure, parent emails and their child files remain on the same worker in order, and the command waits for all queued uploads before returning its final report. The importer automatically logs in again and retries a request once when its access token expires. File streams are rewound before retrying, so a source-container upload is not replayed with an empty body. An existing access token may be supplied through `PVR_IMPORT_ACCESS_TOKEN` instead of initial email/password login; set `PVR_IMPORT_EMAIL` and `PVR_IMPORT_PASSWORD` as well if that token should have automatic re-login available. Useful controls include `--limit`, `--continue-on-error`, and `--no-attachments`. For the Jeb Bush adapter, `--no-attachments` means that only EML items are loaded and text sidecars are skipped.

Every adapter must expose its aggregate inputs as `SourceContainer` records. `BaseImporter` knows how to iterate `MBOX` and `ZIP` containers and how to expand email attachments; an adapter supplies custodian and record-type mappings. Only a genuinely custom source shape, such as Enron's multiline CSV, implements `custom_items`. All emitted items retain their source-container key.
