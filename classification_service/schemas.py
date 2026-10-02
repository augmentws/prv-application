import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

StableKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$"),
]
Sha256 = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrainingScopeType(StrEnum):
    MATTER = "MATTER"
    REVIEW_BATCH = "REVIEW_BATCH"


class ClassificationStrategy(StrEnum):
    BINARY = "BINARY"
    MULTICLASS = "MULTICLASS"
    ONE_VS_REST = "ONE_VS_REST"


class TargetMetadataType(StrEnum):
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"


class TargetCardinality(StrEnum):
    SINGLE = "SINGLE"
    MULTIPLE = "MULTIPLE"


class LabelAuthority(StrEnum):
    GOLD = "GOLD"
    ADJUDICATED = "ADJUDICATED"
    SILVER = "SILVER"


class LabelSourceKind(StrEnum):
    ADJUDICATED_REVIEW = "ADJUDICATED_REVIEW"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    ACCEPTED_JEV = "ACCEPTED_JEV"
    MATTER_ASSERTED = "MATTER_ASSERTED"


class MissingLabelAction(StrEnum):
    EXCLUDE = "EXCLUDE"
    FAIL_SNAPSHOT = "FAIL_SNAPSHOT"


class LabelConflictAction(StrEnum):
    REQUIRE_ADJUDICATION = "REQUIRE_ADJUDICATION"
    EXCLUDE = "EXCLUDE"
    FAIL_SNAPSHOT = "FAIL_SNAPSHOT"


class PredictionConflictAction(StrEnum):
    ABSTAIN = "ABSTAIN"
    HIGHEST_PROBABILITY = "HIGHEST_PROBABILITY"
    FAIL = "FAIL"


class FeatureDataType(StrEnum):
    FLOAT32 = "FLOAT32"
    FLOAT64 = "FLOAT64"


class FeatureNormalization(StrEnum):
    NONE = "NONE"
    L2 = "L2"


class FeaturePooling(StrEnum):
    MEAN = "MEAN"


class ArtifactType(StrEnum):
    CLASSIFICATION_POPULATION = "CLASSIFICATION_POPULATION"
    CLASSIFICATION_FEATURES = "CLASSIFICATION_FEATURES"
    CLASSIFICATION_LABELS = "CLASSIFICATION_LABELS"
    CLASSIFICATION_SPLITS = "CLASSIFICATION_SPLITS"
    CLASSIFICATION_MODEL_PACKAGE = "CLASSIFICATION_MODEL_PACKAGE"
    CLASSIFICATION_PREDICTIONS = "CLASSIFICATION_PREDICTIONS"
    CLASSIFICATION_EVALUATION = "CLASSIFICATION_EVALUATION"
    CLASSIFICATION_RESULT_MANIFEST = "CLASSIFICATION_RESULT_MANIFEST"


class TrainingScopeSpecification(StrictModel):
    scope_type: TrainingScopeType
    scope_id: uuid.UUID


class TargetFieldSnapshot(StrictModel):
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    stable_key: StableKey
    display_name: str = Field(min_length=1, max_length=200)
    metadata_type: TargetMetadataType
    cardinality: TargetCardinality
    schema_fingerprint: Sha256
    active_value_keys: list[StableKey] = Field(default_factory=list, max_length=1000)

    @field_validator("active_value_keys")
    @classmethod
    def validate_unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Target value keys must be unique")
        return values

    @model_validator(mode="after")
    def validate_type_and_values(self) -> "TargetFieldSnapshot":
        if self.metadata_type == TargetMetadataType.BOOLEAN:
            if self.cardinality != TargetCardinality.SINGLE:
                raise ValueError("Boolean classification targets must be single cardinality")
            if self.active_value_keys:
                raise ValueError("Boolean targets use canonical true/false values, not enum value keys")
        elif len(self.active_value_keys) < 2:
            raise ValueError("Enum classification targets require at least two active value keys")
        return self


class BinaryClassMapping(StrictModel):
    negative_class_key: StableKey
    positive_class_key: StableKey
    negative_value_keys: list[StableKey] = Field(min_length=1, max_length=1000)
    positive_value_keys: list[StableKey] = Field(min_length=1, max_length=1000)

    @field_validator("negative_value_keys", "positive_value_keys")
    @classmethod
    def validate_unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Binary value mappings must not contain duplicate keys")
        return values

    @model_validator(mode="after")
    def validate_mapping(self) -> "BinaryClassMapping":
        if self.negative_class_key == self.positive_class_key:
            raise ValueError("Binary positive and negative class keys must differ")
        overlap = set(self.negative_value_keys) & set(self.positive_value_keys)
        if overlap:
            raise ValueError("Binary positive and negative value mappings must be disjoint")
        return self


