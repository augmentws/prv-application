import json
import uuid

import pytest
from pydantic import ValidationError

from classification_service.hashing import canonical_model_hash, canonical_model_json
from classification_service.schemas import (
    ArtifactReference,
    ArtifactType,
    BinaryClassMapping,
    ClassificationScoringResult,
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
)

HASH = "a" * 64


def artifact(artifact_type: ArtifactType) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=uuid.uuid4(),
        artifact_type=artifact_type,
        schema_version=1,
        content_hash=HASH,
        media_type="application/vnd.apache.parquet",
    )


def target_field(
    *,
    cardinality: TargetCardinality = TargetCardinality.SINGLE,
) -> TargetFieldSnapshot:
    return TargetFieldSnapshot(
        tenant_id=uuid.uuid4(),
        matter_id=uuid.uuid4(),
        metadata_definition_id=uuid.uuid4(),
        stable_key="responsiveness",
        display_name="Responsiveness",
        metadata_type=TargetMetadataType.ENUM,
        cardinality=cardinality,
        schema_fingerprint=HASH,
        active_value_keys=["responsive", "not_responsive", "needs_review"],
    )


def base_training_request(target: TargetFieldSnapshot) -> dict[str, object]:
    return {
        "training_run_id": uuid.uuid4(),
        "tenant_id": target.tenant_id,
        "matter_id": target.matter_id,
        "classification_model_id": uuid.uuid4(),
        "configuration_id": uuid.uuid4(),
        "configuration_hash": HASH,
        "training_scope": TrainingScopeSpecification(
            scope_type=TrainingScopeType.MATTER,
            scope_id=target.matter_id,
        ),
        "target_field_snapshot": target,
        "label_policy": LabelPolicy(
            allowed_sources=[
                LabelSourceSpecification(
                    kind=LabelSourceKind.ADJUDICATED_REVIEW,
                    authority=LabelAuthority.GOLD,
                    source_weight=1.0,
                )
            ]
        ),
        "feature_contract": FeatureContract(
            dimensions=768,
            embedding_model="example/embedding-model",
            embedding_model_revision="revision-1",
            text_source_policy="active_text",
        ),
        "trainer_specification": TrainerSpecification(),
        "feature_artifact": artifact(ArtifactType.CLASSIFICATION_FEATURES),
        "label_artifact": artifact(ArtifactType.CLASSIFICATION_LABELS),
        "split_artifact": artifact(ArtifactType.CLASSIFICATION_SPLITS),
        "random_seed": 42,
        "output_derivation_key": HASH,
    }


def test_binary_contract_supports_synthetic_output_classes() -> None:
    target = target_field()
    request = ClassificationTrainingRequest(
        **base_training_request(target),
        classification_strategy=ClassificationStrategy.BINARY,
        ordered_class_keys=["other", "responsive_or_review"],
        binary_mapping=BinaryClassMapping(
            negative_class_key="other",
            positive_class_key="responsive_or_review",
            negative_value_keys=["not_responsive"],
            positive_value_keys=["responsive", "needs_review"],
        ),
        prediction_resolution_policy=PredictionResolutionPolicy(binary_positive_threshold=0.6),
    )

    assert request.ordered_class_keys == ["other", "responsive_or_review"]


def test_binary_contract_rejects_mapping_to_unknown_field_value() -> None:
    target = target_field()

    with pytest.raises(ValidationError, match="Binary mappings must reference target field values"):
        ClassificationTrainingRequest(
            **base_training_request(target),
            classification_strategy=ClassificationStrategy.BINARY,
            ordered_class_keys=["negative", "positive"],
            binary_mapping=BinaryClassMapping(
                negative_class_key="negative",
                positive_class_key="positive",
                negative_value_keys=["not_responsive"],
                positive_value_keys=["unknown_value"],
            ),
            prediction_resolution_policy=PredictionResolutionPolicy(binary_positive_threshold=0.5),
        )


def test_multiclass_contract_rejects_class_not_in_field_schema() -> None:
    target = target_field()

    with pytest.raises(ValidationError, match="Multiclass keys must exist in the target field schema"):
        ClassificationTrainingRequest(
            **base_training_request(target),
            classification_strategy=ClassificationStrategy.MULTICLASS,
            ordered_class_keys=["responsive", "not_a_field_value"],
            prediction_resolution_policy=PredictionResolutionPolicy(multiclass_minimum_confidence=0.5),
        )


def test_single_cardinality_one_vs_rest_requires_complete_resolution_policy() -> None:
    target = target_field()

    with pytest.raises(ValidationError, match="requires zero/multiple match actions"):
        ClassificationTrainingRequest(
            **base_training_request(target),
            classification_strategy=ClassificationStrategy.ONE_VS_REST,
            ordered_class_keys=["responsive", "not_responsive"],
            prediction_resolution_policy=PredictionResolutionPolicy(
                one_vs_rest_thresholds={"responsive": 0.7, "not_responsive": 0.6},
            ),
        )

    request = ClassificationTrainingRequest(
        **base_training_request(target),
        classification_strategy=ClassificationStrategy.ONE_VS_REST,
        ordered_class_keys=["responsive", "not_responsive"],
        prediction_resolution_policy=PredictionResolutionPolicy(
            one_vs_rest_thresholds={"responsive": 0.7, "not_responsive": 0.6},
            zero_match_action=PredictionConflictAction.ABSTAIN,
            multiple_match_action=PredictionConflictAction.HIGHEST_PROBABILITY,
        ),
    )

    assert request.prediction_resolution_policy.zero_match_action == PredictionConflictAction.ABSTAIN


def test_canonical_hash_sorts_nested_mapping_keys() -> None:
    first = PredictionResolutionPolicy(
        one_vs_rest_thresholds={"responsive": 0.7, "not_responsive": 0.6},
    )
    second = PredictionResolutionPolicy(
        one_vs_rest_thresholds={"not_responsive": 0.6, "responsive": 0.7},
    )

    assert canonical_model_json(first) == canonical_model_json(second)
    assert canonical_model_hash(first) == canonical_model_hash(second)
    assert json.loads(canonical_model_json(first))["one_vs_rest_thresholds"] == {
        "not_responsive": 0.6,
        "responsive": 0.7,
    }


def test_scoring_result_enforces_artifact_types_and_counts() -> None:
    with pytest.raises(ValidationError, match="prediction_artifact must reference"):
        ClassificationScoringResult(
            scoring_run_id=uuid.uuid4(),
            output_derivation_key=HASH,
            prediction_artifact=artifact(ArtifactType.CLASSIFICATION_FEATURES),
            result_manifest_artifact=artifact(ArtifactType.CLASSIFICATION_RESULT_MANIFEST),
            population_document_count=10,
            scored_document_count=10,
        )

    with pytest.raises(ValidationError, match="cannot exceed scored document count"):
        ClassificationScoringResult(
            scoring_run_id=uuid.uuid4(),
            output_derivation_key=HASH,
            prediction_artifact=artifact(ArtifactType.CLASSIFICATION_PREDICTIONS),
            result_manifest_artifact=artifact(ArtifactType.CLASSIFICATION_RESULT_MANIFEST),
            population_document_count=10,
            scored_document_count=10,
            abstained_document_count=11,
        )
