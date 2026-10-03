import io
import json
import warnings
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression

from classification_service.artifacts import ClassificationArtifactStore, content_hash
from classification_service.config import ClassificationSettings
from classification_service.hashing import classification_output_artifact_derivation_key
from classification_service.metrics import EvaluationMetrics, evaluate_probabilities
from classification_service.packaging import (
    PACKAGE_MEDIA_TYPE,
    LoadedModelPackage,
    ModelPackageError,
    PackageBuildInput,
    build_model_package,
    load_model_package,
    stable_probability_difference,
)
from classification_service.registry import TRAINERS
from classification_service.schemas import (
    ArtifactReference,
    ArtifactType,
    CandidateMetric,
    ClassificationEvaluationReport,
    ClassificationScoringRequest,
    ClassificationScoringResult,
    ClassificationStrategy,
    ClassificationTrainingRequest,
    ClassificationTrainingResult,
    FeatureContract,
    PackageValidationResult,
)

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
JSON_MEDIA_TYPE = "application/json"
SELECTION_POLICY_VERSION = 1
PREDICTION_SCHEMA_VERSION = 1
EVALUATION_SCHEMA_VERSION = 1
RESULT_MANIFEST_SCHEMA_VERSION = 1


class ClassificationRuntimeError(RuntimeError):
    def __init__(self, message: str, *, code: str = "CLASSIFICATION_RUNTIME_ERROR") -> None:
        super().__init__(message)
        self.code = code


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _metadata_text(table: pa.Table, key: str) -> str | None:
    value = (table.schema.metadata or {}).get(f"pvr.{key}".encode())
    return value.decode() if value is not None else None


def _metadata_json(table: pa.Table, key: str) -> Any:
    value = _metadata_text(table, key)
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ClassificationRuntimeError(
            f"Artifact metadata {key} is invalid JSON",
            code="ARTIFACT_SCHEMA_INVALID",
        ) from exc


def _read_parquet(
    store: ClassificationArtifactStore,
    reference: ArtifactReference,
    *,
    expected_schema: str,
) -> tuple[bytes, pa.Table]:
    content = store.read(reference)
    if content_hash(content) != reference.content_hash:
        raise ClassificationRuntimeError("Input artifact hash mismatch", code="ARTIFACT_HASH_MISMATCH")
    try:
        table = pq.read_table(io.BytesIO(content))
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise ClassificationRuntimeError(
            f"{expected_schema} input is not readable Parquet",
            code="ARTIFACT_SCHEMA_INVALID",
        ) from exc
    if _metadata_text(table, "schema") != expected_schema:
        raise ClassificationRuntimeError(
            f"Expected {expected_schema} input artifact",
            code="ARTIFACT_SCHEMA_INVALID",
        )
    return content, table


@dataclass(frozen=True)
class TrainingDataset:
    document_ids: list[str]
    features: np.ndarray
    folds: np.ndarray
    groups: list[str]
    raw_weights: np.ndarray
    fold_weights: np.ndarray
    binary_labels: np.ndarray | None
    multiclass_labels: np.ndarray | None
    one_vs_rest_labels: np.ndarray | None
    one_vs_rest_observed: np.ndarray | None
    feature_specification_hash: str
    feature_ready_count: int
    excluded_count: int


@dataclass(frozen=True)
class FittedEstimator:
    coefficients: np.ndarray
    intercepts: np.ndarray

    def probabilities(self, features: np.ndarray, strategy: ClassificationStrategy) -> np.ndarray:
        logits = features @ self.coefficients.T + self.intercepts
        if strategy == ClassificationStrategy.BINARY:
            positive = 1.0 / (1.0 + np.exp(-np.clip(logits[:, 0], -709, 709)))
            return np.column_stack((1.0 - positive, positive))
        if strategy == ClassificationStrategy.MULTICLASS:
            shifted = logits - np.max(logits, axis=1, keepdims=True)
            exponentiated = np.exp(shifted)
            return exponentiated / exponentiated.sum(axis=1, keepdims=True)
        return 1.0 / (1.0 + np.exp(-np.clip(logits, -709, 709)))


def _assert_two_classes(values: np.ndarray, *, context: str) -> None:
    if len(np.unique(values)) != 2:
        raise ClassificationRuntimeError(
            f"{context} requires both positive and negative examples",
            code="INSUFFICIENT_CLASS_SUPPORT",
        )


