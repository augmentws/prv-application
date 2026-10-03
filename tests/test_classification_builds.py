import json
import uuid

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_classification_models import (
    auth,
    binary_configuration,
    create_enum_field,
    create_matter,
    create_model,
)

from app.classification_builds import (
    ClassificationBuildConflictError,
    execute_training_run,
    reconcile_training_run,
    training_request,
)
from app.config import get_settings
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationEvaluation,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationModelVersion,
    ClassificationTrainingRun,
)
from classification_service.schemas import (
    ArtifactReference,
    ArtifactType,
    ClassificationTrainingResult,
    ModelInferenceSignature,
    ModelPackageFile,
    ModelPackageManifest,
    PackageValidationResult,
)


def ready_build_fixture(
    client: TestClient,
    token: str,
    tenant_id: str,
    actor_user_id: uuid.UUID,
    *,
    suffix: str,
) -> tuple[dict, dict, ClassificationDatasetSnapshot]:
    matter = create_matter(client, token, tenant_id, suffix)
    definition = create_enum_field(client, token, matter["id"], key=f"target_{suffix.casefold()}")
    model = create_model(client, token, matter["id"], definition["id"])
    configuration_response = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/configurations",
        headers=auth(token),
        json=binary_configuration(),
    )
    assert configuration_response.status_code == 201, configuration_response.text
    configuration_data = configuration_response.json()

    with TestingSessionLocal() as db:
        configuration = db.get(ClassificationModelConfiguration, uuid.UUID(configuration_data["id"]))
        assert configuration is not None
        snapshot = ClassificationDatasetSnapshot(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID(tenant_id),
            matter_id=uuid.UUID(matter["id"]),
            classification_model_id=uuid.UUID(model["id"]),
            configuration_id=configuration.id,
            purpose="TRAINING",
            scope_type="MATTER",
            scope_id=uuid.UUID(matter["id"]),
            target_field_snapshot=configuration.target_field_snapshot,
            label_policy_snapshot=configuration.label_policy,
            population_artifact_id=uuid.uuid4(),
            population_content_hash="1" * 64,
            label_artifact_id=uuid.uuid4(),
            label_content_hash="2" * 64,
            feature_artifact_id=uuid.uuid4(),
            feature_content_hash="3" * 64,
            feature_specification={"schema_version": 1},
            feature_specification_hash="4" * 64,
            split_artifact_id=uuid.uuid4(),
            split_content_hash="5" * 64,
            split_specification={
                "random_seed": 42,
                "validation_policy": {
                    "fold_count": 3,
                    "minimum_labeled_count": 25,
                    "minimum_labeled_count_per_class": 5,
                },
            },
            split_specification_hash="6" * 64,
            training_aggregate_hash="7" * 64,
            aggregate_hash="8" * 64,
            status="READY_FOR_TRAINING",
            population_count=30,
            labeled_count=30,
            unlabeled_count=0,
            excluded_count=0,
            conflicting_count=0,
            family_grouped_count=0,
            exact_duplicate_grouped_count=0,
            missing_grouping_count=0,
            feature_ready_count=30,
            feature_missing_count=0,
            feature_failed_count=0,
            training_eligible_count=30,
            class_support={"negative": 15, "positive": 15},
            source_summary=[],
            warnings=[],
            created_by_user_id=actor_user_id,
        )
        db.add(snapshot)
        db.commit()
        db.expunge(snapshot)
    return matter, model, snapshot


