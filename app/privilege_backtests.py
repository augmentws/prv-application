"""Deterministic family-stratified cohorts for Privilege Review backtests."""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.document_metadata import event_value
from app.embeddings.configuration import canonical_hash
from app.models import (
    DocumentMetadataCurrent,
    Matter,
    MetadataDefinition,
    ReviewBatchRun,
    ReviewBatchRunValue,
    ReviewProgram,
    ReviewProgramChild,
    ReviewProgramRun,
    ReviewProgramVersion,
)
from app.privilege_evidence_context import (
    load_privilege_evidence_packets,
    privilege_evidence_family_groups,
)

RECOMMENDATIONS = ("privileged", "needs_review", "not_privileged")


def _stable_order(seed: str, stratum: str, family_key: str) -> str:
    return hashlib.sha256(f"{seed}:{stratum}:{family_key}".encode()).hexdigest()


def select_stratified_families(
    *,
    family_cells: dict[str, set[str]],
    family_documents: dict[str, list[str]],
    per_stratum: int,
    random_seed: str,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Assign each family to its rarest eligible cell, then sample deterministically."""

    eligible_counts = Counter(cell for cells in family_cells.values() for cell in cells)
    assigned: dict[str, list[str]] = defaultdict(list)
    for family_key, cells in sorted(family_cells.items()):
        stratum = min(cells, key=lambda value: (eligible_counts[value], value))
        assigned[stratum].append(family_key)
    all_strata = [
        f"{recommendation}:{truth}"
        for recommendation in RECOMMENDATIONS
        for truth in ("positive", "negative")
    ]
    selected_by_stratum: dict[str, list[str]] = {}
    for stratum in all_strata:
        ordered = sorted(
            assigned.get(stratum, []),
            key=lambda family_key: _stable_order(random_seed, stratum, family_key),
        )
        selected_by_stratum[stratum] = ordered[:per_stratum]
    selected_family_keys = [
        family_key
        for stratum in all_strata
        for family_key in selected_by_stratum[stratum]
    ]
    selected_document_ids = list(
        dict.fromkeys(
            document_id
            for family_key in selected_family_keys
            for document_id in family_documents[family_key]
        )
    )
    strata = [
        {
            "key": stratum,
            "eligible_family_count": eligible_counts[stratum],
            "assigned_family_count": len(assigned.get(stratum, [])),
            "selected_family_count": len(selected_by_stratum[stratum]),
            "shortfall": max(0, per_stratum - len(selected_by_stratum[stratum])),
        }
        for stratum in all_strata
    ]
    return selected_family_keys, selected_document_ids, strata


def _source_adjudication_run(
    db: Session,
    *,
    program: ReviewProgram,
) -> tuple[ReviewProgramRun, ReviewBatchRun]:
    if program.current_run_id is None:
        raise ValueError("Source Privilege Review has no current run")
    run = db.get(ReviewProgramRun, program.current_run_id)
    if run is None:
        raise ValueError("Source Privilege Review run is unavailable")
    child = db.scalar(
        select(ReviewProgramChild)
        .where(
            ReviewProgramChild.run_id == run.id,
            ReviewProgramChild.child_type == "PRIVILEGE_ADJUDICATION_RUN",
        )
        .order_by(ReviewProgramChild.created_at.desc())
    )
    if child is None:
        raise ValueError("Source Privilege Review has no adjudication run")
    adjudication = db.get(ReviewBatchRun, child.child_id)
    if adjudication is None or adjudication.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ValueError("Source privilege adjudication is not complete")
    return run, adjudication


def build_privilege_backtest_cohort(
    db: Session,
    *,
    program: ReviewProgram,
    truth_metadata_definition_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    per_stratum: int,
    random_seed: str,
) -> dict[str, Any]:
    if per_stratum < 1 or per_stratum > 1_000:
        raise ValueError("Backtest per_stratum must be between 1 and 1000")
    truth_definition = db.get(MetadataDefinition, truth_metadata_definition_id)
    if (
        truth_definition is None
        or truth_definition.matter_id != program.matter_id
        or truth_definition.type != "BOOLEAN"
        or truth_definition.cardinality != "SINGLE"
    ):
        raise ValueError("Backtest truth field must be a single-value Boolean field in this matter")
    if program.current_version_id is None:
        raise ValueError("Source Privilege Review has no frozen version")
    version = db.get(ReviewProgramVersion, program.current_version_id)
    if version is None:
        raise ValueError("Source Privilege Review version is unavailable")
    privilege = (version.configuration_snapshot or {}).get("privilege_review")
    packets_reference = privilege.get("evidence_packets") if isinstance(privilege, dict) else None
    if not isinstance(packets_reference, dict) or not packets_reference.get("artifact_id"):
        raise ValueError("Source Privilege Review has no frozen evidence packets")
    run, adjudication = _source_adjudication_run(db, program=program)
    target_definition_id = uuid.UUID(
        str(adjudication.configuration_snapshot["target_metadata_definition_id"])
    )
    recommendation_values = {
        row.matter_document_id: str(event_value(row))
        for row in db.scalars(
            select(ReviewBatchRunValue).where(
                ReviewBatchRunValue.review_batch_run_id == adjudication.id,
                ReviewBatchRunValue.metadata_definition_id == target_definition_id,
            )
        )
        if str(event_value(row)) in RECOMMENDATIONS
    }
    truth_values = {
        row.matter_document_id: bool(event_value(row))
        for row in db.scalars(
            select(DocumentMetadataCurrent).where(
                DocumentMetadataCurrent.matter_id == program.matter_id,
                DocumentMetadataCurrent.metadata_definition_id == truth_definition.id,
                DocumentMetadataCurrent.resolution_state == "VALUE",
            )
        )
    }
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ValueError("Source Privilege Review matter is unavailable")
    tenant_id = program.tenant_id
    client_id = matter.client_id
    matter_id = program.matter_id
    program_id = program.id
    db.commit()
    packets = load_privilege_evidence_packets(
        packet_reference=packets_reference,
        matter_id=matter_id,
        program_id=program_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    groups = privilege_evidence_family_groups(packets)

    family_cells: dict[str, set[str]] = {}
    family_documents: dict[str, list[str]] = {}
    for group in groups:
        family_key = str(group["family_key"])
        document_ids = [uuid.UUID(str(value)) for value in group["review_target_document_ids"]]
        cells = {
            f"{recommendation}:{'positive' if truth_values[document_id] else 'negative'}"
            for document_id in document_ids
            if (recommendation := recommendation_values.get(document_id)) is not None
            and document_id in truth_values
        }
        if not cells:
            continue
        family_cells[family_key] = cells
        family_documents[family_key] = [str(value) for value in document_ids]
    selected_family_keys, selected_document_ids, strata = select_stratified_families(
        family_cells=family_cells,
        family_documents=family_documents,
        per_stratum=per_stratum,
        random_seed=random_seed,
    )
    identity = {
        "schema_version": 1,
        "source_program_id": str(program.id),
        "source_program_run_id": str(run.id),
        "source_adjudication_run_id": str(adjudication.id),
        "source_program_version_id": str(version.id),
        "evidence_packets_artifact_id": str(packets_reference["artifact_id"]),
        "evidence_packets_content_hash": str(packets_reference["content_hash"]),
        "truth_metadata_definition_id": str(truth_definition.id),
        "per_stratum": per_stratum,
        "random_seed": random_seed,
        "selected_family_keys": selected_family_keys,
        "selected_document_ids": selected_document_ids,
        "strata": strata,
    }
    return {
        **identity,
        "family_count": len(selected_family_keys),
        "document_count": len(selected_document_ids),
        "preview_hash": canonical_hash(identity),
    }
