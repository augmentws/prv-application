"""Run a read-only evidence-first shadow analysis over one frozen document."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.artifact_gateway import (
    get_collection_item_analysis_input_snapshots,
    get_preferred_text_source,
)
from app.assessment_execution import _records, _skill_version
from app.assessment_synthesis import compile_synthesis_manifest, manifest_summary
from app.database import SessionLocal
from app.document_evidence import (
    build_document_map_plan,
    document_map_coverage,
    segment_paragraphs,
)
from app.evidence_compatibility_projection import assemble_direct_compatibility_output
from app.evidence_context import build_document_evidence_context
from app.evidence_first_contracts import (
    CRITERION_ADJUDICATION_INSTRUCTIONS,
    CRITERION_ADJUDICATION_VERSION,
    CRITERION_DECISION_VERSION,
    EVIDENCE_OBSERVATION_VERSION,
    UNMAPPED_CRITERION_KEY,
    AggregatedEvidenceObservation,
    CriterionAdjudicationOutput,
    CriterionDecision,
    CriterionDecisionNormalizationCapture,
    aggregate_observations,
    combine_positive_criterion_decisions,
    criterion_adjudication_schemas,
    criterion_catalog_from_retrieval_plan,
    criterion_decision_from_adjudication,
    schema_enforcement_report,
    validate_criterion_adjudication,
)
from app.model_execution import (
    InstructionLayer,
    ModelExecutionError,
    StructuredModelRequest,
    content_hash,
    execute_structured_model,
)
from app.models import MatterDocument
from app.provider_usage import external_model_identity
from app.skill_execution import PLATFORM_SKILL_INSTRUCTIONS
from scripts.run_evidence_extraction_conformance import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    _load_corpus,
    _observation_signature,
    _run_request,
    _usage,
    _validation_failures,
    _verify_baseline_artifact,
)

EVIDENCE_DOCUMENT_CONFORMANCE_VERSION = "evidence-document-conformance-v10"
DEFAULT_ADJUDICATION_MAX_OUTPUT_TOKENS = 8_000
DEFAULT_STAGE_MAX_ATTEMPTS = 3


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run all frozen windows for one replay document through evidence extraction, deterministic aggregation, "
            "criterion adjudication, and a code-owned document decision. No assessment state is modified."
        )
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--case-id", required=True, help="Matter document ID from the frozen corpus")
    parser.add_argument(
        "--model",
        help="Use one resolved model for both roles; overridden by a role-specific option",
    )
    parser.add_argument("--extraction-model", help="Resolved evidence-extraction model")
    parser.add_argument("--adjudication-model", help="Resolved criterion-adjudication model")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--extraction-max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
    )
    parser.add_argument(
        "--adjudication-max-output-tokens",
        type=int,
        default=DEFAULT_ADJUDICATION_MAX_OUTPUT_TOKENS,
    )
    parser.add_argument(
        "--include-observations",
        action="store_true",
        help="Include protected observation facts in the local report; off by default",
    )
    parser.add_argument(
        "--stage-max-attempts",
        type=int,
        default=DEFAULT_STAGE_MAX_ATTEMPTS,
        help="Bounded attempts for retryable extraction or adjudication failures",
    )
    return parser.parse_args()


def resolve_model_bindings(
    *,
    model: str | None,
    extraction_model: str | None,
    adjudication_model: str | None,
) -> tuple[str, str]:
    shared = str(model or "").strip()
    extraction = str(extraction_model or shared).strip()
    adjudication = str(adjudication_model or shared).strip()
    if not extraction:
        raise ValueError("Specify --model or --extraction-model")
    if not adjudication:
        raise ValueError("Specify --model or --adjudication-model")
    return extraction, adjudication


def _run_with_bounded_retries(
    operation: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int,
) -> tuple[Any | None, list[Any], list[dict[str, Any]], ModelExecutionError | None]:
    """Retry only failures explicitly classified as retryable and retain every attempt diagnostic."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    invocations: list[Any] = []
    failures: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        try:
            return asyncio.run(operation()), invocations, failures, None
        except ModelExecutionError as exc:
            invocations.extend(exc.invocations)
            failures.append(
                {
                    "attempt": attempt,
                    "error_code": exc.code,
                    "error_message": str(exc)[:4_000],
                    "error_details": exc.details,
                    "retryable": exc.retryable,
                }
            )
            if not exc.retryable or attempt == max_attempts:
                return None, invocations, failures, exc
    raise AssertionError("Bounded retry loop exited without a result")