class LabelSourceSpecification(StrictModel):
    kind: LabelSourceKind
    authority: LabelAuthority
    source_weight: float = Field(gt=0, le=1)
    source_reference_id: uuid.UUID | None = None


class LabelPolicy(StrictModel):
    contract_version: Literal[1] = 1
    allowed_sources: list[LabelSourceSpecification] = Field(min_length=1, max_length=20)
    missing_label_action: MissingLabelAction = MissingLabelAction.EXCLUDE
    source_conflict_action: LabelConflictAction = LabelConflictAction.REQUIRE_ADJUDICATION
    target_value_conflict_action: LabelConflictAction = LabelConflictAction.EXCLUDE

    @field_validator("allowed_sources")
    @classmethod
    def validate_unique_sources(
        cls,
        sources: list[LabelSourceSpecification],
    ) -> list[LabelSourceSpecification]:
        identities = [(source.kind, source.source_reference_id) for source in sources]
        if len(identities) != len(set(identities)):
            raise ValueError("Allowed label sources must be unique")
        return sources


class FeatureContract(StrictModel):
    family_key: Literal["DOCUMENT_EMBEDDING_POOLED_V1"] = "DOCUMENT_EMBEDDING_POOLED_V1"
    contract_version: Literal[1] = 1
    dimensions: int = Field(ge=32, le=16384)
    data_type: FeatureDataType = FeatureDataType.FLOAT32
    normalization: FeatureNormalization = FeatureNormalization.L2
    pooling: FeaturePooling = FeaturePooling.MEAN
    embedding_model: str = Field(min_length=1, max_length=500)
    embedding_model_revision: str | None = Field(default=None, max_length=255)
    text_source_policy: StableKey


class PredictionResolutionPolicy(StrictModel):
    contract_version: Literal[1] = 1
    binary_positive_threshold: float | None = Field(default=None, ge=0, le=1)
    multiclass_minimum_confidence: float | None = Field(default=None, ge=0, le=1)
    one_vs_rest_thresholds: dict[StableKey, Annotated[float, Field(ge=0, le=1)]] | None = None
    zero_match_action: PredictionConflictAction | None = None
    multiple_match_action: PredictionConflictAction | None = None


class LogisticRegressionParameters(StrictModel):
    regularization: Literal["l2"] = "l2"
    c_values: list[Annotated[float, Field(gt=0, le=1_000_000)]] = Field(
        default_factory=lambda: [0.1, 1.0, 10.0],
        min_length=1,
        max_length=50,
    )
    maximum_iterations: int = Field(default=1000, ge=1, le=100_000)

    @field_validator("c_values")
    @classmethod
    def validate_c_values(cls, values: list[float]) -> list[float]:
        if len(values) != len(set(values)):
            raise ValueError("Candidate C values must be unique")
        return values


class TrainerSpecification(StrictModel):
    kind: Literal["sklearn_logistic_regression"] = "sklearn_logistic_regression"
    contract_version: Literal[1] = 1
    parameters: LogisticRegressionParameters = Field(default_factory=LogisticRegressionParameters)


