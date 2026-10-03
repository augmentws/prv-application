from dataclasses import dataclass

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    log_loss,
    precision_recall_fscore_support,
    roc_auc_score,
)

from classification_service.schemas import (
    CalibrationBin,
    ClassificationStrategy,
    PredictionResolutionPolicy,
)


@dataclass(frozen=True)
class EvaluationMetrics:
    metrics: dict[str, float | int | None]
    per_class_metrics: dict[str, dict[str, float | int | None]]
    confusion: list[list[int]]
    calibration: dict[str, list[CalibrationBin]]
    warnings: list[str]


def _optional_roc(y_true: np.ndarray, probabilities: np.ndarray) -> float | None:
    if len(np.unique(y_true)) < 2:
        return None
    return float(roc_auc_score(y_true, probabilities))


def _binary_metrics(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    *,
    threshold: float,
) -> dict[str, float | int | None]:
    predictions = (probabilities >= threshold).astype(np.int8)
    tn, fp, fn, tp = confusion_matrix(y_true, predictions, labels=[0, 1]).ravel()
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    false_positive_rate = fp / (fp + tn) if fp + tn else 0.0
    false_negative_rate = fn / (fn + tp) if fn + tp else 0.0
    return {
        "support": len(y_true),
        "positive_support": int(np.sum(y_true == 1)),
        "negative_support": int(np.sum(y_true == 0)),
        "true_positive": int(tp),
        "true_negative": int(tn),
        "false_positive": int(fp),
        "false_negative": int(fn),
        "precision": float(precision),
        "recall": float(recall),
        "false_positive_rate": float(false_positive_rate),
        "false_negative_rate": float(false_negative_rate),
        "pr_auc": (
            float(average_precision_score(y_true, probabilities))
            if np.any(y_true == 1)
            else None
        ),
        "roc_auc": _optional_roc(y_true, probabilities),
        "brier_score": float(brier_score_loss(y_true, probabilities)),
        "log_loss": float(log_loss(y_true, probabilities, labels=[0, 1])),
    }


def _calibration_bins(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    *,
    bin_count: int = 10,
) -> list[CalibrationBin]:
    result: list[CalibrationBin] = []
    for index in range(bin_count):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        selected = (probabilities >= lower) & (
            probabilities <= upper if index == bin_count - 1 else probabilities < upper
        )
        count = int(np.sum(selected))
        result.append(
            CalibrationBin(
                lower_bound=lower,
                upper_bound=upper,
                count=count,
                mean_predicted_probability=(
                    float(np.mean(probabilities[selected])) if count else None
                ),
                observed_rate=float(np.mean(y_true[selected])) if count else None,
            )
        )
    return result


