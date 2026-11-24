from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import uuid
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent_models import resolve_agent_model
from app.artifact_gateway import copy_workflow_artifact_content, store_workflow_artifact
from app.audit import record_audit
from app.config import get_settings
from app.identity_analysis import canonical_hash
from app.job_executions import (
    IDENTITY_DOMAIN_RECOMMENDATION_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.matter_entities import MatterEntityConflict, create_entity, get_entity, normalize_entity_name, update_entity
from app.models import (
    Matter,
    MatterEntity,
    MatterEntityDomain,
    MatterIdentityAnalysis,
    MatterIdentityDomainDecision,
    MatterIdentityRecommendationRun,
    SkillDefinitionVersion,
    WorkflowStepRun,
    utcnow,
)
from app.skill_execution import execute_skill_run
from app.workflow_specs import IDENTITY_DOMAIN_RECOMMENDATION_SPEC, binding_snapshot, resolve_workflow_skill_bindings
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

SCHEMA_VERSION = 2
DOMAIN_PACKET_SIZE = 100
DOMAIN_REVIEWER_ROLE = "domain_reviewer"
COMMON_PERSONAL_EMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "earthlink.net",
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "me.com",
        "msn.com",
        "outlook.com",
        "proton.me",
        "protonmail.com",
        "yahoo.com",
    }
)
RECOMMENDABLE_ORGANIZATION_CATEGORIES = frozenset(
    {
        "LAW_FIRM",
        "CORPORATION",
        "GOVERNMENT",
        "ACCOUNTING_FIRM",
        "CONSULTING_FIRM",
        "INSURER",
        "EMAIL_PROVIDER",
        "OTHER",
    }
)


class IdentityDomainRecommendationError(ValueError):
    pass


def normalize_domain(value: str) -> str:
    domain = value.strip().strip(".").casefold()
    if not domain or len(domain) > 253 or "@" in domain or any(character.isspace() for character in domain):
        raise IdentityDomainRecommendationError(f"Invalid domain: {value}")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise IdentityDomainRecommendationError(f"Invalid domain: {value}") from exc
    labels = domain.split(".")
    if len(labels) < 2 or any(not label or len(label) > 63 for label in labels):
        raise IdentityDomainRecommendationError(f"Invalid domain: {value}")
    return domain


def _read_inventory(
    analysis: MatterIdentityAnalysis,
    matter: Matter,
) -> dict[str, Any]:
    if analysis.result_artifact_id is None:
        raise IdentityDomainRecommendationError("Identity Analysis has no result artifact")
    content = io.BytesIO()
    copy_workflow_artifact_content(
        artifact_id=analysis.result_artifact_id,
        destination=content,
        actor_user_id=analysis.created_by_user_id,
        tenant_id=analysis.tenant_id,
        client_id=matter.client_id,
    )
    try:
        result = json.loads(content.getvalue())
    except (TypeError, ValueError) as exc:
        raise IdentityDomainRecommendationError("Identity Analysis result is not valid JSON") from exc
    if result.get("analysis_id") != str(analysis.id) or not isinstance(result.get("domains"), list):
        raise IdentityDomainRecommendationError("Identity Analysis result does not match the selected analysis")
    return result