def _validate_configuration_shape(
    *,
    target: TargetFieldSnapshot,
    strategy: ClassificationStrategy,
    ordered_class_keys: list[str],
    binary_mapping: BinaryClassMapping | None,
    prediction_policy: PredictionResolutionPolicy,
) -> None:
    if len(ordered_class_keys) != len(set(ordered_class_keys)):
        raise ValueError("Ordered class keys must be unique")
    if target.metadata_type == TargetMetadataType.BOOLEAN and strategy != ClassificationStrategy.BINARY:
        raise ValueError("Boolean targets support only binary classification")
    if target.cardinality == TargetCardinality.MULTIPLE and strategy == ClassificationStrategy.MULTICLASS:
        raise ValueError("Multiple-cardinality targets do not support native multiclass classification")

    available_values = (
        set(target.active_value_keys)
        if target.metadata_type == TargetMetadataType.ENUM
        else {"false", "true"}
    )
    if strategy == ClassificationStrategy.BINARY:
        if binary_mapping is None:
            raise ValueError("Binary classification requires binary_mapping")
        if len(ordered_class_keys) != 2:
            raise ValueError("Binary classification requires exactly two ordered class keys")
        expected_order = [binary_mapping.negative_class_key, binary_mapping.positive_class_key]
        if ordered_class_keys != expected_order:
            raise ValueError("Binary class order must be [negative_class_key, positive_class_key]")
        mapped_values = set(binary_mapping.negative_value_keys + binary_mapping.positive_value_keys)
        if not mapped_values.issubset(available_values):
            raise ValueError("Binary mappings must reference target field values")
        if any(
            value is not None
            for value in (
                prediction_policy.multiclass_minimum_confidence,
                prediction_policy.one_vs_rest_thresholds,
                prediction_policy.zero_match_action,
                prediction_policy.multiple_match_action,
            )
        ):
            raise ValueError("Binary prediction policy contains fields for another strategy")
        if prediction_policy.binary_positive_threshold is None:
            raise ValueError("Binary classification requires binary_positive_threshold")
        return

    if binary_mapping is not None:
        raise ValueError("binary_mapping is valid only for binary classification")
    if strategy == ClassificationStrategy.MULTICLASS:
        if target.cardinality != TargetCardinality.SINGLE:
            raise ValueError("Native multiclass requires a single-cardinality target")
        if not set(ordered_class_keys).issubset(available_values):
            raise ValueError("Multiclass keys must exist in the target field schema")
        if any(
            value is not None
            for value in (
                prediction_policy.binary_positive_threshold,
                prediction_policy.one_vs_rest_thresholds,
                prediction_policy.zero_match_action,
                prediction_policy.multiple_match_action,
            )
        ):
            raise ValueError("Multiclass prediction policy contains fields for another strategy")
        return

    if not set(ordered_class_keys).issubset(available_values):
        raise ValueError("One-vs-rest class keys must exist in the target field schema")
    thresholds = prediction_policy.one_vs_rest_thresholds
    if thresholds is None or set(thresholds) != set(ordered_class_keys):
        raise ValueError("One-vs-rest thresholds must match ordered class keys")
    if target.cardinality == TargetCardinality.SINGLE and (
        prediction_policy.zero_match_action is None or prediction_policy.multiple_match_action is None
    ):
        raise ValueError("Single-cardinality one-vs-rest requires zero/multiple match actions")
    if target.cardinality == TargetCardinality.MULTIPLE and (
        prediction_policy.zero_match_action is not None or prediction_policy.multiple_match_action is not None
    ):
        raise ValueError("Multiple-cardinality one-vs-rest does not use match-resolution actions")
    if (
        prediction_policy.binary_positive_threshold is not None
        or prediction_policy.multiclass_minimum_confidence is not None
    ):
        raise ValueError("One-vs-rest prediction policy contains fields for another strategy")


class ClassificationModelConfigurationSpecification(StrictModel):
    configuration_schema_version: Literal[1] = 1
    target_field_snapshot: TargetFieldSnapshot
    classification_strategy: ClassificationStrategy
    ordered_class_keys: list[StableKey] = Field(min_length=2, max_length=1000)
    binary_mapping: BinaryClassMapping | None = None
    label_policy: LabelPolicy
    feature_contract: FeatureContract
    prediction_resolution_policy: PredictionResolutionPolicy
    trainer_specification: TrainerSpecification

    @model_validator(mode="after")
    def validate_configuration(self) -> "ClassificationModelConfigurationSpecification":
        _validate_configuration_shape(
            target=self.target_field_snapshot,
            strategy=self.classification_strategy,
            ordered_class_keys=self.ordered_class_keys,
            binary_mapping=self.binary_mapping,
            prediction_policy=self.prediction_resolution_policy,
        )
        return self


class ArtifactReference(StrictModel):
    artifact_id: uuid.UUID
    artifact_type: ArtifactType
    schema_version: int = Field(ge=1)
    content_hash: Sha256
    media_type: str = Field(min_length=1, max_length=255)


class ValidationPolicy(StrictModel):
    contract_version: Literal[1] = 1
    fold_count: int = Field(default=5, ge=2, le=20)
    minimum_labeled_count: int = Field(default=20, ge=2)
    minimum_labeled_count_per_class: int = Field(default=5, ge=1)