def _fit_logistic(
    features: np.ndarray,
    labels: np.ndarray,
    weights: np.ndarray,
    *,
    c_value: float,
    maximum_iterations: int,
    random_seed: int,
    expected_classes: list[int],
) -> LogisticRegression:
    if sorted(np.unique(labels).astype(int).tolist()) != expected_classes:
        raise ClassificationRuntimeError(
            "Training fold does not contain every required class",
            code="INSUFFICIENT_CLASS_SUPPORT",
        )
    estimator = LogisticRegression(
        C=c_value,
        solver="lbfgs",
        max_iter=maximum_iterations,
        class_weight=None,
        random_state=random_seed,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        try:
            estimator.fit(features, labels, sample_weight=weights)
        except ConvergenceWarning as exc:
            raise ClassificationRuntimeError(
                "Logistic regression did not converge within maximum_iterations",
                code="TRAINER_DID_NOT_CONVERGE",
            ) from exc
    return estimator


def _fit_strategy(
    dataset: TrainingDataset,
    request: ClassificationTrainingRequest,
    indices: np.ndarray,
    weights: np.ndarray,
    *,
    c_value: float,
) -> FittedEstimator:
    parameters = request.trainer_specification.parameters
    features = dataset.features[indices]
    if request.classification_strategy == ClassificationStrategy.BINARY:
        assert dataset.binary_labels is not None
        estimator = _fit_logistic(
            features,
            dataset.binary_labels[indices],
            weights,
            c_value=c_value,
            maximum_iterations=parameters.maximum_iterations,
            random_seed=request.random_seed,
            expected_classes=[0, 1],
        )
        return FittedEstimator(estimator.coef_.astype(np.float64), estimator.intercept_.astype(np.float64))
    if request.classification_strategy == ClassificationStrategy.MULTICLASS:
        assert dataset.multiclass_labels is not None
        estimator = _fit_logistic(
            features,
            dataset.multiclass_labels[indices],
            weights,
            c_value=c_value,
            maximum_iterations=parameters.maximum_iterations,
            random_seed=request.random_seed,
            expected_classes=list(range(len(request.ordered_class_keys))),
        )
        return FittedEstimator(estimator.coef_.astype(np.float64), estimator.intercept_.astype(np.float64))

    assert dataset.one_vs_rest_labels is not None
    assert dataset.one_vs_rest_observed is not None
    coefficients: list[np.ndarray] = []
    intercepts: list[float] = []
    for class_index, class_key in enumerate(request.ordered_class_keys):
        observed = dataset.one_vs_rest_observed[indices, class_index]
        component_indices = indices[observed]
        component_labels = dataset.one_vs_rest_labels[component_indices, class_index]
        _assert_two_classes(component_labels, context=f"One-vs-rest class {class_key}")
        estimator = _fit_logistic(
            dataset.features[component_indices],
            component_labels,
            weights[observed],
            c_value=c_value,
            maximum_iterations=parameters.maximum_iterations,
            random_seed=request.random_seed + class_index,
            expected_classes=[0, 1],
        )
        coefficients.append(estimator.coef_[0].astype(np.float64))
        intercepts.append(float(estimator.intercept_[0]))
    return FittedEstimator(np.stack(coefficients), np.asarray(intercepts, dtype=np.float64))


def _evaluate(
    dataset: TrainingDataset,
    request: ClassificationTrainingRequest,
    probabilities: np.ndarray,
) -> EvaluationMetrics:
    return evaluate_probabilities(
        strategy=request.classification_strategy,
        ordered_class_keys=request.ordered_class_keys,
        prediction_policy=request.prediction_resolution_policy,
        probabilities=probabilities,
        binary_labels=dataset.binary_labels,
        multiclass_labels=dataset.multiclass_labels,
        one_vs_rest_labels=dataset.one_vs_rest_labels,
        one_vs_rest_observed=dataset.one_vs_rest_observed,
    )


def _evaluate_indices(
    dataset: TrainingDataset,
    request: ClassificationTrainingRequest,
    probabilities: np.ndarray,
    indices: np.ndarray,
) -> EvaluationMetrics:
    return evaluate_probabilities(
        strategy=request.classification_strategy,
        ordered_class_keys=request.ordered_class_keys,
        prediction_policy=request.prediction_resolution_policy,
        probabilities=probabilities[indices],
        binary_labels=(dataset.binary_labels[indices] if dataset.binary_labels is not None else None),
        multiclass_labels=(
            dataset.multiclass_labels[indices] if dataset.multiclass_labels is not None else None
        ),
        one_vs_rest_labels=(
            dataset.one_vs_rest_labels[indices]
            if dataset.one_vs_rest_labels is not None
            else None
        ),
        one_vs_rest_observed=(
            dataset.one_vs_rest_observed[indices]
            if dataset.one_vs_rest_observed is not None
            else None
        ),
    )


def _cross_fit(
    dataset: TrainingDataset,
    request: ClassificationTrainingRequest,
    *,
    c_value: float,
) -> tuple[np.ndarray, EvaluationMetrics]:
    probability_columns = (
        2
        if request.classification_strategy == ClassificationStrategy.BINARY
        else len(request.ordered_class_keys)
    )
    probabilities = np.full((len(dataset.document_ids), probability_columns), np.nan, dtype=np.float64)
    for fold in range(request.validation_policy.fold_count):
        train_indices = np.flatnonzero(dataset.folds != fold)
        validation_indices = np.flatnonzero(dataset.folds == fold)
        if not len(train_indices) or not len(validation_indices):
            raise ClassificationRuntimeError("Every fold must contain training and validation rows")
        weights = dataset.fold_weights[train_indices, fold]
        if np.any(~np.isfinite(weights)) or np.any(weights <= 0):
            raise ClassificationRuntimeError(
                "Training-fold weights are incomplete or invalid",
                code="SPLIT_WEIGHT_INVALID",
            )
        estimator = _fit_strategy(
            dataset,
            request,
            train_indices,
            weights,
            c_value=c_value,
        )
        probabilities[validation_indices] = estimator.probabilities(
            dataset.features[validation_indices],
            request.classification_strategy,
        )
    if np.any(~np.isfinite(probabilities)):
        raise ClassificationRuntimeError("Cross-fit prediction coverage is incomplete")
    return probabilities, _evaluate(dataset, request, probabilities)


def _full_fit(
    dataset: TrainingDataset,
    request: ClassificationTrainingRequest,
    *,
    c_value: float,
) -> FittedEstimator:
    weights = dataset.raw_weights * (len(dataset.raw_weights) / np.sum(dataset.raw_weights))
    return _fit_strategy(
        dataset,
        request,
        np.arange(len(dataset.document_ids)),
        weights,
        c_value=c_value,
    )


def _load_training_dataset(
    store: ClassificationArtifactStore,
    request: ClassificationTrainingRequest,
) -> TrainingDataset:
    _, feature_table = _read_parquet(
        store,
        request.feature_artifact,
        expected_schema="classification-features",
    )
    _, label_table = _read_parquet(
        store,
        request.label_artifact,
        expected_schema="classification-labels",
    )
    _, split_table = _read_parquet(
        store,
        request.split_artifact,
        expected_schema="classification-splits",
    )
    if _metadata_text(label_table, "configuration_hash") != request.configuration_hash:
        raise ClassificationRuntimeError("Label artifact configuration hash mismatch")
    feature_specification_hash = _metadata_text(feature_table, "feature_specification_hash")
    if feature_specification_hash is None:
        raise ClassificationRuntimeError("Feature specification hash is missing")
    feature_specification = _metadata_json(feature_table, "feature_specification") or {}
    try:
        pinned_feature_contract = FeatureContract.model_validate(
            feature_specification["feature_contract"]
        )
    except (KeyError, ValueError) as exc:
        raise ClassificationRuntimeError("Feature artifact contract is invalid") from exc
    if pinned_feature_contract != request.feature_contract:
        raise ClassificationRuntimeError(
            "Feature artifact is incompatible with the training request",
            code="FEATURE_CONTRACT_MISMATCH",
        )
    if _metadata_text(split_table, "feature_content_hash") != request.feature_artifact.content_hash:
        raise ClassificationRuntimeError("Split artifact references a different feature artifact")
    split_specification = _metadata_json(split_table, "split_specification") or {}
    if split_specification.get("validation_policy") != request.validation_policy.model_dump(mode="json"):
        raise ClassificationRuntimeError("Split validation policy does not match the training request")
    if split_specification.get("random_seed") != request.random_seed:
        raise ClassificationRuntimeError("Split random seed does not match the training request")

    features = {row["matter_document_id"]: row for row in feature_table.to_pylist()}
    labels = {row["matter_document_id"]: row for row in label_table.to_pylist()}
    split_rows = split_table.to_pylist()
    split_ids = {row["matter_document_id"] for row in split_rows}
    if (
        len(features) != feature_table.num_rows
        or len(labels) != label_table.num_rows
        or len(split_ids) != len(split_rows)
    ):
        raise ClassificationRuntimeError(
            "Feature, label, and split artifacts must contain unique document IDs",
            code="DATASET_ROW_ALIGNMENT_FAILED",
        )
    if set(features) != set(labels) or set(features) != split_ids:
        raise ClassificationRuntimeError(
            "Feature, label, and split artifacts do not align by document ID",
            code="DATASET_ROW_ALIGNMENT_FAILED",
        )
    eligible = [row for row in split_rows if row["training_eligible"]]
    if len(eligible) < request.validation_policy.minimum_labeled_count:
        raise ClassificationRuntimeError(
            "Training dataset does not meet minimum labeled support",
            code="INSUFFICIENT_CLASS_SUPPORT",
        )
    group_folds: dict[str, set[int]] = defaultdict(set)
    for row in eligible:
        if row["fold_index"] is None:
            raise ClassificationRuntimeError("Eligible split row has no fold assignment")
        group_folds[row["group_id"]].add(int(row["fold_index"]))
    if any(len(folds) != 1 for folds in group_folds.values()):
        raise ClassificationRuntimeError("Connected group leakage was detected across folds")

    document_ids = [row["matter_document_id"] for row in eligible]
    feature_values = [features[document_id]["embedding"] for document_id in document_ids]
    if any(value is None or len(value) != request.feature_contract.dimensions for value in feature_values):
        raise ClassificationRuntimeError("Eligible feature rows have invalid dimensions")
    matrix = np.asarray(feature_values, dtype=np.float64)
    if not np.all(np.isfinite(matrix)):
        raise ClassificationRuntimeError("Feature matrix contains non-finite values")
    folds = np.asarray([row["fold_index"] for row in eligible], dtype=np.int16)
    if set(folds.tolist()) != set(range(request.validation_policy.fold_count)):
        raise ClassificationRuntimeError("Split artifact does not contain every configured fold")
    raw_weights = np.asarray([row["source_weight"] for row in eligible], dtype=np.float64)
    if np.any(~np.isfinite(raw_weights)) or np.any(raw_weights <= 0):
        raise ClassificationRuntimeError(
            "Source weights must be finite and positive",
            code="SPLIT_WEIGHT_INVALID",
        )
    fold_weights = np.asarray(
        [row["normalized_training_weights"] for row in eligible],
        dtype=np.float64,
    )
    if fold_weights.shape != (len(eligible), request.validation_policy.fold_count):
        raise ClassificationRuntimeError("Split weight matrix has an invalid shape")
    # Held-out rows intentionally carry null/NaN at their own fold; training rows must not.
    for row_index, fold in enumerate(folds):
        if not np.isnan(fold_weights[row_index, fold]):
            raise ClassificationRuntimeError("Held-out rows must not carry a training weight")

    ordered_classes = {value: index for index, value in enumerate(request.ordered_class_keys)}
    binary_labels: np.ndarray | None = None
    multiclass_labels: np.ndarray | None = None
    ovr_labels: np.ndarray | None = None
    ovr_observed: np.ndarray | None = None
    selected_labels = [labels[document_id] for document_id in document_ids]
    if request.classification_strategy == ClassificationStrategy.BINARY:
        binary_labels = np.asarray([row["binary_label"] for row in selected_labels], dtype=np.int8)
        _assert_two_classes(binary_labels, context="Binary classification")
    elif request.classification_strategy == ClassificationStrategy.MULTICLASS:
        try:
            multiclass_labels = np.asarray(
                [ordered_classes[row["multiclass_key"]] for row in selected_labels],
                dtype=np.int16,
            )
        except KeyError as exc:
            raise ClassificationRuntimeError("Multiclass label is outside the configured class order") from exc
        if set(multiclass_labels.tolist()) != set(range(len(request.ordered_class_keys))):
            raise ClassificationRuntimeError(
                "Multiclass training data does not contain every configured class",
                code="INSUFFICIENT_CLASS_SUPPORT",
            )
    else:
        ovr_labels = np.asarray(
            [row["one_vs_rest_labels"] for row in selected_labels],
            dtype=np.int8,
        )
        ovr_observed = np.asarray(
            [row["one_vs_rest_observed_mask"] for row in selected_labels],
            dtype=bool,
        )
        expected_shape = (len(selected_labels), len(request.ordered_class_keys))
        if ovr_labels.shape != expected_shape or ovr_observed.shape != expected_shape:
            raise ClassificationRuntimeError("One-vs-rest labels have an invalid shape")
        for class_index, class_key in enumerate(request.ordered_class_keys):
            _assert_two_classes(
                ovr_labels[ovr_observed[:, class_index], class_index],
                context=f"One-vs-rest class {class_key}",
            )
    minimum_per_fold = request.validation_policy.minimum_labeled_count_per_class
    for fold in range(request.validation_policy.fold_count):
        held_out = folds == fold
        if request.classification_strategy == ClassificationStrategy.BINARY:
            assert binary_labels is not None
            supports = [int(np.sum(binary_labels[held_out] == value)) for value in (0, 1)]
        elif request.classification_strategy == ClassificationStrategy.MULTICLASS:
            assert multiclass_labels is not None
            supports = [
                int(np.sum(multiclass_labels[held_out] == value))
                for value in range(len(request.ordered_class_keys))
            ]
        else:
            assert ovr_labels is not None and ovr_observed is not None
            supports = [
                int(np.sum((ovr_labels[held_out, index] == 1) & ovr_observed[held_out, index]))
                for index in range(len(request.ordered_class_keys))
            ]
        if any(support < minimum_per_fold for support in supports):
            raise ClassificationRuntimeError(
                f"Validation fold {fold} does not meet minimum per-class support",
                code="INSUFFICIENT_CLASS_SUPPORT",
            )
    return TrainingDataset(
        document_ids=document_ids,
        features=matrix,
        folds=folds,
        groups=[row["group_id"] for row in eligible],
        raw_weights=raw_weights,
        fold_weights=fold_weights,
        binary_labels=binary_labels,
        multiclass_labels=multiclass_labels,
        one_vs_rest_labels=ovr_labels,
        one_vs_rest_observed=ovr_observed,
        feature_specification_hash=feature_specification_hash,
        feature_ready_count=sum(
            row["feature_status"] == "READY" for row in split_rows
        ),
        excluded_count=sum(not row["training_eligible"] for row in split_rows),
    )


def _prediction_rows(
    dataset: TrainingDataset,
    request: ClassificationTrainingRequest,
    probabilities: np.ndarray,
    *,
    selected_c: float,
    evaluation_kind: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, document_id in enumerate(dataset.document_ids):
        values = probabilities[index]
        predicted: list[str] = []
        abstained = False
        if request.classification_strategy == ClassificationStrategy.BINARY:
            configured_threshold = request.prediction_resolution_policy.binary_positive_threshold
            threshold = configured_threshold if configured_threshold is not None else 0.5
            predicted = [request.ordered_class_keys[int(values[1] >= threshold)]]
        elif request.classification_strategy == ClassificationStrategy.MULTICLASS:
            selected = int(np.argmax(values))
            minimum = request.prediction_resolution_policy.multiclass_minimum_confidence
            if minimum is not None and values[selected] < minimum:
                abstained = True
            else:
                predicted = [request.ordered_class_keys[selected]]
        else:
            thresholds = request.prediction_resolution_policy.one_vs_rest_thresholds or {}
            matched = [
                class_key
                for class_key, probability in zip(request.ordered_class_keys, values, strict=True)
                if probability >= thresholds[class_key]
            ]
            if len(matched) == 1 or request.target_field_snapshot.cardinality.value == "MULTIPLE":
                predicted = matched
            else:
                action = (
                    request.prediction_resolution_policy.zero_match_action
                    if not matched
                    else request.prediction_resolution_policy.multiple_match_action
                )
                if action is None or action.value == "ABSTAIN":
                    abstained = True
                elif action.value == "FAIL":
                    raise ClassificationRuntimeError("One-vs-rest prediction resolution failed")
                else:
                    predicted = [request.ordered_class_keys[int(np.argmax(values))]]
        rows.append(
            {
                "matter_document_id": document_id,
                "fold_index": int(dataset.folds[index]) if evaluation_kind == "CROSS_FIT_DEVELOPMENT" else None,
                "probabilities": values.astype(float).tolist(),
                "predicted_class_keys": predicted,
                "abstained": abstained,
                "evaluation_kind": evaluation_kind,
                "selected_c": selected_c,
            }
        )
    return rows


def _prediction_parquet(
    rows: list[dict[str, Any]],
    *,
    request: ClassificationTrainingRequest | ClassificationScoringRequest,
    feature_specification_hash: str,
) -> bytes:
    schema = pa.schema(
        [
            ("matter_document_id", pa.string()),
            ("fold_index", pa.int16()),
            ("probabilities", pa.list_(pa.float64())),
            ("predicted_class_keys", pa.list_(pa.string())),
            ("abstained", pa.bool_()),
            ("evaluation_kind", pa.string()),
            ("selected_c", pa.float64()),
        ],
        metadata={
            b"pvr.schema": b"classification-predictions",
            b"pvr.schema_version": str(PREDICTION_SCHEMA_VERSION).encode(),
            b"pvr.feature_specification_hash": feature_specification_hash.encode(),
            b"pvr.classification_strategy": request.classification_strategy.value.encode(),
            b"pvr.ordered_class_keys": _canonical_json(request.ordered_class_keys),
        },
    )
    table = pa.Table.from_pylist(rows, schema=schema)
    output = io.BytesIO()
    pq.write_table(
        table,
        output,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
    )
    return output.getvalue()


def _scoring_prediction_parquet(
    rows: list[dict[str, Any]],
    *,
    request: ClassificationScoringRequest,
    feature_specification_hash: str,
) -> bytes:
    schema = pa.schema(
        [
            ("matter_document_id", pa.string()),
            ("collection_item_id", pa.string()),
            ("scope_ordinal", pa.int64()),
            ("source_vector_content_hash", pa.string()),
            ("scoring_run_id", pa.string()),
            ("model_version_id", pa.string()),
            ("result_status", pa.string()),
            ("failure_reason", pa.string()),
            ("probabilities", pa.list_(pa.float64())),
            ("predicted_class_keys", pa.list_(pa.string())),
            ("abstained", pa.bool_()),
            ("probability_margin", pa.float64()),
            ("uncertainty", pa.float64()),
            ("evaluation_kind", pa.string()),
            ("selected_c", pa.float64()),
        ],
        metadata={
            b"pvr.schema": b"classification-scoring-predictions",
            b"pvr.schema_version": b"1",
            b"pvr.feature_specification_hash": feature_specification_hash.encode(),
            b"pvr.classification_strategy": request.classification_strategy.value.encode(),
            b"pvr.ordered_class_keys": _canonical_json(request.ordered_class_keys),
            b"pvr.scoring_run_id": str(request.scoring_run_id).encode(),
            b"pvr.model_version_id": str(request.model_version_id).encode(),
        },
    )
    table = pa.Table.from_pylist(rows, schema=schema)
    output = io.BytesIO()
    pq.write_table(
        table,
        output,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
    )
    return output.getvalue()


def _artifact_derivation(output_derivation_key: str, artifact_type: ArtifactType) -> str:
    return classification_output_artifact_derivation_key(
        output_derivation_key,
        artifact_type.value,
    )


class ClassificationBackend:
    def __init__(self, settings: ClassificationSettings) -> None:
        self.settings = settings

    def validate_package(
        self,
        store: ClassificationArtifactStore,
        reference: ArtifactReference,
    ) -> PackageValidationResult:
        if reference.artifact_type != ArtifactType.CLASSIFICATION_MODEL_PACKAGE:
            raise ModelPackageError("Package validation requires a model-package artifact")
        content = store.read(reference)
        package = load_model_package(content, maximum_bytes=self.settings.package_max_bytes)
        if package.content_hash != reference.content_hash:
            raise ModelPackageError("Package artifact hash does not match validated bytes")
        return PackageValidationResult(
            valid=True,
            package_content_hash=package.content_hash,
            manifest=package.manifest,
            signature=package.signature,
        )

    def train(
        self,
        request: ClassificationTrainingRequest,
        store: ClassificationArtifactStore,
    ) -> ClassificationTrainingResult:
        registration = TRAINERS.get(request.trainer_specification.kind)
        if registration is None or request.classification_strategy not in registration.supported_strategies:
            raise ClassificationRuntimeError("Requested trainer is not installed")
        for reference in (
            request.feature_artifact,
            request.label_artifact,
            request.split_artifact,
        ):
            if reference.schema_version != 1:
                raise ClassificationRuntimeError("Unsupported training artifact schema version")
        dataset = _load_training_dataset(store, request)
        candidate_results: list[tuple[float, np.ndarray, EvaluationMetrics]] = []
        for c_value in sorted(request.trainer_specification.parameters.c_values):
            probabilities, evaluation = _cross_fit(dataset, request, c_value=c_value)
            primary = evaluation.metrics.get("primary_pr_auc")
            if primary is None:
                raise ClassificationRuntimeError("Candidate primary metric is unavailable")
            candidate_results.append((c_value, probabilities, evaluation))
        selected_c, oof_probabilities, selected_evaluation = max(
            candidate_results,
            key=lambda value: (float(value[2].metrics["primary_pr_auc"] or -1), -value[0]),
        )
        final_estimator = _full_fit(dataset, request, c_value=selected_c)
        final_probabilities = final_estimator.probabilities(
            dataset.features,
            request.classification_strategy,
        )
        package_bytes = build_model_package(
            PackageBuildInput(
                training_run_id=request.training_run_id,
                tenant_id=request.tenant_id,
                matter_id=request.matter_id,
                classification_model_id=request.classification_model_id,
                configuration_id=request.configuration_id,
                configuration_hash=request.configuration_hash,
                target_field_snapshot=request.target_field_snapshot,
                classification_strategy=request.classification_strategy,
                ordered_class_keys=request.ordered_class_keys,
                binary_mapping=(
                    request.binary_mapping.model_dump(mode="json") if request.binary_mapping else None
                ),
                prediction_resolution_policy=request.prediction_resolution_policy,
                trainer_specification=request.trainer_specification,
                feature_contract=request.feature_contract,
                feature_specification_hash=dataset.feature_specification_hash,
                package_derivation_key=request.output_derivation_key,
                input_artifact_hashes={
                    "features": request.feature_artifact.content_hash,
                    "labels": request.label_artifact.content_hash,
                    "splits": request.split_artifact.content_hash,
                },
                selected_c=selected_c,
                runtime_image_digest=self.settings.runtime_image_digest,
                source_revision=self.settings.source_revision,
                coefficients=final_estimator.coefficients,
                intercepts=final_estimator.intercepts,
                model_card={
                    "intended_use": "Matter-scoped metadata-field classification",
                    "target_field_id": str(request.target_field_snapshot.metadata_definition_id),
                    "target_field_name_snapshot": request.target_field_snapshot.display_name,
                    "evaluation_kind": "CROSS_FIT_DEVELOPMENT",
                    "training_document_count": len(dataset.document_ids),
                    "excluded_document_count": dataset.excluded_count,
                    "limitations": [
                        "Development metrics are group-aware out-of-fold estimates, not formal validation",
                        "Probability calibration uses the estimator's native probabilities (IDENTITY_V1)",
                    ],
                },
            )
        )
        loaded = load_model_package(package_bytes, maximum_bytes=self.settings.package_max_bytes)
        reloaded_probabilities = loaded.predict_probabilities(dataset.features)
        difference = stable_probability_difference(final_probabilities, reloaded_probabilities)
        if difference > 1e-12:
            raise ClassificationRuntimeError(
                "Reloaded package does not reproduce fitted probabilities",
                code="PACKAGE_ROUND_TRIP_FAILED",
            )

        candidate_metrics = [
            CandidateMetric(
                c_value=c_value,
                primary_metric_name="primary_pr_auc",
                primary_metric_value=float(evaluation.metrics["primary_pr_auc"] or 0),
            )
            for c_value, _, evaluation in candidate_results
        ]
        runtime_warnings = [
            "CROSS_FIT_DEVELOPMENT_NOT_FORMAL_VALIDATION",
            "IDENTITY_CALIBRATION_V1",
        ]
        report = ClassificationEvaluationReport(
            training_run_id=request.training_run_id,
            tenant_id=request.tenant_id,
            matter_id=request.matter_id,
            classification_model_id=request.classification_model_id,
            configuration_id=request.configuration_id,
            configuration_hash=request.configuration_hash,
            classification_strategy=request.classification_strategy,
            ordered_class_keys=request.ordered_class_keys,
            labeled_document_count=len(dataset.document_ids),
            training_document_count=len(dataset.document_ids),
            validation_document_count=len(dataset.document_ids),
            metrics=selected_evaluation.metrics,
            per_class_metrics=selected_evaluation.per_class_metrics,
            cohort_metrics={
                f"validation_fold_{fold}": _evaluate_indices(
                    dataset,
                    request,
                    oof_probabilities,
                    np.flatnonzero(dataset.folds == fold),
                ).metrics
                for fold in range(request.validation_policy.fold_count)
            },
            confusion_matrix=selected_evaluation.confusion,
            calibration=selected_evaluation.calibration,
            coverage={
                "population_feature_ready": dataset.feature_ready_count,
                "cross_fit_prediction_count": len(dataset.document_ids),
                "excluded_count": dataset.excluded_count,
                "scoring_failure_count": 0,
            },
            candidate_metrics=candidate_metrics,
            selected_hyperparameters={"C": selected_c},
            warnings=runtime_warnings + selected_evaluation.warnings,
        )
        evaluation_bytes = _canonical_json(report.model_dump(mode="json"))
        prediction_bytes = _prediction_parquet(
            _prediction_rows(
                dataset,
                request,
                oof_probabilities,
                selected_c=selected_c,
                evaluation_kind="CROSS_FIT_DEVELOPMENT",
            ),
            request=request,
            feature_specification_hash=dataset.feature_specification_hash,
        )
        input_lineage = [request.feature_artifact, request.label_artifact, request.split_artifact]
        model_reference = store.write(
            artifact_type=ArtifactType.CLASSIFICATION_MODEL_PACKAGE,
            core_scope_kind="CLASSIFICATION_TRAINING_RUN",
            core_scope_id=request.training_run_id,
            schema_version=1,
            derivation_key=_artifact_derivation(
                request.output_derivation_key,
                ArtifactType.CLASSIFICATION_MODEL_PACKAGE,
            ),
            content=package_bytes,
            media_type=PACKAGE_MEDIA_TYPE,
            producer_run_id=request.training_run_id,
            metadata={
                "configuration_hash": request.configuration_hash,
                "feature_specification_hash": dataset.feature_specification_hash,
                "package_schema_version": 1,
            },
            lineage=input_lineage,
            filename="classification-model-package.zip",
        )
        prediction_reference = store.write(
            artifact_type=ArtifactType.CLASSIFICATION_PREDICTIONS,
            core_scope_kind="CLASSIFICATION_TRAINING_RUN",
            core_scope_id=request.training_run_id,
            schema_version=1,
            derivation_key=_artifact_derivation(
                request.output_derivation_key,
                ArtifactType.CLASSIFICATION_PREDICTIONS,
            ),
            content=prediction_bytes,
            media_type=PARQUET_MEDIA_TYPE,
            producer_run_id=request.training_run_id,
            metadata={
                "evaluation_kind": "CROSS_FIT_DEVELOPMENT",
                "row_count": len(dataset.document_ids),
                "selected_c": selected_c,
            },
            lineage=input_lineage,
            filename="classification-cross-fit-predictions.parquet",
        )
        evaluation_reference = store.write(
            artifact_type=ArtifactType.CLASSIFICATION_EVALUATION,
            core_scope_kind="CLASSIFICATION_TRAINING_RUN",
            core_scope_id=request.training_run_id,
            schema_version=1,
            derivation_key=_artifact_derivation(
                request.output_derivation_key,
                ArtifactType.CLASSIFICATION_EVALUATION,
            ),
            content=evaluation_bytes,
            media_type=JSON_MEDIA_TYPE,
            producer_run_id=request.training_run_id,
            metadata={
                "evaluation_kind": "CROSS_FIT_DEVELOPMENT",
                "metric_schema_version": 1,
                "selected_c": selected_c,
            },
            lineage=[prediction_reference, *input_lineage],
            filename="classification-evaluation.json",
        )
        result_manifest = {
            "result_manifest_schema_version": RESULT_MANIFEST_SCHEMA_VERSION,
            "status": "COMPLETED",
            "training_run_id": str(request.training_run_id),
            "output_derivation_key": request.output_derivation_key,
            "configuration_hash": request.configuration_hash,
            "classification_strategy": request.classification_strategy.value,
            "ordered_class_keys": request.ordered_class_keys,
            "feature_specification_hash": dataset.feature_specification_hash,
            "trainer_specification": request.trainer_specification.model_dump(mode="json"),
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "selected_hyperparameters": {"C": selected_c},
            "runtime_image_digest": self.settings.runtime_image_digest,
            "source_revision": self.settings.source_revision,
            "input_artifact_hashes": {
                "features": request.feature_artifact.content_hash,
                "labels": request.label_artifact.content_hash,
                "splits": request.split_artifact.content_hash,
            },
            "outputs": {
                "model": model_reference.model_dump(mode="json"),
                "predictions": prediction_reference.model_dump(mode="json"),
                "evaluation": evaluation_reference.model_dump(mode="json"),
            },
            "metrics": report.metrics,
            "coverage": report.coverage,
            "warnings": report.warnings,
        }
        result_manifest_bytes = _canonical_json(result_manifest)
        result_reference = store.write(
            artifact_type=ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
            core_scope_kind="CLASSIFICATION_TRAINING_RUN",
            core_scope_id=request.training_run_id,
            schema_version=1,
            derivation_key=_artifact_derivation(
                request.output_derivation_key,
                ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
            ),
            content=result_manifest_bytes,
            media_type=JSON_MEDIA_TYPE,
            producer_run_id=request.training_run_id,
            metadata={
                "status": "COMPLETED",
                "model_artifact_id": str(model_reference.artifact_id),
                "evaluation_artifact_id": str(evaluation_reference.artifact_id),
            },
            lineage=[model_reference, prediction_reference, evaluation_reference],
            filename="classification-training-result.json",
        )
        return ClassificationTrainingResult(
            training_run_id=request.training_run_id,
            output_derivation_key=request.output_derivation_key,
            model_artifact=model_reference,
            prediction_artifact=prediction_reference,
            evaluation_artifact=evaluation_reference,
            result_manifest_artifact=result_reference,
            model_content_hash=model_reference.content_hash,
            runtime_image_digest=self.settings.runtime_image_digest,
            metrics=report.metrics,
            cohort_metrics=report.cohort_metrics,
            warnings=report.warnings,
        )

    def score(
        self,
        request: ClassificationScoringRequest,
        store: ClassificationArtifactStore,
    ) -> ClassificationScoringResult:
        package_content = store.read(request.model_artifact)
        package = load_model_package(
            package_content,
            maximum_bytes=self.settings.package_max_bytes,
        )
        if package.content_hash != request.model_artifact.content_hash:
            raise ClassificationRuntimeError("Model package artifact hash mismatch")
        self._validate_scoring_package(request, package)
        _, feature_table = _read_parquet(
            store,
            request.feature_artifact,
            expected_schema="classification-features",
        )
        feature_hash = _metadata_text(feature_table, "feature_specification_hash")
        if feature_hash != package.signature.feature_specification_hash:
            raise ClassificationRuntimeError(
                "Scoring features are incompatible with the model package",
                code="FEATURE_CONTRACT_MISMATCH",
            )
        feature_rows = feature_table.to_pylist()
        ready = [row for row in feature_rows if row["feature_status"] == "READY"]
        if len(ready) > package.signature.maximum_batch_size:
            raise ClassificationRuntimeError("Scoring batch exceeds the package maximum batch size")
        if ready:
            matrix = np.asarray([row["embedding"] for row in ready], dtype=np.float64)
            probabilities = package.predict_probabilities(matrix)
        else:
            output_columns = (
                2
                if request.classification_strategy == ClassificationStrategy.BINARY
                else len(request.ordered_class_keys)
            )
            probabilities = np.empty((0, output_columns), dtype=np.float64)
        # Reuse the training-row serializer with a narrow runtime view.
        selected_c = float(package.manifest.selected_hyperparameters.get("C", 0.0))
        scored_by_document: dict[str, dict[str, Any]] = {}
        for source, values in zip(ready, probabilities, strict=True):
            sorted_probabilities = np.sort(values)
            margin = float(sorted_probabilities[-1] - sorted_probabilities[-2])
            try:
                predicted, abstained = self._resolve_scoring_prediction(request, values)
                result_status = "SUCCESS"
                failure_reason = None
            except ClassificationRuntimeError as exc:
                predicted, abstained = [], False
                result_status = "FAILED"
                failure_reason = exc.code
            scored_by_document[source["matter_document_id"]] = {
                "probabilities": values.astype(float).tolist(),
                "predicted_class_keys": predicted,
                "abstained": abstained,
                "probability_margin": margin,
                "uncertainty": float(1.0 - np.max(values)),
                "result_status": result_status,
                "failure_reason": failure_reason,
            }
        rows = []
        for source in feature_rows:
            outcome = scored_by_document.get(source["matter_document_id"])
            if outcome is None:
                feature_status = source.get("feature_status")
                outcome = {
                    "probabilities": [],
                    "predicted_class_keys": [],
                    "abstained": False,
                    "probability_margin": None,
                    "uncertainty": None,
                    "result_status": "FAILED" if feature_status == "FAILED" else "EXCLUDED",
                    "failure_reason": source.get("failure_reason") or f"FEATURE_{feature_status}",
                }
            rows.append(
                {
                    "matter_document_id": source["matter_document_id"],
                    "collection_item_id": source.get("collection_item_id"),
                    "scope_ordinal": source.get("scope_ordinal"),
                    "source_vector_content_hash": source.get("source_vector_content_hash"),
                    "scoring_run_id": str(request.scoring_run_id),
                    "model_version_id": str(request.model_version_id),
                    **outcome,
                    "fold_index": None,
                    "evaluation_kind": "SCORING",
                    "selected_c": selected_c,
                }
            )
        scored_count = sum(row["result_status"] == "SUCCESS" for row in rows)
        excluded_count = sum(row["result_status"] == "EXCLUDED" for row in rows)
        failed_count = sum(row["result_status"] == "FAILED" for row in rows)
        abstained_count = sum(
            row["result_status"] == "SUCCESS" and row["abstained"] for row in rows
        )
        prediction_bytes = _scoring_prediction_parquet(
            rows,
            request=request,
            feature_specification_hash=feature_hash,
        )
        prediction_reference = store.write(
            artifact_type=ArtifactType.CLASSIFICATION_PREDICTIONS,
            core_scope_kind="CLASSIFICATION_SCORING_RUN",
            core_scope_id=request.scoring_run_id,
            schema_version=1,
            derivation_key=_artifact_derivation(
                request.output_derivation_key,
                ArtifactType.CLASSIFICATION_PREDICTIONS,
            ),
            content=prediction_bytes,
            media_type=PARQUET_MEDIA_TYPE,
            producer_run_id=request.scoring_run_id,
            metadata={"row_count": len(rows), "scoring_failure_count": failed_count},
            lineage=[request.model_artifact, request.feature_artifact],
            filename="classification-scoring-predictions.parquet",
        )
        manifest_bytes = _canonical_json(
            {
                "result_manifest_schema_version": 1,
                "status": "COMPLETED_WITH_ERRORS" if failed_count else "COMPLETED",
                "scoring_run_id": str(request.scoring_run_id),
                "output_derivation_key": request.output_derivation_key,
                "model_version_id": str(request.model_version_id),
                "configuration_hash": request.configuration_hash,
                "feature_specification_hash": feature_hash,
                "input_artifact_hashes": {
                    "model": request.model_artifact.content_hash,
                    "features": request.feature_artifact.content_hash,
                },
                "prediction_artifact": prediction_reference.model_dump(mode="json"),
                "population_document_count": len(rows),
                "scored_document_count": scored_count,
                "abstained_document_count": abstained_count,
                "excluded_document_count": excluded_count,
                "failed_document_count": failed_count,
            }
        )
        manifest_reference = store.write(
            artifact_type=ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
            core_scope_kind="CLASSIFICATION_SCORING_RUN",
            core_scope_id=request.scoring_run_id,
            schema_version=1,
            derivation_key=_artifact_derivation(
                request.output_derivation_key,
                ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
            ),
            content=manifest_bytes,
            media_type=JSON_MEDIA_TYPE,
            producer_run_id=request.scoring_run_id,
            metadata={
                "status": "COMPLETED_WITH_ERRORS" if failed_count else "COMPLETED",
                "row_count": len(rows),
            },
            lineage=[prediction_reference],
            filename="classification-scoring-result.json",
        )
        return ClassificationScoringResult(
            scoring_run_id=request.scoring_run_id,
            output_derivation_key=request.output_derivation_key,
            prediction_artifact=prediction_reference,
            result_manifest_artifact=manifest_reference,
            population_document_count=len(rows),
            scored_document_count=scored_count,
            abstained_document_count=abstained_count,
            excluded_document_count=excluded_count,
            failed_document_count=failed_count,
        )

    @staticmethod
    def _validate_scoring_package(
        request: ClassificationScoringRequest,
        package: LoadedModelPackage,
    ) -> None:
        manifest = package.manifest
        if (
            manifest.tenant_id != request.tenant_id
            or manifest.matter_id != request.matter_id
            or manifest.classification_model_id != request.classification_model_id
            or manifest.configuration_hash != request.configuration_hash
            or manifest.target_field_snapshot != request.target_field_snapshot
        ):
            raise ClassificationRuntimeError("Model package identity does not match scoring request")
        if (
            manifest.classification_strategy != request.classification_strategy
            or manifest.ordered_class_keys != request.ordered_class_keys
            or manifest.prediction_resolution_policy != request.prediction_resolution_policy
        ):
            raise ClassificationRuntimeError("Model package class semantics do not match scoring request")

    @staticmethod
    def _resolve_scoring_prediction(
        request: ClassificationScoringRequest,
        probabilities: np.ndarray,
    ) -> tuple[list[str], bool]:
        policy = request.prediction_resolution_policy
        if request.classification_strategy == ClassificationStrategy.BINARY:
            threshold = (
                policy.binary_positive_threshold
                if policy.binary_positive_threshold is not None
                else 0.5
            )
            return [request.ordered_class_keys[int(probabilities[1] >= threshold)]], False
        if request.classification_strategy == ClassificationStrategy.MULTICLASS:
            selected = int(np.argmax(probabilities))
            if (
                policy.multiclass_minimum_confidence is not None
                and probabilities[selected] < policy.multiclass_minimum_confidence
            ):
                return [], True
            return [request.ordered_class_keys[selected]], False
        thresholds = policy.one_vs_rest_thresholds or {}
        matched = [
            key
            for key, probability in zip(request.ordered_class_keys, probabilities, strict=True)
            if probability >= thresholds[key]
        ]
        if len(matched) == 1 or request.target_field_snapshot.cardinality.value == "MULTIPLE":
            return matched, False
        action = policy.zero_match_action if not matched else policy.multiple_match_action
        if action is None or action.value == "ABSTAIN":
            return [], True
        if action.value == "FAIL":
            raise ClassificationRuntimeError("One-vs-rest prediction resolution failed")
        selected = int(np.argmax(probabilities))
        return [request.ordered_class_keys[selected]], False
