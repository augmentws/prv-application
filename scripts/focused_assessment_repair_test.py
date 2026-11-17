"""Dry-run focused repairs for failed assessment document outputs without database writes."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.artifact_gateway import get_preferred_text_source
from app.assessment_execution import _records, _skill_version
from app.assessment_output_repair import (
    focused_repair_plan,
    normalize_or_repair_document_analysis,
    safe_normalize_document_analysis,
)
from app.config import get_settings
from app.database import SessionLocal
from app.document_analysis_schemas import DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA
from app.document_evidence import (
    build_document_map_plan,
    document_map_coverage,
    segment_paragraphs,
    validate_document_analysis_citations,
)
from app.model_batching import BatchModelResult, get_batch_adapter
from app.model_execution import validate_structured_output
from app.models import (
    MatterDefinitionAssessmentProviderBatch,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    ReviewBatchRunDocument,
    SkillRun,
)

TERMINAL_ASSESSMENT_STATUSES = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect every failed document in a completed definition assessment and optionally run focused, "
            "read-only model repairs. The assessment database is never modified."
        )
    )
    parser.add_argument("--assessment-id", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Local JSON report path")
    parser.add_argument(
        "--execute-repairs",
        action="store_true",
        help="Call the pinned model for supported failures; otherwise only plan repairs",
    )
    parser.add_argument("--max-documents", type=int, help="Optional diagnostic limit")
    parser.add_argument(
        "--allow-running",
        action="store_true",
        help="Inspect the current failure set before the assessment is terminal",
    )
    parser.add_argument(
        "--include-repaired-output",
        action="store_true",
        help="Include repaired document-analysis JSON in the local report",
    )
    return parser.parse_args()


def _batch_results(
    batches: list[MatterDefinitionAssessmentProviderBatch],
) -> tuple[dict[str, BatchModelResult], list[dict[str, Any]]]:
    results: dict[str, BatchModelResult] = {}
    diagnostics: list[dict[str, Any]] = []
    for batch in batches:
        if not batch.provider_batch_id:
            continue
        state, values = get_batch_adapter(batch.provider).results(batch.provider_batch_id)
        diagnostics.append(
            {
                "batch_id": str(batch.id),
                "ordinal": batch.ordinal,
                "provider": batch.provider,
                "provider_batch_id": batch.provider_batch_id,
                "provider_state": state,
                "result_count": len(values),
            }
        )
        for value in values:
            if value.custom_id:
                results[value.custom_id] = value
    return results, diagnostics


def _latest_skill_errors(
    db,
    assessment: MatterDefinitionAssessmentRun,
    document_ids: list[uuid.UUID],
) -> dict[uuid.UUID, SkillRun]:
    runs = list(
        db.scalars(
            select(SkillRun)
            .where(
                SkillRun.workflow_run_id == assessment.workflow_run_id,
                SkillRun.scope_type == "MATTER_DOCUMENT",
                SkillRun.scope_id.in_(document_ids),
            )
            .order_by(SkillRun.created_at)
        )
    )
    return {run.scope_id: run for run in runs if run.scope_id is not None}


def _validate_complete_output(*, output: dict[str, Any], paragraph_map, version) -> None:
    """Apply the same full validation performed when provider-batch results are finalized."""

    validate_structured_output(output, DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA)
    validate_document_analysis_citations(output, paragraph_map)
    map_plan = build_document_map_plan(
        paragraph_map,
        max_characters=get_settings().definition_assessment_map_max_characters,
    )
    if len(map_plan.windows) != 1:
        raise ValueError("The original provider-batch document no longer fits in one analysis window")
    full_output = deepcopy(output)
    full_output["coverage"] = document_map_coverage(
        paragraph_map,
        map_plan,
        successful_window_ids={map_plan.windows[0].window_id},
    )
    validate_structured_output(full_output, version.output_schema)


async def _repair(
    *,
    assessment: MatterDefinitionAssessmentRun,
    revision: MatterDefinitionRevision,
    version,
    selected_model: str,
    output: dict[str, Any],
    plan,
    paragraph_map,
) -> tuple[dict[str, Any], dict[str, Any]]:
    del plan
    outcome = await normalize_or_repair_document_analysis(
        output=output,
        paragraph_map=paragraph_map,
        matter_definition=revision.content_markdown,
        model_key=version.model_key,
        model_policy=version.model_policy,
        limits=version.limits,
        cache_identity={"assessment_id": str(assessment.id), "diagnostic_test": True},
        run_id=f"focused-repair-test:{assessment.id}",
        model=selected_model,
    )
    if outcome.envelope is None:
        raise ValueError("Focused repair test did not execute a model repair")
    _validate_complete_output(output=outcome.output, paragraph_map=paragraph_map, version=version)
    return outcome.output, {
        "requests": outcome.envelope.request_count,
        "input_tokens": outcome.envelope.input_tokens,
        "cached_input_tokens": outcome.envelope.cached_input_tokens,
        "output_tokens": outcome.envelope.output_tokens,
        "provider": outcome.envelope.provider,
        "model": outcome.envelope.provider_model,
    }


def main() -> None:
    arguments = _arguments()
    if arguments.max_documents is not None and arguments.max_documents <= 0:
        raise ValueError("--max-documents must be positive")
    with SessionLocal() as db:
        assessment, workflow, revision, _matter = _records(db, arguments.assessment_id)
        if not arguments.allow_running and assessment.status not in TERMINAL_ASSESSMENT_STATUSES:
            raise ValueError(
                f"Assessment is {assessment.status}; wait for a terminal status or use --allow-running for inspection"
            )
        if assessment.review_batch_run_id is None:
            raise ValueError("Assessment does not have a review-batch run")
        if workflow.client_id is None:
            raise ValueError("Assessment workflow does not identify a client")
        version = _skill_version(db, assessment, "document_analysis")
        resolved_models = assessment.configuration_snapshot.get("resolved_models")
        selected_model = resolved_models.get("document_analysis") if isinstance(resolved_models, dict) else None
        if arguments.execute_repairs and not isinstance(selected_model, str):
            raise ValueError("Assessment does not have a pinned document-analysis model")

        failed_rows = list(
            db.scalars(
                select(ReviewBatchRunDocument)
                .where(
                    ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id,
                    ReviewBatchRunDocument.status == "FAILED",
                )
                .order_by(ReviewBatchRunDocument.matter_document_id)
            )
        )
        if arguments.max_documents is not None:
            failed_rows = failed_rows[: arguments.max_documents]
        failed_ids = [row.matter_document_id for row in failed_rows]
        skill_errors = _latest_skill_errors(db, assessment, failed_ids)
        batches = list(
            db.scalars(
                select(MatterDefinitionAssessmentProviderBatch)
                .where(MatterDefinitionAssessmentProviderBatch.assessment_run_id == assessment.id)
                .order_by(MatterDefinitionAssessmentProviderBatch.ordinal)
            )
        )
        provider_results, provider_diagnostics = _batch_results(batches)

        documents: list[dict[str, Any]] = []
        for index, row in enumerate(failed_rows, start=1):
            document_id = row.matter_document_id
            skill_run = skill_errors.get(document_id)
            provider_result = provider_results.get(str(document_id))
            record: dict[str, Any] = {
                "matter_document_id": str(document_id),
                "original_error_code": skill_run.error_code if skill_run else row.failure_code,
                "original_error_message": skill_run.error_message if skill_run else row.failure_message,
                "provider_error": provider_result.error if provider_result else None,
            }
            if provider_result is None or not provider_result.output_text:
                record["outcome"] = "RAW_OUTPUT_UNAVAILABLE"
                documents.append(record)
                continue
            try:
                original_output = json.loads(provider_result.output_text)
            except json.JSONDecodeError as exc:
                record.update({"outcome": "INVALID_JSON", "test_error": str(exc)})
                documents.append(record)
                continue
            if not isinstance(original_output, dict):
                record.update({"outcome": "NON_OBJECT_OUTPUT"})
                documents.append(record)
                continue

            normalized, normalization_changes = safe_normalize_document_analysis(original_output)
            record["normalization_changes"] = normalization_changes
            plan = focused_repair_plan(normalized)
            record["validation_errors"] = list(plan.validation_errors)
            record["unsupported_errors"] = list(plan.unsupported_errors)
            if not plan.validation_errors:
                document = db.get(MatterDocument, document_id)
                if document is None:
                    record.update({"outcome": "VALIDATION_FAILED", "test_error": "Matter document is unavailable"})
                    documents.append(record)
                    continue
                source = get_preferred_text_source(
                    collection_item_id=document.collection_item_id,
                    actor_user_id=assessment.initiated_by_user_id,
                    tenant_id=workflow.tenant_id,
                    client_id=workflow.client_id,
                )
                if source is None:
                    record.update(
                        {"outcome": "VALIDATION_FAILED", "test_error": "Preferred text source is unavailable"}
                    )
                    documents.append(record)
                    continue
                try:
                    _validate_complete_output(
                        output=normalized,
                        paragraph_map=segment_paragraphs(source.text),
                        version=version,
                    )
                    record["outcome"] = "NORMALIZED_VALID" if normalization_changes else "ALREADY_VALID"
                    if arguments.include_repaired_output:
                        record["repaired_output"] = normalized
                except Exception as exc:  # noqa: BLE001 - record all per-document validation failures
                    record.update(
                        {
                            "outcome": "VALIDATION_FAILED",
                            "test_error": str(exc),
                            "test_error_type": type(exc).__name__,
                        }
                    )
                documents.append(record)
                continue
            if not plan.repairable:
                record["outcome"] = "UNSUPPORTED_REPAIR"
                documents.append(record)
                continue
            record["repair_targets"] = {
                "determination": plan.determination_required,
                "citation_paths": [".".join(str(value) for value in path) for path in plan.citation_paths],
            }
            if not arguments.execute_repairs:
                record["outcome"] = "FOCUSED_REPAIR_PLANNED"
                documents.append(record)
                continue
            assert isinstance(selected_model, str)

            document = db.get(MatterDocument, document_id)
            if document is None:
                record.update({"outcome": "REPAIR_FAILED", "test_error": "Matter document is unavailable"})
                documents.append(record)
                continue
            source = get_preferred_text_source(
                collection_item_id=document.collection_item_id,
                actor_user_id=assessment.initiated_by_user_id,
                tenant_id=workflow.tenant_id,
                client_id=workflow.client_id,
            )
            if source is None:
                record.update({"outcome": "REPAIR_FAILED", "test_error": "Preferred text source is unavailable"})
                documents.append(record)
                continue
            try:
                repaired, usage = asyncio.run(
                    _repair(
                        assessment=assessment,
                        revision=revision,
                        version=version,
                        selected_model=selected_model,
                        output=normalized,
                        plan=plan,
                        paragraph_map=segment_paragraphs(source.text),
                    )
                )
                record.update({"outcome": "FOCUSED_REPAIR_VALID", "usage": usage})
                if arguments.include_repaired_output:
                    record["repaired_output"] = repaired
            except Exception as exc:  # noqa: BLE001 - the harness records every per-document failure
                record.update(
                    {
                        "outcome": "REPAIR_FAILED",
                        "test_error": str(exc),
                        "test_error_type": type(exc).__name__,
                    }
                )
            documents.append(record)
            print(f"Processed {index}/{len(failed_rows)} failed documents", flush=True)

        outcome_counts = Counter(str(item["outcome"]) for item in documents)
        report = {
            "assessment_id": str(assessment.id),
            "assessment_name": assessment.name,
            "assessment_status": assessment.status,
            "assessment_requested_document_count": assessment.requested_document_count,
            "failed_document_count_at_test": len(failed_rows),
            "execute_repairs": arguments.execute_repairs,
            "database_modified": False,
            "provider_batches": provider_diagnostics,
            "outcome_counts": dict(sorted(outcome_counts.items())),
            "documents": documents,
        }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "documents"}, indent=2))


if __name__ == "__main__":
    main()
