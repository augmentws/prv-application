import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.classification_gateway import (
    ClassificationArtifactAccess,
    ClassificationGateway,
    ClassificationGatewayError,
)
from app.config import Settings
from classification_service.artifacts import ClassificationArtifactStore
from classification_service.backend import ClassificationBackend, ClassificationRuntimeError
from classification_service.config import ClassificationSettings, get_classification_settings
from classification_service.main import app as classification_app
from classification_service.packaging import (
    ModelPackageError,
    _deterministic_zip,
    load_model_package,
)
from classification_service.schemas import (
    ArtifactReference,
    ArtifactType,
    BinaryClassMapping,
    ClassificationScoringRequest,
    ClassificationStrategy,
    ClassificationTrainingRequest,
    FeatureContract,
    LabelAuthority,
    LabelPolicy,
    LabelSourceKind,
    LabelSourceSpecification,
    PredictionConflictAction,
    PredictionResolutionPolicy,
    TargetCardinality,
    TargetFieldSnapshot,
    TargetMetadataType,
    TrainerSpecification,
    TrainingScopeSpecification,
    TrainingScopeType,
    ValidationPolicy,
)

HASH = "a" * 64
FEATURE_HASH = "b" * 64
PARQUET = "application/vnd.apache.parquet"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parquet(rows: list[dict], metadata: dict[str, object]) -> bytes:
    table = pa.Table.from_pylist(rows).replace_schema_metadata(
        {
            f"pvr.{key}".encode(): (
                json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
                if isinstance(value, (dict, list))
                else str(value).encode()
            )
            for key, value in metadata.items()
        }
    )
    output = io.BytesIO()
    pq.write_table(table, output, compression="zstd", use_dictionary=False)
    return output.getvalue()


@dataclass
class MemoryArtifactStore(ClassificationArtifactStore):
    content: dict[uuid.UUID, bytes] = field(default_factory=dict)
    derivations: dict[tuple[ArtifactType, str], ArtifactReference] = field(default_factory=dict)

    def add(self, artifact_type: ArtifactType, value: bytes, media_type: str = PARQUET) -> ArtifactReference:
        reference = ArtifactReference(
            artifact_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{artifact_type}:{_sha256(value)}"),
            artifact_type=artifact_type,
            schema_version=1,
            content_hash=_sha256(value),
            media_type=media_type,
        )
        self.content[reference.artifact_id] = value
        return reference

    def read(self, reference: ArtifactReference) -> bytes:
        return self.content[reference.artifact_id]

    def write(
        self,
        *,
        artifact_type: ArtifactType,
        core_scope_kind: str,
        core_scope_id: uuid.UUID,
        schema_version: int,
        derivation_key: str,
        content: bytes,
        media_type: str,
        producer_run_id: uuid.UUID,
        metadata: dict,
        lineage: list[ArtifactReference],
        filename: str,
    ) -> ArtifactReference:
        del core_scope_kind, core_scope_id, producer_run_id, metadata, lineage, filename
        key = (artifact_type, derivation_key)
        existing = self.derivations.get(key)
        if existing is not None:
            if self.content[existing.artifact_id] != content:
                raise AssertionError("A derivation produced conflicting bytes")
            return existing
        reference = ArtifactReference(
            artifact_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{artifact_type}:{derivation_key}"),
            artifact_type=artifact_type,
            schema_version=schema_version,
            content_hash=_sha256(content),
            media_type=media_type,
        )
        self.content[reference.artifact_id] = content
        self.derivations[key] = reference
        return reference


def _settings() -> ClassificationSettings:
    return ClassificationSettings(
        CLASSIFICATION_SERVICE_TOKEN="test-classification-service-token",
        CLASSIFICATION_RUNTIME_IMAGE_DIGEST="sha256:test-runtime",
        CLASSIFICATION_SOURCE_REVISION="test-revision",
    )


