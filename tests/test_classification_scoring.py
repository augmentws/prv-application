import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from test_classification_builds import FakeClassificationGateway, launch_build, ready_build_fixture
from test_classification_lifecycle import approve, promote, promotion_preview, second_candidate
from test_classification_models import auth

from app.classification_builds import execute_training_run
from app.classification_scoring import execute_scoring_run
from app.models import ClassificationModel, ClassificationScoringRun
from classification_service.schemas import ArtifactReference, ArtifactType, ClassificationScoringResult


class FakeScoringGateway:
    def __init__(self, training_gateway: FakeClassificationGateway) -> None:
        assert training_gateway.validation is not None
        self.validation = training_gateway.validation
        self.scored_model_version_id: uuid.UUID | None = None
        self.manifest: dict | None = None

    @staticmethod
    def reference(artifact_type: ArtifactType, digit: str) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=uuid.uuid4(),
            artifact_type=artifact_type,
            schema_version=1,
            content_hash=digit * 64,
            media_type=(
                "application/vnd.apache.parquet"
                if artifact_type == ArtifactType.CLASSIFICATION_PREDICTIONS
                else "application/json"
            ),
        )

    def validate_package(self, reference, access):
        del reference, access
        return self.validation

    def score(self, request, access) -> ClassificationScoringResult:
        del access
        self.scored_model_version_id = request.model_version_id
        predictions = self.reference(ArtifactType.CLASSIFICATION_PREDICTIONS, "b")
        manifest = self.reference(ArtifactType.CLASSIFICATION_RESULT_MANIFEST, "c")
        self.manifest = {
            "status": "COMPLETED_WITH_ERRORS",
            "scoring_run_id": str(request.scoring_run_id),
            "output_derivation_key": request.output_derivation_key,
            "model_version_id": str(request.model_version_id),
            "configuration_hash": request.configuration_hash,
            "feature_specification_hash": "4" * 64,
            "input_artifact_hashes": {
                "model": request.model_artifact.content_hash,
                "features": request.feature_artifact.content_hash,
            },
            "prediction_artifact": predictions.model_dump(mode="json"),
            "population_document_count": 30,
            "scored_document_count": 28,
            "abstained_document_count": 2,
            "excluded_document_count": 1,
            "failed_document_count": 1,
        }
        return ClassificationScoringResult(
            scoring_run_id=request.scoring_run_id,
            output_derivation_key=request.output_derivation_key,
            prediction_artifact=predictions,
            result_manifest_artifact=manifest,
            population_document_count=30,
            scored_document_count=28,
            abstained_document_count=2,
            excluded_document_count=1,
            failed_document_count=1,
        )

    def verify(self, result, access) -> dict:
        del result, access
        assert self.manifest is not None
        return self.manifest


def approved_version_fixture(
    client: TestClient,
    token: str,
    root_admin,
) -> tuple[dict, dict, object, uuid.UUID, FakeClassificationGateway]:
    matter, model, snapshot = ready_build_fixture(
        client,
        token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="Scoring",
    )
    launched = launch_build(client, token, matter, model, snapshot.id)
    training_gateway = FakeClassificationGateway()
    version_id = execute_training_run(
        uuid.UUID(launched["training_run"]["id"]),
        session_factory=TestingSessionLocal,
        gateway=training_gateway,
        published_result_verifier=training_gateway.verify_published_result,
    )
    assert version_id is not None
    approve(client, token, matter["id"], model["id"], version_id)
    return matter, model, snapshot, version_id, training_gateway


