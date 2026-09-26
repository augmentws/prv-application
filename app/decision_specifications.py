from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QuestionType = Literal["choice", "score", "noul"]
ConfidenceKind = Literal[
    "PROVIDER_CONFIDENCE",
    "SELECTED_PROBABILITY",
    "DERIVED_PROBABILITY",
    "NONE",
]
AggregationOperator = Literal[
    "ANY_WINDOW",
    "ALL_WINDOWS",
    "MAX_PROBABILITY",
    "HIGHEST_CONFIDENCE_CHOICE",
    "INSUFFICIENT_IF_PARTIAL",
]
InstructionValue = str | dict[str, Any] | list[Any]
CriterionValue = str | dict[str, Any] | list[Any] | None

QUESTION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
OPTION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


class SpecificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionSourceReference(SpecificationModel):
    task_version_id: uuid.UUID
    heading: str | None = Field(default=None, min_length=1, max_length=500)
    excerpt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class DecisionAggregation(SpecificationModel):
    operator: AggregationOperator


class DecisionEvidencePolicy(SpecificationModel):
    required: bool = True
    minimum_exists_probability: float = Field(default=0.7, ge=0, le=1)


class DecisionUncertaintyMapping(SpecificationModel):
    kind: ConfidenceKind
    source: Literal[
        "PROVIDER_CONFIDENCE",
        "SELECTED_OPTION_PROBABILITY",
        "MAPPED_BOOLEAN_PROBABILITY",
        "NONE",
    ]

    @model_validator(mode="after")
    def validate_kind_source(self) -> DecisionUncertaintyMapping:
        expected = {
            "PROVIDER_CONFIDENCE": "PROVIDER_CONFIDENCE",
            "SELECTED_PROBABILITY": "SELECTED_OPTION_PROBABILITY",
            "DERIVED_PROBABILITY": "MAPPED_BOOLEAN_PROBABILITY",
            "NONE": "NONE",
        }
        if expected[self.kind] != self.source:
            raise ValueError(f"{self.kind} requires uncertainty source {expected[self.kind]}")
        return self


class DecisionFieldMapping(SpecificationModel):
    metadata_definition_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    value: Any
    uncertainty: DecisionUncertaintyMapping = Field(
        default_factory=lambda: DecisionUncertaintyMapping(kind="NONE", source="NONE")
    )


class NoulCriteria(SpecificationModel):
    true: CriterionValue
    false: CriterionValue


class DecisionQuestionBase(SpecificationModel):
    instructions: InstructionValue
    source_refs: list[DecisionSourceReference] = Field(min_length=1, max_length=100)
    aggregation: DecisionAggregation
    evidence: DecisionEvidencePolicy = Field(default_factory=DecisionEvidencePolicy)
    field_mapping: DecisionFieldMapping | None = None

    @field_validator("instructions")
    @classmethod
    def validate_instructions(cls, value: InstructionValue) -> InstructionValue:
        if isinstance(value, str) and not value.strip():
            raise ValueError("instructions must not be blank")
        if isinstance(value, (dict, list)) and not value:
            raise ValueError("structured instructions must not be empty")
        return value


class ChoiceDecisionQuestion(DecisionQuestionBase):
    type: Literal["choice"]
    criteria: dict[str, CriterionValue] = Field(min_length=2, max_length=255)

    @field_validator("criteria")
    @classmethod
    def validate_option_keys(cls, value: dict[str, CriterionValue]) -> dict[str, CriterionValue]:
        invalid = sorted(key for key in value if not OPTION_KEY_PATTERN.fullmatch(key))
        if invalid:
            raise ValueError(f"invalid Choice option keys: {', '.join(invalid)}")
        return value

    @model_validator(mode="after")
    def validate_uncertainty_mapping(self) -> ChoiceDecisionQuestion:
        if self.field_mapping and self.field_mapping.uncertainty.kind == "DERIVED_PROBABILITY":
            raise ValueError("Choice field mappings cannot use DERIVED_PROBABILITY")
        return self


class ScoreDecisionQuestion(DecisionQuestionBase):
    type: Literal["score"]
    criteria: list[CriterionValue] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def validate_uncertainty_mapping(self) -> ScoreDecisionQuestion:
        if self.field_mapping and self.field_mapping.uncertainty.kind == "DERIVED_PROBABILITY":
            raise ValueError("Score field mappings cannot use DERIVED_PROBABILITY")
        return self


class NoulDecisionQuestion(DecisionQuestionBase):
    type: Literal["noul"]
    criteria: NoulCriteria | None = None

    @model_validator(mode="after")
    def validate_uncertainty_mapping(self) -> NoulDecisionQuestion:
        if self.field_mapping and self.field_mapping.uncertainty.kind in {
            "PROVIDER_CONFIDENCE",
            "SELECTED_PROBABILITY",
        }:
            raise ValueError("Noul field mappings cannot claim provider confidence or selected-option probability")
        return self


