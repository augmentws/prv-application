from dataclasses import dataclass

from classification_service.schemas import (
    ClassificationCapabilities,
    ClassificationStrategy,
    LogisticRegressionParameters,
)

RUNTIME_VERSION = "1"


@dataclass(frozen=True)
class TrainerRegistration:
    kind: str
    contract_version: int
    parameter_schema: type[LogisticRegressionParameters]
    supported_strategies: tuple[ClassificationStrategy, ...]
    required_artifact_schema_versions: dict[str, int]
    package_schema_versions: tuple[int, ...]
    supports_cpu: bool
    supports_gpu: bool


LOGISTIC_REGRESSION = TrainerRegistration(
    kind="sklearn_logistic_regression",
    contract_version=1,
    parameter_schema=LogisticRegressionParameters,
    supported_strategies=(
        ClassificationStrategy.BINARY,
        ClassificationStrategy.MULTICLASS,
        ClassificationStrategy.ONE_VS_REST,
    ),
    required_artifact_schema_versions={
        "CLASSIFICATION_FEATURES": 1,
        "CLASSIFICATION_LABELS": 1,
        "CLASSIFICATION_SPLITS": 1,
    },
    package_schema_versions=(1,),
    supports_cpu=True,
    supports_gpu=False,
)

TRAINERS = {LOGISTIC_REGRESSION.kind: LOGISTIC_REGRESSION}


def capabilities() -> ClassificationCapabilities:
    return ClassificationCapabilities(
        runtime_version=RUNTIME_VERSION,
        package_schema_versions=[1],
        trainers=[
            {
                "kind": registration.kind,
                "contract_version": registration.contract_version,
                "supported_strategies": [value.value for value in registration.supported_strategies],
                "required_artifact_schema_versions": registration.required_artifact_schema_versions,
                "package_schema_versions": list(registration.package_schema_versions),
                "supports_cpu": registration.supports_cpu,
                "supports_gpu": registration.supports_gpu,
            }
            for registration in TRAINERS.values()
        ],
        supported_strategies=list(LOGISTIC_REGRESSION.supported_strategies),
    )