def _aggregated_signature(observation: AggregatedEvidenceObservation) -> dict[str, Any]:
    return {
        "observation_id": observation.observation_id,
        "criterion_key": observation.criterion_key,
        "signal_type": observation.signal_type,
        "citation_ids": list(observation.citation_ids),
        "source_unit_ids": list(observation.source_unit_ids),
    }


async def _adjudicate(
    *,
    model: str,
    version,
    workflow,
    assessment,
    revision,
    corpus_hash: str,
    source_hash: str,
    criterion,
    observations: list[AggregatedEvidenceObservation],
    max_output_tokens: int,
) -> tuple[
    CriterionDecision,
    CriterionAdjudicationOutput,
    list[Any],
    list[dict[str, str]],
    list[dict[str, str]],
]:
    observation_ids = [value.observation_id for value in observations]
    identity = external_model_identity(model)
    provider = identity[0] if identity is not None else None
    application_schema, admitted_schema = criterion_adjudication_schemas(
        criterion_key=criterion.criterion_key,
        observation_ids=observation_ids,
        provider=provider,
    )
    normalization = CriterionDecisionNormalizationCapture(admitted_observation_ids=set(observation_ids))

    def validate(value: dict[str, Any]) -> None:
        validate_criterion_adjudication(
            value,
            criterion_key=criterion.criterion_key,
            admitted_observation_ids=set(observation_ids),
        )

    request = StructuredModelRequest(
        instruction_layers=(
            InstructionLayer("platform_security", PLATFORM_SKILL_INSTRUCTIONS),
            InstructionLayer("criterion_adjudication", CRITERION_ADJUDICATION_INSTRUCTIONS),
        ),
        stable_context={
            "review_guidance": revision.content_markdown,
            "criterion": criterion.model_dump(mode="json"),
        },
        dynamic_input={"evidence_packet": [value.model_dump(mode="json") for value in observations]},
        output_schema=application_schema,
        model_key=version.model_key,
        model_settings=version.model_policy,
        limits={
            "max_requests": 1,
            "max_output_tokens": max_output_tokens,
            "max_output_retries": 0,
        },
        cache_policy={"enabled": False},
        cache_identity={
            "tenant_id": str(workflow.tenant_id),
            "matter_id": str(assessment.matter_id),
            "replay_corpus_hash": corpus_hash,
            "contract_version": EVIDENCE_DOCUMENT_CONFORMANCE_VERSION,
            "source_hash": source_hash,
            "criterion_key": criterion.criterion_key,
        },
        request_type="criterion-adjudication-conformance",
        output_normalizers=(normalization,),
        output_validators=(validate,),
        run_id=(f"criterion-conformance:{corpus_hash}:{source_hash}:{criterion.criterion_key}"),
    )
    envelope, _assembly = await execute_structured_model(request, model=model)
    adjudication = validate_criterion_adjudication(
        envelope.output,
        criterion_key=criterion.criterion_key,
        admitted_observation_ids=set(observation_ids),
    )
    decision = criterion_decision_from_adjudication(adjudication)
    return (
        decision,
        adjudication,
        list(envelope.invocations),
        schema_enforcement_report(application_schema, admitted_schema),
        normalization.adjustments,
    )


def _empty_decision(criterion_key: str) -> CriterionDecision:
    return CriterionDecision(
        criterion_key=criterion_key,
        disposition="NO_MATCH",
        confidence=1,
        primary_observation_id=None,
        additional_observation_id_1=None,
        additional_observation_id_2=None,
        additional_observation_id_3=None,
        additional_observation_id_4=None,
    )