def _domain_catalog(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    display_names: dict[str, Counter[str]] = defaultdict(Counter)
    for identity in inventory.get("identities", []):
        try:
            domain = normalize_domain(identity.get("domain", ""))
        except IdentityDomainRecommendationError:
            continue
        for row in identity.get("display_names", []):
            value = " ".join(str(row.get("value", "")).split())
            if value:
                display_names[domain][value] += max(1, int(row.get("observation_count", 1)))

    catalog: list[dict[str, Any]] = []
    for row in inventory.get("domains", []):
        try:
            domain = normalize_domain(row.get("domain", ""))
        except IdentityDomainRecommendationError:
            continue
        if domain in COMMON_PERSONAL_EMAIL_DOMAINS:
            continue
        catalog.append(
            {
                "domain": domain,
                "identity_count": max(0, int(row.get("identity_count", 0))),
                "document_count": max(0, int(row.get("document_count", 0))),
                "sender_count": max(0, int(row.get("sender_count", 0))),
                "recipient_count": max(0, int(row.get("recipient_count", 0))),
                "display_name_samples": [name for name, _count in display_names[domain].most_common(8)],
            }
        )
    return sorted(catalog, key=lambda row: (-row["document_count"], row["domain"]))


def create_identity_domain_recommendation_run(
    db: Session,
    *,
    matter: Matter,
    analysis: MatterIdentityAnalysis,
    actor_user_id: uuid.UUID,
) -> MatterIdentityRecommendationRun:
    if analysis.matter_id != matter.id or analysis.status != "COMPLETED" or analysis.result_artifact_id is None:
        raise IdentityDomainRecommendationError("Select a completed Identity Analysis with a result artifact")
    active = db.scalar(
        select(MatterIdentityRecommendationRun).where(
            MatterIdentityRecommendationRun.identity_analysis_id == analysis.id,
            MatterIdentityRecommendationRun.status.in_(("QUEUED", "RUNNING")),
        )
    )
    if active is not None:
        raise JobExecutionConflict("A domain review is already active for this Identity Analysis")
    try:
        resolved = resolve_workflow_skill_bindings(
            db,
            workflow_key=IDENTITY_DOMAIN_RECOMMENDATION_SPEC.key,
            tenant_id=matter.client.tenant_id,
        )
    except ValueError as exc:
        raise IdentityDomainRecommendationError(f"Identity domain reviewer is not configured: {exc}") from exc
    skill_version = resolved[DOMAIN_REVIEWER_ROLE][2]
    resolved_model = resolve_agent_model(skill_version.model_key, get_settings())
    run_id = uuid.uuid4()
    workflow_id = f"identity-domain-review:{run_id}:attempt:1"
    run = MatterIdentityRecommendationRun(
        id=run_id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        identity_analysis_id=analysis.id,
        status="QUEUED",
        workflow_id=workflow_id,
        current_stage="QUEUED",
        domain_count=analysis.domain_count,
        created_by_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    workflow = create_job_execution(
        db,
        job_type=IDENTITY_DOMAIN_RECOMMENDATION_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(run.tenant_id, matter.client_id, matter.id),
        workflow_key=IDENTITY_DOMAIN_RECOMMENDATION_SPEC.key,
        code_version=IDENTITY_DOMAIN_RECOMMENDATION_SPEC.code_version,
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=f"identity-domain-review:{analysis.id}:{analysis.result_content_hash}",
        current_stage="QUEUED",
        progress_total=analysis.domain_count,
        progress_unit="domains",
        input_snapshot={
            "identity_analysis_id": str(analysis.id),
            "identity_artifact_id": str(analysis.result_artifact_id),
            "identity_artifact_hash": analysis.result_content_hash,
        },
        binding_snapshot=binding_snapshot(resolved),
        configuration_snapshot={"resolved_models": {DOMAIN_REVIEWER_ROLE: str(resolved_model)}},
    )
    db.add(
        WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key=DOMAIN_REVIEWER_ROLE,
            ordinal=1,
            fan_out_group="domain_packets",
            status="QUEUED",
            total_count=max(1, math.ceil(max(1, analysis.domain_count) / DOMAIN_PACKET_SIZE)),
        )
    )
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="identity_domain_recommendation.requested",
        target_type="matter_identity_recommendation_run",
        target_id=run.id,
        details={"matter_id": str(matter.id), "identity_analysis_id": str(analysis.id)},
    )
    return run


