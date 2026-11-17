"""Export a read-only model-execution baseline for a Matter Definition assessment."""

from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import inspect, select

from app.assessment_execution import _records, _skill_version
from app.database import SessionLocal
from app.document_analysis_schemas import DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA
from app.models import (
    MatterDefinitionAssessmentProviderBatch,
    ModelInvocation,
    ReviewBatchRunDocument,
    SkillRun,
)
from app.provider_schemas import provider_output_schema
from app.provider_usage import external_model_identity


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export assessment timing, invocation, validation, schema-admission, and usage diagnostics without "
            "model calls or database writes. Document text and model output are never included."
        )
    )
    parser.add_argument("--assessment-id", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--include-documents",
        action="store_true",
        help="Include per-document identifiers and execution counters; source text and output remain excluded",
    )
    return parser.parse_args()


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _seconds(started_at: datetime | None, completed_at: datetime | None) -> float | None:
    if started_at is None or completed_at is None:
        return None
    return max(0.0, (completed_at - started_at).total_seconds())


def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _schema_adjustments(original: Any, admitted: Any, *, path: str = "$") -> list[dict[str, Any]]:
    adjustments: list[dict[str, Any]] = []
    if isinstance(original, dict) and isinstance(admitted, dict):
        for key, value in original.items():
            child_path = f"{path}.{key}"
            if key not in admitted:
                adjustments.append(
                    {
                        "path": child_path,
                        "action": "REMOVED",
                        "original": _json_value(value),
                    }
                )
                continue
            adjustments.extend(_schema_adjustments(value, admitted[key], path=child_path))
        for key, value in admitted.items():
            if key not in original:
                adjustments.append(
                    {
                        "path": f"{path}.{key}",
                        "action": "ADDED",
                        "admitted": _json_value(value),
                    }
                )
        return adjustments
    if isinstance(original, list) and isinstance(admitted, list):
        for index, (left, right) in enumerate(zip(original, admitted, strict=False)):
            adjustments.extend(_schema_adjustments(left, right, path=f"{path}[{index}]"))
        if len(original) != len(admitted):
            adjustments.append(
                {
                    "path": path,
                    "action": "CHANGED",
                    "original_length": len(original),
                    "admitted_length": len(admitted),
                }
            )
        return adjustments
    if original != admitted:
        adjustments.append(
            {
                "path": path,
                "action": "CHANGED",
                "original": _json_value(original),
                "admitted": _json_value(admitted),
            }
        )
    return adjustments


def _schema_report(model_key: str | None) -> dict[str, Any]:
    identity = external_model_identity(model_key)
    provider = identity[0] if identity is not None else None
    realtime = provider_output_schema(
        DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA,
        provider=provider,
    )
    report: dict[str, Any] = {
        "model_key": model_key,
        "provider": provider,
        "realtime_adjustments": _schema_adjustments(DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA, realtime),
    }
    if provider == "google":
        batch = provider_output_schema(
            DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA,
            provider=provider,
            inline_references=True,
            include_additional_properties=False,
        )
        report["batch_adjustments"] = _schema_adjustments(DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA, batch)
    return report


def _usage(invocations: list[ModelInvocation]) -> dict[str, int]:
    return {
        "invocation_rows": len(invocations),
        "request_count": sum(value.request_count for value in invocations),
        "input_tokens": sum(value.input_tokens for value in invocations),
        "cached_input_tokens": sum(value.cached_input_tokens for value in invocations),
        "cache_write_tokens": sum(value.cache_write_tokens for value in invocations),
        "output_tokens": sum(value.output_tokens for value in invocations),
        "latency_ms": sum(value.latency_ms for value in invocations),
    }


def _skill_runs(db, workflow_id: uuid.UUID) -> list[SimpleNamespace]:
    """Load only columns available before and after the diagnostics migration."""

    available_columns = {
        column["name"] for column in inspect(db.get_bind()).get_columns("skill_run")
    }
    selected_columns = [
        SkillRun.id,
        SkillRun.scope_id,
        SkillRun.status,
        SkillRun.error_code,
        SkillRun.started_at,
        SkillRun.completed_at,
        SkillRun.created_at,
    ]
    if "error_details" in available_columns:
        selected_columns.append(SkillRun.error_details)
    rows = db.execute(
        select(*selected_columns)
        .where(
            SkillRun.workflow_run_id == workflow_id,
            SkillRun.scope_type == "MATTER_DOCUMENT",
        )
        .order_by(SkillRun.created_at, SkillRun.id)
    ).mappings()
    return [
        SimpleNamespace(
            **dict(row),
            **({} if "error_details" in row else {"error_details": {}}),
        )
        for row in rows
    ]


