"""Build or publish a collection-level evidence-first outcome manifest."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from app.database import SessionLocal
from app.evidence_outcome_launcher import (
    outcome_manifest_report,
    prepare_evidence_outcome_manifest,
    publish_prepared_evidence_outcome_manifest,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble a deterministic operator manifest across evidence extraction, adjudication, "
            "document decision, compatibility, and optional narrative. Dry-run is the default."
        )
    )
    parser.add_argument("--actor-user-id", type=uuid.UUID, required=True)
    parser.add_argument("--assessment-id", type=uuid.UUID, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    with SessionLocal() as db:
        prepared = prepare_evidence_outcome_manifest(
            db,
            actor_user_id=arguments.actor_user_id,
            assessment_id=arguments.assessment_id,
        )
        report = (
            publish_prepared_evidence_outcome_manifest(prepared)
            if arguments.publish
            else outcome_manifest_report(prepared)
        )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
