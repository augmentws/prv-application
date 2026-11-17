"""Replay a frozen assessment corpus against one model without database writes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from app.artifact_gateway import get_preferred_text_source, read_artifact_bytes
from app.assessment_execution import _records, _skill_version
from app.assessment_output_repair import normalize_document_analysis_output
from app.assessment_replay import (
    ASSESSMENT_REPLAY_RESULT_VERSION,
    analysis_signature,
    compare_analysis_signatures,
    validate_replay_corpus,
)
from app.database import SessionLocal
from app.document_analysis_schemas import (
    DOCUMENT_ANALYSIS_DOCUMENT_INSTRUCTIONS,
    DOCUMENT_ANALYSIS_MAP_INSTRUCTIONS,
    DOCUMENT_ANALYSIS_MAP_OUTPUT_SCHEMA,
    DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA,
    DOCUMENT_ANALYSIS_PIPELINE_VERSION,
    DOCUMENT_ANALYSIS_REDUCE_INSTRUCTIONS,
)
from app.document_evidence import (
    PARAGRAPH_MAP_VERSION,
    EvidenceParagraph,
    ParagraphMap,
    build_document_map_plan,
    build_document_reduce_input,
    document_citation_ids,
    document_map_coverage,
    preserve_document_map_signals,
    render_model_document,
    segment_paragraphs,
    validate_document_analysis_citations,
    validate_document_map_citations,
)
from app.model_execution import (
    InstructionLayer,
    ModelExecutionError,
    StructuredModelRequest,
    content_hash,
    execute_structured_model,
    validate_structured_output,
)
from app.models import MatterDocument
from app.skill_execution import PLATFORM_SKILL_INSTRUCTIONS


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the current direct/map-reduce document-analysis contract over a frozen corpus. Source content is "
            "read from protected artifacts, results are written only to the requested local report, and assessment "
            "state is never modified."
        )
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--model", help="Resolved candidate model, for example google:model-name")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify the frozen contract and protected artifacts without making model calls",
    )
    parser.add_argument("--max-cases", type=int)
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        help="Explicit cumulative output-token override for each structured model call",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Replay only this matter document ID; may be supplied more than once",
    )
    parser.add_argument(
        "--include-outputs",
        action="store_true",
        help="Include complete candidate outputs in the local report; off by default",
    )
    return parser.parse_args()


def _load_corpus(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Replay corpus must be a JSON object")
    validate_replay_corpus(value)
    return value


def _contract_drift(corpus: dict[str, Any], assessment, revision, version) -> list[str]:
    contract = corpus["contract"]
    source = corpus["source_assessment"]
    comparisons = {
        "assessment_id": (source["assessment_id"], str(assessment.id)),
        "matter_definition_revision_id": (source["matter_definition_revision_id"], str(revision.id)),
        "definition_content_hash": (source["definition_content_hash"], assessment.definition_content_hash),
        "skill_definition_version_id": (contract["skill_definition_version_id"], str(version.id)),
        "skill_instructions_hash": (contract["skill_instructions_hash"], content_hash(version.instructions)),
        "output_schema_hash": (contract["output_schema_hash"], content_hash(version.output_schema)),
        "model_policy_hash": (contract["model_policy_hash"], content_hash(version.model_policy)),
        "limits_hash": (contract["limits_hash"], content_hash(version.limits)),
        "cache_policy_hash": (contract["cache_policy_hash"], content_hash(version.cache_policy)),
        "document_analysis_pipeline_version": (
            contract["document_analysis_pipeline_version"],
            DOCUMENT_ANALYSIS_PIPELINE_VERSION,
        ),
        "paragraph_map_version": (contract["paragraph_map_version"], PARAGRAPH_MAP_VERSION),
    }
    return [key for key, (expected, current) in comparisons.items() if expected != current]


def _request(
    *,
    version,
    stable_context: dict[str, Any],
    dynamic_input: dict[str, Any],
    cache_identity: dict[str, Any],
    output_schema: dict[str, Any],
    limits: dict[str, Any],
    additional_instructions: str,
    validators=(),
    normalizers=(),
    run_id: str,
) -> StructuredModelRequest:
    return StructuredModelRequest(
        instruction_layers=(
            InstructionLayer("platform_security", PLATFORM_SKILL_INSTRUCTIONS),
            InstructionLayer("managed_skill", version.instructions),
            InstructionLayer("analysis_stage", additional_instructions),
        ),
        stable_context=stable_context,
        dynamic_input=dynamic_input,
        output_schema=output_schema,
        model_key=version.model_key,
        model_settings=version.model_policy,
        limits=limits,
        cache_policy=version.cache_policy,
        cache_identity=cache_identity,
        request_type="assessment-replay-document-analysis",
        output_normalizers=tuple(normalizers),
        output_validators=tuple(validators),
        run_id=run_id,
    )


async def _execute_case(
    *,
    corpus_hash: str,
    model: str,
    assessment,
    workflow,
    revision,
    version,
    document: MatterDocument,
    source,
    max_characters: int,
    max_output_tokens: int | None,
) -> tuple[dict[str, Any], list[Any], dict[str, Any]]:
    paragraph_map = segment_paragraphs(source.text)
    plan = build_document_map_plan(paragraph_map, max_characters=max_characters)
    stable_context = {"matter_definition": revision.content_markdown}
    cache_identity = {
        "tenant_id": str(workflow.tenant_id),
        "matter_id": str(assessment.matter_id),
        "guidance_id": str(revision.matter_definition_id),
        "revision_hash": assessment.definition_content_hash,
        "skill_version_id": str(version.id),
        "schema_version": version.output_schema_key,
        "paragraph_map_version": PARAGRAPH_MAP_VERSION,
        "analysis_pipeline_version": DOCUMENT_ANALYSIS_PIPELINE_VERSION,
        "replay_corpus_hash": corpus_hash,
    }
    metadata = {
        "matter_document_id": str(document.id),
        "collection_item_id": str(document.collection_item_id),
        "source_artifact_id": str(source.artifact_id),
        "source_hash": source.content_hash,
    }
    invocations: list[Any] = []
    run_prefix = f"assessment-replay:{corpus_hash}:{document.id}"
    execution_limits = dict(version.limits)
    if max_output_tokens is not None:
        execution_limits["max_output_tokens"] = max_output_tokens

    async def execute(request: StructuredModelRequest):
        try:
            envelope, assembly = await execute_structured_model(request, model=model)
        except ModelExecutionError as exc:
            raise ModelExecutionError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                invocations=tuple(invocations) + exc.invocations,
                details=exc.details,
            ) from exc
        invocations.extend(envelope.invocations)
        return envelope, assembly

    if len(plan.windows) == 1:
        request = _request(
            version=version,
            stable_context=stable_context,
            dynamic_input={
                "analysis_stage": "DOCUMENT",
                "document": {**metadata, "text": render_model_document(paragraph_map)},
                "paragraph_map": paragraph_map.as_dict()["paragraphs"],
            },
            cache_identity=cache_identity,
            output_schema=DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA,
            limits=execution_limits,
            additional_instructions=DOCUMENT_ANALYSIS_DOCUMENT_INSTRUCTIONS,
            validators=(lambda value: validate_document_analysis_citations(value, paragraph_map),),
            normalizers=(normalize_document_analysis_output,),
            run_id=f"{run_prefix}:direct",
        )
        envelope, _ = await execute(request)
        output = envelope.output
        output["coverage"] = document_map_coverage(
            paragraph_map,
            plan,
            successful_window_ids={plan.windows[0].window_id},
        )
        validate_structured_output(output, version.output_schema)
        return output, invocations, {"mode": "DIRECT", "window_count": 1}

    map_results: dict[str, dict[str, Any]] = {}
    paragraphs_by_id = {item.paragraph_id: item for item in paragraph_map.paragraphs}
    for window in plan.windows:
        window_ids = list(dict.fromkeys(segment.paragraph_id for segment in window.segments))
        window_paragraphs = []
        for paragraph_id in window_ids:
            segments = [item for item in window.segments if item.paragraph_id == paragraph_id]
            original = paragraphs_by_id[paragraph_id]
            window_paragraphs.append(
                EvidenceParagraph(
                    paragraph_id=paragraph_id,
                    text="".join(item.text for item in segments),
                    char_start=min(item.char_start for item in segments),
                    char_end=max(item.char_end for item in segments),
                    line_start=original.line_start,
                    line_end=original.line_end,
                )
            )
        window_map = ParagraphMap(
            version=paragraph_map.version,
            source_length=paragraph_map.source_length,
            paragraphs=tuple(window_paragraphs),
        )
        request = _request(
            version=version,
            stable_context=stable_context,
            dynamic_input={
                "analysis_stage": "MAP",
                "window_id": window.window_id,
                "document": {**metadata, "text": window.text},
                "paragraph_map": window_map.as_dict()["paragraphs"],
            },
            cache_identity=cache_identity,
            output_schema=DOCUMENT_ANALYSIS_MAP_OUTPUT_SCHEMA,
            limits=execution_limits,
            additional_instructions=DOCUMENT_ANALYSIS_MAP_INSTRUCTIONS,
            validators=(lambda value, allowed=window_map: validate_document_map_citations(value, allowed),),
            run_id=f"{run_prefix}:map:{window.window_id}",
        )
        envelope, _ = await execute(request)
        map_results[window.window_id] = envelope.output

    reduce_input = build_document_reduce_input(paragraph_map, plan, map_results=map_results)
    reduce_citation_ids = document_citation_ids(map_results)
    reduce_paragraph_map = ParagraphMap(
        version=paragraph_map.version,
        source_length=paragraph_map.source_length,
        paragraphs=tuple(
            paragraph
            for paragraph in paragraph_map.paragraphs
            if paragraph.paragraph_id in reduce_citation_ids
        ),
    )
    request = _request(
        version=version,
        stable_context=stable_context,
        dynamic_input={
            "analysis_stage": "REDUCE",
            "document": metadata,
            "paragraph_ids": [item.paragraph_id for item in reduce_paragraph_map.paragraphs],
            "map_plan_version": reduce_input["map_plan_version"],
            "paragraph_map_version": reduce_input["paragraph_map_version"],
            "map_results": reduce_input["map_results"],
            "failed_window_ids": reduce_input["failed_window_ids"],
        },
        cache_identity=cache_identity,
        output_schema=DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA,
        limits=execution_limits,
        additional_instructions=DOCUMENT_ANALYSIS_REDUCE_INSTRUCTIONS,
        validators=(lambda value: validate_document_analysis_citations(value, reduce_paragraph_map),),
        normalizers=(normalize_document_analysis_output,),
        run_id=f"{run_prefix}:reduce",
    )
    envelope, _ = await execute(request)
    output, preservation = preserve_document_map_signals(envelope.output, map_results)
    output["coverage"] = reduce_input["coverage"]
    validate_structured_output(output, version.output_schema)
    return output, invocations, {
        "mode": "MAP_REDUCE",
        "window_count": len(plan.windows),
        **preservation,
    }


def _usage(invocations: list[Any]) -> dict[str, int]:
    return {
        "provider_requests": len(invocations),
        "input_tokens": sum(value.input_tokens for value in invocations),
        "cached_input_tokens": sum(value.cached_input_tokens for value in invocations),
        "cache_write_tokens": sum(value.cache_write_tokens for value in invocations),
        "output_tokens": sum(value.output_tokens for value in invocations),
        "latency_ms": sum(value.latency_ms for value in invocations),
        "validation_failure_count": sum(value.error_code is not None for value in invocations),
    }


def _validation_failures(invocations: list[Any]) -> list[dict[str, Any]]:
    """Retain bounded validation diagnostics without copying a complete response."""

    return [
        {
            "request_sequence": value.request_sequence,
            "category": value.error_code,
            "message": (value.error_message or "")[:2000] or None,
        }
        for value in invocations
        if value.error_code
    ]


def main() -> None:
    arguments = _arguments()
    if not arguments.verify_only and not arguments.model:
        raise ValueError("--model is required unless --verify-only is used")
    if arguments.max_cases is not None and arguments.max_cases <= 0:
        raise ValueError("--max-cases must be positive")
    if arguments.max_output_tokens is not None and arguments.max_output_tokens <= 0:
        raise ValueError("--max-output-tokens must be positive")
    corpus = _load_corpus(arguments.corpus)
    requested_ids = set(arguments.case_id)
    cases = [case for case in corpus["cases"] if not requested_ids or case["matter_document_id"] in requested_ids]
    missing = requested_ids - {case["matter_document_id"] for case in cases}
    if missing:
        raise ValueError(f"Requested case IDs are absent from the corpus: {', '.join(sorted(missing))}")
    if arguments.max_cases is not None:
        cases = cases[: arguments.max_cases]
    if not cases:
        raise ValueError("No replay cases were selected")

    results: list[dict[str, Any]] = []
    with SessionLocal() as db:
        assessment_id = uuid.UUID(corpus["source_assessment"]["assessment_id"])
        assessment, workflow, revision, _matter = _records(db, assessment_id)
        version = _skill_version(db, assessment, "document_analysis")
        drift = _contract_drift(corpus, assessment, revision, version)
        if drift:
            raise ValueError(f"Frozen replay contract has drifted: {', '.join(drift)}")

        for case in cases:
            document_id = uuid.UUID(case["matter_document_id"])
            document = db.get(MatterDocument, document_id)
            record: dict[str, Any] = {
                "matter_document_id": str(document_id),
                "selection_reasons": case["selection_reasons"],
                "baseline": case.get("baseline", {}).get("signature") if case.get("baseline") else None,
            }
            if document is None:
                record.update({"status": "FAILED", "error_code": "DOCUMENT_UNAVAILABLE"})
                results.append(record)
                continue
            source = get_preferred_text_source(
                collection_item_id=document.collection_item_id,
                actor_user_id=assessment.initiated_by_user_id,
                tenant_id=workflow.tenant_id,
                client_id=workflow.client_id,
            )
            if source is None or source.content_hash != case["source_hash"]:
                record.update({"status": "FAILED", "error_code": "SOURCE_HASH_MISMATCH"})
                results.append(record)
                continue
            baseline = case.get("baseline")
            if baseline:
                raw_baseline = read_artifact_bytes(
                    artifact_id=uuid.UUID(baseline["output_artifact_id"]),
                    actor_user_id=assessment.initiated_by_user_id,
                    tenant_id=workflow.tenant_id,
                    client_id=workflow.client_id,
                )
                if hashlib.sha256(raw_baseline).hexdigest() != baseline["artifact_sha256"]:
                    record.update({"status": "FAILED", "error_code": "BASELINE_ARTIFACT_HASH_MISMATCH"})
                    results.append(record)
                    continue
            if arguments.verify_only:
                record["status"] = "VERIFIED"
                results.append(record)
                continue
            assert isinstance(arguments.model, str)
            try:
                output, invocations, diagnostics = asyncio.run(
                    _execute_case(
                        corpus_hash=corpus["corpus_hash"],
                        model=arguments.model,
                        assessment=assessment,
                        workflow=workflow,
                        revision=revision,
                        version=version,
                        document=document,
                        source=source,
                        max_characters=corpus["contract"]["map_max_characters"],
                        max_output_tokens=arguments.max_output_tokens,
                    )
                )
                signature = analysis_signature(output)
                record.update(
                    {
                        "status": "COMPLETED",
                        "candidate": signature,
                        "comparison": compare_analysis_signatures(record["baseline"], signature),
                        "usage": _usage(invocations),
                        "pipeline": diagnostics,
                        "validation_categories": sorted(
                            {value.error_code for value in invocations if value.error_code}
                        ),
                        "validation_failures": _validation_failures(invocations),
                    }
                )
                if arguments.include_outputs:
                    record["output"] = output
            except ModelExecutionError as exc:
                validation_categories = sorted(
                    {
                        str(value.get("category"))
                        for value in exc.details.get("validation_failures", [])
                        if isinstance(value, dict) and value.get("category")
                    }
                )
                record.update(
                    {
                        "status": "FAILED",
                        "error_code": exc.code,
                        "usage": _usage(list(exc.invocations)),
                        "validation_categories": validation_categories,
                        "validation_failures": _validation_failures(list(exc.invocations)),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - isolate each replay case
                record.update(
                    {
                        "status": "FAILED",
                        "error_code": type(exc).__name__,
                    }
                )
            results.append(record)

    completed = [value for value in results if value["status"] == "COMPLETED"]
    verified = [value for value in results if value["status"] == "VERIFIED"]
    comparable = [value for value in completed if value["comparison"]["baseline_available"]]
    report = {
        "result_version": ASSESSMENT_REPLAY_RESULT_VERSION,
        "corpus_hash": corpus["corpus_hash"],
        "candidate_model": arguments.model,
        "max_output_tokens_override": arguments.max_output_tokens,
        "includes_complete_outputs": arguments.include_outputs,
        "summary": {
            "case_count": len(results),
            "verified_count": len(verified),
            "completed_count": len(completed),
            "failed_count": len(results) - len(completed) - len(verified),
            "comparable_count": len(comparable),
            "determination_agreement_count": sum(
                value["comparison"]["determination_agreement"] is True for value in comparable
            ),
            "provider_requests": sum(value.get("usage", {}).get("provider_requests", 0) for value in results),
            "input_tokens": sum(value.get("usage", {}).get("input_tokens", 0) for value in results),
            "output_tokens": sum(value.get("usage", {}).get("output_tokens", 0) for value in results),
            "validation_failure_count": sum(
                value.get("usage", {}).get("validation_failure_count", 0) for value in results
            ),
        },
        "cases": results,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