def _fixture(strategy: ClassificationStrategy) -> tuple[MemoryArtifactStore, ClassificationTrainingRequest]:
    store = MemoryArtifactStore()
    tenant_id = uuid.uuid4()
    matter_id = uuid.uuid4()
    field_id = uuid.uuid4()
    dimensions = 32
    if strategy == ClassificationStrategy.BINARY:
        ordered = ["negative", "positive"]
        values = ["no", "yes"]
        labels = [index % 2 for index in range(36)]
        binary_mapping = BinaryClassMapping(
            negative_class_key="negative",
            positive_class_key="positive",
            negative_value_keys=["no"],
            positive_value_keys=["yes"],
        )
        policy = PredictionResolutionPolicy(binary_positive_threshold=0.5)
    else:
        ordered = ["alpha", "beta", "gamma"]
        values = ordered
        labels = [index % 3 for index in range(36)]
        binary_mapping = None
        policy = (
            PredictionResolutionPolicy(multiclass_minimum_confidence=0.4)
            if strategy == ClassificationStrategy.MULTICLASS
            else PredictionResolutionPolicy(
                one_vs_rest_thresholds={key: 0.5 for key in ordered},
                zero_match_action=PredictionConflictAction.ABSTAIN,
                multiple_match_action=PredictionConflictAction.HIGHEST_PROBABILITY,
            )
        )
    contract = FeatureContract(
        dimensions=dimensions,
        embedding_model="fixture/embedding",
        embedding_model_revision="revision-1",
        text_source_policy="active_text",
    )
    document_ids = [str(uuid.uuid5(matter_id, f"document-{index}")) for index in range(36)]
    feature_rows = []
    label_rows = []
    folds = []
    raw_weights = []
    for index, (document_id, label) in enumerate(zip(document_ids, labels, strict=True)):
        vector = np.zeros(dimensions, dtype=np.float32)
        vector[label] = 1.0
        vector[3 + index % (dimensions - 3)] = 0.05
        vector /= np.linalg.norm(vector)
        feature_rows.append(
            {
                "matter_document_id": document_id,
                "feature_status": "READY",
                "embedding": vector.tolist(),
            }
        )
        label_rows.append(
            {
                "matter_document_id": document_id,
                "binary_label": label if strategy == ClassificationStrategy.BINARY else None,
                "multiclass_key": ordered[label] if strategy == ClassificationStrategy.MULTICLASS else None,
                "one_vs_rest_labels": (
                    [int(class_index == label) for class_index in range(len(ordered))]
                    if strategy == ClassificationStrategy.ONE_VS_REST
                    else []
                ),
                "one_vs_rest_observed_mask": (
                    [True] * len(ordered) if strategy == ClassificationStrategy.ONE_VS_REST else []
                ),
            }
        )
        folds.append((index // len(ordered)) % 3)
        raw_weights.append(1.0 if index % 4 else 0.5)
    feature_bytes = _parquet(
        feature_rows,
        {
            "schema": "classification-features",
            "feature_specification_hash": FEATURE_HASH,
            "feature_specification": {"feature_contract": contract.model_dump(mode="json")},
        },
    )
    feature_reference = store.add(ArtifactType.CLASSIFICATION_FEATURES, feature_bytes)
    label_reference = store.add(
        ArtifactType.CLASSIFICATION_LABELS,
        _parquet(
            label_rows,
            {"schema": "classification-labels", "configuration_hash": HASH},
        ),
    )
    split_rows = []
    for index, document_id in enumerate(document_ids):
        normalized = []
        for fold in range(3):
            if folds[index] == fold:
                normalized.append(None)
                continue
            training_weights = [
                weight
                for row_index, weight in enumerate(raw_weights)
                if folds[row_index] != fold
            ]
            normalized.append(raw_weights[index] / (sum(training_weights) / len(training_weights)))
        split_rows.append(
            {
                "matter_document_id": document_id,
                "group_id": f"group-{index}",
                "feature_status": "READY",
                "training_eligible": True,
                "fold_index": folds[index],
                "source_weight": raw_weights[index],
                "normalized_training_weights": normalized,
            }
        )
    validation = ValidationPolicy(
        fold_count=3,
        minimum_labeled_count=12,
        minimum_labeled_count_per_class=2,
    )
    split_reference = store.add(
        ArtifactType.CLASSIFICATION_SPLITS,
        _parquet(
            split_rows,
            {
                "schema": "classification-splits",
                "feature_content_hash": feature_reference.content_hash,
                "split_specification": {
                    "validation_policy": validation.model_dump(mode="json"),
                    "random_seed": 1729,
                },
            },
        ),
    )
    target = TargetFieldSnapshot(
        tenant_id=tenant_id,
        matter_id=matter_id,
        metadata_definition_id=field_id,
        stable_key="target_field",
        display_name="Target Field",
        metadata_type=TargetMetadataType.ENUM,
        cardinality=TargetCardinality.SINGLE,
        schema_fingerprint="c" * 64,
        active_value_keys=values,
    )
    request = ClassificationTrainingRequest(
        training_run_id=uuid.uuid4(),
        tenant_id=tenant_id,
        matter_id=matter_id,
        classification_model_id=uuid.uuid4(),
        configuration_id=uuid.uuid4(),
        configuration_hash=HASH,
        training_scope=TrainingScopeSpecification(
            scope_type=TrainingScopeType.MATTER,
            scope_id=matter_id,
        ),
        target_field_snapshot=target,
        classification_strategy=strategy,
        ordered_class_keys=ordered,
        binary_mapping=binary_mapping,
        label_policy=LabelPolicy(
            allowed_sources=[
                LabelSourceSpecification(
                    kind=LabelSourceKind.HUMAN_REVIEW,
                    authority=LabelAuthority.GOLD,
                    source_weight=1.0,
                )
            ]
        ),
        feature_contract=contract,
        trainer_specification=TrainerSpecification(
            parameters={"c_values": [0.1, 1.0], "maximum_iterations": 500}
        ),
        feature_artifact=feature_reference,
        label_artifact=label_reference,
        split_artifact=split_reference,
        validation_policy=validation,
        prediction_resolution_policy=policy,
        random_seed=1729,
        output_derivation_key="d" * 64,
    )
    return store, request


@pytest.mark.parametrize("strategy", list(ClassificationStrategy))
def test_train_reload_and_score_all_strategies(strategy: ClassificationStrategy) -> None:
    store, request = _fixture(strategy)
    backend = ClassificationBackend(_settings())

    result = backend.train(request, store)
    package_bytes = store.read(result.model_artifact)
    loaded = load_model_package(package_bytes, maximum_bytes=10_000_000)

    assert loaded.manifest.target_field_snapshot.metadata_definition_id == request.target_field_snapshot.metadata_definition_id
    assert loaded.manifest.ordered_class_keys == request.ordered_class_keys
    assert result.metrics["primary_pr_auc"] is not None
    assert set(zipfile.ZipFile(io.BytesIO(package_bytes)).namelist()) == {
        "manifest.json",
        "signature.json",
        "estimator.npz",
        "preprocessing.json",
        "calibration.npz",
        "labels.json",
        "environment.lock",
        "model-card.json",
        "checksums.json",
    }

    repeated = backend.train(request, store)
    assert repeated.model_content_hash == result.model_content_hash
    assert store.read(repeated.model_artifact) == package_bytes

    score = backend.score(
        ClassificationScoringRequest(
            scoring_run_id=uuid.uuid4(),
            tenant_id=request.tenant_id,
            matter_id=request.matter_id,
            classification_model_id=request.classification_model_id,
            model_version_id=uuid.uuid4(),
            configuration_hash=request.configuration_hash,
            target_field_snapshot=request.target_field_snapshot,
            classification_strategy=request.classification_strategy,
            ordered_class_keys=request.ordered_class_keys,
            prediction_resolution_policy=request.prediction_resolution_policy,
            model_artifact=result.model_artifact,
            feature_artifact=request.feature_artifact,
            output_derivation_key="e" * 64,
        ),
        store,
    )
    assert score.scored_document_count == 36
    assert pq.read_table(io.BytesIO(store.read(score.prediction_artifact))).num_rows == 36


def test_training_rejects_group_leakage_and_missing_fold_support() -> None:
    store, request = _fixture(ClassificationStrategy.BINARY)
    split = pq.read_table(io.BytesIO(store.read(request.split_artifact))).to_pylist()
    split[2]["group_id"] = split[0]["group_id"]
    broken = _parquet(
        split,
        {
            "schema": "classification-splits",
            "feature_content_hash": request.feature_artifact.content_hash,
            "split_specification": {
                "validation_policy": request.validation_policy.model_dump(mode="json"),
                "random_seed": request.random_seed,
            },
        },
    )
    request = request.model_copy(update={"split_artifact": store.add(ArtifactType.CLASSIFICATION_SPLITS, broken)})

    with pytest.raises(ClassificationRuntimeError, match="group leakage"):
        ClassificationBackend(_settings()).train(request, store)

    store, request = _fixture(ClassificationStrategy.BINARY)
    labels = pq.read_table(io.BytesIO(store.read(request.label_artifact))).to_pylist()
    for row in labels:
        row["binary_label"] = 0
    request = request.model_copy(
        update={
            "label_artifact": store.add(
                ArtifactType.CLASSIFICATION_LABELS,
                _parquet(
                    labels,
                    {"schema": "classification-labels", "configuration_hash": HASH},
                ),
            )
        }
    )
    with pytest.raises(ClassificationRuntimeError, match="requires both positive and negative"):
        ClassificationBackend(_settings()).train(request, store)


def test_source_weights_change_fit_and_class_weight_is_not_configurable() -> None:
    weighted_store, weighted_request = _fixture(ClassificationStrategy.BINARY)
    weighted_result = ClassificationBackend(_settings()).train(weighted_request, weighted_store)
    weighted_package = load_model_package(
        weighted_store.read(weighted_result.model_artifact),
        maximum_bytes=10_000_000,
    )

    uniform_store, uniform_request = _fixture(ClassificationStrategy.BINARY)
    split_table = pq.read_table(io.BytesIO(uniform_store.read(uniform_request.split_artifact)))
    split = split_table.to_pylist()
    for row in split:
        row["source_weight"] = 1.0
        row["normalized_training_weights"] = [
            None if fold == row["fold_index"] else 1.0
            for fold in range(uniform_request.validation_policy.fold_count)
        ]
    uniform_request = uniform_request.model_copy(
        update={
            "split_artifact": uniform_store.add(
                ArtifactType.CLASSIFICATION_SPLITS,
                _parquet(
                    split,
                    {
                        "schema": "classification-splits",
                        "feature_content_hash": uniform_request.feature_artifact.content_hash,
                        "split_specification": {
                            "validation_policy": uniform_request.validation_policy.model_dump(mode="json"),
                            "random_seed": uniform_request.random_seed,
                        },
                    },
                ),
            )
        }
    )
    uniform_result = ClassificationBackend(_settings()).train(uniform_request, uniform_store)
    uniform_package = load_model_package(
        uniform_store.read(uniform_result.model_artifact),
        maximum_bytes=10_000_000,
    )

    assert not np.array_equal(weighted_package.coefficients, uniform_package.coefficients)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TrainerSpecification(parameters={"class_weight": "balanced"})


def _object_array_package(package_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    estimator = io.BytesIO()
    np.savez(
        estimator,
        coefficients=np.asarray([[object()]], dtype=object),
        intercepts=np.asarray([0.0]),
    )
    files["estimator.npz"] = estimator.getvalue()
    manifest = json.loads(files["manifest.json"])
    for item in manifest["files"]:
        if item["path"] == "estimator.npz":
            item["byte_size"] = len(files["estimator.npz"])
            item["content_hash"] = _sha256(files["estimator.npz"])
    files["manifest.json"] = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    checksums = json.loads(files["checksums.json"])
    for path in checksums["files"]:
        checksums["files"][path] = {
            "byte_size": len(files[path]),
            "content_hash": _sha256(files[path]),
        }
    files["checksums.json"] = json.dumps(checksums, separators=(",", ":"), sort_keys=True).encode()
    return _deterministic_zip(files)


def test_package_rejects_tampering_unexpected_files_and_object_arrays() -> None:
    store, request = _fixture(ClassificationStrategy.BINARY)
    result = ClassificationBackend(_settings()).train(request, store)
    package_bytes = store.read(result.model_artifact)
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as source:
        files = {name: source.read(name) for name in source.namelist()}

    files["labels.json"] += b" "
    with pytest.raises(ModelPackageError, match="integrity check"):
        load_model_package(_deterministic_zip(files), maximum_bytes=10_000_000)

    files["unexpected.pkl"] = b"not executable, but prohibited by the file allowlist"
    with pytest.raises(ModelPackageError, match="file set"):
        load_model_package(_deterministic_zip(files), maximum_bytes=10_000_000)

    with pytest.raises(ModelPackageError, match="invalid NPZ|Object arrays"):
        load_model_package(_object_array_package(package_bytes), maximum_bytes=10_000_000)


def test_scoring_requires_exact_field_snapshot_and_model_hash() -> None:
    store, training = _fixture(ClassificationStrategy.BINARY)
    backend = ClassificationBackend(_settings())
    result = backend.train(training, store)
    scoring = ClassificationScoringRequest(
        scoring_run_id=uuid.uuid4(),
        tenant_id=training.tenant_id,
        matter_id=training.matter_id,
        classification_model_id=training.classification_model_id,
        model_version_id=uuid.uuid4(),
        configuration_hash=training.configuration_hash,
        target_field_snapshot=training.target_field_snapshot.model_copy(
            update={"display_name": "A different field-name snapshot"}
        ),
        classification_strategy=training.classification_strategy,
        ordered_class_keys=training.ordered_class_keys,
        prediction_resolution_policy=training.prediction_resolution_policy,
        model_artifact=result.model_artifact,
        feature_artifact=training.feature_artifact,
        output_derivation_key="f" * 64,
    )
    with pytest.raises(ClassificationRuntimeError, match="identity"):
        backend.score(scoring, store)

    scoring = scoring.model_copy(
        update={
            "target_field_snapshot": training.target_field_snapshot,
            "model_artifact": result.model_artifact.model_copy(update={"content_hash": "0" * 64}),
        }
    )
    with pytest.raises(ClassificationRuntimeError, match="hash mismatch"):
        backend.score(scoring, store)


def test_runtime_api_requires_service_token() -> None:
    settings = _settings()
    classification_app.dependency_overrides[get_classification_settings] = lambda: settings
    try:
        client = TestClient(classification_app)
        assert client.get("/health").status_code == 200
        assert client.get("/v1/capabilities").status_code == 401
        response = client.get(
            "/v1/capabilities",
            headers={"x-classification-service-token": settings.service_token},
        )
        assert response.status_code == 200
        assert response.json()["supported_strategies"] == [
            "BINARY",
            "MULTICLASS",
            "ONE_VS_REST",
        ]
    finally:
        classification_app.dependency_overrides.clear()


def test_runtime_api_train_and_package_validation_match_embedded(monkeypatch) -> None:
    source_store, request = _fixture(ClassificationStrategy.BINARY)
    embedded_store = MemoryArtifactStore(content=dict(source_store.content))
    remote_store = MemoryArtifactStore(content=dict(source_store.content))
    embedded = ClassificationBackend(_settings()).train(request, embedded_store)
    settings = _settings()
    classification_app.dependency_overrides[get_classification_settings] = lambda: settings
    monkeypatch.setattr("classification_service.api._store", lambda **_: remote_store)
    headers = {
        "x-classification-service-token": settings.service_token,
        "x-artifact-tenant-id": str(request.tenant_id),
        "x-artifact-client-id": str(uuid.uuid4()),
        "x-artifact-delegation-token": "delegated-artifact-token",
    }
    try:
        client = TestClient(classification_app)
        response = client.post(
            "/v1/train",
            headers=headers,
            json=request.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        assert response.json()["model_content_hash"] == embedded.model_content_hash
        package_reference = ArtifactReference.model_validate(response.json()["model_artifact"])
        validation = client.post(
            "/v1/packages/validate",
            headers=headers,
            json={"package_artifact": package_reference.model_dump(mode="json")},
        )
        assert validation.status_code == 200, validation.text
        assert validation.json()["valid"] is True
        assert validation.json()["manifest"]["ordered_class_keys"] == request.ordered_class_keys
    finally:
        classification_app.dependency_overrides.clear()


def test_embedded_and_remote_gateway_training_are_equivalent() -> None:
    source_store, request = _fixture(ClassificationStrategy.MULTICLASS)
    embedded_store = MemoryArtifactStore(content=dict(source_store.content))
    remote_store = MemoryArtifactStore(content=dict(source_store.content))
    access = ClassificationArtifactAccess(
        tenant_id=request.tenant_id,
        client_id=uuid.uuid4(),
        actor_user_id=uuid.uuid4(),
    )
    common = {
        "classification_service_token": "test-classification-service-token",
        "classification_runtime_image_digest": "sha256:test-runtime",
        "classification_source_revision": "test-revision",
    }
    embedded = ClassificationGateway(
        Settings(classification_mode="embedded", **common),
        artifact_store=embedded_store,
    ).train(request, access)

    def remote_handler(http_request):
        assert http_request.headers["x-classification-service-token"] == common["classification_service_token"]
        assert http_request.headers["x-artifact-tenant-id"] == str(request.tenant_id)
        parsed = ClassificationTrainingRequest.model_validate_json(http_request.content)
        result = ClassificationBackend(_settings()).train(parsed, remote_store)
        return httpx.Response(200, json=result.model_dump(mode="json"))

    import httpx

    remote = ClassificationGateway(
        Settings(classification_mode="remote", **common),
        transport=httpx.MockTransport(remote_handler),
    ).train(request, access)

    assert remote.model_content_hash == embedded.model_content_hash
    assert remote.metrics == embedded.metrics
    assert remote_store.read(remote.model_artifact) == embedded_store.read(embedded.model_artifact)


def test_remote_gateway_rejects_malformed_responses_and_maps_timeouts() -> None:
    import httpx

    settings = Settings(
        classification_mode="remote",
        classification_service_token="test-classification-service-token",
    )
    malformed = ClassificationGateway(
        settings,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    with pytest.raises(ClassificationGatewayError, match="invalid response"):
        malformed.get_capabilities()

    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)

    unavailable = ClassificationGateway(settings, transport=httpx.MockTransport(timeout))
    with pytest.raises(ClassificationGatewayError, match="timed out"):
        unavailable.get_capabilities()