DecisionQuestion = Annotated[
    ChoiceDecisionQuestion | ScoreDecisionQuestion | NoulDecisionQuestion,
    Field(discriminator="type"),
]


class DecisionPredicate(SpecificationModel):
    question_key: str
    measure: Literal["noul", "confidence", "score", "probability"]
    comparator: Literal["GT", "GTE", "LT", "LTE", "EQ"]
    threshold: float
    option: str | None = None

    @model_validator(mode="after")
    def validate_probability_option(self) -> DecisionPredicate:
        if self.measure == "probability" and self.option is None:
            raise ValueError("probability predicates require an option")
        if self.measure != "probability" and self.option is not None:
            raise ValueError("option is only valid for probability predicates")
        return self


class DecisionPolicyExpression(SpecificationModel):
    operator: Literal["PREDICATE", "ALL", "ANY", "NOT"]
    predicate: DecisionPredicate | None = None
    operands: list[DecisionPolicyExpression] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_shape(self) -> DecisionPolicyExpression:
        if self.operator == "PREDICATE":
            if self.predicate is None or self.operands:
                raise ValueError("PREDICATE requires predicate and no operands")
        elif self.predicate is not None:
            raise ValueError(f"{self.operator} does not accept predicate")
        elif self.operator == "NOT" and len(self.operands) != 1:
            raise ValueError("NOT requires exactly one operand")
        elif self.operator in {"ALL", "ANY"} and not self.operands:
            raise ValueError(f"{self.operator} requires at least one operand")
        return self


class DecisionPolicy(SpecificationModel):
    version: Literal["decision-policy-v1"] = "decision-policy-v1"
    recommendations: dict[str, DecisionPolicyExpression] = Field(default_factory=dict, max_length=100)
    routes: dict[str, DecisionPolicyExpression] = Field(default_factory=dict, max_length=100)


class DecisionStateContract(SpecificationModel):
    builder_version: str = Field(min_length=1, max_length=100)
    required_paths: list[str] = Field(min_length=1, max_length=100)


class DecisionSpecification(SpecificationModel):
    schema_version: Literal["review-decision-specification-v1"] = "review-decision-specification-v1"
    questions: dict[str, DecisionQuestion] = Field(min_length=1, max_length=500)
    decision_policy: DecisionPolicy = Field(default_factory=DecisionPolicy)
    state_contract: DecisionStateContract

    @field_validator("questions")
    @classmethod
    def validate_question_keys(cls, value: dict[str, DecisionQuestion]) -> dict[str, DecisionQuestion]:
        invalid = sorted(key for key in value if not QUESTION_KEY_PATTERN.fullmatch(key))
        if invalid:
            raise ValueError(f"invalid question keys: {', '.join(invalid)}")
        return value

    @model_validator(mode="after")
    def validate_policy_references(self) -> DecisionSpecification:
        for expression in [
            *self.decision_policy.recommendations.values(),
            *self.decision_policy.routes.values(),
        ]:
            self._validate_expression(expression)
        return self

    def _validate_expression(self, expression: DecisionPolicyExpression) -> None:
        if expression.predicate is not None:
            predicate = expression.predicate
            question = self.questions.get(predicate.question_key)
            if question is None:
                raise ValueError(f"policy references unknown question {predicate.question_key}")
            if predicate.measure == "noul" and question.type != "noul":
                raise ValueError(f"noul measure requires a Noul question: {predicate.question_key}")
            if predicate.measure == "confidence" and question.type == "noul":
                raise ValueError(f"Noul question has no provider confidence: {predicate.question_key}")
            if predicate.measure == "score" and question.type != "score":
                raise ValueError(f"score measure requires a Score question: {predicate.question_key}")
            if predicate.measure == "probability":
                if question.type == "noul":
                    raise ValueError(f"use noul measure for a Noul question: {predicate.question_key}")
                options = question.criteria if isinstance(question.criteria, dict) else None
                if options is not None and predicate.option not in options:
                    raise ValueError(
                        f"policy references unknown option {predicate.option} for {predicate.question_key}"
                    )
        for operand in expression.operands:
            self._validate_expression(operand)


def validate_specification_for_task_version(
    specification: DecisionSpecification,
    task_version_id: uuid.UUID,
) -> None:
    mismatched = sorted(
        {
            str(reference.task_version_id)
            for question in specification.questions.values()
            for reference in question.source_refs
            if reference.task_version_id != task_version_id
        }
    )
    if mismatched:
        raise ValueError(
            "question source references must point to the enclosing task version; found " + ", ".join(mismatched)
        )
