import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import TestingSessionLocal, create_client_tenant
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_classification_builds import (
    FakeClassificationGateway,
    launch_build,
    ready_build_fixture,
)
from test_classification_models import auth, create_model

from app.classification_builds import execute_training_run
from app.classification_lifecycle import (
    ClassificationLifecycleConflictError,
    promote_model_version,
)
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelDecision,
    ClassificationModelVersion,
    MetadataDefinition,
)


def candidate_fixture(
    client: TestClient,
    token: str,
    root_admin,
    *,
    suffix: str,
) -> tuple[dict, dict, uuid.UUID]:
    matter, model, snapshot = ready_build_fixture(
        client,
        token,
        create_client_tenant(client, token),
        root_admin.id,
        suffix=suffix,
    )
    launched = launch_build(client, token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    gateway = FakeClassificationGateway()
    version_id = execute_training_run(
        run_id,
        session_factory=TestingSessionLocal,
        gateway=gateway,
        published_result_verifier=gateway.verify_published_result,
    )
    assert version_id is not None
    return matter, model, version_id


def approve(
    client: TestClient,
    token: str,
    matter_id: str,
    model_id: str,
    version_id: uuid.UUID,
) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/classification-models/{model_id}/versions/{version_id}/approve",
        headers=auth(token),
        json={"reason": "Validation evidence is sufficient for controlled use."},
    )
    assert response.status_code == 201, response.text
    return response.json()


def promotion_preview(
    client: TestClient,
    token: str,
    matter_id: str,
    model_id: str,
    version_id: uuid.UUID,
    *,
    action: str = "activation",
) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/classification-models/{model_id}/{action}/preview",
        headers=auth(token),
        json={"model_version_id": str(version_id)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def promote(
    client: TestClient,
    token: str,
    matter_id: str,
    model_id: str,
    preview: dict,
    *,
    action: str = "activation",
) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/classification-models/{model_id}/{action}",
        headers=auth(token),
        json={
            "model_version_id": preview["model_version"]["id"],
            "expected_model_row_version": preview["expected_model_row_version"],
            "preview_token": preview["preview_token"],
            "reason": f"Proceed with governed {action}.",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def second_candidate(
    client: TestClient,
    token: str,
    matter: dict,
    model: dict,
    first_version_id: uuid.UUID,
) -> uuid.UUID:
    with TestingSessionLocal() as db:
        first = db.get(ClassificationModelVersion, first_version_id)
        assert first is not None
        source = db.get(ClassificationDatasetSnapshot, first.dataset_snapshot_id)
        assert source is not None
        values = {
            column.name: getattr(source, column.name)
            for column in ClassificationDatasetSnapshot.__table__.columns
            if column.name != "id"
        }
        values.update(
            id=uuid.uuid4(),
            training_aggregate_hash="9" * 64,
            aggregate_hash="a" * 64,
        )
        snapshot = ClassificationDatasetSnapshot(**values)
        db.add(snapshot)
        db.commit()
        snapshot_id = snapshot.id
    launched = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/builds",
        headers=auth(token),
        json={"dataset_snapshot_id": str(snapshot_id), "idempotency_key": "second-model-version"},
    )
    assert launched.status_code == 202, launched.text
    run_id = uuid.UUID(launched.json()["training_run"]["id"])
    gateway = FakeClassificationGateway()
    version_id = execute_training_run(
        run_id,
        session_factory=TestingSessionLocal,
        gateway=gateway,
        published_result_verifier=gateway.verify_published_result,
    )
    assert version_id is not None
    return version_id


def test_candidate_decisions_require_reasons_and_remain_separate_from_activation(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, version_id = candidate_fixture(
        client,
        root_token,
        root_admin,
        suffix="Governed Decision",
    )
    path = (
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
        f"/versions/{version_id}/approve"
    )
    missing_reason = client.post(path, headers=auth(root_token), json={"reason": "  "})
    assert missing_reason.status_code == 422

    decision = approve(client, root_token, matter["id"], model["id"], version_id)
    assert decision["decision_type"] == "APPROVAL"
    assert decision["before_active_model_version_id"] is None
    assert decision["after_active_model_version_id"] is None
    assert decision["metric_snapshot"]["evaluations"]

    version = client.get(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/versions/{version_id}",
        headers=auth(root_token),
    )
    assert version.status_code == 200
    assert version.json()["status"] == "APPROVED"
    model_read = client.get(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}",
        headers=auth(root_token),
    )
    assert model_read.json()["active_model_version_id"] is None


def test_rejection_is_terminal_and_versions_cannot_cross_classifier_boundaries(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, version_id = candidate_fixture(
        client,
        root_token,
        root_admin,
        suffix="Reject Candidate",
    )
    second_model = create_model(
        client,
        root_token,
        matter["id"],
        model["target_metadata_definition_id"],
    )
    wrong_classifier = client.get(
        f"/v1/matters/{matter['id']}/classification-models/{second_model['id']}/versions/{version_id}",
        headers=auth(root_token),
    )
    assert wrong_classifier.status_code == 404

    rejected = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
        f"/versions/{version_id}/reject",
        headers=auth(root_token),
        json={"reason": "The observed error rate is not acceptable."},
    )
    assert rejected.status_code == 201, rejected.text
    assert rejected.json()["decision_type"] == "REJECTION"
    approval_after_rejection = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
        f"/versions/{version_id}/approve",
        headers=auth(root_token),
        json={"reason": "Attempt to reverse a terminal decision."},
    )
    assert approval_after_rejection.status_code == 409