def evaluate_probabilities(
    *,
    strategy: ClassificationStrategy,
    ordered_class_keys: list[str],
    prediction_policy: PredictionResolutionPolicy,
    probabilities: np.ndarray,
    binary_labels: np.ndarray | None = None,
    multiclass_labels: np.ndarray | None = None,
    one_vs_rest_labels: np.ndarray | None = None,
    one_vs_rest_observed: np.ndarray | None = None,
) -> EvaluationMetrics:
    warnings: list[str] = []
    per_class: dict[str, dict[str, float | int | None]] = {}
    calibration: dict[str, list[CalibrationBin]] = {}
    if strategy == ClassificationStrategy.BINARY:
        if binary_labels is None or probabilities.shape != (len(binary_labels), 2):
            raise ValueError("Binary evaluation inputs have incompatible shapes")
        threshold = prediction_policy.binary_positive_threshold
        if threshold is None:
            raise ValueError("Binary evaluation requires a positive threshold")
        positive = probabilities[:, 1]
        positive_metrics = _binary_metrics(binary_labels, positive, threshold=threshold)
        negative_metrics = _binary_metrics(1 - binary_labels, 1 - positive, threshold=1 - threshold)
        per_class = {
            ordered_class_keys[0]: negative_metrics,
            ordered_class_keys[1]: positive_metrics,
        }
        predictions = (positive >= threshold).astype(np.int8)
        matrix = confusion_matrix(binary_labels, predictions, labels=[0, 1]).astype(int).tolist()
        calibration = {
            ordered_class_keys[0]: _calibration_bins(1 - binary_labels, 1 - positive),
            ordered_class_keys[1]: _calibration_bins(binary_labels, positive),
        }
        metrics: dict[str, float | int | None] = {
            "primary_pr_auc": positive_metrics["pr_auc"],
            "macro_pr_auc": float(
                np.mean([negative_metrics["pr_auc"], positive_metrics["pr_auc"]])
            ),
            "micro_pr_auc": positive_metrics["pr_auc"],
            "accuracy": float(np.mean(predictions == binary_labels)),
            "brier_score": positive_metrics["brier_score"],
            "log_loss": positive_metrics["log_loss"],
        }
        return EvaluationMetrics(metrics, per_class, matrix, calibration, warnings)

    if strategy == ClassificationStrategy.MULTICLASS:
        if multiclass_labels is None or probabilities.shape != (
            len(multiclass_labels),
            len(ordered_class_keys),
        ):
            raise ValueError("Multiclass evaluation inputs have incompatible shapes")
        one_hot = np.eye(len(ordered_class_keys), dtype=np.int8)[multiclass_labels]
        predictions = np.argmax(probabilities, axis=1)
        matrix = confusion_matrix(
            multiclass_labels,
            predictions,
            labels=list(range(len(ordered_class_keys))),
        ).astype(int).tolist()
        for class_index, class_key in enumerate(ordered_class_keys):
            per_class[class_key] = _binary_metrics(
                one_hot[:, class_index],
                probabilities[:, class_index],
                threshold=0.5,
            )
            calibration[class_key] = _calibration_bins(
                one_hot[:, class_index],
                probabilities[:, class_index],
            )
        precision, recall, _, _ = precision_recall_fscore_support(
            multiclass_labels,
            predictions,
            labels=list(range(len(ordered_class_keys))),
            zero_division=0,
        )
        pr_values = [
            value["pr_auc"] for value in per_class.values() if value["pr_auc"] is not None
        ]
        metrics = {
            "primary_pr_auc": float(np.mean(pr_values)),
            "macro_pr_auc": float(np.mean(pr_values)),
            "micro_pr_auc": float(average_precision_score(one_hot.ravel(), probabilities.ravel())),
            "macro_precision": float(np.mean(precision)),
            "macro_recall": float(np.mean(recall)),
            "accuracy": float(np.mean(predictions == multiclass_labels)),
            "brier_score": float(np.mean(np.sum((probabilities - one_hot) ** 2, axis=1))),
            "log_loss": float(
                log_loss(
                    multiclass_labels,
                    probabilities,
                    labels=list(range(len(ordered_class_keys))),
                )
            ),
        }
        return EvaluationMetrics(metrics, per_class, matrix, calibration, warnings)

    if one_vs_rest_labels is None or one_vs_rest_observed is None:
        raise ValueError("One-vs-rest evaluation requires labels and observed masks")
    if probabilities.shape != one_vs_rest_labels.shape or probabilities.shape != one_vs_rest_observed.shape:
        raise ValueError("One-vs-rest evaluation inputs have incompatible shapes")
    thresholds = prediction_policy.one_vs_rest_thresholds
    if thresholds is None:
        raise ValueError("One-vs-rest evaluation requires thresholds")
    flattened_labels: list[int] = []
    flattened_probabilities: list[float] = []
    confusion_rows: list[list[int]] = []
    for class_index, class_key in enumerate(ordered_class_keys):
        selected = one_vs_rest_observed[:, class_index]
        labels = one_vs_rest_labels[selected, class_index]
        class_probabilities = probabilities[selected, class_index]
        if not len(labels):
            raise ValueError(f"One-vs-rest class {class_key} has no observed labels")
        class_metrics = _binary_metrics(
            labels,
            class_probabilities,
            threshold=thresholds[class_key],
        )
        per_class[class_key] = class_metrics
        calibration[class_key] = _calibration_bins(labels, class_probabilities)
        confusion_rows.append(
            [
                int(class_metrics["true_negative"] or 0),
                int(class_metrics["false_positive"] or 0),
                int(class_metrics["false_negative"] or 0),
                int(class_metrics["true_positive"] or 0),
            ]
        )
        flattened_labels.extend(labels.astype(int).tolist())
        flattened_probabilities.extend(class_probabilities.astype(float).tolist())
    pr_values = [value["pr_auc"] for value in per_class.values() if value["pr_auc"] is not None]
    flat_labels = np.asarray(flattened_labels, dtype=np.int8)
    flat_probabilities = np.asarray(flattened_probabilities, dtype=np.float64)
    metrics = {
        "primary_pr_auc": float(np.mean(pr_values)),
        "macro_pr_auc": float(np.mean(pr_values)),
        "micro_pr_auc": float(average_precision_score(flat_labels, flat_probabilities)),
        "brier_score": float(brier_score_loss(flat_labels, flat_probabilities)),
        "log_loss": float(log_loss(flat_labels, flat_probabilities, labels=[0, 1])),
    }
    return EvaluationMetrics(metrics, per_class, confusion_rows, calibration, warnings)
