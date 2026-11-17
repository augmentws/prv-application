"""Plan or execute optional narrative over an evidence-first compatibility result."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from app.database import SessionLocal
from app.evidence_narrative_launcher import (
    execute_prepared_narrative_shadow,
    narrative_plan_report,
    prepare_narrative_shadow_launch,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or explicitly execute an optional narrative over a completed evidence-first "
            "compatibility result. Dry-run is the default."
        )
    )
    parser.add_argument("--actor-user-id", type=uuid.UUID, required=True)
    parser.add_argument("--assessment-id", type=uuid.UUID, required=True)
    parser.add_argument("--compatibility-artifact-id", type=uuid.UUID, required=True)
    parser.add_argument(
        "--narrative-model",
        help="Optional resolved provider:model override for this isolated shadow stage",
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
        prepared = prepare_narrative_shadow_launch(
            db,
            actor_user_id=arguments.actor_user_id,
            assessment_id=arguments.assessment_id,
            compatibility_artifact_id=arguments.compatibility_artifact_id,
            narrative_model=arguments.narrative_model,
        )
        report = (
            asyncio.run(
                execute_prepared_narrative_shadow(
                    db,
                    prepared,
                    retry_failed=arguments.retry_failed,
                )
            )
            if arguments.execute
            else narrative_plan_report(prepared)
        )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