def test_activation_is_atomic_rejects_stale_previews_and_protects_active_retirement(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, version_id = candidate_fixture(
        client,
        root_token,
        root_admin,
        suffix="Activate",
    )
    approve(client, root_token, matter["id"], model["id"], version_id)
    stale = promotion_preview(client, root_token, matter["id"], model["id"], version_id)
    assert stale["can_promote"] is True

    with TestingSessionLocal() as db:
        record = db.get(ClassificationModel, uuid.UUID(model["id"]))
        assert record is not None
        record.description = "Concurrent edit invalidates the preview"
        db.commit()
    stale_response = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/activation",
        headers=auth(root_token),
        json={
            "model_version_id": str(version_id),
            "expected_model_row_version": stale["expected_model_row_version"],
            "preview_token": stale["preview_token"],
            "reason": "This preview should be stale.",
        },
    )
    assert stale_response.status_code == 409

    current = promotion_preview(client, root_token, matter["id"], model["id"], version_id)
    promoted = promote(client, root_token, matter["id"], model["id"], current)
    assert promoted["status"] == "COMPLETED"
    assert promoted["model"]["active_model_version_id"] == str(version_id)
    assert promoted["decision"]["decision_type"] == "ACTIVATION"
    assert promoted["decision"]["after_active_model_version_id"] == str(version_id)
    repeated = promote(client, root_token, matter["id"], model["id"], current)
    assert repeated["status"] == "COMPLETED"
    assert repeated["decision"]["id"] == promoted["decision"]["id"]

    with TestingSessionLocal() as db:
        replayed = promote_model_version(
            db,
            matter_id=uuid.UUID(matter["id"]),
            classification_model_id=uuid.UUID(model["id"]),
            model_version_id=version_id,
            action="ACTIVATION",
            expected_model_row_version=current["expected_model_row_version"],
            preview_token=current["preview_token"],
            reason="Proceed with governed activation.",
            actor_user_id=root_admin.id,
            workflow_id=promoted["workflow_id"],
        )
        assert str(replayed.id) == promoted["decision"]["id"]
        assert (
            db.scalar(
                select(func.count()).select_from(ClassificationModelDecision).where(
                    ClassificationModelDecision.decision_type == "ACTIVATION"
                )
            )
            == 1
        )

    retirement = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/versions/{version_id}/retire",
        headers=auth(root_token),
        json={"reason": "Attempt to retire the active version."},
    )
    assert retirement.status_code == 409


