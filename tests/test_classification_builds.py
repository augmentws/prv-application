import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
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
from classification_service.hashing import classification_output_artifact_derivation_key
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
    definition = create_enum_field(
        client,
        token,
        matter["id"],
        key=f"target_{suffix.casefold().replace(' ', '_')}",
    )
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
        self.result: ClassificationTrainingResult | None = None
        self.published_manifest: dict | None = None
        self.published_manifest_bytes: bytes | None = None

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
        self.published_manifest = {
            "status": "COMPLETED",
            "training_run_id": str(request.training_run_id),
            "output_derivation_key": request.output_derivation_key,
            "configuration_hash": request.configuration_hash,
            "feature_specification_hash": "4" * 64,
            "runtime_image_digest": settings.classification_runtime_image_digest,
            "source_revision": settings.classification_source_revision,
            "input_artifact_hashes": {
                "features": request.feature_artifact.content_hash,
                "labels": request.label_artifact.content_hash,
                "splits": request.split_artifact.content_hash,
            },
            "outputs": {
                "model": model.model_dump(mode="json"),
                "predictions": predictions.model_dump(mode="json"),
                "evaluation": evaluation.model_dump(mode="json"),
            },
            "metrics": {"accuracy": 0.9},
            "warnings": [],
        }
        self.published_manifest_bytes = json.dumps(
            self.published_manifest,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        result_manifest = ArtifactReference(
            artifact_id=uuid.uuid4(),
            artifact_type=ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
            schema_version=1,
            content_hash=hashlib.sha256(self.published_manifest_bytes).hexdigest(),
            media_type="application/json",
        )
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
        self.result = ClassificationTrainingResult(
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
        return self.result

    def validate_package(self, reference, access) -> PackageValidationResult:
        del reference, access
        assert self.validation is not None
        return self.validation

    def verify_published_result(self, result, access) -> dict:
        del result, access
        assert self.published_manifest is not None
        return self.published_manifest


def patch_authoritative_published_outputs(
    monkeypatch,
    *,
    gateway: FakeClassificationGateway,
    training_run_id: uuid.UUID,
) -> None:
    with TestingSessionLocal() as db:
        run = db.get(ClassificationTrainingRun, training_run_id)
        assert run is not None
        tenant_id = run.tenant_id
        client_id = run.classification_model.matter.client_id
        derivation_key = run.derivation_key

    def find_reference(*, artifact_type, **kwargs):
        assert gateway.result is not None
        assert kwargs["derivation_key"] == classification_output_artifact_derivation_key(
            derivation_key,
            artifact_type.value,
        )
        references = {
            reference.artifact_type: reference
            for reference in (
                gateway.result.model_artifact,
                gateway.result.prediction_artifact,
                gateway.result.evaluation_artifact,
                gateway.result.result_manifest_artifact,
            )
        }
        reference = references[artifact_type]
        return SimpleNamespace(
            artifact_id=reference.artifact_id,
            tenant_id=tenant_id,
            client_id=client_id,
            core_scope_kind="CLASSIFICATION_TRAINING_RUN",
            core_scope_id=training_run_id,
            artifact_type=reference.artifact_type,
            schema_version=reference.schema_version,
            derivation_key=kwargs["derivation_key"],
            content_hash=reference.content_hash,
            media_type=reference.media_type,
            byte_length=1,
            producer_kind="CLASSIFICATION_RUNTIME",
            producer_run_id=training_run_id,
            metadata={},
        )

    def copy_manifest(*, artifact_id, destination, **kwargs):
        del kwargs
        assert gateway.result is not None
        assert artifact_id == gateway.result.result_manifest_artifact.artifact_id
        assert gateway.published_manifest_bytes is not None
        destination.write(gateway.published_manifest_bytes)

    monkeypatch.setattr("app.classification_builds.find_workflow_artifact_reference", find_reference)
    monkeypatch.setattr("app.classification_builds.copy_workflow_artifact_content", copy_manifest)


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


def test_launch_recovers_an_existing_run_after_a_uniqueness_race(
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
        suffix="Launch Race",
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)

    def simulate_race(*args, **kwargs):
        del args, kwargs
        raise IntegrityError("INSERT", {}, RuntimeError("unique violation"))

    monkeypatch.setattr(
        "app.routers.classification_models.create_training_run",
        simulate_race,
    )
    response = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/builds",
        headers=auth(root_token),
        json={"dataset_snapshot_id": str(snapshot.id), "idempotency_key": "first-launch"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["created"] is False
    assert response.json()["training_run"]["id"] == launched["training_run"]["id"]


def test_runtime_finalization_creates_one_inactive_candidate(
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
        suffix="Finalize",
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    gateway = FakeClassificationGateway()
    patch_authoritative_published_outputs(
        monkeypatch,
        gateway=gateway,
        training_run_id=run_id,
    )

    version_id = execute_training_run(
        run_id,
        session_factory=TestingSessionLocal,
        gateway=gateway,
    )
    assert version_id is not None
    assert (
        execute_training_run(
            run_id,
            session_factory=TestingSessionLocal,
            gateway=gateway,
        )
        is None
    )
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

    with TestingSessionLocal() as db:
        version = db.get(ClassificationModelVersion, version_id)
        assert version is not None
        configuration = db.get(ClassificationModelConfiguration, version.configuration_id)
        assert configuration is not None
        report = {
            "report_schema_version": 1,
            "training_run_id": str(run_id),
            "tenant_id": str(version.tenant_id),
            "matter_id": str(version.matter_id),
            "classification_model_id": str(version.classification_model_id),
            "configuration_id": str(version.configuration_id),
            "configuration_hash": configuration.configuration_hash,
            "classification_strategy": "BINARY",
            "ordered_class_keys": ["negative", "positive"],
            "labeled_document_count": 30,
            "training_document_count": 30,
            "validation_document_count": 30,
            "metrics": {"accuracy": 0.9, "macro_f1": 0.89},
            "per_class_metrics": {
                "negative": {"positive_support": 15, "precision": 0.88, "recall": 0.9, "f1": 0.89},
                "positive": {"positive_support": 15, "precision": 0.9, "recall": 0.88, "f1": 0.89},
            },
            "confusion_matrix": [[14, 1], [2, 13]],
        }
        evaluation_bytes = json.dumps(report, separators=(",", ":"), sort_keys=True).encode()
        evaluation_hash = hashlib.sha256(evaluation_bytes).hexdigest()
        version.evaluation_content_hash = evaluation_hash
        evaluation = db.scalar(
            select(ClassificationEvaluation).where(ClassificationEvaluation.model_version_id == version_id)
        )
        assert evaluation is not None
        evaluation.evaluation_content_hash = evaluation_hash
        db.commit()

    def copy_evaluation(*, artifact_id, destination, **kwargs):
        del kwargs
        assert gateway.result is not None
        assert artifact_id == gateway.result.evaluation_artifact.artifact_id
        destination.write(evaluation_bytes)

    monkeypatch.setattr(
        "app.routers.classification_models.copy_workflow_artifact_content",
        copy_evaluation,
    )
    evaluation_response = client.get(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
        f"/versions/{version_id}/evaluation",
        headers=auth(root_token),
    )
    assert evaluation_response.status_code == 200, evaluation_response.text
    assert evaluation_response.json()["confusion_matrix"] == [[14, 1], [2, 13]]
    assert evaluation_response.json()["metrics"]["macro_f1"] == 0.89


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
        execute_training_run(
            run_id,
            session_factory=TestingSessionLocal,
            gateway=gateway,
            published_result_verifier=gateway.verify_published_result,
        )

    with TestingSessionLocal() as db:
        run = db.get(ClassificationTrainingRun, run_id)
        assert run is not None and run.status == "FAILED"
        assert run.failure_summary
        assert run.failure_retriable is (not wrong_tenant)
        assert db.scalar(
            select(func.count()).select_from(ClassificationModelVersion).where(
                ClassificationModelVersion.training_run_id == run_id
            )
        ) == 0


def test_worker_reauthorizes_the_execution_actor_before_runtime_dispatch(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot = ready_build_fixture(
        client,
        root_token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="Reauthorize",
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    with TestingSessionLocal() as db:
        user = db.get(type(root_admin), root_admin.id)
        assert user is not None
        user.status = "SUSPENDED"
        db.commit()

    gateway = FakeClassificationGateway()
    with pytest.raises(ClassificationBuildConflictError, match="no longer authorized"):
        execute_training_run(
            run_id,
            session_factory=TestingSessionLocal,
            gateway=gateway,
            published_result_verifier=gateway.verify_published_result,
        )
    assert gateway.calls == 0
    with TestingSessionLocal() as db:
        run = db.get(ClassificationTrainingRun, run_id)
        assert run is not None
        assert run.status == "FAILED"
        assert run.failure_code == "BUILD_AUTHORIZATION_REVOKED"
        assert run.failure_retriable is False


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


def test_retriable_failed_build_reuses_the_run_with_a_new_workflow_attempt(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot = ready_build_fixture(
        client,
        root_token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="Retry",
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    failed_gateway = FakeClassificationGateway(fail=True)
    with pytest.raises(RuntimeError):
        execute_training_run(
            run_id,
            session_factory=TestingSessionLocal,
            gateway=failed_gateway,
            published_result_verifier=failed_gateway.verify_published_result,
        )

    retry = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/builds/{run_id}/retry",
        headers=auth(root_token),
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] == str(run_id)
    assert retry.json()["attempt_number"] == 2
    assert retry.json()["status"] == "QUEUED"
    assert retry.json()["workflow_id"].endswith(":attempt:2")
    assert retry.json()["failure_code"] is None

    successful_gateway = FakeClassificationGateway()
    version_id = execute_training_run(
        run_id,
        session_factory=TestingSessionLocal,
        gateway=successful_gateway,
        published_result_verifier=successful_gateway.verify_published_result,
    )
    assert version_id is not None


def test_non_retriable_integrity_failure_cannot_be_requeued(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot = ready_build_fixture(
        client,
        root_token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="No Retry",
    )
    launched = launch_build(client, root_token, matter, model, snapshot.id)
    run_id = uuid.UUID(launched["training_run"]["id"])
    gateway = FakeClassificationGateway(wrong_tenant=True)
    with pytest.raises(ClassificationBuildConflictError):
        execute_training_run(
            run_id,
            session_factory=TestingSessionLocal,
            gateway=gateway,
            published_result_verifier=gateway.verify_published_result,
        )

    retry = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/builds/{run_id}/retry",
        headers=auth(root_token),
    )
    assert retry.status_code == 409


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
            reference.artifact_type: SimpleNamespace(
                artifact_id=reference.artifact_id,
                tenant_id=run.tenant_id,
                client_id=run.classification_model.matter.client_id,
                core_scope_kind="CLASSIFICATION_TRAINING_RUN",
                core_scope_id=run.id,
                artifact_type=reference.artifact_type,
                schema_version=reference.schema_version,
                derivation_key=classification_output_artifact_derivation_key(
                    request.output_derivation_key,
                    reference.artifact_type.value,
                ),
                content_hash=reference.content_hash,
                media_type=reference.media_type,
                byte_length=1,
                producer_kind="CLASSIFICATION_RUNTIME",
                producer_run_id=run.id,
                metadata={},
            )
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
        assert gateway.published_manifest_bytes is not None
        destination.write(gateway.published_manifest_bytes)

    monkeypatch.setattr("app.classification_builds.find_workflow_artifact_reference", find_reference)
    monkeypatch.setattr("app.classification_builds.copy_workflow_artifact_content", copy_manifest)

    with TestingSessionLocal() as db:
        run = db.get(ClassificationTrainingRun, run_id)
        assert run is not None
        version_id = reconcile_training_run(db, run=run, gateway=gateway)
        assert db.get(ClassificationModelVersion, version_id) is not None
        db.refresh(run)
        assert run.status == "COMPLETED"
