import hashlib
import io
import json
import math
import sys
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any

import numpy as np
import sklearn

from classification_service.schemas import (
    ClassificationStrategy,
    FeatureContract,
    ModelInferenceSignature,
    ModelPackageFile,
    ModelPackageManifest,
    PredictionResolutionPolicy,
    TargetFieldSnapshot,
    TrainerSpecification,
)

PACKAGE_MEDIA_TYPE = "application/zip"
PACKAGE_SCHEMA_VERSION = 1
PACKAGE_FILENAMES = frozenset(
    {
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
)
JSON_FILES = frozenset(
    {
        "manifest.json",
        "signature.json",
        "preprocessing.json",
        "labels.json",
        "environment.lock",
        "model-card.json",
        "checksums.json",
    }
)
PROHIBITED_METADATA_KEYS = frozenset(
    {"password", "secret", "token", "credential", "evidence_text", "reviewer_notes"}
)


class ModelPackageError(ValueError):
    pass


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    info.create_system = 3
    return info


def _deterministic_zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            archive.writestr(_zip_info(path), files[path], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def _npy_bytes(array: np.ndarray) -> bytes:
    output = io.BytesIO()
    np.lib.format.write_array(output, np.ascontiguousarray(array), allow_pickle=False)
    return output.getvalue()


def deterministic_npz(arrays: dict[str, np.ndarray]) -> bytes:
    return _deterministic_zip({f"{name}.npy": _npy_bytes(array) for name, array in arrays.items()})


def _safe_npz(content: bytes, *, expected_arrays: set[str]) -> dict[str, np.ndarray]:
    try:
        with np.load(io.BytesIO(content), allow_pickle=False) as archive:
            if set(archive.files) != expected_arrays:
                raise ModelPackageError("NPZ array names do not match the package schema")
            arrays = {name: np.array(archive[name], copy=True) for name in archive.files}
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ModelPackageError):
            raise
        raise ModelPackageError("Package contains an invalid NPZ file") from exc
    if any(array.dtype.hasobject for array in arrays.values()):
        raise ModelPackageError("Object arrays are prohibited in model packages")
    if any(not np.all(np.isfinite(array)) for array in arrays.values() if np.issubdtype(array.dtype, np.number)):
        raise ModelPackageError("Model package arrays must contain finite values")
    return arrays


def _scan_metadata(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold()
            if any(prohibited in normalized for prohibited in PROHIBITED_METADATA_KEYS):
                raise ModelPackageError(f"Prohibited metadata key at {path}.{key}")
            _scan_metadata(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _scan_metadata(nested, path=f"{path}[{index}]")


@dataclass(frozen=True)
class PackageBuildInput:
    training_run_id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    configuration_hash: str
    target_field_snapshot: TargetFieldSnapshot
    classification_strategy: ClassificationStrategy
    ordered_class_keys: list[str]
    binary_mapping: dict[str, Any] | None
    prediction_resolution_policy: PredictionResolutionPolicy
    trainer_specification: TrainerSpecification
    feature_contract: FeatureContract
    feature_specification_hash: str
    package_derivation_key: str
    input_artifact_hashes: dict[str, str]
    selected_c: float
    runtime_image_digest: str
    source_revision: str
    coefficients: np.ndarray
    intercepts: np.ndarray
    model_card: dict[str, Any]


@dataclass(frozen=True)
class LoadedModelPackage:
    content_hash: str
    manifest: ModelPackageManifest
    signature: ModelInferenceSignature
    coefficients: np.ndarray
    intercepts: np.ndarray
    labels: dict[str, Any]

    def predict_probabilities(self, features: np.ndarray) -> np.ndarray:
        if features.ndim != 2 or features.shape[1] != self.signature.feature_dimensions:
            raise ModelPackageError("Feature matrix does not match the inference signature")
        if not np.all(np.isfinite(features)):
            raise ModelPackageError("Feature matrix contains non-finite values")
        logits = features @ self.coefficients.T + self.intercepts
        if self.signature.classification_strategy == ClassificationStrategy.BINARY:
            positive = 1.0 / (1.0 + np.exp(-np.clip(logits[:, 0], -709, 709)))
            return np.column_stack((1.0 - positive, positive))
        if self.signature.classification_strategy == ClassificationStrategy.MULTICLASS:
            shifted = logits - np.max(logits, axis=1, keepdims=True)
            exponentiated = np.exp(shifted)
            return exponentiated / exponentiated.sum(axis=1, keepdims=True)
        return 1.0 / (1.0 + np.exp(-np.clip(logits, -709, 709)))


def build_model_package(payload: PackageBuildInput) -> bytes:
    expected_rows = (
        1
        if payload.classification_strategy == ClassificationStrategy.BINARY
        else len(payload.ordered_class_keys)
    )
    coefficients = np.asarray(payload.coefficients, dtype=np.float64)
    intercepts = np.asarray(payload.intercepts, dtype=np.float64)
    if coefficients.shape != (expected_rows, payload.feature_contract.dimensions):
        raise ModelPackageError("Estimator coefficient shape does not match the package signature")
    if intercepts.shape != (expected_rows,):
        raise ModelPackageError("Estimator intercept shape does not match the package signature")

    signature = ModelInferenceSignature(
        feature_dimensions=payload.feature_contract.dimensions,
        feature_data_type=payload.feature_contract.data_type,
        feature_normalization=payload.feature_contract.normalization,
        feature_specification_hash=payload.feature_specification_hash,
        classification_strategy=payload.classification_strategy,
        ordered_class_keys=payload.ordered_class_keys,
    )
    files: dict[str, bytes] = {
        "signature.json": _json_bytes(signature.model_dump(mode="json")),
        "estimator.npz": deterministic_npz(
            {
                "coefficients": coefficients,
                "intercepts": intercepts,
            }
        ),
        "preprocessing.json": _json_bytes(
            {
                "schema_version": 1,
                "feature_contract": payload.feature_contract.model_dump(mode="json"),
                "feature_specification_hash": payload.feature_specification_hash,
                "missing_feature_action": "REJECT",
            }
        ),
        "calibration.npz": deterministic_npz(
            {
                "method_code": np.array([0], dtype=np.int8),
                "schema_version": np.array([1], dtype=np.int16),
            }
        ),
        "labels.json": _json_bytes(
            {
                "schema_version": 1,
                "classification_strategy": payload.classification_strategy.value,
                "ordered_class_keys": payload.ordered_class_keys,
                "binary_mapping": payload.binary_mapping,
                "prediction_resolution_policy": payload.prediction_resolution_policy.model_dump(
                    mode="json"
                ),
            }
        ),
        "environment.lock": _json_bytes(
            {
                "schema_version": 1,
                "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "numpy": np.__version__,
                "scikit_learn": sklearn.__version__,
            }
        ),
        "model-card.json": _json_bytes({"schema_version": 1, **payload.model_card}),
    }
    manifest_files = [
        ModelPackageFile(
            path=path,
            media_type=(
                "application/octet-stream"
                if path.endswith(".npz")
                else "application/json"
            ),
            byte_size=len(content),
            content_hash=_sha256(content),
        )
        for path, content in sorted(files.items())
    ]
    package_id = uuid.uuid5(payload.training_run_id, payload.package_derivation_key)
    manifest = ModelPackageManifest(
        package_id=package_id,
        training_run_id=payload.training_run_id,
        tenant_id=payload.tenant_id,
        matter_id=payload.matter_id,
        classification_model_id=payload.classification_model_id,
        configuration_id=payload.configuration_id,
        configuration_hash=payload.configuration_hash,
        target_field_snapshot=payload.target_field_snapshot,
        classification_strategy=payload.classification_strategy,
        ordered_class_keys=payload.ordered_class_keys,
        trainer_specification=payload.trainer_specification,
        feature_specification_hash=payload.feature_specification_hash,
        runtime_image_digest=payload.runtime_image_digest,
        source_revision=payload.source_revision,
        package_derivation_key=payload.package_derivation_key,
        input_artifact_hashes=payload.input_artifact_hashes,
        prediction_resolution_policy=payload.prediction_resolution_policy,
        selected_hyperparameters={"C": payload.selected_c},
        files=manifest_files,
    )
    files["manifest.json"] = _json_bytes(manifest.model_dump(mode="json"))
    files["checksums.json"] = _json_bytes(
        {
            "schema_version": 1,
            "files": {
                path: {"byte_size": len(content), "content_hash": _sha256(content)}
                for path, content in sorted(files.items())
            },
        }
    )
    return _deterministic_zip(files)


def load_model_package(content: bytes, *, maximum_bytes: int) -> LoadedModelPackage:
    if not content or len(content) > maximum_bytes:
        raise ModelPackageError("Model package size is outside the allowed range")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except zipfile.BadZipFile as exc:
        raise ModelPackageError("Model package is not a valid ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        paths = [info.filename for info in infos]
        if len(paths) != len(set(paths)):
            raise ModelPackageError("Model package contains duplicate paths")
        if set(paths) != PACKAGE_FILENAMES:
            raise ModelPackageError("Model package file set does not match package schema version 1")
        for info in infos:
            if info.is_dir() or info.filename.startswith("/") or ".." in info.filename.split("/"):
                raise ModelPackageError("Model package contains an unsafe path")
            if info.file_size > maximum_bytes or info.compress_size > maximum_bytes:
                raise ModelPackageError("Model package entry exceeds the allowed size")
        if sum(info.file_size for info in infos) > maximum_bytes:
            raise ModelPackageError("Expanded model package exceeds the allowed size")
        files = {path: archive.read(path) for path in paths}

    documents: dict[str, Any] = {}
    for path in JSON_FILES:
        try:
            documents[path] = json.loads(files[path])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelPackageError(f"{path} is not valid JSON") from exc
        _scan_metadata(documents[path])
    try:
        manifest = ModelPackageManifest.model_validate(documents["manifest.json"])
        signature = ModelInferenceSignature.model_validate(documents["signature.json"])
    except ValueError as exc:
        raise ModelPackageError("Model package document validation failed") from exc
    if manifest.classification_strategy != signature.classification_strategy:
        raise ModelPackageError("Manifest and signature strategies do not match")
    if manifest.ordered_class_keys != signature.ordered_class_keys:
        raise ModelPackageError("Manifest and signature class order does not match")
    if manifest.feature_specification_hash != signature.feature_specification_hash:
        raise ModelPackageError("Manifest and signature feature specifications do not match")
    if (
        manifest.target_field_snapshot.tenant_id != manifest.tenant_id
        or manifest.target_field_snapshot.matter_id != manifest.matter_id
    ):
        raise ModelPackageError("Manifest target field scope does not match the package identity")
    if manifest.package_derivation_key is None:
        raise ModelPackageError("Manifest package derivation key is required")
    if set(manifest.input_artifact_hashes) != {"features", "labels", "splits"}:
        raise ModelPackageError("Manifest input artifact hashes are incomplete")
    selected_c = manifest.selected_hyperparameters.get("C")
    if not isinstance(selected_c, (float, int)) or isinstance(selected_c, bool) or selected_c <= 0:
        raise ModelPackageError("Manifest selected C value is invalid")

    preprocessing = documents["preprocessing.json"]
    try:
        feature_contract = FeatureContract.model_validate(preprocessing["feature_contract"])
    except (KeyError, ValueError) as exc:
        raise ModelPackageError("Preprocessing feature contract is invalid") from exc
    if (
        preprocessing.get("schema_version") != 1
        or preprocessing.get("feature_specification_hash") != manifest.feature_specification_hash
        or preprocessing.get("missing_feature_action") != signature.missing_feature_action
        or feature_contract.family_key != signature.feature_family_key
        or feature_contract.contract_version != signature.feature_contract_version
        or feature_contract.dimensions != signature.feature_dimensions
        or feature_contract.data_type != signature.feature_data_type
        or feature_contract.normalization != signature.feature_normalization
    ):
        raise ModelPackageError("Preprocessing contract does not match the inference signature")

    labels = documents["labels.json"]
    try:
        labels_policy = PredictionResolutionPolicy.model_validate(
            labels["prediction_resolution_policy"]
        )
    except (KeyError, ValueError) as exc:
        raise ModelPackageError("Labels prediction policy is invalid") from exc
    if (
        labels.get("schema_version") != 1
        or labels.get("classification_strategy") != signature.classification_strategy.value
        or labels.get("ordered_class_keys") != signature.ordered_class_keys
        or labels_policy != manifest.prediction_resolution_policy
    ):
        raise ModelPackageError("Labels contract does not match the package manifest")

    environment = documents["environment.lock"]
    if (
        environment.get("schema_version") != 1
        or not all(
            isinstance(environment.get(key), str) and environment[key]
            for key in ("python", "numpy", "scikit_learn")
        )
    ):
        raise ModelPackageError("Environment lock is invalid")
    model_card = documents["model-card.json"]
    if (
        model_card.get("schema_version") != 1
        or model_card.get("target_field_id")
        != str(manifest.target_field_snapshot.metadata_definition_id)
        or model_card.get("target_field_name_snapshot")
        != manifest.target_field_snapshot.display_name
    ):
        raise ModelPackageError("Model card target field identity does not match the manifest")

    manifest_files = {value.path: value for value in manifest.files}
    expected_payloads = PACKAGE_FILENAMES - {"manifest.json", "checksums.json"}
    if set(manifest_files) != expected_payloads:
        raise ModelPackageError("Manifest file inventory does not match the package")
    for path, declared in manifest_files.items():
        if len(files[path]) != declared.byte_size or _sha256(files[path]) != declared.content_hash:
            raise ModelPackageError(f"Manifest integrity check failed for {path}")
    checksum_document = documents["checksums.json"]
    if checksum_document.get("schema_version") != 1:
        raise ModelPackageError("Unsupported checksums schema version")
    checksums = checksum_document.get("files")
    if not isinstance(checksums, dict) or set(checksums) != PACKAGE_FILENAMES - {"checksums.json"}:
        raise ModelPackageError("Checksum inventory does not match the package")
    for path, expected in checksums.items():
        if (
            not isinstance(expected, dict)
            or expected.get("byte_size") != len(files[path])
            or expected.get("content_hash") != _sha256(files[path])
        ):
            raise ModelPackageError(f"Checksum validation failed for {path}")

    estimator = _safe_npz(files["estimator.npz"], expected_arrays={"coefficients", "intercepts"})
    calibration = _safe_npz(
        files["calibration.npz"],
        expected_arrays={"method_code", "schema_version"},
    )
    if calibration["method_code"].tolist() != [0] or calibration["schema_version"].tolist() != [1]:
        raise ModelPackageError("Unsupported calibration representation")
    coefficients = np.asarray(estimator["coefficients"], dtype=np.float64)
    intercepts = np.asarray(estimator["intercepts"], dtype=np.float64)
    expected_rows = (
        1
        if signature.classification_strategy == ClassificationStrategy.BINARY
        else len(signature.ordered_class_keys)
    )
    if coefficients.shape != (expected_rows, signature.feature_dimensions):
        raise ModelPackageError("Estimator coefficient shape does not match the signature")
    if intercepts.shape != (expected_rows,):
        raise ModelPackageError("Estimator intercept shape does not match the signature")
    return LoadedModelPackage(
        content_hash=_sha256(content),
        manifest=manifest,
        signature=signature,
        coefficients=coefficients,
        intercepts=intercepts,
        labels=labels,
    )


def validate_model_package(content: bytes, *, maximum_bytes: int) -> LoadedModelPackage:
    return load_model_package(content, maximum_bytes=maximum_bytes)


def stable_probability_difference(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        return math.inf
    return float(np.max(np.abs(left - right), initial=0.0))