class ClassificationTrainingRequest(StrictModel):
    contract_version: Literal[1] = 1
    training_run_id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    configuration_hash: Sha256
    training_scope: TrainingScopeSpecification
    target_field_snapshot: TargetFieldSnapshot
    classification_strategy: ClassificationStrategy
    ordered_class_keys: list[StableKey] = Field(min_length=2, max_length=1000)
    binary_mapping: BinaryClassMapping | None = None
    label_policy: LabelPolicy
    feature_contract: FeatureContract
    trainer_specification: TrainerSpecification
    feature_artifact: ArtifactReference
    label_artifact: ArtifactReference
    split_artifact: ArtifactReference
    validation_policy: ValidationPolicy = Field(default_factory=ValidationPolicy)
    prediction_resolution_policy: PredictionResolutionPolicy
    random_seed: int = Field(ge=0, le=2**32 - 1)
    output_derivation_key: Sha256

    @field_validator("ordered_class_keys")
    @classmethod
    def validate_unique_classes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Ordered class keys must be unique")
        return values

    @model_validator(mode="after")
    def validate_training_contract(self) -> "ClassificationTrainingRequest":
        target = self.target_field_snapshot
        if target.tenant_id != self.tenant_id or target.matter_id != self.matter_id:
            raise ValueError("Target field scope must match the training request")
        if self.training_scope.scope_type == TrainingScopeType.MATTER and self.training_scope.scope_id != self.matter_id:
            raise ValueError("Matter training scope ID must match matter_id")

        expected_artifact_types = {
            "feature_artifact": ArtifactType.CLASSIFICATION_FEATURES,
            "label_artifact": ArtifactType.CLASSIFICATION_LABELS,
            "split_artifact": ArtifactType.CLASSIFICATION_SPLITS,
        }
        for field_name, artifact_type in expected_artifact_types.items():
            if getattr(self, field_name).artifact_type != artifact_type:
                raise ValueError(f"{field_name} must reference {artifact_type.value}")

        _validate_configuration_shape(
            target=target,
            strategy=self.classification_strategy,
            ordered_class_keys=self.ordered_class_keys,
            binary_mapping=self.binary_mapping,
            prediction_policy=self.prediction_resolution_policy,
        )
        return self


class ModelPackageFile(StrictModel):
    path: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
    media_type: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(ge=0)
    content_hash: Sha256

    @field_validator("path")
    @classmethod
    def validate_safe_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("Package paths must be relative and must not traverse parent directories")
        return value


class ModelPackageManifest(StrictModel):
    package_schema_version: Literal[1] = 1
    package_id: uuid.UUID
    training_run_id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    configuration_hash: Sha256
    target_field_snapshot: TargetFieldSnapshot
    classification_strategy: ClassificationStrategy
    ordered_class_keys: list[StableKey] = Field(min_length=2, max_length=1000)
    trainer_specification: TrainerSpecification
    feature_specification_hash: Sha256
    runtime_image_digest: str = Field(min_length=1, max_length=500)
    source_revision: str = Field(min_length=1, max_length=255)
    files: list[ModelPackageFile] = Field(min_length=1, max_length=100)

    @field_validator("files")
    @classmethod
    def validate_unique_paths(cls, files: list[ModelPackageFile]) -> list[ModelPackageFile]:
        paths = [file.path for file in files]
        if len(paths) != len(set(paths)):
            raise ValueError("Model package file paths must be unique")
        return files


class ModelInferenceSignature(StrictModel):
    signature_schema_version: Literal[1] = 1
    package_schema_version: Literal[1] = 1
    feature_family_key: Literal["DOCUMENT_EMBEDDING_POOLED_V1"] = "DOCUMENT_EMBEDDING_POOLED_V1"
    feature_contract_version: Literal[1] = 1
    feature_dimensions: int = Field(ge=32, le=16384)
    feature_data_type: FeatureDataType
    classification_strategy: ClassificationStrategy
    ordered_class_keys: list[StableKey] = Field(min_length=2, max_length=1000)

    @field_validator("ordered_class_keys")
    @classmethod
    def validate_unique_classes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Ordered class keys must be unique")
        return values

    @model_validator(mode="after")
    def validate_output_shape(self) -> "ModelInferenceSignature":
        if self.classification_strategy == ClassificationStrategy.BINARY and len(self.ordered_class_keys) != 2:
            raise ValueError("Binary inference signatures require exactly two class keys")
        return self


MetricValue = float | int | None