def main() -> None:
    arguments = _arguments()
    extraction_model, adjudication_model = resolve_model_bindings(
        model=arguments.model,
        extraction_model=arguments.extraction_model,
        adjudication_model=arguments.adjudication_model,
    )
    if arguments.extraction_max_output_tokens <= 0:
        raise ValueError("--extraction-max-output-tokens must be positive")
    if arguments.adjudication_max_output_tokens <= 0:
        raise ValueError("--adjudication-max-output-tokens must be positive")
    if arguments.stage_max_attempts <= 0:
        raise ValueError("--stage-max-attempts must be positive")
    corpus = _load_corpus(arguments.corpus)
    case = next(
        (value for value in corpus["cases"] if value["matter_document_id"] == arguments.case_id),
        None,
    )
    if case is None:
        raise ValueError(f"Case {arguments.case_id} is absent from the frozen corpus")

    report: dict[str, Any] = {
        "result_version": EVIDENCE_DOCUMENT_CONFORMANCE_VERSION,
        "evidence_observation_version": EVIDENCE_OBSERVATION_VERSION,
        "criterion_adjudication_version": CRITERION_ADJUDICATION_VERSION,
        "criterion_decision_version": CRITERION_DECISION_VERSION,
        "corpus_hash": corpus["corpus_hash"],
        "matter_document_id": arguments.case_id,
        "candidate_model": (
            extraction_model
            if extraction_model == adjudication_model
            else f"extraction={extraction_model};adjudication={adjudication_model}"
        ),
        "model_bindings": {
            "extraction": extraction_model,
            "adjudication": adjudication_model,
        },
        "includes_observation_facts": arguments.include_observations,
        "baseline_signature": (case.get("baseline") or {}).get("signature"),
    }
    with SessionLocal() as db:
        assessment_id = uuid.UUID(corpus["source_assessment"]["assessment_id"])
        assessment, workflow, revision, _matter = _records(db, assessment_id)
        version = _skill_version(db, assessment, "document_analysis")
        retrieval_plan = assessment.configuration_snapshot.get("retrieval_plan")
        if not isinstance(retrieval_plan, dict):
            raise TypeError("Assessment does not contain a frozen retrieval plan")
        catalog = criterion_catalog_from_retrieval_plan(retrieval_plan)
        document = db.get(MatterDocument, uuid.UUID(arguments.case_id))
        if document is None:
            raise ValueError("Frozen replay document is unavailable")
        source = get_preferred_text_source(
            collection_item_id=document.collection_item_id,
            actor_user_id=assessment.initiated_by_user_id,
            tenant_id=workflow.tenant_id,
            client_id=workflow.client_id,
        )
        if source is None or source.content_hash != case["source_hash"]:
            raise ValueError("Frozen source content hash does not match")
        input_snapshot = get_collection_item_analysis_input_snapshots(
            item_ids=[document.collection_item_id],
            actor_user_id=assessment.initiated_by_user_id,
            tenant_id=workflow.tenant_id,
            client_id=workflow.client_id,
        ).get(document.collection_item_id)
        if input_snapshot is None:
            raise ValueError("Frozen document analysis metadata is unavailable")
        document_context = build_document_evidence_context(input_snapshot)
        evidence_input_hash = content_hash(
            {
                "source_content_hash": source.content_hash,
                "document_context_hash": document_context.context_hash,
            }
        )
        report["document_context_hash"] = document_context.context_hash
        report["document_context_item_count"] = len(document_context.items)
        _verify_baseline_artifact(case, assessment=assessment, workflow=workflow)
        paragraph_map = segment_paragraphs(source.text)
        plan = build_document_map_plan(
            paragraph_map,
            max_characters=corpus["contract"]["map_max_characters"],
        )
        if len(plan.windows) != case["window_count"]:
            raise ValueError("Frozen document-map window count has drifted")

        extraction_records: list[dict[str, Any]] = []
        extracted = []
        all_invocations: list[Any] = []
        for window in plan.windows:
            record: dict[str, Any] = {
                "window_id": window.window_id,
                "ordinal": window.ordinal,
                "character_count": len(window.text),
                "paragraph_count": len({segment.paragraph_id for segment in window.segments}),
            }
            value, retry_invocations, retry_failures, terminal_error = _run_with_bounded_retries(
                lambda window=window: _run_request(
                        model=extraction_model,
                        version=version,
                        revision=revision,
                        workflow=workflow,
                        assessment=assessment,
                        corpus_hash=corpus["corpus_hash"],
                        case=case,
                        source=source,
                        window=window,
                        catalog=catalog,
                        document_context=document_context,
                        max_output_tokens=arguments.extraction_max_output_tokens,
                    ),
                max_attempts=arguments.stage_max_attempts,
            )
            all_invocations.extend(retry_invocations)
            if terminal_error is None:
                result, invocations, enforcement, _schemas = value
                all_invocations.extend(invocations)
                extracted.extend(result.observations)
                record.update(
                    {
                        "status": "COMPLETED",
                        "source_observation_count": result.source_item_count,
                        "accepted_observation_count": result.accepted_item_count,
                        "dropped_observation_count": result.dropped_item_count,
                        "adjustment_counts": dict(sorted(Counter(value.code for value in result.adjustments).items())),
                        "usage": _usage(invocations),
                        "validation_failures": _validation_failures(invocations),
                        "observation_signatures": [_observation_signature(value) for value in result.observations],
                        "schema_enforcement": enforcement,
                        "attempt_count": len(retry_failures) + 1,
                        "retry_failures": retry_failures,
                    }
                )
            else:
                record.update(
                    {
                        "status": "FAILED",
                        "error_code": terminal_error.code,
                        "error_message": str(terminal_error)[:4_000],
                        "error_details": terminal_error.details,
                        "usage": _usage(retry_invocations),
                        "validation_failures": _validation_failures(retry_invocations),
                        "attempt_count": len(retry_failures),
                        "retry_failures": retry_failures,
                    }
                )
            extraction_records.append(record)

        aggregated = aggregate_observations(extracted, source_hash=evidence_input_hash)
        by_criterion: dict[str, list[AggregatedEvidenceObservation]] = defaultdict(list)
        for observation in aggregated:
            by_criterion[observation.criterion_key].append(observation)
        extraction_complete = all(value["status"] == "COMPLETED" for value in extraction_records)
        dropped_observation_count = sum(value.get("dropped_observation_count", 0) for value in extraction_records)
        extraction_adjustment_count = sum(
            sum((value.get("adjustment_counts") or {}).values()) for value in extraction_records
        )
        evidence_complete = extraction_complete and dropped_observation_count == 0
        decisions: list[CriterionDecision] = []
        adjudication_records: list[dict[str, Any]] = []
        if extraction_complete:
            for criterion in catalog.criteria:
                if criterion.criterion_key == UNMAPPED_CRITERION_KEY:
                    continue
                packet = by_criterion.get(criterion.criterion_key, [])
                if not packet:
                    decision = _empty_decision(criterion.criterion_key)
                    decisions.append(decision)
                    adjudication_records.append(
                        {
                            "criterion_key": criterion.criterion_key,
                            "status": "COMPLETED_WITHOUT_MODEL",
                            "observation_count": 0,
                            "decision": decision.model_dump(mode="json"),
                            "usage": _usage([]),
                        }
                    )
                    continue
                record = {
                    "criterion_key": criterion.criterion_key,
                    "observation_count": len(packet),
                }
                value, retry_invocations, retry_failures, terminal_error = _run_with_bounded_retries(
                    lambda criterion=criterion, packet=packet: _adjudicate(
                            model=adjudication_model,
                            version=version,
                            workflow=workflow,
                            assessment=assessment,
                            revision=revision,
                            corpus_hash=corpus["corpus_hash"],
                            source_hash=evidence_input_hash,
                            criterion=criterion,
                            observations=packet,
                            max_output_tokens=arguments.adjudication_max_output_tokens,
                        ),
                    max_attempts=arguments.stage_max_attempts,
                )
                all_invocations.extend(retry_invocations)
                if terminal_error is None:
                    decision, adjudication, invocations, enforcement, adjustments = value
                    all_invocations.extend(invocations)
                    decisions.append(decision)
                    record.update(
                        {
                            "status": "COMPLETED",
                            "relationship": adjudication.relationship,
                            "boundary_explanation": adjudication.boundary_explanation,
                            "decision": decision.model_dump(mode="json"),
                            "usage": _usage(invocations),
                            "validation_failures": _validation_failures(invocations),
                            "schema_enforcement": enforcement,
                            "adjustments": adjustments,
                            "attempt_count": len(retry_failures) + 1,
                            "retry_failures": retry_failures,
                        }
                    )
                else:
                    record.update(
                        {
                            "status": "FAILED",
                            "error_code": terminal_error.code,
                            "error_message": str(terminal_error)[:4_000],
                            "error_details": terminal_error.details,
                            "usage": _usage(retry_invocations),
                            "validation_failures": _validation_failures(retry_invocations),
                            "attempt_count": len(retry_failures),
                            "retry_failures": retry_failures,
                        }
                    )
                adjudication_records.append(record)

        adjudication_complete = extraction_complete and all(
            value["status"] in {"COMPLETED", "COMPLETED_WITHOUT_MODEL"} for value in adjudication_records
        )
        document_decision = None
        if adjudication_complete:
            document_decision = combine_positive_criterion_decisions(
                decisions,
                catalog=catalog,
                coverage_complete=evidence_complete,
                has_unmapped_observations=bool(by_criterion.get(UNMAPPED_CRITERION_KEY)),
            )
        adjudication_adjustment_count = sum(len(value.get("adjustments") or []) for value in adjudication_records)
        has_adjustments = dropped_observation_count + extraction_adjustment_count + adjudication_adjustment_count > 0
        compatibility_output = None
        synthesis_compatibility = None
        if document_decision is not None:
            successful_window_ids = {
                str(value["window_id"]) for value in extraction_records if value["status"] == "COMPLETED"
            }
            compatibility_output = assemble_direct_compatibility_output(
                document_decision=document_decision,
                criterion_decisions=decisions,
                observations=aggregated,
                catalog=catalog,
                coverage=document_map_coverage(
                    paragraph_map,
                    plan,
                    successful_window_ids=successful_window_ids,
                ),
                evidence_complete=evidence_complete,
                material_adjustment_count=(
                    dropped_observation_count + extraction_adjustment_count + adjudication_adjustment_count
                ),
                unmapped_observation_count=len(by_criterion.get(UNMAPPED_CRITERION_KEY, ())),
                include_evidence_digest=arguments.include_observations,
            )
            synthesis_manifest = compile_synthesis_manifest(
                [
                    {
                        "matter_document_id": arguments.case_id,
                        "analysis": compatibility_output,
                    }
                ],
                {
                    "selected_document_count": 1,
                    "analyzed_document_count": 1,
                    "skipped_document_count": 0,
                    "failed_document_count": 0,
                    "partial_coverage_document_count": (0 if evidence_complete else 1),
                    "invalid_result_count": 0,
                },
                retrieval_plan,
            )
            synthesis_compatibility = {
                "compatible": True,
                "manifest": manifest_summary(synthesis_manifest),
            }
        report.update(
            {
                "status": (
                    "COMPLETED_WITH_ADJUSTMENTS"
                    if document_decision is not None and has_adjustments
                    else "COMPLETED"
                    if document_decision is not None
                    else "COMPLETED_WITH_ERRORS"
                ),
                "adjustment_summary": {
                    "dropped_observation_count": dropped_observation_count,
                    "extraction_adjustment_count": extraction_adjustment_count,
                    "adjudication_adjustment_count": adjudication_adjustment_count,
                },
                "coverage": {
                    "window_count": len(plan.windows),
                    "completed_window_count": sum(value["status"] == "COMPLETED" for value in extraction_records),
                    "paragraph_count": len(paragraph_map.paragraphs),
                    "complete": extraction_complete,
                    "evidence_complete": evidence_complete,
                },
                "extraction": extraction_records,
                "aggregation": {
                    "source_observation_count": len(extracted),
                    "aggregated_observation_count": len(aggregated),
                    "exact_duplicate_count": len(extracted) - len(aggregated),
                    "criterion_counts": dict(sorted(Counter(value.criterion_key for value in aggregated).items())),
                    "observation_signatures": [_aggregated_signature(value) for value in aggregated],
                },
                "adjudication": adjudication_records,
                "document_decision": (
                    document_decision.model_dump(mode="json") if document_decision is not None else None
                ),
                "compatibility_output": compatibility_output,
                "synthesis_compatibility": synthesis_compatibility,
                "comparison": {
                    "baseline_determination": (
                        ((case.get("baseline") or {}).get("signature") or {}).get("determination")
                    ),
                    "candidate_determination": (
                        document_decision.determination if document_decision is not None else None
                    ),
                    "determination_agreement": (
                        document_decision is not None
                        and document_decision.determination
                        == ((case.get("baseline") or {}).get("signature") or {}).get("determination")
                    ),
                },
                "usage": _usage(all_invocations),
            }
        )
        if arguments.include_observations:
            report["aggregated_observations"] = [value.model_dump(mode="json") for value in aggregated]

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "coverage": report["coverage"],
                "aggregation": {
                    key: report["aggregation"][key]
                    for key in (
                        "source_observation_count",
                        "aggregated_observation_count",
                        "exact_duplicate_count",
                        "criterion_counts",
                    )
                },
                "document_decision": report["document_decision"],
                "compatibility": (
                    {
                        "determination": report["compatibility_output"]["determination"],
                        "criterion_match_count": len(report["compatibility_output"]["criterion_matches"]),
                        "limitation_count": len(report["compatibility_output"]["limitations"]),
                        "coverage_status": report["compatibility_output"]["coverage"]["status"],
                        "synthesis_input_compatible": bool(
                            (report.get("synthesis_compatibility") or {}).get("compatible")
                        ),
                    }
                    if report.get("compatibility_output") is not None
                    else None
                ),
                "comparison": report["comparison"],
                "usage": report["usage"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
