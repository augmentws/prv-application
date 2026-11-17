"""Exercise the evidence-first extraction contract over one frozen source window."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from app.artifact_gateway import (
    get_collection_item_analysis_input_snapshots,
    get_preferred_text_source,
    read_artifact_bytes,
)
from app.assessment_execution import _records, _skill_version
from app.assessment_replay import validate_replay_corpus
from app.database import SessionLocal
from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.evidence_context import DocumentEvidenceContext, build_document_evidence_context
from app.evidence_first_contracts import (
    DOCUMENT_DECISION_VERSION,
    EVIDENCE_EXTRACTION_INSTRUCTIONS,
    EVIDENCE_OBSERVATION_VERSION,
    ExtractionNormalizationCapture,
    criterion_catalog_from_retrieval_plan,
    extraction_output_schemas,
    schema_enforcement_report,
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

EVIDENCE_EXTRACTION_CONFORMANCE_VERSION = "evidence-extraction-conformance-v4"
DEFAULT_MAX_OUTPUT_TOKENS = 40_000


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one frozen assessment source window through the evidence-first extraction contract. "
            "The command reads Core and Artifact data, calls the selected model, and writes only a local report."
        )
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--case-id", required=True, help="Matter document ID from the frozen corpus")
    parser.add_argument("--window-ordinal", type=int, required=True, help="One-based map-window ordinal")
    parser.add_argument("--model", required=True, help="Resolved model, for example google:model-name")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument(
        "--include-observations",
        action="store_true",
        help="Include observation facts in the local report; off by default",
    )
    return parser.parse_args()


def _load_corpus(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Replay corpus must be a JSON object")
    validate_replay_corpus(value)
    return value


def _usage(invocations: list[Any]) -> dict[str, int]:
    return {
        "provider_requests": len(invocations),
        "input_tokens": sum(value.input_tokens for value in invocations),
        "cached_input_tokens": sum(value.cached_input_tokens for value in invocations),
        "cache_write_tokens": sum(value.cache_write_tokens for value in invocations),
        "output_tokens": sum(value.output_tokens for value in invocations),
        "latency_ms": sum(value.latency_ms for value in invocations),
    }


def _validation_failures(invocations: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "request_sequence": value.request_sequence,
            "category": value.error_code,
            "message": (value.error_message or "")[:2_000] or None,
        }
        for value in invocations
        if value.error_code
    ]


def _observation_signature(observation: Any) -> dict[str, Any]:
    return {
        "observation_id": observation.observation_id,
        "criterion_key": observation.criterion_key,
        "signal_type": observation.signal_type,
        "citation_ids": list(observation.citation_ids),
    }


def _verify_baseline_artifact(case: dict[str, Any], *, assessment, workflow) -> None:
    baseline = case.get("baseline")
    if not isinstance(baseline, dict):
        return
    raw = read_artifact_bytes(
        artifact_id=uuid.UUID(baseline["output_artifact_id"]),
        actor_user_id=assessment.initiated_by_user_id,
        tenant_id=workflow.tenant_id,
        client_id=workflow.client_id,
    )
    if hashlib.sha256(raw).hexdigest() != baseline["artifact_sha256"]:
        raise ValueError("Frozen baseline artifact hash does not match")


async def _run_request(
    *,
    model: str,
    version,
    revision,
    workflow,
    assessment,
    corpus_hash: str,
    case: dict[str, Any],
    source,
    window,
    catalog,
    document_context: DocumentEvidenceContext,
    max_output_tokens: int,
) -> tuple[Any, list[Any], list[dict[str, str]], dict[str, Any]]:
    paragraph_ids = list(dict.fromkeys(segment.paragraph_id for segment in window.segments))
    allowed_citation_ids = paragraph_ids + sorted(document_context.citation_ids)
    evidence_input_hash = content_hash(
        {
            "source_content_hash": source.content_hash,
            "document_context_hash": document_context.context_hash,
        }
    )
    identity = external_model_identity(model)
    provider = identity[0] if identity is not None else None
    application_schema, admitted_schema = extraction_output_schemas(
        catalog=catalog,
        paragraph_ids=allowed_citation_ids,
        provider=provider,
    )
    capture = ExtractionNormalizationCapture(
        catalog=catalog,
        allowed_paragraph_ids=set(allowed_citation_ids),
        source_hash=evidence_input_hash,
        source_unit_id=f"{case['matter_document_id']}:{window.window_id}",
    )
    request = StructuredModelRequest(
        instruction_layers=(
            InstructionLayer("platform_security", PLATFORM_SKILL_INSTRUCTIONS),
            InstructionLayer("evidence_extraction", EVIDENCE_EXTRACTION_INSTRUCTIONS),
        ),
        stable_context={
            "review_guidance": revision.content_markdown,
            "criterion_catalog": catalog.model_dump(mode="json"),
        },
        dynamic_input={
            "metadata_evidence": document_context.as_model_input(),
            "source_unit": {
                "matter_document_id": case["matter_document_id"],
                "window_id": window.window_id,
                "paragraph_ids": paragraph_ids,
                "text": window.text,
            },
        },
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
            "contract_version": EVIDENCE_EXTRACTION_CONFORMANCE_VERSION,
            "criterion_catalog_hash": catalog.catalog_hash,
            "source_hash": source.content_hash,
            "document_context_hash": document_context.context_hash,
            "window_id": window.window_id,
        },
        request_type="evidence-extraction-conformance",
        output_normalizers=(capture,),
        run_id=(f"evidence-conformance:{corpus_hash}:{case['matter_document_id']}:{window.window_id}"),
    )
    envelope, _assembly = await execute_structured_model(request, model=model)
    if capture.result is None:
        raise RuntimeError("Evidence normalizer did not receive provider output")
    return (
        capture.result,
        list(envelope.invocations),
        schema_enforcement_report(application_schema, admitted_schema),
        {
            "application_schema": application_schema,
            "provider_schema": admitted_schema,
        },
    )


def main() -> None:
    arguments = _arguments()
    if arguments.window_ordinal <= 0:
        raise ValueError("--window-ordinal must be positive")
    if arguments.max_output_tokens <= 0:
        raise ValueError("--max-output-tokens must be positive")
    corpus = _load_corpus(arguments.corpus)
    case = next(
        (value for value in corpus["cases"] if value["matter_document_id"] == arguments.case_id),
        None,
    )
    if case is None:
        raise ValueError(f"Case {arguments.case_id} is absent from the frozen corpus")

    report: dict[str, Any] = {
        "result_version": EVIDENCE_EXTRACTION_CONFORMANCE_VERSION,
        "evidence_observation_version": EVIDENCE_OBSERVATION_VERSION,
        "future_document_decision_version": DOCUMENT_DECISION_VERSION,
        "corpus_hash": corpus["corpus_hash"],
        "matter_document_id": arguments.case_id,
        "window_ordinal": arguments.window_ordinal,
        "candidate_model": arguments.model,
        "max_output_tokens": arguments.max_output_tokens,
        "includes_observation_facts": arguments.include_observations,
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
        _verify_baseline_artifact(case, assessment=assessment, workflow=workflow)
        paragraph_map = segment_paragraphs(source.text)
        plan = build_document_map_plan(
            paragraph_map,
            max_characters=corpus["contract"]["map_max_characters"],
        )
        if len(plan.windows) != case["window_count"]:
            raise ValueError("Frozen document-map window count has drifted")
        if arguments.window_ordinal > len(plan.windows):
            raise ValueError(f"Document contains only {len(plan.windows)} map windows")
        window = plan.windows[arguments.window_ordinal - 1]
        report.update(
            {
                "window_id": window.window_id,
                "window_character_count": len(window.text),
                "window_paragraph_count": len({segment.paragraph_id for segment in window.segments}),
                "criterion_catalog_hash": catalog.catalog_hash,
                "criterion_keys": sorted(catalog.criterion_keys()),
                "document_context_hash": document_context.context_hash,
                "document_context_item_count": len(document_context.items),
            }
        )
        try:
            result, invocations, enforcement, schemas = asyncio.run(
                _run_request(
                    model=arguments.model,
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
                    max_output_tokens=arguments.max_output_tokens,
                )
            )
            report.update(
                {
                    "status": "COMPLETED",
                    "source_observation_count": result.source_item_count,
                    "accepted_observation_count": result.accepted_item_count,
                    "dropped_observation_count": result.dropped_item_count,
                    "adjustment_counts": dict(sorted(Counter(value.code for value in result.adjustments).items())),
                    "adjustments": [value.as_dict() for value in result.adjustments],
                    "schema_enforcement": enforcement,
                    "usage": _usage(invocations),
                    "validation_failures": _validation_failures(invocations),
                    "observation_signatures": [_observation_signature(value) for value in result.observations],
                }
            )
            if arguments.include_observations:
                report["observations"] = [value.model_dump(mode="json") for value in result.observations]
            # Hashes make the two schemas auditable without expanding the default report.
            report["schema_hashes"] = {
                key: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                for key, value in schemas.items()
            }
        except ModelExecutionError as exc:
            invocations = list(exc.invocations)
            report.update(
                {
                    "status": "FAILED",
                    "error_code": exc.code,
                    "error_message": str(exc)[:4_000],
                    "usage": _usage(invocations),
                    "validation_failures": _validation_failures(invocations),
                    "model_error_details": exc.details,
                }
            )

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "status",
                    "window_id",
                    "source_observation_count",
                    "accepted_observation_count",
                    "dropped_observation_count",
                    "adjustment_counts",
                    "usage",
                    "error_code",
                )
                if report.get(key) is not None
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