class ClassificationEvaluationReport(StrictModel):
    report_schema_version: Literal[1] = 1
    training_run_id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    configuration_hash: Sha256
    classification_strategy: ClassificationStrategy
    ordered_class_keys: list[StableKey] = Field(min_length=2, max_length=1000)
    labeled_document_count: int = Field(ge=0)
    training_document_count: int = Field(ge=0)
    validation_document_count: int = Field(ge=0)
    metrics: dict[str, MetricValue]
    per_class_metrics: dict[StableKey, dict[str, MetricValue]] = Field(default_factory=dict)
    cohort_metrics: dict[str, dict[str, MetricValue]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_counts_and_classes(self) -> "ClassificationEvaluationReport":
        if self.training_document_count + self.validation_document_count > self.labeled_document_count:
            raise ValueError("Training and validation counts cannot exceed the labeled document count")
        unknown_classes = set(self.per_class_metrics) - set(self.ordered_class_keys)
        if unknown_classes:
            raise ValueError("Per-class metrics must reference ordered class keys")
        return self


class ClassificationTrainingResult(StrictModel):
    contract_version: Literal[1] = 1
    training_run_id: uuid.UUID
    output_derivation_key: Sha256
    model_artifact: ArtifactReference
    prediction_artifact: ArtifactReference
    evaluation_artifact: ArtifactReference
    result_manifest_artifact: ArtifactReference
    model_content_hash: Sha256
    package_schema_version: Literal[1] = 1
    runtime_image_digest: str = Field(min_length=1, max_length=500)
    metrics: dict[str, float | int | None]
    cohort_metrics: dict[str, dict[str, float | int | None]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_artifact_types(self) -> "ClassificationTrainingResult":
        expected_artifact_types = {
            "model_artifact": ArtifactType.CLASSIFICATION_MODEL_PACKAGE,
            "prediction_artifact": ArtifactType.CLASSIFICATION_PREDICTIONS,
            "evaluation_artifact": ArtifactType.CLASSIFICATION_EVALUATION,
            "result_manifest_artifact": ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
        }
        for field_name, artifact_type in expected_artifact_types.items():
            if getattr(self, field_name).artifact_type != artifact_type:
                raise ValueError(f"{field_name} must reference {artifact_type.value}")
        return self


class ClassificationScoringRequest(StrictModel):
    contract_version: Literal[1] = 1
    scoring_run_id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    model_version_id: uuid.UUID
    configuration_hash: Sha256
    target_field_snapshot: TargetFieldSnapshot
    classification_strategy: ClassificationStrategy
    ordered_class_keys: list[StableKey] = Field(min_length=2, max_length=1000)
    prediction_resolution_policy: PredictionResolutionPolicy
    model_artifact: ArtifactReference
    feature_artifact: ArtifactReference
    output_derivation_key: Sha256

    @field_validator("ordered_class_keys")
    @classmethod
    def validate_unique_classes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Ordered class keys must be unique")
        return values

    @model_validator(mode="after")
    def validate_scoring_contract(self) -> "ClassificationScoringRequest":
        target = self.target_field_snapshot
        if target.tenant_id != self.tenant_id or target.matter_id != self.matter_id:
            raise ValueError("Target field scope must match the scoring request")
        if self.model_artifact.artifact_type != ArtifactType.CLASSIFICATION_MODEL_PACKAGE:
            raise ValueError("model_artifact must reference CLASSIFICATION_MODEL_PACKAGE")
        if self.feature_artifact.artifact_type != ArtifactType.CLASSIFICATION_FEATURES:
            raise ValueError("feature_artifact must reference CLASSIFICATION_FEATURES")
        if self.classification_strategy == ClassificationStrategy.BINARY:
            if len(self.ordered_class_keys) != 2:
                raise ValueError("Binary scoring requires exactly two ordered class keys")
            if self.prediction_resolution_policy.binary_positive_threshold is None:
                raise ValueError("Binary scoring requires binary_positive_threshold")
        elif self.prediction_resolution_policy.binary_positive_threshold is not None:
            raise ValueError("binary_positive_threshold is valid only for binary scoring")
        if self.classification_strategy == ClassificationStrategy.ONE_VS_REST:
            thresholds = self.prediction_resolution_policy.one_vs_rest_thresholds
            if thresholds is None or set(thresholds) != set(self.ordered_class_keys):
                raise ValueError("One-vs-rest thresholds must match ordered class keys")
        elif self.prediction_resolution_policy.one_vs_rest_thresholds is not None:
            raise ValueError("one_vs_rest_thresholds is valid only for one-vs-rest scoring")
        return self


class ClassificationScoringResult(StrictModel):
    contract_version: Literal[1] = 1
    scoring_run_id: uuid.UUID
    output_derivation_key: Sha256
    prediction_artifact: ArtifactReference
    result_manifest_artifact: ArtifactReference
    scored_document_count: int = Field(ge=0)
    abstained_document_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_result(self) -> "ClassificationScoringResult":
        if self.prediction_artifact.artifact_type != ArtifactType.CLASSIFICATION_PREDICTIONS:
            raise ValueError("prediction_artifact must reference CLASSIFICATION_PREDICTIONS")
        if self.result_manifest_artifact.artifact_type != ArtifactType.CLASSIFICATION_RESULT_MANIFEST:
            raise ValueError("result_manifest_artifact must reference CLASSIFICATION_RESULT_MANIFEST")
        if self.abstained_document_count > self.scored_document_count:
            raise ValueError("Abstained document count cannot exceed scored document count")
        return self
