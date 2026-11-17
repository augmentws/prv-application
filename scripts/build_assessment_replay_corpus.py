"""Build a content-free, immutable replay corpus manifest from an assessment."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from app.artifact_gateway import get_preferred_text_source, read_artifact_bytes
from app.assessment_execution import _records, _skill_version
from app.assessment_replay import (
    ASSESSMENT_REPLAY_CORPUS_VERSION,
    DEFAULT_REPLAY_CORPUS_SIZE,
    analysis_signature,
    seal_replay_corpus,
    select_replay_cases,
    validate_replay_corpus,
)
from app.config import get_settings
from app.database import SessionLocal
from app.document_analysis_schemas import DOCUMENT_ANALYSIS_PIPELINE_VERSION
from app.document_evidence import PARAGRAPH_MAP_VERSION, build_document_map_plan, segment_paragraphs
from app.model_execution import content_hash
from app.models import MatterDocument, ModelInvocation, ReviewBatchRunDocument, SkillRun


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic replay corpus for one assessment. The manifest contains IDs, hashes, compact "
            "result signatures, and measurements; it never contains source text or complete model output."
        )
    )
    parser.add_argument("--assessment-id", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-size", type=int, default=DEFAULT_REPLAY_CORPUS_SIZE)
    return parser.parse_args()


def _artifact_payload(
    artifact_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> tuple[bytes, dict[str, Any]]:
    raw = read_artifact_bytes(
        artifact_id=artifact_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        raise TypeError("Document-analysis artifact does not contain an object result")
    return raw, value


def _skill_runs(db, *, workflow_id: uuid.UUID, document_ids: list[uuid.UUID]) -> list[SimpleNamespace]:
    """Select only columns available before and after diagnostics migration 0119."""

    rows = db.execute(
        select(
            SkillRun.id,
            SkillRun.scope_id,
            SkillRun.status,
            SkillRun.output_artifact_id,
            SkillRun.created_at,
        )
        .where(
            SkillRun.workflow_run_id == workflow_id,
            SkillRun.scope_type == "MATTER_DOCUMENT",
            SkillRun.scope_id.in_(document_ids),
        )
        .order_by(SkillRun.created_at, SkillRun.id)
    ).mappings()
    return [SimpleNamespace(**dict(row)) for row in rows]


def main() -> None:
    arguments = _arguments()
    if arguments.target_size <= 0:
        raise ValueError("--target-size must be positive")
    settings = get_settings()
    with SessionLocal() as db:
        assessment, workflow, revision, _matter = _records(db, arguments.assessment_id)
        if assessment.review_batch_run_id is None:
            raise ValueError("Assessment does not have a frozen review-batch run")
        version = _skill_version(db, assessment, "document_analysis")
        run_documents = list(
            db.scalars(
                select(ReviewBatchRunDocument)
                .where(ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id)
                .order_by(ReviewBatchRunDocument.matter_document_id)
            )
        )
        document_ids = [row.matter_document_id for row in run_documents]
        skill_runs = _skill_runs(db, workflow_id=workflow.id, document_ids=document_ids)
        run_ids = [run.id for run in skill_runs]
        invocations = (
            list(
                db.scalars(
                    select(ModelInvocation)
                    .where(ModelInvocation.skill_run_id.in_(run_ids))
                    .order_by(
                        ModelInvocation.skill_run_id,
                        ModelInvocation.attempt,
                        ModelInvocation.request_sequence,
                    )
                )
            )
            if run_ids
            else []
        )
        runs_by_document: dict[uuid.UUID, list[SimpleNamespace]] = defaultdict(list)
        for run in skill_runs:
            if run.scope_id is not None:
                runs_by_document[run.scope_id].append(run)
        invocations_by_run: dict[uuid.UUID, list[ModelInvocation]] = defaultdict(list)
        for invocation in invocations:
            if invocation.skill_run_id is not None:
                invocations_by_run[invocation.skill_run_id].append(invocation)

        candidates: list[dict[str, Any]] = []
        for row in run_documents:
            document_runs = runs_by_document.get(row.matter_document_id, [])
            document_invocations = [
                invocation for run in document_runs for invocation in invocations_by_run.get(run.id, [])
            ]
            candidates.append(
                {
                    "matter_document_id": str(row.matter_document_id),
                    "final_status": row.status,
                    "failure_code": row.failure_code,
                    "skill_run_count": len(document_runs),
                    "failed_skill_run_count": sum(run.status == "FAILED" for run in document_runs),
                    "baseline_invocation_count": sum(
                        invocation.request_sequence == 1 for invocation in document_invocations
                    ),
                    "repair_invocation_count": sum(
                        invocation.request_sequence > 1 for invocation in document_invocations
                    ),
                    "input_tokens": sum(invocation.input_tokens for invocation in document_invocations),
                    "output_tokens": sum(invocation.output_tokens for invocation in document_invocations),
                    "latency_ms": sum(invocation.latency_ms for invocation in document_invocations),
                    "validation_categories": dict(
                        sorted(
                            Counter(
                                invocation.error_code
                                for invocation in document_invocations
                                if invocation.error_code
                            ).items()
                        )
                    ),
                }
            )

        selected = select_replay_cases(
            candidates,
            seed=str(assessment.id),
            target_size=arguments.target_size,
        )
        selected_cases: list[dict[str, Any]] = []
        for candidate in selected:
            document_id = uuid.UUID(candidate["matter_document_id"])
            document = db.get(MatterDocument, document_id)
            if document is None:
                raise ValueError(f"Replay document {document_id} is unavailable")
            source = get_preferred_text_source(
                collection_item_id=document.collection_item_id,
                actor_user_id=assessment.initiated_by_user_id,
                tenant_id=workflow.tenant_id,
                client_id=workflow.client_id,
            )
            if source is None:
                raise ValueError(f"Replay document {document_id} no longer has a preferred text source")
            paragraph_map = segment_paragraphs(source.text)
            map_plan = build_document_map_plan(
                paragraph_map,
                max_characters=settings.definition_assessment_map_max_characters,
            )
            document_runs = runs_by_document.get(document_id, [])
            completed_runs = [
                run for run in document_runs if run.status == "COMPLETED" and run.output_artifact_id is not None
            ]
            completed_runs.sort(key=lambda run: (run.created_at, run.id))
            baseline: dict[str, Any] | None = None
            baseline_error: str | None = None
            if completed_runs:
                baseline_run = completed_runs[-1]
                assert baseline_run.output_artifact_id is not None
                try:
                    raw_artifact, payload = _artifact_payload(
                        baseline_run.output_artifact_id,
                        actor_user_id=assessment.initiated_by_user_id,
                        tenant_id=workflow.tenant_id,
                        client_id=workflow.client_id,
                    )
                    baseline = {
                        "skill_run_id": str(baseline_run.id),
                        "output_artifact_id": str(baseline_run.output_artifact_id),
                        "artifact_sha256": hashlib.sha256(raw_artifact).hexdigest(),
                        "signature": analysis_signature(payload["result"]),
                    }
                except (json.JSONDecodeError, KeyError, TypeError, ValueError, PermissionError) as exc:
                    baseline_error = f"{type(exc).__name__}: {exc}"
            selected_cases.append(
                {
                    **candidate,
                    "collection_item_id": str(document.collection_item_id),
                    "source_artifact_id": str(source.artifact_id),
                    "source_hash": source.content_hash,
                    "source_character_count": len(source.text),
                    "paragraph_count": len(paragraph_map.paragraphs),
                    "window_count": len(map_plan.windows),
                    "pipeline_mode": "DIRECT" if len(map_plan.windows) == 1 else "MAP_REDUCE",
                    "baseline": baseline,
                    "baseline_error": baseline_error,
                }
            )

        resolved_models = assessment.configuration_snapshot.get("resolved_models")
        resolved_model = resolved_models.get("document_analysis") if isinstance(resolved_models, dict) else None
        manifest = seal_replay_corpus(
            {
                "corpus_version": ASSESSMENT_REPLAY_CORPUS_VERSION,
                "frozen_at": (assessment.completed_at or assessment.updated_at).isoformat(),
                "source_assessment": {
                    "assessment_id": str(assessment.id),
                    "workflow_run_id": str(workflow.id),
                    "matter_id": str(assessment.matter_id),
                    "review_batch_run_id": str(assessment.review_batch_run_id),
                    "matter_definition_revision_id": str(revision.id),
                    "definition_content_hash": assessment.definition_content_hash,
                    "status": assessment.status,
                },
                "contract": {
                    "skill_definition_version_id": str(version.id),
                    "skill_instructions_hash": content_hash(version.instructions),
                    "output_schema_key": version.output_schema_key,
                    "output_schema_hash": content_hash(version.output_schema),
                    "model_key": version.model_key,
                    "resolved_model": resolved_model,
                    "model_policy_hash": content_hash(version.model_policy),
                    "limits_hash": content_hash(version.limits),
                    "cache_policy_hash": content_hash(version.cache_policy),
                    "document_analysis_pipeline_version": DOCUMENT_ANALYSIS_PIPELINE_VERSION,
                    "paragraph_map_version": PARAGRAPH_MAP_VERSION,
                    "map_max_characters": settings.definition_assessment_map_max_characters,
                },
                "selection": {
                    "algorithm": "assessment-replay-stratified-v1",
                    "seed": str(assessment.id),
                    "target_size": arguments.target_size,
                    "population_size": len(candidates),
                    "selected_size": len(selected_cases),
                    "reason_counts": dict(
                        sorted(
                            Counter(
                                reason
                                for case in selected_cases
                                for reason in case["selection_reasons"]
                            ).items()
                        )
                    ),
                },
                "data_policy": {
                    "contains_source_text": False,
                    "contains_model_output": False,
                    "source_loaded_at_replay": True,
                    "full_output_loaded_from_protected_artifact": True,
                },
                "cases": selected_cases,
            }
        )
        validate_replay_corpus(manifest)

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "corpus_hash": manifest["corpus_hash"],
                "population_size": manifest["selection"]["population_size"],
                "selected_size": manifest["selection"]["selected_size"],
                "reason_counts": manifest["selection"]["reason_counts"],
                "contains_source_text": False,
                "contains_model_output": False,
                "output": str(arguments.output),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
