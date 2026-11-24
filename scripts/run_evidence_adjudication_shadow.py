"""Plan or execute bounded criterion adjudication over one observations artifact."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from app.database import SessionLocal
from app.evidence_adjudication_launcher import (
    adjudication_plan_report,
    execute_prepared_adjudication_shadow,
    prepare_adjudication_shadow_launch,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build or explicitly execute a non-authoritative criterion-adjudication plan over one durable "
            "evidence-observations artifact. Dry-run is the default."
        )
    )
    parser.add_argument("--actor-user-id", type=uuid.UUID, required=True)
    parser.add_argument("--assessment-id", type=uuid.UUID, required=True)
    parser.add_argument("--observations-artifact-id", type=uuid.UUID, required=True)
    parser.add_argument(
        "--target-matter-document-id",
        type=uuid.UUID,
        help=(
            "Optional family member to adjudicate. When supplied, only evidence cited to this "
            "document is considered while retaining the family artifact as provenance."
        ),
    )
    parser.add_argument(
        "--adjudication-model",
        help="Optional resolved provider:model override for this isolated shadow plan",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    if arguments.retry_failed and not arguments.execute:
        raise ValueError("--retry-failed requires --execute")
    with SessionLocal() as db:
        prepared = prepare_adjudication_shadow_launch(
            db,
            actor_user_id=arguments.actor_user_id,
            assessment_id=arguments.assessment_id,
            observations_artifact_id=arguments.observations_artifact_id,
            adjudication_model=arguments.adjudication_model,
            target_matter_document_id=arguments.target_matter_document_id,
        )
        report = (
            asyncio.run(
                execute_prepared_adjudication_shadow(
                    db,
                    prepared,
                    retry_failed=arguments.retry_failed,
                )
            )
            if arguments.execute
            else adjudication_plan_report(prepared)
        )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
