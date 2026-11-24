"""Create an adjudication-only Privilege Review retest from frozen evidence.

This deliberately reuses the source program's accepted definition, population,
evidence packets, and completed evidence run. It creates a new program/run and
then enters the ordinary privilege adjudication workflow, preserving the source
program and its recommendations for comparison.
"""

from __future__ import annotations

import argparse
import copy
import json
import uuid

from sqlalchemy import select

from app.database import SessionLocal
from app.embeddings.configuration import canonical_hash
from app.models import (
    ReviewBatch,
    ReviewProgram,
    ReviewProgramRun,
    ReviewProgramStageAttempt,
    ReviewProgramVersion,
    User,
    utcnow,
)
from app.review_programs import start_privilege_adjudication


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and start a Privilege Review adjudication-only retest",
    )
    parser.add_argument("--source-program-id", type=uuid.UUID, required=True)
    parser.add_argument("--actor-user-id", type=uuid.UUID, required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--name", required=True)
    return parser.parse_args()


def _source_records(db, source_program_id: uuid.UUID):
    source = db.get(ReviewProgram, source_program_id)
    if source is None or source.template_key != "PRIVILEGE_REVIEW":
        raise ValueError("Source program must be an existing Privilege Review")
    if source.current_version_id is None or source.current_run_id is None:
        raise ValueError("Source program has no frozen version or run")
    version = db.get(ReviewProgramVersion, source.current_version_id)
    run = db.get(ReviewProgramRun, source.current_run_id)
    if version is None or run is None:
        raise ValueError("Source program version or run is unavailable")
    evidence = dict((run.progress_summary or {}).get("privilege_evidence") or {})
    if evidence.get("status") not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ValueError("Source program evidence is not terminal and usable")
    source_attempt = db.scalar(
        select(ReviewProgramStageAttempt)
        .where(
            ReviewProgramStageAttempt.run_id == run.id,
            ReviewProgramStageAttempt.stage_key == "PRIVILEGE_ADJUDICATION",
        )
        .order_by(ReviewProgramStageAttempt.attempt.desc())
    )
    if source_attempt is None:
        raise ValueError("Source program has no privilege adjudication stage")
    return source, version, run, source_attempt


def main() -> None:
    arguments = _arguments()
    if len(arguments.key) > 100:
        raise ValueError("--key must not exceed 100 characters")
    if len(arguments.name) > 200:
        raise ValueError("--name must not exceed 200 characters")

    with SessionLocal() as db:
        source, source_version, source_run, source_attempt = _source_records(
            db,
            arguments.source_program_id,
        )
        actor = db.get(User, arguments.actor_user_id)
        if actor is None:
            raise ValueError("Actor user is unavailable")
        existing = db.scalar(
            select(ReviewProgram).where(
                ReviewProgram.matter_id == source.matter_id,
                ReviewProgram.key == arguments.key,
            )
        )
        if existing is not None:
            raise ValueError(f"A Review Program with key {arguments.key!r} already exists")

        program = ReviewProgram(
            tenant_id=source.tenant_id,
            matter_id=source.matter_id,
            template_key=source.template_key,
            template_version=source.template_version,
            template_content_hash=source.template_content_hash,
            key=arguments.key,
            name=arguments.name,
            description=(
                "Adjudication-only retest using frozen evidence from "
                f"{source.name} ({source.id})."
            ),
            status="ACTIVE",
            matter_definition_id=source.matter_definition_id,
            target_metadata_definition_id=source.target_metadata_definition_id,
            owner_user_id=source.owner_user_id,
            created_by_user_id=actor.id,
        )
        db.add(program)
        db.flush()

        configuration = copy.deepcopy(source_version.configuration_snapshot or {})
        source_hashes = copy.deepcopy(source_version.source_hashes or {})
        source_hashes["adjudication_retest_source_program_id"] = str(source.id)
        source_hashes["adjudication_retest_source_program_run_id"] = str(source_run.id)
        version_content_hash = canonical_hash(
            {"configuration": configuration, "source_hashes": source_hashes}
        )
        version = ReviewProgramVersion(
            program_id=program.id,
            version=1,
            predecessor_version_id=None,
            template_key=source_version.template_key,
            template_version=source_version.template_version,
            template_content_hash=source_version.template_content_hash,
            configuration_stage="PRIVILEGE_ADJUDICATION",
            configuration_snapshot=configuration,
            source_hashes=source_hashes,
            content_hash=version_content_hash,
            created_by_user_id=actor.id,
        )
        db.add(version)
        db.flush()
        program.current_version_id = version.id

        progress = copy.deepcopy(source_run.progress_summary or {})
        progress["start_key"] = arguments.key
        progress["retest_source"] = {
            "program_id": str(source.id),
            "program_run_id": str(source_run.id),
            "program_version_id": str(source_version.id),
            "mode": "ADJUDICATION_ONLY",
        }
        progress["completed_stages"] = [
            stage
            for stage in progress.get("completed_stages", [])
            if stage not in {"PRIVILEGE_ADJUDICATION", "PRIVILEGE_HUMAN_REVIEW"}
        ]
        progress["last_completed_stage"] = "PRIVILEGE_EVIDENCE"
        progress.pop("privilege_adjudication", None)
        progress.pop("privilege_adjudication_review_batch_run_id", None)

        run = ReviewProgramRun(
            tenant_id=source.tenant_id,
            matter_id=source.matter_id,
            program_id=program.id,
            starting_program_version_id=version.id,
            current_program_version_id=version.id,
            status="QUEUED",
            current_stage="PRIVILEGE_ADJUDICATION",
            current_stage_ordinal=source_attempt.stage_ordinal,
            current_discovery_round=source_run.current_discovery_round,
            current_training_round=source_run.current_training_round,
            progress_summary=progress,
            frozen_configuration_hash=version.content_hash,
            initiated_by_user_id=actor.id,
            started_at=utcnow(),
        )
        db.add(run)
        db.flush()
        program.current_run_id = run.id

        attempt = ReviewProgramStageAttempt(
            run_id=run.id,
            stage_key="PRIVILEGE_ADJUDICATION",
            stage_ordinal=source_attempt.stage_ordinal,
            attempt=1,
            status="QUEUED",
            idempotency_key=f"{arguments.key}:privilege-adjudication:1",
            input_snapshot={
                "source_program_id": str(source.id),
                "source_program_run_id": str(source_run.id),
                "source_evidence_assessment_id": progress["privilege_evidence"]["assessment_id"],
                "source_evidence_review_batch_run_id": progress["privilege_evidence"][
                    "review_batch_run_id"
                ],
            },
            policy_hashes={
                "family_recommendation_policy": "DOCUMENT_SPECIFIC_V2",
                "screening_policy": "AFFIRMATIVE_SUPPORT_V2",
            },
        )
        db.add(attempt)
        db.flush()

        adjudication = start_privilege_adjudication(
            db,
            program=program,
            run=run,
            attempt=attempt,
            actor=actor,
        )
        batch = db.get(ReviewBatch, adjudication.review_batch_id)
        if batch is None:
            raise ValueError("Adjudication batch was not created")
        db.commit()
        print(
            json.dumps(
                {
                    "program_id": str(program.id),
                    "program_run_id": str(run.id),
                    "program_version_id": str(version.id),
                    "adjudication_review_batch_run_id": str(adjudication.id),
                    "source_program_id": str(source.id),
                    "source_evidence_assessment_id": progress["privilege_evidence"]["assessment_id"],
                    "source_evidence_review_batch_run_id": progress["privilege_evidence"][
                        "review_batch_run_id"
                    ],
                    "document_count": batch.document_count,
                    "status": adjudication.status,
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