def test_scoring_pins_previewed_version_across_activation_change(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot, first_version_id, training_gateway = approved_version_fixture(
        client,
        root_token,
        root_admin,
    )
    promote(
        client,
        root_token,
        matter["id"],
        model["id"],
        promotion_preview(client, root_token, matter["id"], model["id"], first_version_id),
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs"
    preview_response = client.post(
        f"{base}/preview",
        headers=auth(root_token),
        json={"dataset_snapshot_id": str(snapshot.id)},
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["can_launch"] is True
    assert preview["model_version_id"] == str(first_version_id)

    launch_response = client.post(
        base,
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": preview["model_version_id"],
            "idempotency_key": "score-previewed-version",
        },
    )
    assert launch_response.status_code == 202, launch_response.text
    launched = launch_response.json()
    assert launched["created"] is True
    assert launched["scoring_run"]["model_version_id"] == str(first_version_id)

    second_version_id = second_candidate(client, root_token, matter, model, first_version_id)
    approve(client, root_token, matter["id"], model["id"], second_version_id)
    promote(
        client,
        root_token,
        matter["id"],
        model["id"],
        promotion_preview(client, root_token, matter["id"], model["id"], second_version_id),
    )
    with TestingSessionLocal() as db:
        registry = db.get(ClassificationModel, uuid.UUID(model["id"]))
        assert registry is not None and registry.active_model_version_id == second_version_id

    historical_preview = client.post(
        f"{base}/preview",
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": str(first_version_id),
        },
    )
    assert historical_preview.status_code == 200
    assert historical_preview.json()["can_launch"] is True

    scoring_gateway = FakeScoringGateway(training_gateway)
    run_id = uuid.UUID(launched["scoring_run"]["id"])
    execute_scoring_run(
        run_id,
        session_factory=TestingSessionLocal,
        gateway=scoring_gateway,
        published_result_verifier=scoring_gateway.verify,
    )
    assert scoring_gateway.scored_model_version_id == first_version_id
    with TestingSessionLocal() as db:
        run = db.get(ClassificationScoringRun, run_id)
        assert run is not None
        assert run.status == "COMPLETED_WITH_ERRORS"
        assert run.population_document_count == 30
        assert run.scored_document_count == 28
        assert run.excluded_document_count == 1
        assert run.failed_document_count == 1

    read = client.get(f"{base}/{run_id}", headers=auth(root_token))
    assert read.status_code == 200
    assert read.json()["model_version_id"] == str(first_version_id)
    assert read.json()["prediction_artifact_id"] is not None


def test_scoring_launch_is_idempotent_and_cancelable(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot, version_id, _ = approved_version_fixture(client, root_token, root_admin)
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs"
    payload = {
        "dataset_snapshot_id": str(snapshot.id),
        "model_version_id": str(version_id),
        "idempotency_key": "idempotent-score",
    }
    launched = client.post(base, headers=auth(root_token), json=payload)
    assert launched.status_code == 202, launched.text
    repeated = client.post(base, headers=auth(root_token), json=payload)
    assert repeated.status_code == 202
    assert repeated.json()["created"] is False
    assert repeated.json()["scoring_run"]["id"] == launched.json()["scoring_run"]["id"]

    run_id = launched.json()["scoring_run"]["id"]
    retirement_path = (
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
        f"/versions/{version_id}/retire"
    )
    blocked_retirement = client.post(
        retirement_path,
        headers=auth(root_token),
        json={"reason": "Attempt retirement while scoring is queued."},
    )
    assert blocked_retirement.status_code == 409

    canceled = client.post(f"{base}/{run_id}/cancel", headers=auth(root_token))
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "CANCELED"
    retired = client.post(
        retirement_path,
        headers=auth(root_token),
        json={"reason": "No nonterminal scoring references remain."},
    )
    assert retired.status_code == 201
    assert retired.json()["policy_snapshot"]["blocking_reference_count"] == 0


def test_scoring_preview_blocks_incompatible_feature_contract(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot, version_id, _ = approved_version_fixture(client, root_token, root_admin)
    with TestingSessionLocal() as db:
        persisted = db.get(type(snapshot), snapshot.id)
        assert persisted is not None
        persisted.feature_specification_hash = "f" * 64
        db.commit()

    response = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs/preview",
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": str(version_id),
        },
    )
    assert response.status_code == 200
    assert response.json()["can_launch"] is False
    assert {issue["code"] for issue in response.json()["blocking_errors"]} == {
        "FEATURE_CONTRACT_MISMATCH"
    }
