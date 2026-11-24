"""Plan or explicitly execute a bounded, non-authoritative evidence-first shadow cohort."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from app.database import SessionLocal
from app.evidence_first_launcher import (
    MAX_SHADOW_DOCUMENTS,
    execute_prepared_evidence_shadow,
    prepare_evidence_shadow_launch,
    shadow_plan_report,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic evidence-first plan for selected documents in an existing Matter Definition "
            "assessment. The default is a read-only dry run. --execute requires the disabled-by-default shadow "
            "feature flag and writes only isolated workflow/artifact results."
        )
    )
    parser.add_argument(
        "--actor-user-id",
        type=uuid.UUID,
        required=True,
        help="Active root or owning-tenant administrator running this controlled operation",
    )
    parser.add_argument("--assessment-id", type=uuid.UUID, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--document-id",
        type=uuid.UUID,
        action="append",
        help="Assessment-batch Matter document ID; repeat for a bounded cohort",
    )
    selection.add_argument(
        "--max-documents",
        type=int,
        help=f"Select the first N assessment-batch documents, from 1 to {MAX_SHADOW_DOCUMENTS}",
    )
    parser.add_argument(
        "--map-max-characters",
        type=int,
        help="Override the configured deterministic extraction-window size",
    )
    parser.add_argument(
        "--extraction-model",
        help=(
            "Override provider:model for this isolated shadow launch only; does not change environment or "
            "persisted workflow bindings"
        ),
    )
    parser.add_argument(
        "--extraction-model-settings-json",
        help=(
            "Override model settings as a JSON object for this isolated shadow launch only; "
            "use '{}' to omit the published skill's provider-specific settings"
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the managed shadow path; otherwise only validate and report the plans",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="During --execute, advance only failed or canceled units to their next bounded attempt",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    if arguments.retry_failed and not arguments.execute:
        raise ValueError("--retry-failed requires --execute")
    if arguments.map_max_characters is not None and arguments.map_max_characters < 100:
        raise ValueError("--map-max-characters must be at least 100")
    extraction_model_settings = None
    if arguments.extraction_model_settings_json is not None:
        extraction_model_settings = json.loads(arguments.extraction_model_settings_json)
        if not isinstance(extraction_model_settings, dict):
            raise ValueError("--extraction-model-settings-json must contain a JSON object")
    with SessionLocal() as db:
        prepared = prepare_evidence_shadow_launch(
            db,
            actor_user_id=arguments.actor_user_id,
            assessment_id=arguments.assessment_id,
            document_ids=arguments.document_id,
            max_documents=arguments.max_documents,
            map_max_characters=arguments.map_max_characters,
            extraction_model=arguments.extraction_model,
            extraction_model_settings=extraction_model_settings,
        )
        report = (
            asyncio.run(
                execute_prepared_evidence_shadow(
                    db,
                    prepared,
                    retry_failed=arguments.retry_failed,
                )
            )
            if arguments.execute
            else shadow_plan_report(prepared)
        )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