def main() -> None:
    arguments = _arguments()
    with SessionLocal() as db:
        assessment, workflow, _revision, _matter = _records(db, arguments.assessment_id)
        version = _skill_version(db, assessment, "document_analysis")
        resolved_models = assessment.configuration_snapshot.get("resolved_models")
        model_key = resolved_models.get("document_analysis") if isinstance(resolved_models, dict) else None

        skill_runs = _skill_runs(db, workflow.id)
        skill_run_ids = [run.id for run in skill_runs]
        invocations = (
            list(
                db.scalars(
                    select(ModelInvocation)
                    .where(ModelInvocation.skill_run_id.in_(skill_run_ids))
                    .order_by(
                        ModelInvocation.skill_run_id,
                        ModelInvocation.attempt,
                        ModelInvocation.request_sequence,
                    )
                )
            )
            if skill_run_ids
            else []
        )
        invocations_by_run: dict[uuid.UUID, list[ModelInvocation]] = defaultdict(list)
        for invocation in invocations:
            if invocation.skill_run_id is not None:
                invocations_by_run[invocation.skill_run_id].append(invocation)
        runs_by_document: dict[uuid.UUID, list[SimpleNamespace]] = defaultdict(list)
        for run in skill_runs:
            if run.scope_id is not None:
                runs_by_document[run.scope_id].append(run)

        review_outcomes = []
        if assessment.review_batch_run_id is not None:
            review_outcomes = list(
                db.scalars(
                    select(ReviewBatchRunDocument).where(
                        ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id
                    )
                )
            )
        provider_batches = list(
            db.scalars(
                select(MatterDefinitionAssessmentProviderBatch)
                .where(MatterDefinitionAssessmentProviderBatch.assessment_run_id == assessment.id)
                .order_by(MatterDefinitionAssessmentProviderBatch.ordinal)
            )
        )

        validation_categories = Counter(
            invocation.error_code for invocation in invocations if invocation.error_code
        )
        request_sequences = Counter(invocation.request_sequence for invocation in invocations)
        model_counts = Counter(f"{invocation.provider}:{invocation.model}" for invocation in invocations)
        skill_statuses = Counter(run.status for run in skill_runs)
        skill_errors = Counter(run.error_code for run in skill_runs if run.error_code)
        review_statuses = Counter(row.status for row in review_outcomes)

        report: dict[str, Any] = {
            "report_version": "assessment-model-baseline-v1",
            "assessment": {
                "id": str(assessment.id),
                "status": assessment.status,
                "requested_document_count": assessment.requested_document_count,
                "selected_count": assessment.selected_count,
                "summarized_count": assessment.summarized_count,
                "skipped_count": assessment.skipped_count,
                "failed_count": assessment.failed_count,
                "estimated_input_tokens": assessment.estimated_input_tokens,
                "estimated_output_tokens": assessment.estimated_output_tokens,
                "started_at": _timestamp(assessment.started_at),
                "completed_at": _timestamp(assessment.completed_at),
                "elapsed_seconds": _seconds(assessment.started_at, assessment.completed_at),
                "workflow_id": str(workflow.id),
            },
            "document_analysis": {
                "skill_version_id": str(version.id),
                "output_schema_key": version.output_schema_key,
                "model_key": model_key,
                "distinct_document_count": len(runs_by_document),
                "document_skill_run_count": len(skill_runs),
                "replayed_document_count": sum(
                    len(document_runs) > 1 for document_runs in runs_by_document.values()
                ),
                "skill_status_counts": dict(sorted(skill_statuses.items())),
                "skill_error_counts": dict(sorted(skill_errors.items())),
                "review_outcome_counts": dict(sorted(review_statuses.items())),
                "usage": _usage(invocations),
                "baseline_invocation_rows": sum(value.request_sequence == 1 for value in invocations),
                "repair_invocation_rows": sum(value.request_sequence > 1 for value in invocations),
                "request_sequence_counts": {
                    str(key): value for key, value in sorted(request_sequences.items())
                },
                "validation_category_counts": dict(sorted(validation_categories.items())),
                "model_invocation_counts": dict(sorted(model_counts.items())),
                "schema_admission": _schema_report(model_key if isinstance(model_key, str) else None),
            },
            "provider_batches": [
                {
                    "id": str(batch.id),
                    "ordinal": batch.ordinal,
                    "status": batch.status,
                    "provider": batch.provider,
                    "model": batch.model,
                    "request_count": batch.request_count,
                    "submitted_at": _timestamp(batch.submitted_at),
                    "completed_at": _timestamp(batch.completed_at),
                    "elapsed_seconds": _seconds(batch.submitted_at, batch.completed_at),
                }
                for batch in provider_batches
            ],
        }

        if arguments.include_documents:
            documents: list[dict[str, Any]] = []
            for document_id, document_runs in sorted(runs_by_document.items(), key=lambda item: str(item[0])):
                document_runs.sort(key=lambda run: (run.created_at, run.id))
                document_invocations = [
                    invocation
                    for run in document_runs
                    for invocation in invocations_by_run[run.id]
                ]
                latest_run = document_runs[-1]
                documents.append(
                    {
                        "matter_document_id": str(document_id),
                        "skill_run_ids": [str(run.id) for run in document_runs],
                        "skill_run_count": len(document_runs),
                        "skill_status_counts": dict(
                            sorted(Counter(run.status for run in document_runs).items())
                        ),
                        "final_status": latest_run.status,
                        "skill_errors": [
                            {
                                "skill_run_id": str(run.id),
                                "error_code": run.error_code,
                                "error_details": run.error_details,
                            }
                            for run in document_runs
                            if run.error_code
                        ],
                        "started_at": _timestamp(
                            min(
                                (run.started_at for run in document_runs if run.started_at is not None),
                                default=None,
                            )
                        ),
                        "completed_at": _timestamp(
                            max(
                                (run.completed_at for run in document_runs if run.completed_at is not None),
                                default=None,
                            )
                        ),
                        "usage": _usage(document_invocations),
                        "baseline_invocation_rows": sum(
                            invocation.request_sequence == 1 for invocation in document_invocations
                        ),
                        "repair_invocation_rows": sum(
                            invocation.request_sequence > 1 for invocation in document_invocations
                        ),
                        "maximum_stage_attempt": max(
                            (invocation.attempt for invocation in document_invocations),
                            default=0,
                        ),
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
            report["documents"] = documents

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(_json_value(report), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    document_summary = {
        key: value
        for key, value in report["document_analysis"].items()
        if key != "schema_admission"
    }
    document_summary["schema_adjustment_counts"] = {
        key: len(value)
        for key, value in report["document_analysis"]["schema_admission"].items()
        if key.endswith("_adjustments") and isinstance(value, list)
    }
    print(json.dumps(document_summary, indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
