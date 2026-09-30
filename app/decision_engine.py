from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.decision_specifications import DecisionQuestion


class DecisionEngineError(RuntimeError):
    pass


class DecisionEngineNotRegistered(DecisionEngineError):
    pass


class DecisionEngineProviderError(DecisionEngineError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        code: str,
        retryable: bool,
        provider_request_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.retryable = retryable
        self.provider_request_id = provider_request_id
        self.retry_after_seconds = retry_after_seconds


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChoiceDecisionAnswer(DecisionModel):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float] = Field(min_length=2, max_length=255)
    confidence: float = Field(ge=0, le=1)

    @field_validator("probabilities")
    @classmethod
    def validate_probabilities(cls, value: dict[str, float]) -> dict[str, float]:
        if any(probability < 0 or probability > 1 for probability in value.values()):
            raise ValueError("probabilities must be between 0 and 1")
        return value

    @model_validator(mode="after")
    def validate_selected_choice(self) -> ChoiceDecisionAnswer:
        if self.choice not in self.probabilities:
            raise ValueError("selected choice must be present in probabilities")
        return self


class ScoreDecisionAnswer(DecisionModel):
    type: Literal["score"]
    score: float
    legend: dict[str, Any] = Field(min_length=2, max_length=100)
    probabilities: dict[str, float] = Field(min_length=2, max_length=100)
    confidence: float = Field(ge=0, le=1)

    @field_validator("probabilities")
    @classmethod
    def validate_probabilities(cls, value: dict[str, float]) -> dict[str, float]:
        if any(probability < 0 or probability > 1 for probability in value.values()):
            raise ValueError("probabilities must be between 0 and 1")
        return value

    @model_validator(mode="after")
    def validate_legend_probabilities(self) -> ScoreDecisionAnswer:
        if set(self.legend) != set(self.probabilities):
            raise ValueError("Score legend and probability keys must match")
        return self


class NoulDecisionAnswer(DecisionModel):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


DecisionAnswer = Annotated[
    ChoiceDecisionAnswer | ScoreDecisionAnswer | NoulDecisionAnswer,
    Field(discriminator="type"),
]


class DecisionUsage(DecisionModel):
    request_count: int = Field(default=1, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class DecisionRequest(DecisionModel):
    state: Any
    questions: dict[str, DecisionQuestion] = Field(min_length=1, max_length=500)
    model_key: str = Field(min_length=1, max_length=500)
    model_settings: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = Field(default=None, max_length=500)
    request_type: str = Field(default="system-one-decision", min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, max_length=500)


class DecisionEnvelope(DecisionModel):
    answers: dict[str, DecisionAnswer]
    provider: str
    model: str
    provider_request_id: str | None = None
    usage: DecisionUsage = Field(default_factory=DecisionUsage)
    latency_ms: int = Field(default=0, ge=0)
    started_at: datetime
    completed_at: datetime
    attempts: int = Field(default=1, ge=1)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_order(self) -> DecisionEnvelope:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class SystemOneDecisionEngine(Protocol):
    async def evaluate(self, request: DecisionRequest) -> DecisionEnvelope: ...


@dataclass
class DecisionEngineRegistry:
    _engines: dict[str, SystemOneDecisionEngine] = field(default_factory=dict)

    def register(self, key: str, engine: SystemOneDecisionEngine) -> None:
        normalized = key.strip().lower()
        if not normalized:
            raise ValueError("decision engine key must not be blank")
        if normalized in self._engines:
            raise ValueError(f"decision engine already registered: {normalized}")
        self._engines[normalized] = engine

    def get(self, key: str) -> SystemOneDecisionEngine:
        normalized = key.strip().lower()
        try:
            return self._engines[normalized]
        except KeyError as exc:
            raise DecisionEngineNotRegistered(f"decision engine is not registered: {normalized}") from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._engines))


decision_engine_registry = DecisionEngineRegistry()
