# Dataset Import Client

This package preprocesses local datasets and uploads them through the Priv-View Core API. It never writes directly to the Core database, Artifact database, or object store.

## Design

- `api.py` resolves methods and paths from `web/openapi.json` by FastAPI `operationId`. A missing or renamed operation fails immediately instead of silently calling a stale hard-coded route.
- `importer.py` is the dataset-neutral base importer. It handles authentication state, tenant storage, collection reuse, custodian reuse/creation, source-container preservation, iteration, standard MBOX/ZIP enumeration, basic email metadata, attachment extraction, parent/family links, idempotent item uploads, and reporting.
- `adapters/` owns only source discovery and dataset-specific mapping or parsing that cannot be generalized.

Current adapters:

- `enron-csv` reads the full Enron multiline `file,message` CSV format and derives the custodian key from the first maildir path segment. `top.csv` is the head-100 sample of the same `enron.csv` format. The base importer parses each emitted email and its attachments.
- `emc2` discovers each custodian MBOX and the DOJ ZIP, maps their custodians, and classifies known chat/transcript filenames. The base importer performs all MBOX message, attachment, and ZIP-member iteration. The published EMC-2 `crisis_team_evaluation.txt` attachment has one malformed base64 character; the adapter applies a hash-verified repair and records it in the item's raw metadata.

## Inspect without uploading

```bash
pipenv run python -m scripts.import_client enron-csv \
  --source ../enron/top.csv \
  --dry-run

pipenv run python -m scripts.import_client emc2 \
  --source ../EMC-2 \
  --dry-run
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
```

The importer automatically logs in again and retries a request once when its access token expires. File streams are rewound before retrying, so a source-container upload is not replayed with an empty body. An existing access token may be supplied through `PVR_IMPORT_ACCESS_TOKEN` instead of initial email/password login; set `PVR_IMPORT_EMAIL` and `PVR_IMPORT_PASSWORD` as well if that token should have automatic re-login available. Useful controls include `--limit`, `--continue-on-error`, and the EMC-2-specific `--no-attachments`.

Every adapter must expose its aggregate inputs as `SourceContainer` records. `BaseImporter` knows how to iterate `MBOX` and `ZIP` containers and how to expand email attachments; an adapter supplies custodian and record-type mappings. Only a genuinely custom source shape, such as Enron's multiline CSV, implements `custom_items`. All emitted items retain their source-container key.