def _normalize_packet_output(allowed_domains: set[str]):
    def normalize(output: dict[str, Any]) -> dict[str, Any]:
        recommendations = []
        for raw in output.get("recommendations", []):
            recommendation = dict(raw)
            recommendation["domain"] = normalize_domain(recommendation["domain"])
            recommendation["canonical_domain"] = normalize_domain(recommendation["canonical_domain"])
            recommendation["organization_name"] = " ".join(recommendation["organization_name"].split())
            recommendation["rationale"] = " ".join(recommendation["rationale"].split())
            recommendation["evidence_signals"] = [
                " ".join(str(value).split()) for value in recommendation.get("evidence_signals", [])
            ]
            recommendations.append(recommendation)
        return {"recommendations": recommendations}

    def validate(output: dict[str, Any]) -> None:
        seen: set[str] = set()
        for recommendation in output["recommendations"]:
            domain = recommendation["domain"]
            canonical_domain = recommendation["canonical_domain"]
            if domain not in allowed_domains:
                raise ValueError(f"Recommendation references a domain outside the supplied packet: {domain}")
            if canonical_domain != domain and not domain.endswith(f".{canonical_domain}"):
                raise ValueError(f"Canonical domain is not the supplied domain or its parent: {canonical_domain}")
            if domain in seen:
                raise ValueError(f"Duplicate domain recommendation: {domain}")
            seen.add(domain)
            if recommendation["organization_category"] not in RECOMMENDABLE_ORGANIZATION_CATEGORIES:
                raise ValueError("Domain review returned an unsupported organization category")

    return normalize, validate


def _publish_organization_proposals(
    db: Session,
    *,
    run: MatterIdentityRecommendationRun,
    workflow_id: uuid.UUID,
    recommendations: list[dict[str, Any]],
    matter: Matter,
) -> dict[str, int]:
    """Project artifact-backed recommendations into the existing proposed-entity review queue."""

    published_by_domain: dict[str, MatterEntity] = {}
    published_by_name: dict[str, MatterEntity] = {}
    resolved_by_name: dict[str, MatterEntity | None] = {}
    created_count = 0
    already_proposed_count = 0
    already_confirmed_count = 0
    for recommendation in recommendations:
        canonical_domain = normalize_domain(recommendation["canonical_domain"])
        normalized_name = normalize_entity_name(recommendation["organization_name"])
        mapped = db.scalar(
            select(MatterEntityDomain).where(
                MatterEntityDomain.matter_id == matter.id,
                MatterEntityDomain.normalized_domain == canonical_domain,
            )
        )
        entity = published_by_domain.get(canonical_domain)
        if entity is None and mapped is not None:
            entity = db.get(MatterEntity, mapped.entity_id)
        if entity is None:
            entity = published_by_name.get(normalized_name)
        if entity is None and normalized_name not in resolved_by_name:
            exact_name_matches = list(
                db.scalars(
                    select(MatterEntity)
                    .where(
                        MatterEntity.matter_id == matter.id,
                        MatterEntity.parent_id.is_(None),
                        MatterEntity.entity_type == "ORGANIZATION",
                        MatterEntity.status != "REJECTED",
                        MatterEntity.normalized_name == normalized_name,
                    )
                    .order_by(MatterEntity.created_at, MatterEntity.id)
                )
            )
            resolved_by_name[normalized_name] = exact_name_matches[0] if exact_name_matches else None
        if entity is None:
            entity = resolved_by_name.get(normalized_name)
        if entity is not None:
            if entity.organization_category is None:
                update_entity(
                    db,
                    matter=matter,
                    entity=entity,
                    lock_version=entity.lock_version,
                    changes={"organization_category": recommendation["organization_category"]},
                    actor_user_id=None,
                    actor_type="AGENT",
                    workflow_run_id=workflow_id,
                )
            if mapped is None:
                db.add(
                    MatterEntityDomain(
                        matter_id=matter.id,
                        entity_id=entity.id,
                        domain=canonical_domain,
                        normalized_domain=canonical_domain,
                        source_recommendation_run_id=run.id,
                        created_by_user_id=run.created_by_user_id,
                    )
                )
                db.flush()
            published_by_domain[canonical_domain] = entity
            published_by_name[normalized_name] = entity
            recommendation["proposed_entity_id"] = str(entity.id)
            if entity.status == "CONFIRMED":
                recommendation["publication_status"] = "ALREADY_CONFIRMED"
                already_confirmed_count += 1
            elif entity.status == "PROPOSED":
                recommendation["publication_status"] = "ALREADY_PROPOSED"
                already_proposed_count += 1
            else:
                recommendation["publication_status"] = "SKIPPED_REJECTED_MAPPING"
                recommendation["proposed_entity_id"] = None
            continue

        entity = create_entity(
            db,
            matter=matter,
            entity_type="ORGANIZATION",
            organization_category=recommendation["organization_category"],
            display_name=recommendation["organization_name"],
            status="PROPOSED",
            actor_type="AGENT",
            actor_user_id=None,
            workflow_run_id=workflow_id,
            event_context={
                "identity_domain_recommendation_run_id": str(run.id),
                "domain": canonical_domain,
                "confidence": recommendation["confidence"],
            },
        )
        db.add(
            MatterEntityDomain(
                matter_id=matter.id,
                entity_id=entity.id,
                domain=canonical_domain,
                normalized_domain=canonical_domain,
                source_recommendation_run_id=run.id,
                created_by_user_id=run.created_by_user_id,
            )
        )
        db.flush()
        published_by_domain[canonical_domain] = entity
        published_by_name[normalized_name] = entity
        resolved_by_name[normalized_name] = entity
        recommendation["proposed_entity_id"] = str(entity.id)
        recommendation["publication_status"] = "PROPOSED"
        created_count += 1

    return {
        "proposed_entity_count": created_count,
        "already_proposed_count": already_proposed_count,
        "already_confirmed_count": already_confirmed_count,
    }


