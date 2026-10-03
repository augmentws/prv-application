import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.classification_models import active_value_keys
from app.config import Settings, get_settings
from app.models import (
    ClassificationEvaluation,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationModelDecision,
    ClassificationModelVersion,
    ClientMembership,
    Matter,
    MatterMembership,
    User,
)

DECISION_POLICY_VERSION = 1
REQUIRED_EVALUATION_KIND = "CROSS_FIT_DEVELOPMENT"
REQUIRED_METRIC_POLICY_VERSION = 1
PromotionAction = Literal["ACTIVATION", "ROLLBACK"]


class ClassificationLifecycleError(ValueError):
    def __init__(self, message: str, *, code: str = "CLASSIFICATION_LIFECYCLE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class ClassificationLifecycleNotFoundError(ClassificationLifecycleError):
    pass


class ClassificationLifecycleConflictError(ClassificationLifecycleError):
    pass


@dataclass(frozen=True)
class PromotionPreview:
    action: PromotionAction
    model: ClassificationModel
    version: ClassificationModelVersion
    current_active_model_version_id: uuid.UUID | None
    expected_model_row_version: int
    preview_token: str
    blocking_reasons: list[str]
    policy_snapshot: dict[str, Any]
    metric_snapshot: dict[str, Any]

    @property
    def can_promote(self) -> bool:
        return not self.blocking_reasons


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _preview_token(payload: dict[str, Any], settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    return hmac.new(
        resolved.jwt_secret.encode(),
        _canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()


def _get_model(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    for_update: bool = False,
) -> ClassificationModel:
    statement = select(ClassificationModel).where(
        ClassificationModel.id == classification_model_id,
        ClassificationModel.matter_id == matter_id,
    )
    if for_update:
        statement = statement.with_for_update()
    model = db.scalar(statement)
    if model is None:
        raise ClassificationLifecycleNotFoundError(
            "Classification model not found",
            code="CLASSIFICATION_MODEL_NOT_FOUND",
        )
    return model


def get_model_version(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    model_version_id: uuid.UUID,
    for_update: bool = False,
) -> ClassificationModelVersion:
    statement = select(ClassificationModelVersion).where(
        ClassificationModelVersion.id == model_version_id,
        ClassificationModelVersion.matter_id == matter_id,
        ClassificationModelVersion.classification_model_id == classification_model_id,
    )
    if for_update:
        statement = statement.with_for_update()
    version = db.scalar(statement)
    if version is None:
        raise ClassificationLifecycleNotFoundError(
            "Classification model version not found",
            code="CLASSIFICATION_MODEL_VERSION_NOT_FOUND",
        )
    return version


def authorization_context(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    user = db.get(User, actor_user_id)
    if (
        user is None
        or user.status != "ACTIVE"
        or user.tenant.status != "ACTIVE"
        or matter.status != "ACTIVE"
        or matter.client.status != "ACTIVE"
        or matter.client.tenant.status != "ACTIVE"
    ):
        raise ClassificationLifecycleConflictError(
            "Promotion actor or matter scope is no longer active",
            code="PROMOTION_AUTHORIZATION_REVOKED",
        )
    authorization_source: str | None = None
    if user.tenant.is_root and user.tenant_role == "ADMIN":
        authorization_source = "ROOT_TENANT_ADMIN"
    elif user.tenant_id == matter.client.tenant_id and user.tenant_role == "ADMIN":
        authorization_source = "TENANT_ADMIN"
    elif db.scalar(
        select(ClientMembership.id).where(
            ClientMembership.client_id == matter.client_id,
            ClientMembership.user_id == user.id,
            ClientMembership.role == "ADMIN",
        )
    ):
        authorization_source = "CLIENT_ADMIN"
    elif db.scalar(
        select(MatterMembership.id).where(
            MatterMembership.matter_id == matter.id,
            MatterMembership.user_id == user.id,
            MatterMembership.role == "ADMIN",
        )
    ):
        authorization_source = "MATTER_ADMIN"
    if authorization_source is None:
        raise ClassificationLifecycleConflictError(
            "Promotion actor is no longer authorized for the matter",
            code="PROMOTION_AUTHORIZATION_REVOKED",
        )
    return {
        "schema_version": 1,
        "required_role": "MATTER_ADMIN",
        "authorization_source": authorization_source,
        "actor_tenant_id": str(user.tenant_id),
        "matter_id": str(matter.id),
        "client_id": str(matter.client_id),
    }


def _evaluation_snapshot(
    db: Session,
    version: ClassificationModelVersion,
) -> tuple[dict[str, Any], bool]:
    evaluations = db.scalars(
        select(ClassificationEvaluation)
        .where(ClassificationEvaluation.model_version_id == version.id)
        .order_by(
            ClassificationEvaluation.evaluation_kind,
            ClassificationEvaluation.metric_policy_version,
            ClassificationEvaluation.id,
        )
    ).all()
    values = [
        {
            "evaluation_id": str(evaluation.id),
            "evaluation_kind": evaluation.evaluation_kind,
            "metric_policy_version": evaluation.metric_policy_version,
            "dataset_snapshot_id": str(evaluation.dataset_snapshot_id),
            "evaluation_artifact_id": str(evaluation.evaluation_artifact_id),
            "evaluation_content_hash": evaluation.evaluation_content_hash,
            "metric_summary": evaluation.metric_summary,
        }
        for evaluation in evaluations
    ]
    required_present = any(
        evaluation.evaluation_kind == REQUIRED_EVALUATION_KIND
        and evaluation.metric_policy_version == REQUIRED_METRIC_POLICY_VERSION
        for evaluation in evaluations
    )
    return {
        "schema_version": 1,
        "model_version_metric_summary": version.metric_summary,
        "evaluations": values,
    }, required_present


def _policy_snapshot(version: ClassificationModelVersion) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "required_evaluation_kind": REQUIRED_EVALUATION_KIND,
        "required_metric_policy_version": REQUIRED_METRIC_POLICY_VERSION,
        "required_package_schema_version": 1,
        "package_verification": "FINALIZATION_ATTESTATION_V1",
        "configuration_id": str(version.configuration_id),
        "target_field_snapshot": version.target_field_snapshot,
    }


def _version_compatibility_reasons(
    db: Session,
    *,
    model: ClassificationModel,
    version: ClassificationModelVersion,
    required_evaluation_present: bool,
) -> list[str]:
    reasons: list[str] = []
    definition = model.target_metadata_definition
    configuration = db.get(ClassificationModelConfiguration, version.configuration_id)
    run = version.training_run
    if model.status != "ACTIVE":
        reasons.append("Classification model is not active")
    if definition.status != "ACTIVE":
        reasons.append("Target metadata definition is not active")
    if not definition.reviewable:
        reasons.append("Target metadata definition is not reviewable")
    snapshot = version.target_field_snapshot
    if (
        snapshot.get("metadata_definition_id") != str(definition.id)
        or snapshot.get("stable_key") != definition.key
        or snapshot.get("metadata_type") != definition.type
        or snapshot.get("cardinality") != definition.cardinality
    ):
        reasons.append("Target field identity is incompatible with the model version")
    if configuration is None or configuration.classification_model_id != model.id:
        reasons.append("Model version configuration is unavailable")
    else:
        if configuration.target_field_snapshot != version.target_field_snapshot:
            reasons.append("Model version target snapshot does not match its configuration")
        available_values = set(active_value_keys(definition)) if definition.type == "ENUM" else {"false", "true"}
        if configuration.classification_strategy == "BINARY":
            mapping = configuration.binary_mapping or {}
            selected_values = set(mapping.get("negative_value_keys", [])) | set(
                mapping.get("positive_value_keys", [])
            )
        else:
            selected_values = set(configuration.ordered_class_keys)
        if not selected_values.issubset(available_values):
            reasons.append("One or more model classes are inactive or missing from the target field")
    if not required_evaluation_present:
        reasons.append("Required cross-fit development evaluation is missing")
    if version.package_schema_version != 1:
        reasons.append("Model package schema is not supported for activation")
    if (
        run.status != "COMPLETED"
        or run.configuration_id != version.configuration_id
        or run.dataset_snapshot_id != version.dataset_snapshot_id
        or run.package_schema_version != version.package_schema_version
        or run.runtime_image_digest != version.runtime_image_digest
        or run.source_revision != version.source_revision
        or run.model_artifact_id != version.model_artifact_id
        or run.model_content_hash != version.model_content_hash
        or run.prediction_artifact_id != version.prediction_artifact_id
        or run.prediction_content_hash != version.prediction_content_hash
        or run.evaluation_artifact_id != version.evaluation_artifact_id
        or run.evaluation_content_hash != version.evaluation_content_hash
        or run.result_manifest_artifact_id != version.result_manifest_artifact_id
        or run.result_manifest_content_hash != version.result_manifest_content_hash
    ):
        reasons.append("Model package does not match its verified build finalization")
    return reasons


def _build_promotion_preview(
    db: Session,
    *,
    action: PromotionAction,
    model: ClassificationModel,
    version: ClassificationModelVersion,
    settings: Settings | None = None,
) -> PromotionPreview:
    metric_snapshot, required_evaluation_present = _evaluation_snapshot(db, version)
    policy_snapshot = _policy_snapshot(version)
    reasons = _version_compatibility_reasons(
        db,
        model=model,
        version=version,
        required_evaluation_present=required_evaluation_present,
    )
    if version.status != "APPROVED":
        reasons.append("Model version must be approved")
    if model.active_model_version_id == version.id:
        reasons.append("Model version is already active")
    if action == "ROLLBACK":
        if model.active_model_version_id is None:
            reasons.append("A rollback requires a currently active model version")
        was_previously_active = db.scalar(
            select(ClassificationModelDecision.id).where(
                ClassificationModelDecision.classification_model_id == model.id,
                ClassificationModelDecision.decision_type.in_(("ACTIVATION", "ROLLBACK")),
                ClassificationModelDecision.after_active_model_version_id == version.id,
            )
        )
        if was_previously_active is None:
            reasons.append("Rollback target was never active")
    token_payload = {
        "schema_version": 1,
        "action": action,
        "classification_model_id": str(model.id),
        "model_version_id": str(version.id),
        "current_active_model_version_id": (
            str(model.active_model_version_id) if model.active_model_version_id is not None else None
        ),
        "expected_model_row_version": model.row_version,
        "blocking_reasons": reasons,
        "policy_snapshot": policy_snapshot,
        "metric_snapshot": metric_snapshot,
    }
    return PromotionPreview(
        action=action,
        model=model,
        version=version,
        current_active_model_version_id=model.active_model_version_id,
        expected_model_row_version=model.row_version,
        preview_token=_preview_token(token_payload, settings),
        blocking_reasons=reasons,
        policy_snapshot=policy_snapshot,
        metric_snapshot=metric_snapshot,
    )


def preview_promotion(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    model_version_id: uuid.UUID,
    action: PromotionAction,
    settings: Settings | None = None,
) -> PromotionPreview:
    model = _get_model(
        db,
        matter_id=matter_id,
        classification_model_id=classification_model_id,
    )
    version = get_model_version(
        db,
        matter_id=matter_id,
        classification_model_id=classification_model_id,
        model_version_id=model_version_id,
    )
    return _build_promotion_preview(
        db,
        action=action,
        model=model,
        version=version,
        settings=settings,
    )


def _append_decision(
    db: Session,
    *,
    model: ClassificationModel,
    version: ClassificationModelVersion,
    decision_type: str,
    workflow_id: str | None,
    reason: str,
    policy_snapshot: dict[str, Any],
    metric_snapshot: dict[str, Any],
    before_active_model_version_id: uuid.UUID | None,
    after_active_model_version_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
    authorization: dict[str, Any],
) -> ClassificationModelDecision:
    decision = ClassificationModelDecision(
        tenant_id=model.tenant_id,
        matter_id=model.matter_id,
        classification_model_id=model.id,
        model_version_id=version.id,
        decision_type=decision_type,
        workflow_id=workflow_id,
        reason=reason,
        policy_snapshot=policy_snapshot,
        metric_snapshot=metric_snapshot,
        before_active_model_version_id=before_active_model_version_id,
        after_active_model_version_id=after_active_model_version_id,
        authorization_context=authorization,
        actor_user_id=actor_user_id,
    )
    db.add(decision)
    db.flush()
    return decision


def decide_candidate(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    model_version_id: uuid.UUID,
    decision_type: Literal["APPROVAL", "REJECTION"],
    reason: str,
    actor_user_id: uuid.UUID,
) -> ClassificationModelDecision:
    model = _get_model(
        db,
        matter_id=matter_id,
        classification_model_id=classification_model_id,
        for_update=True,
    )
    version = get_model_version(
        db,
        matter_id=matter_id,
        classification_model_id=classification_model_id,
        model_version_id=model_version_id,
        for_update=True,
    )
    authorization = authorization_context(db, matter=model.matter, actor_user_id=actor_user_id)
    if version.status != "CANDIDATE":
        raise ClassificationLifecycleConflictError(
            "Only candidate model versions can be approved or rejected",
            code="VERSION_NOT_CANDIDATE",
        )
    metric_snapshot, required_evaluation_present = _evaluation_snapshot(db, version)
    policy_snapshot = _policy_snapshot(version)
    if decision_type == "APPROVAL":
        reasons = _version_compatibility_reasons(
            db,
            model=model,
            version=version,
            required_evaluation_present=required_evaluation_present,
        )
        if reasons:
            raise ClassificationLifecycleConflictError(
                "; ".join(reasons),
                code="VERSION_NOT_APPROVABLE",
            )
        version.status = "APPROVED"
    else:
        version.status = "REJECTED"
    decision = _append_decision(
        db,
        model=model,
        version=version,
        decision_type=decision_type,
        workflow_id=None,
        reason=reason,
        policy_snapshot=policy_snapshot,
        metric_snapshot=metric_snapshot,
        before_active_model_version_id=model.active_model_version_id,
        after_active_model_version_id=model.active_model_version_id,
        actor_user_id=actor_user_id,
        authorization=authorization,
    )
    record_audit(
        db,
        tenant_id=model.tenant_id,
        actor_user_id=actor_user_id,
        action=f"classification_model_version.{decision_type.casefold()}",
        target_type="classification_model_version",
        target_id=version.id,
        details={
            "classification_model_id": str(model.id),
            "decision_id": str(decision.id),
            "reason": reason,
        },
    )
    db.flush()
    return decision


def promote_model_version(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    model_version_id: uuid.UUID,
    action: PromotionAction,
    expected_model_row_version: int,
    preview_token: str,
    reason: str,
    actor_user_id: uuid.UUID,
    workflow_id: str | None = None,
    settings: Settings | None = None,
) -> ClassificationModelDecision:
    model = _get_model(
        db,
        matter_id=matter_id,
        classification_model_id=classification_model_id,
        for_update=True,
    )
    if workflow_id is not None:
        existing = db.scalar(
            select(ClassificationModelDecision).where(
                ClassificationModelDecision.workflow_id == workflow_id
            )
        )
        if existing is not None:
            if (
                existing.classification_model_id != classification_model_id
                or existing.model_version_id != model_version_id
                or existing.decision_type != action
                or existing.actor_user_id != actor_user_id
                or existing.reason != reason
            ):
                raise ClassificationLifecycleConflictError(
                    "Promotion workflow identity was reused for different inputs",
                    code="PROMOTION_WORKFLOW_ID_REUSED",
                )
            return existing
    version = get_model_version(
        db,
        matter_id=matter_id,
        classification_model_id=classification_model_id,
        model_version_id=model_version_id,
        for_update=True,
    )
    authorization = authorization_context(db, matter=model.matter, actor_user_id=actor_user_id)
    current = _build_promotion_preview(
        db,
        action=action,
        model=model,
        version=version,
        settings=settings,
    )
    if model.row_version != expected_model_row_version or not hmac.compare_digest(
        current.preview_token,
        preview_token,
    ):
        raise ClassificationLifecycleConflictError(
            "Promotion preview is stale; request a new preview",
            code="STALE_PROMOTION_PREVIEW",
        )
    if current.blocking_reasons:
        raise ClassificationLifecycleConflictError(
            "; ".join(current.blocking_reasons),
            code="PROMOTION_BLOCKED",
        )
    before_active_model_version_id = model.active_model_version_id
    model.active_model_version_id = version.id
    model.updated_by_user_id = actor_user_id
    decision = _append_decision(
        db,
        model=model,
        version=version,
        decision_type=action,
        workflow_id=workflow_id,
        reason=reason,
        policy_snapshot=current.policy_snapshot,
        metric_snapshot=current.metric_snapshot,
        before_active_model_version_id=before_active_model_version_id,
        after_active_model_version_id=version.id,
        actor_user_id=actor_user_id,
        authorization=authorization,
    )
    record_audit(
        db,
        tenant_id=model.tenant_id,
        actor_user_id=actor_user_id,
        action=f"classification_model.{action.casefold()}",
        target_type="classification_model",
        target_id=model.id,
        details={
            "decision_id": str(decision.id),
            "model_version_id": str(version.id),
            "before_active_model_version_id": (
                str(before_active_model_version_id) if before_active_model_version_id is not None else None
            ),
            "after_active_model_version_id": str(version.id),
            "reason": reason,
        },
    )
    db.flush()
    return decision


def retire_model_version(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    model_version_id: uuid.UUID,
    reason: str,
    actor_user_id: uuid.UUID,
) -> ClassificationModelDecision:
    model = _get_model(
        db,
        matter_id=matter_id,
        classification_model_id=classification_model_id,
        for_update=True,
    )
    version = get_model_version(
        db,
        matter_id=matter_id,
        classification_model_id=classification_model_id,
        model_version_id=model_version_id,
        for_update=True,
    )
    authorization = authorization_context(db, matter=model.matter, actor_user_id=actor_user_id)
    if model.active_model_version_id == version.id:
        raise ClassificationLifecycleConflictError(
            "The active model version cannot be retired",
            code="ACTIVE_VERSION_RETIREMENT_BLOCKED",
        )
    if version.status == "RETIRED":
        raise ClassificationLifecycleConflictError(
            "Model version is already retired",
            code="VERSION_ALREADY_RETIRED",
        )
    metric_snapshot, _ = _evaluation_snapshot(db, version)
    policy_snapshot = {
        **_policy_snapshot(version),
        "retention_check": "NO_ACTIVE_OR_SCORING_REFERENCES_V1",
        "blocking_reference_count": 0,
    }
    version.status = "RETIRED"
    decision = _append_decision(
        db,
        model=model,
        version=version,
        decision_type="RETIREMENT",
        workflow_id=None,
        reason=reason,
        policy_snapshot=policy_snapshot,
        metric_snapshot=metric_snapshot,
        before_active_model_version_id=model.active_model_version_id,
        after_active_model_version_id=model.active_model_version_id,
        actor_user_id=actor_user_id,
        authorization=authorization,
    )
    record_audit(
        db,
        tenant_id=model.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_model_version.retirement",
        target_type="classification_model_version",
        target_id=version.id,
        details={
            "classification_model_id": str(model.id),
            "decision_id": str(decision.id),
            "reason": reason,
        },
    )
    db.flush()
    return decision