def test_activation_api_queues_the_durable_promotion_in_dbos_mode(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter, model, version_id = candidate_fixture(
        client,
        root_token,
        root_admin,
        suffix="Queued Activation",
    )
    approve(client, root_token, matter["id"], model["id"], version_id)
    preview = promotion_preview(client, root_token, matter["id"], model["id"], version_id)
    enqueue = Mock()
    monkeypatch.setattr(
        "app.routers.classification_models.get_settings",
        lambda: SimpleNamespace(dbos_enabled=True),
    )
    monkeypatch.setattr(
        "app.routers.classification_models.enqueue_classification_model_promotion",
        enqueue,
    )

    queued = promote(client, root_token, matter["id"], model["id"], preview)

    assert queued["status"] == "QUEUED"
    assert queued["decision"] is None
    assert queued["model"] is None
    enqueue.assert_called_once()
    assert enqueue.call_args.args[1] == queued["workflow_id"]
    with TestingSessionLocal() as db:
        registry_model = db.get(ClassificationModel, uuid.UUID(model["id"]))
        assert registry_model is not None
        assert registry_model.active_model_version_id is None


def test_rollback_requires_prior_activation_and_preserves_complete_history(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, first_version_id = candidate_fixture(
        client,
        root_token,
        root_admin,
        suffix="Rollback",
    )
    approve(client, root_token, matter["id"], model["id"], first_version_id)
    first_preview = promotion_preview(client, root_token, matter["id"], model["id"], first_version_id)
    promote(client, root_token, matter["id"], model["id"], first_preview)

    second_version_id = second_candidate(
        client,
        root_token,
        matter,
        model,
        first_version_id,
    )
    approve(client, root_token, matter["id"], model["id"], second_version_id)
    second_preview = promotion_preview(client, root_token, matter["id"], model["id"], second_version_id)
    promote(client, root_token, matter["id"], model["id"], second_preview)

    rollback_preview = promotion_preview(
        client,
        root_token,
        matter["id"],
        model["id"],
        first_version_id,
        action="rollback",
    )
    assert rollback_preview["can_promote"] is True
    rolled_back = promote(
        client,
        root_token,
        matter["id"],
        model["id"],
        rollback_preview,
        action="rollback",
    )
    assert rolled_back["model"]["active_model_version_id"] == str(first_version_id)

    retired = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
        f"/versions/{second_version_id}/retire",
        headers=auth(root_token),
        json={"reason": "Superseded after a verified rollback."},
    )
    assert retired.status_code == 201, retired.text
    assert retired.json()["decision_type"] == "RETIREMENT"

    history = client.get(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/decisions",
        headers=auth(root_token),
    )
    assert history.status_code == 200
    decisions = history.json()
    assert [decision["decision_type"] for decision in decisions] == [
        "APPROVAL",
        "ACTIVATION",
        "APPROVAL",
        "ACTIVATION",
        "ROLLBACK",
        "RETIREMENT",
    ]
    activation_intervals = [
        (
            decision["before_active_model_version_id"],
            decision["after_active_model_version_id"],
        )
        for decision in decisions
        if decision["decision_type"] in {"ACTIVATION", "ROLLBACK"}
    ]
    assert activation_intervals == [
        (None, str(first_version_id)),
        (str(first_version_id), str(second_version_id)),
        (str(second_version_id), str(first_version_id)),
    ]


def test_field_deactivation_blocks_approval_and_execution_actor_is_reauthorized(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, version_id = candidate_fixture(
        client,
        root_token,
        root_admin,
        suffix="Lifecycle Authorization",
    )
    approve(client, root_token, matter["id"], model["id"], version_id)

    with TestingSessionLocal() as db:
        registry_model = db.get(ClassificationModel, uuid.UUID(model["id"]))
        assert registry_model is not None
        definition = db.get(MetadataDefinition, registry_model.target_metadata_definition_id)
        assert definition is not None
        definition.status = "SUSPENDED"
        db.commit()
    blocked = promotion_preview(client, root_token, matter["id"], model["id"], version_id)
    assert blocked["can_promote"] is False
    assert "Target metadata definition is not active" in blocked["blocking_reasons"]
    with TestingSessionLocal() as db:
        registry_model = db.get(ClassificationModel, uuid.UUID(model["id"]))
        assert registry_model is not None
        definition = db.get(MetadataDefinition, registry_model.target_metadata_definition_id)
        assert definition is not None
        definition.status = "ACTIVE"
        db.commit()
    preview = promotion_preview(client, root_token, matter["id"], model["id"], version_id)

    with TestingSessionLocal() as db:
        root = db.get(type(root_admin), root_admin.id)
        assert root is not None
        root.status = "SUSPENDED"
        db.commit()
    with TestingSessionLocal() as db, pytest.raises(
        ClassificationLifecycleConflictError,
        match="no longer active",
    ):
        promote_model_version(
            db,
            matter_id=uuid.UUID(matter["id"]),
            classification_model_id=uuid.UUID(model["id"]),
            model_version_id=version_id,
            action="ACTIVATION",
            expected_model_row_version=preview["expected_model_row_version"],
            preview_token=preview["preview_token"],
            reason="Execution-time authorization check.",
            actor_user_id=root_admin.id,
        )
    with TestingSessionLocal() as db:
        assert db.get(ClassificationModel, uuid.UUID(model["id"])).active_model_version_id is None
        assert db.scalar(select(func.count()).select_from(ClassificationModelDecision)) == 1