def run_identity_domain_recommendations(
    run_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        run = db.get(MatterIdentityRecommendationRun, run_id)
        if run is None:
            raise IdentityDomainRecommendationError("Identity domain review not found")
        analysis = db.get(MatterIdentityAnalysis, run.identity_analysis_id)
        matter = db.get(Matter, run.matter_id)
        workflow = current_job_execution(db, job_type=IDENTITY_DOMAIN_RECOMMENDATION_JOB_TYPE, job_id=run.id)
        if analysis is None or matter is None or workflow is None:
            raise IdentityDomainRecommendationError("Identity domain review context is unavailable")
        step = db.scalar(
            select(WorkflowStepRun).where(
                WorkflowStepRun.workflow_run_id == workflow.id,
                WorkflowStepRun.role_key == DOMAIN_REVIEWER_ROLE,
            )
        )
        skill_snapshot = workflow.binding_snapshot.get(DOMAIN_REVIEWER_ROLE, {})
        skill_version_id = skill_snapshot.get("skill_definition_version_id")
        skill_version = db.get(SkillDefinitionVersion, uuid.UUID(skill_version_id)) if skill_version_id else None
        if step is None or skill_version is None:
            raise IdentityDomainRecommendationError("Pinned domain-review skill is unavailable")
        context = {
            "analysis_id": analysis.id,
            "source_artifact_id": analysis.result_artifact_id,
            "source_artifact_hash": analysis.result_content_hash,
            "actor_user_id": run.created_by_user_id,
            "tenant_id": run.tenant_id,
            "client_id": matter.client_id,
            "matter_id": matter.id,
            "workflow_id": workflow.id,
            "model": workflow.configuration_snapshot.get("resolved_models", {}).get(DOMAIN_REVIEWER_ROLE),
        }
        # Release the Core DB connection before reading the artifact service and
        # before any model request. The pinned ORM objects remain usable because
        # the application Session factory disables expiration on commit.
        db.commit()
        inventory = _read_inventory(analysis, matter)
        catalog = _domain_catalog(inventory)
        source_domains: set[str] = set()
        for row in inventory.get("domains", []):
            try:
                source_domains.add(normalize_domain(row.get("domain", "")))
            except IdentityDomainRecommendationError:
                continue
        excluded_personal_domains = sorted(source_domains & COMMON_PERSONAL_EMAIL_DOMAINS)
        transition_job_execution(
            db,
            job_type=IDENTITY_DOMAIN_RECOMMENDATION_JOB_TYPE,
            job_id=run.id,
            status="RUNNING",
            current_stage="REVIEWING_DOMAINS",
            progress_total=len(catalog),
            progress={
                "source_domain_count": analysis.domain_count,
                "excluded_personal_email_domains": len(excluded_personal_domains),
            },
        )
        step.status = "RUNNING"
        step.total_count = max(1, math.ceil(max(1, len(catalog)) / DOMAIN_PACKET_SIZE))
        step.started_at = step.started_at or utcnow()
        db.commit()

        recommendations_by_domain: dict[str, dict[str, Any]] = {}
        for start in range(0, len(catalog), DOMAIN_PACKET_SIZE):
            packet = catalog[start : start + DOMAIN_PACKET_SIZE]
            allowed = {row["domain"] for row in packet}
            normalizer, validator = _normalize_packet_output(allowed)
            output, _skill_run = asyncio.run(
                execute_skill_run(
                    db,
                    workflow=workflow,
                    step=step,
                    skill_version=skill_version,
                    scope_type="MATTER_IDENTITY_ANALYSIS",
                    scope_id=analysis.id,
                    stable_context={
                        "task": (
                            "Identify credible organizations and their categories from aggregate participant "
                            "domain metadata only."
                        ),
                        "source_contract": inventory.get("source_contract"),
                    },
                    dynamic_input={"domains": packet},
                    cache_identity={
                        "identity_artifact_hash": analysis.result_content_hash,
                        "skill_version_id": str(skill_version.id),
                        "packet": start // DOMAIN_PACKET_SIZE,
                    },
                    output_normalizers=(normalizer,),
                    output_validators=(validator,),
                    usage_job_type=IDENTITY_DOMAIN_RECOMMENDATION_JOB_TYPE,
                    model=context["model"],
                )
            )
            for recommendation in output["recommendations"]:
                previous = recommendations_by_domain.get(recommendation["domain"])
                if previous is None or recommendation["confidence"] > previous["confidence"]:
                    recommendations_by_domain[recommendation["domain"]] = recommendation
            completed = min(len(catalog), start + len(packet))
            step.completed_count = min(step.total_count, (start // DOMAIN_PACKET_SIZE) + 1)
            transition_job_execution(
                db,
                job_type=IDENTITY_DOMAIN_RECOMMENDATION_JOB_TYPE,
                job_id=run.id,
                current_stage="REVIEWING_DOMAINS",
                progress_completed=completed,
                progress={"recommendation_count": len(recommendations_by_domain)},
            )
            db.commit()

        recommendations = sorted(
            recommendations_by_domain.values(), key=lambda row: (-row["confidence"], row["domain"])
        )
        publication_summary = _publish_organization_proposals(
            db,
            run=run,
            workflow_id=workflow.id,
            recommendations=recommendations,
            matter=matter,
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "recommendation_run_id": str(run.id),
            "identity_analysis_id": str(analysis.id),
            "source_artifact_id": str(analysis.result_artifact_id),
            "source_artifact_hash": analysis.result_content_hash,
            "reviewed_domain_count": len(catalog),
            "excluded_personal_email_domains": excluded_personal_domains,
            "recommendations": recommendations,
            "guardrails": {
                "document_text_used": False,
                "privilege_determination": False,
                "operator_confirmation_required": True,
            },
        }
        content_bytes = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        artifact, _created = store_workflow_artifact(
            content=io.BytesIO(content_bytes),
            artifact_type=ArtifactType.IDENTITY_DOMAIN_RECOMMENDATIONS,
            core_scope_kind="MATTER",
            core_scope_id=matter.id,
            schema_version=SCHEMA_VERSION,
            derivation_key=canonical_hash(
                {
                    "artifact_type": ArtifactType.IDENTITY_DOMAIN_RECOMMENDATIONS.value,
                    "recommendation_run_id": str(run.id),
                    "source_artifact_hash": analysis.result_content_hash,
                    "skill_version_id": str(skill_version.id),
                    "schema_version": SCHEMA_VERSION,
                }
            ),
            content_hash=content_hash,
            byte_length=len(content_bytes),
            media_type="application/json",
            producer_kind="CORE",
            producer_run_id=workflow.id,
            artifact_metadata={
                "recommendation_run_id": str(run.id),
                "identity_analysis_id": str(analysis.id),
                "recommendation_count": len(recommendations),
            },
            lineage=[
                WorkflowArtifactLineageInput(
                    source_kind="WORKFLOW_ARTIFACT", source_artifact_id=analysis.result_artifact_id
                )
            ],
            actor_user_id=run.created_by_user_id,
            tenant_id=run.tenant_id,
            client_id=matter.client_id,
            filename=f"identity-domain-recommendations-{run.id}.json",
        )
        run.result_artifact_id = artifact.artifact_id
        run.result_content_hash = artifact.content_hash
        run.result_byte_length = artifact.byte_length
        run.recommendation_count = len(recommendations)
        run.summary = {
            "source_domain_count": analysis.domain_count,
            "reviewed_domain_count": len(catalog),
            "excluded_personal_email_domain_count": len(excluded_personal_domains),
            "recommendation_count": len(recommendations),
            "high_confidence_count": sum(1 for row in recommendations if row["confidence"] >= 0.8),
            **publication_summary,
        }
        step.status = "COMPLETED"
        step.completed_count = step.total_count
        step.completed_at = utcnow()
        transition_job_execution(
            db,
            job_type=IDENTITY_DOMAIN_RECOMMENDATION_JOB_TYPE,
            job_id=run.id,
            status="COMPLETED",
            current_stage="COMPLETED",
            progress_completed=len(catalog),
            progress_total=len(catalog),
            progress=run.summary,
        )
        record_audit(
            db,
            tenant_id=run.tenant_id,
            actor_user_id=run.created_by_user_id,
            action="identity_domain_recommendation.completed",
            target_type="matter_identity_recommendation_run",
            target_id=run.id,
            details={"matter_id": str(matter.id), "recommendation_count": len(recommendations)},
        )
        db.commit()


def fail_identity_domain_recommendations(db: Session, run_id: uuid.UUID, error: Exception) -> None:
    run = db.get(MatterIdentityRecommendationRun, run_id)
    if run is None or run.status in {"COMPLETED", "FAILED", "CANCELED"}:
        return
    transition_job_execution(
        db,
        job_type=IDENTITY_DOMAIN_RECOMMENDATION_JOB_TYPE,
        job_id=run.id,
        status="FAILED",
        current_stage="FAILED",
        failure_code=type(error).__name__.upper(),
        failure_summary=" ".join(str(error).split())[:4000],
        failure_retriable=False,
    )
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == run.current_workflow_run_id,
            WorkflowStepRun.role_key == DOMAIN_REVIEWER_ROLE,
        )
    )
    if step is not None and step.status not in {"COMPLETED", "FAILED", "CANCELED"}:
        step.status = "FAILED"
        step.error_message = " ".join(str(error).split())[:4000]
        step.completed_at = utcnow()


def recommendation_result(run: MatterIdentityRecommendationRun, matter: Matter) -> dict[str, Any]:
    if run.status != "COMPLETED" or run.result_artifact_id is None:
        raise IdentityDomainRecommendationError("Domain recommendations are not complete")
    content = io.BytesIO()
    copy_workflow_artifact_content(
        artifact_id=run.result_artifact_id,
        destination=content,
        actor_user_id=run.created_by_user_id,
        tenant_id=run.tenant_id,
        client_id=matter.client_id,
    )
    result = json.loads(content.getvalue())
    if result.get("recommendation_run_id") != str(run.id):
        raise IdentityDomainRecommendationError("Recommendation artifact does not match the selected run")
    return result


def decide_domain_recommendation(
    db: Session,
    *,
    matter: Matter,
    run: MatterIdentityRecommendationRun,
    domain: str,
    decision: str,
    actor_user_id: uuid.UUID,
    organization_name: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> MatterIdentityDomainDecision:
    normalized_domain = normalize_domain(domain)
    existing = db.scalar(
        select(MatterIdentityDomainDecision).where(
            MatterIdentityDomainDecision.recommendation_run_id == run.id,
            MatterIdentityDomainDecision.domain == normalized_domain,
        )
    )
    if existing is not None:
        raise MatterEntityConflict("This domain recommendation has already been decided")
    result = recommendation_result(run, matter)
    recommendation = next(
        (row for row in result.get("recommendations", []) if row.get("domain") == normalized_domain), None
    )
    if recommendation is None:
        raise IdentityDomainRecommendationError("Domain recommendation not found")
    canonical_domain = normalize_domain(recommendation["canonical_domain"])
    organization_category = recommendation["organization_category"]
    if organization_category not in RECOMMENDABLE_ORGANIZATION_CATEGORIES:
        raise IdentityDomainRecommendationError("Unsupported organization category")
    chosen_name = " ".join((organization_name or recommendation["organization_name"]).split())
    if not chosen_name:
        raise IdentityDomainRecommendationError("Organization name is required")
    if decision == "REJECTED":
        row = MatterIdentityDomainDecision(
            recommendation_run_id=run.id,
            matter_id=matter.id,
            domain=normalized_domain,
            decision="REJECTED",
            entity_id=None,
            organization_name=chosen_name,
            organization_category=organization_category,
            decided_by_user_id=actor_user_id,
        )
        db.add(row)
        db.flush()
        record_audit(
            db,
            tenant_id=run.tenant_id,
            actor_user_id=actor_user_id,
            action="identity_domain_recommendation.decided",
            target_type="matter_identity_domain_decision",
            target_id=row.id,
            details={
                "matter_id": str(matter.id),
                "domain": normalized_domain,
                "decision": decision,
                "entity_id": None,
                "organization_category": organization_category,
            },
        )
        return row
    if decision != "ACCEPTED":
        raise IdentityDomainRecommendationError("Unsupported domain recommendation decision")

    mapped = db.scalar(
        select(MatterEntityDomain).where(
            MatterEntityDomain.matter_id == matter.id,
            MatterEntityDomain.normalized_domain == canonical_domain,
        )
    )
    entity: MatterEntity | None = None
    if entity_id is not None:
        entity = get_entity(db, matter_id=matter.id, entity_id=entity_id, for_update=True)
        if entity.parent_id is not None or entity.entity_type != "ORGANIZATION" or entity.status == "REJECTED":
            raise MatterEntityConflict("Select an active canonical organization")
        if mapped is not None and mapped.entity_id != entity.id:
            raise MatterEntityConflict("This domain is already assigned to another organization")
    elif mapped is not None:
        entity = get_entity(db, matter_id=matter.id, entity_id=mapped.entity_id, for_update=True)
    else:
        matches = list(
            db.scalars(
                select(MatterEntity).where(
                    MatterEntity.matter_id == matter.id,
                    MatterEntity.parent_id.is_(None),
                    MatterEntity.entity_type == "ORGANIZATION",
                    MatterEntity.status != "REJECTED",
                    MatterEntity.normalized_name == normalize_entity_name(chosen_name),
                )
            )
        )
        if len(matches) > 1:
            raise MatterEntityConflict("Multiple organizations have this name; select the intended organization")
        entity = matches[0] if matches else None
    if entity is None:
        entity = create_entity(
            db,
            matter=matter,
            entity_type="ORGANIZATION",
            organization_category=organization_category,
            display_name=chosen_name,
            status="CONFIRMED",
            actor_user_id=actor_user_id,
            event_context={"identity_domain_recommendation_run_id": str(run.id), "domain": normalized_domain},
        )
    elif entity.organization_category != organization_category:
        update_entity(
            db,
            matter=matter,
            entity=entity,
            lock_version=entity.lock_version,
            changes={"organization_category": organization_category},
            actor_user_id=actor_user_id,
        )

    if mapped is None:
        db.add(
            MatterEntityDomain(
                matter_id=matter.id,
                entity_id=entity.id,
                domain=canonical_domain,
                normalized_domain=canonical_domain,
                source_recommendation_run_id=run.id,
                created_by_user_id=actor_user_id,
            )
        )
    row = MatterIdentityDomainDecision(
        recommendation_run_id=run.id,
        matter_id=matter.id,
        domain=normalized_domain,
        decision="ACCEPTED",
        entity_id=entity.id,
        organization_name=entity.display_name or chosen_name,
        organization_category=organization_category,
        decided_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="identity_domain_recommendation.decided",
        target_type="matter_identity_domain_decision",
        target_id=row.id,
        details={
            "matter_id": str(matter.id),
            "domain": normalized_domain,
            "decision": decision,
            "entity_id": str(entity.id),
            "organization_category": organization_category,
        },
    )
    return row