class FakeClassificationGateway:
    def __init__(self, *, wrong_tenant: bool = False, fail: bool = False) -> None:
        self.wrong_tenant = wrong_tenant
        self.fail = fail
        self.calls = 0
        self.validation: PackageValidationResult | None = None

    @staticmethod
    def reference(artifact_type: ArtifactType, digit: str) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=uuid.uuid4(),
            artifact_type=artifact_type,
            schema_version=1,
            content_hash=digit * 64,
            media_type=(
                "application/zip"
                if artifact_type == ArtifactType.CLASSIFICATION_MODEL_PACKAGE
                else "application/json"
            ),
        )

    def train(self, request, access) -> ClassificationTrainingResult:
        del access
        self.calls += 1
        if self.fail:
            raise RuntimeError("runtime unavailable")
        settings = get_settings()
        model = self.reference(ArtifactType.CLASSIFICATION_MODEL_PACKAGE, "a")
        predictions = self.reference(ArtifactType.CLASSIFICATION_PREDICTIONS, "b")
        evaluation = self.reference(ArtifactType.CLASSIFICATION_EVALUATION, "c")
        result_manifest = self.reference(ArtifactType.CLASSIFICATION_RESULT_MANIFEST, "d")
        manifest = ModelPackageManifest(
            package_id=uuid.uuid4(),
            training_run_id=request.training_run_id,
            tenant_id=(uuid.uuid4() if self.wrong_tenant else request.tenant_id),
            matter_id=request.matter_id,
            classification_model_id=request.classification_model_id,
            configuration_id=request.configuration_id,
            configuration_hash=request.configuration_hash,
            target_field_snapshot=request.target_field_snapshot,
            classification_strategy=request.classification_strategy,
            ordered_class_keys=request.ordered_class_keys,
            trainer_specification=request.trainer_specification,
            feature_specification_hash="4" * 64,
            runtime_image_digest=settings.classification_runtime_image_digest,
            source_revision=settings.classification_source_revision,
            package_derivation_key=request.output_derivation_key,
            input_artifact_hashes={
                "features": request.feature_artifact.content_hash,
                "labels": request.label_artifact.content_hash,
                "splits": request.split_artifact.content_hash,
            },
            prediction_resolution_policy=request.prediction_resolution_policy,
            files=[
                ModelPackageFile(
                    path="model.npz",
                    media_type="application/octet-stream",
                    byte_size=1,
                    content_hash="e" * 64,
                )
            ],
        )
        signature = ModelInferenceSignature(
            feature_dimensions=request.feature_contract.dimensions,
            feature_data_type=request.feature_contract.data_type,
            feature_specification_hash="4" * 64,
            classification_strategy=request.classification_strategy,
            ordered_class_keys=request.ordered_class_keys,
        )
        self.validation = PackageValidationResult(
            valid=True,
            package_content_hash=model.content_hash,
            manifest=manifest,
            signature=signature,
        )
        return ClassificationTrainingResult(
            training_run_id=request.training_run_id,
            output_derivation_key=request.output_derivation_key,
            model_artifact=model,
            prediction_artifact=predictions,
            evaluation_artifact=evaluation,
            result_manifest_artifact=result_manifest,
            model_content_hash=model.content_hash,
            runtime_image_digest=settings.classification_runtime_image_digest,
            metrics={"accuracy": 0.9},
        )

    def validate_package(self, reference, access) -> PackageValidationResult:
        del reference, access
        assert self.validation is not None
        return self.validation


