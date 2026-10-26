from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis_task_batch import resolve_decision_binding
from app.analysis_tasks import decision_provider_model, json_content_hash
from app.models import (
    AnalysisTaskExecutionProfile,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
)

PROVIDER_ADAPTER_VERSIONS = {
    "JEV": "jev-decision-engine-v1",
    "OPENAI_DECISIONS": "openai-decisions-v1",
}


class AnalysisTaskExecutionProfileError(ValueError):
    pass


def create_execution_profile(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    key: str,
    name: str,
    decision_provider: str,
    actor_user_id: uuid.UUID,
) -> AnalysisTaskExecutionProfile:
    if (
        task.task_type != "QUESTION_ANSWERING"
        or task.matter_id != matter.id
        or task_version.matter_analysis_task_id != task.id
    ):
        raise AnalysisTaskExecutionProfileError(
            "Execution profiles require a question-answering task version in this matter"
        )
    adapter_version = PROVIDER_ADAPTER_VERSIONS.get(decision_provider)
    if adapter_version is None:
        raise AnalysisTaskExecutionProfileError("Unsupported decision provider")
    existing = db.scalar(
        select(AnalysisTaskExecutionProfile).where(
            AnalysisTaskExecutionProfile.matter_analysis_task_version_id == task_version.id,
            AnalysisTaskExecutionProfile.key == key,
        )
    )
    resolved, _ = resolve_decision_binding(db, matter.client.tenant_id)
    _, _, skill_version = resolved["decision_evaluation"]
    if decision_provider == "JEV":
        model_key = skill_version.model_key
        resolved_model = model_key
        model_policy = skill_version.model_policy
    else:
        resolved_model = decision_provider_model(decision_provider)
        if resolved_model is None:
            raise AnalysisTaskExecutionProfileError("Decision provider has no configured model")
        model_key = resolved_model
        model_policy = {"temperature": 0}
    snapshot = {
        "schema_version": 1,
        "task_version_id": str(task_version.id),
        "decision_provider": decision_provider,
        "model_key": model_key,
        "resolved_model": resolved_model,
        "model_policy": model_policy,
        "required_capabilities": sorted(skill_version.required_capabilities),
        "skill_definition_version_id": str(skill_version.id),
        "provider_adapter_version": adapter_version,
    }
    profile_hash = json_content_hash(snapshot)
    if existing is not None:
        if existing.content_hash != profile_hash:
            raise AnalysisTaskExecutionProfileError(
                "An execution profile with this key already has different immutable settings"
            )
        return existing
    profile = AnalysisTaskExecutionProfile(
        matter_analysis_task_version_id=task_version.id,
        key=key,
        name=" ".join(name.split()),
        decision_provider=decision_provider,
        model_key=model_key,
        resolved_model=resolved_model,
        model_policy=model_policy,
        required_capabilities=sorted(skill_version.required_capabilities),
        skill_definition_version_id=skill_version.id,
        provider_adapter_version=adapter_version,
        content_hash=profile_hash,
        created_by_user_id=actor_user_id,
    )
    db.add(profile)
    db.flush()
    return profile


def execution_profiles_for_version(
    db: Session,
    task_version_id: uuid.UUID,
) -> list[AnalysisTaskExecutionProfile]:
    return list(
        db.scalars(
            select(AnalysisTaskExecutionProfile)
            .where(AnalysisTaskExecutionProfile.matter_analysis_task_version_id == task_version_id)
            .order_by(AnalysisTaskExecutionProfile.created_at, AnalysisTaskExecutionProfile.id)
        )
    )


def validate_dual_execution_profiles(
    db: Session,
    *,
    task_version: MatterAnalysisTaskVersion,
    profile_ids: list[uuid.UUID],
) -> list[AnalysisTaskExecutionProfile]:
    if len(profile_ids) != 2 or len(set(profile_ids)) != 2:
        raise AnalysisTaskExecutionProfileError("Exactly two distinct execution profiles are required")
    profiles_by_id = {
        profile.id: profile
        for profile in db.scalars(
            select(AnalysisTaskExecutionProfile).where(
                AnalysisTaskExecutionProfile.id.in_(profile_ids),
                AnalysisTaskExecutionProfile.matter_analysis_task_version_id == task_version.id,
            )
        )
    }
    if len(profiles_by_id) != 2:
        raise AnalysisTaskExecutionProfileError(
            "Both execution profiles must belong to the same Analysis Task version"
        )
    profiles = [profiles_by_id[profile_id] for profile_id in profile_ids]
    if len({profile.content_hash for profile in profiles}) != 2:
        raise AnalysisTaskExecutionProfileError("Execution profiles must have distinct immutable settings")
    return profiles