def launch_build(client: TestClient, token: str, matter: dict, model: dict, snapshot_id: uuid.UUID) -> dict:
    response = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/builds",
        headers=auth(token),
        json={"dataset_snapshot_id": str(snapshot_id), "idempotency_key": "first-launch"},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_build_preview_launch_is_idempotent_and_scoped(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot = ready_build_fixture(
        client,
        root_token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="Build",
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/builds"
    preview = client.post(
        f"{base}/preview",
        headers=auth(root_token),
        json={"dataset_snapshot_id": str(snapshot.id)},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_launch"] is True
    assert preview.json()["existing_training_run_id"] is None

    launched = launch_build(client, root_token, matter, model, snapshot.id)
    assert launched["created"] is True
    assert launched["training_run"]["status"] == "QUEUED"

    duplicate = client.post(
        base,
        headers=auth(root_token),
        json={"dataset_snapshot_id": str(snapshot.id), "idempotency_key": "first-launch"},
    )
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["created"] is False
    assert duplicate.json()["training_run"]["id"] == launched["training_run"]["id"]

    same_derivation = client.post(
        base,
        headers=auth(root_token),
        json={"dataset_snapshot_id": str(snapshot.id), "idempotency_key": "second-launch"},
    )
    assert same_derivation.status_code == 202, same_derivation.text
    assert same_derivation.json()["created"] is False

    listed = client.get(base, headers=auth(root_token))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [launched["training_run"]["id"]]

    second_matter = create_matter(client, root_token, str(root_admin.tenant_id), "Wrong Build Scope")
    wrong_scope = client.get(
        f"/v1/matters/{second_matter['id']}/classification-models/{model['id']}"
        f"/builds/{launched['training_run']['id']}",
        headers=auth(root_token),
    )
    assert wrong_scope.status_code == 404


def test_runtime_finalization_creates_one_inactive_candidate(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot = ready_build_fixture(
        client,
        root_token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="Finalize",
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    gateway = FakeClassificationGateway()

    version_id = execute_training_run(
        run_id,
        session_factory=TestingSessionLocal,
        gateway=gateway,
    )
    assert version_id is not None
    assert execute_training_run(run_id, session_factory=TestingSessionLocal, gateway=gateway) is None
    assert gateway.calls == 1

    with TestingSessionLocal() as db:
        run = db.get(ClassificationTrainingRun, run_id)
        version = db.get(ClassificationModelVersion, version_id)
        registry_model = db.get(ClassificationModel, uuid.UUID(model["id"]))
        assert run is not None and run.status == "COMPLETED"
        assert version is not None and version.status == "CANDIDATE"
        assert registry_model is not None and registry_model.active_model_version_id is None
        assert db.scalar(
            select(func.count()).select_from(ClassificationModelVersion).where(
                ClassificationModelVersion.training_run_id == run_id
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(ClassificationEvaluation).where(
                ClassificationEvaluation.model_version_id == version_id
            )
        ) == 1

    read = client.get(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/builds/{run_id}",
        headers=auth(root_token),
    )
    assert read.status_code == 200, read.text
    assert read.json()["model_version"]["status"] == "CANDIDATE"


@pytest.mark.parametrize("wrong_tenant", [False, True])
def test_runtime_failure_is_persisted_without_a_candidate(
    client: TestClient,
    root_token: str,
    root_admin,
    wrong_tenant: bool,
) -> None:
    suffix = "Mismatch" if wrong_tenant else "Unavailable"
    matter, model, snapshot = ready_build_fixture(
        client,
        root_token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix=suffix,
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    gateway = FakeClassificationGateway(wrong_tenant=wrong_tenant, fail=not wrong_tenant)

    expected = ClassificationBuildConflictError if wrong_tenant else RuntimeError
    with pytest.raises(expected):
        execute_training_run(run_id, session_factory=TestingSessionLocal, gateway=gateway)

    with TestingSessionLocal() as db:
        run = db.get(ClassificationTrainingRun, run_id)
        assert run is not None and run.status == "FAILED"
        assert run.failure_summary
        assert db.scalar(
            select(func.count()).select_from(ClassificationModelVersion).where(
                ClassificationModelVersion.training_run_id == run_id
            )
        ) == 0


def test_canceled_build_is_not_dispatched_to_runtime(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot = ready_build_fixture(
        client,
        root_token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="Cancel",
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    cancel = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/builds/{run_id}/cancel",
        headers=auth(root_token),
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "CANCELED"

    gateway = FakeClassificationGateway()
    assert execute_training_run(run_id, session_factory=TestingSessionLocal, gateway=gateway) is None
    assert gateway.calls == 0


def test_reconciliation_recovers_outputs_published_before_a_timeout(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter, model, snapshot = ready_build_fixture(
        client,
        root_token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="Reconcile",
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    gateway = FakeClassificationGateway()

    with TestingSessionLocal() as db:
        run = db.get(ClassificationTrainingRun, run_id)
        assert run is not None
        request = training_request(run)
        result = gateway.train(request, None)
        references = {
            reference.artifact_type: reference
            for reference in (
                result.model_artifact,
                result.prediction_artifact,
                result.evaluation_artifact,
                result.result_manifest_artifact,
            )
        }
        run.status = "FAILED"
        run.failure_code = "FINALIZATION_TIMEOUT"
        run.failure_summary = "API timed out after output publication"
        db.commit()

    def find_reference(*, artifact_type, **kwargs):
        del kwargs
        return references[artifact_type]

    def copy_manifest(*, destination, **kwargs):
        del kwargs
        destination.write(
            json.dumps(
                {
                    "status": "COMPLETED",
                    "training_run_id": str(run_id),
                    "output_derivation_key": request.output_derivation_key,
                    "configuration_hash": request.configuration_hash,
                    "feature_specification_hash": "4" * 64,
                    "runtime_image_digest": get_settings().classification_runtime_image_digest,
                    "source_revision": get_settings().classification_source_revision,
                    "input_artifact_hashes": {
                        "features": request.feature_artifact.content_hash,
                        "labels": request.label_artifact.content_hash,
                        "splits": request.split_artifact.content_hash,
                    },
                    "outputs": {
                        "model": result.model_artifact.model_dump(mode="json"),
                        "predictions": result.prediction_artifact.model_dump(mode="json"),
                        "evaluation": result.evaluation_artifact.model_dump(mode="json"),
                    },
                    "metrics": {"accuracy": 0.9},
                    "warnings": [],
                }
            ).encode()
        )

    monkeypatch.setattr("app.classification_builds.find_workflow_artifact_reference", find_reference)
    monkeypatch.setattr("app.classification_builds.copy_workflow_artifact_content", copy_manifest)

    with TestingSessionLocal() as db:
        run = db.get(ClassificationTrainingRun, run_id)
        assert run is not None
        version_id = reconcile_training_run(db, run=run, gateway=gateway)
        assert db.get(ClassificationModelVersion, version_id) is not None
        db.refresh(run)
        assert run.status == "COMPLETED"
