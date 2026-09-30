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
STATE_PATH_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
QuestionKey = Annotated[str, Field(pattern=QUESTION_KEY_PATTERN.pattern)]
StatePath = Annotated[str, Field(pattern=STATE_PATH_PATTERN.pattern)]

DOCUMENT_REVIEW_STATE_BUILDER_VERSION = "document-review-state-v1"
NON_ASSIGNABLE_CHOICE_OPTIONS = frozenset({"other", "unclear", "insufficient_evidence", "needs_review"})
DOCUMENT_REVIEW_STATE_PATHS = frozenset(
    {
        "matter.id",
        "matter.decision_context",
        "runtime.today",
        "document.id",
        "document.collection_item_id",
        "document.source_content_hash",
        "document.text",
        "document.metadata",
        "document.paragraphs",
    }
)


class SpecificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionContextExample(SpecificationModel):
    description: str = Field(min_length=1, max_length=4000)
    expected_outcome: str = Field(min_length=1, max_length=500)
    rationale: str | None = Field(default=None, max_length=4000)


class DecisionContextSource(SpecificationModel):
    heading: str | None = Field(default=None, min_length=1, max_length=500)
    excerpt: str = Field(min_length=1, max_length=100_000)
    excerpt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class MatterDecisionContext(SpecificationModel):
    """Frozen matter guidance supplied with every document decision in a task version."""

    summary: str | None = Field(default=None, max_length=8000)
    controlling_guidance: list[str] = Field(default_factory=list, max_length=200)
    responsiveness_scope: list[str] = Field(default_factory=list, max_length=200)
    inclusion_criteria: list[str] = Field(default_factory=list, max_length=200)
    exclusion_criteria: list[str] = Field(default_factory=list, max_length=200)
    issue_definitions: dict[str, str] = Field(default_factory=dict, max_length=200)
    key_entities: list[str] = Field(default_factory=list, max_length=500)
    date_scope: list[str] = Field(default_factory=list, max_length=100)
    terminology: dict[str, str] = Field(default_factory=dict, max_length=500)
    examples: list[DecisionContextExample] = Field(default_factory=list, max_length=100)
    source_material: list[DecisionContextSource] = Field(default_factory=list, max_length=2000)


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
    value_source: Literal["STATIC", "SELECTED_OPTION"] = "STATIC"
    value: Any | None = None
    option_value_map: dict[str, Any] = Field(default_factory=dict, max_length=255)
    uncertainty: DecisionUncertaintyMapping = Field(
        default_factory=lambda: DecisionUncertaintyMapping(kind="NONE", source="NONE")
    )

    @model_validator(mode="after")
    def validate_value_source(self) -> DecisionFieldMapping:
        if self.value_source == "STATIC":
            if self.value is None:
                raise ValueError("STATIC field mappings require value")
            if self.option_value_map:
                raise ValueError("STATIC field mappings cannot define option_value_map")
        else:
            if self.value is not None:
                raise ValueError("SELECTED_OPTION field mappings cannot define a static value")
            if not self.option_value_map:
                raise ValueError("SELECTED_OPTION field mappings require option_value_map")
        return self


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
        if self.field_mapping and self.field_mapping.value_source == "SELECTED_OPTION":
            if self.field_mapping.uncertainty.kind != "SELECTED_PROBABILITY":
                raise ValueError("SELECTED_OPTION field mappings require SELECTED_PROBABILITY uncertainty")
            unknown = sorted(set(self.field_mapping.option_value_map) - set(self.criteria))
            if unknown:
                raise ValueError(
                    "SELECTED_OPTION field mapping references unknown Choice options: " + ", ".join(unknown)
                )
        return self


class ScoreDecisionQuestion(DecisionQuestionBase):
    type: Literal["score"]
    criteria: list[CriterionValue] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def validate_uncertainty_mapping(self) -> ScoreDecisionQuestion:
        if self.field_mapping and self.field_mapping.uncertainty.kind == "DERIVED_PROBABILITY":
            raise ValueError("Score field mappings cannot use DERIVED_PROBABILITY")
        if self.field_mapping and self.field_mapping.value_source == "SELECTED_OPTION":
            raise ValueError("SELECTED_OPTION field mappings require a Choice question")
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
        if self.field_mapping and self.field_mapping.value_source == "SELECTED_OPTION":
            raise ValueError("SELECTED_OPTION field mappings require a Choice question")
        return self


DecisionQuestion = Annotated[
    ChoiceDecisionQuestion | ScoreDecisionQuestion | NoulDecisionQuestion,
    Field(discriminator="type"),
]


class DecisionPredicate(SpecificationModel):
    question_key: QuestionKey
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
        if self.measure in {"noul", "confidence", "probability"} and not 0 <= self.threshold <= 1:
            raise ValueError(f"{self.measure} predicate thresholds must be between 0 and 1")
        return self


class DecisionPolicyExpression(SpecificationModel):
    operator: Literal["PREDICATE", "ALL", "ANY", "NOT"]
    predicate: DecisionPredicate | None = None
    operands: list[DecisionPredicate] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_shape(self) -> DecisionPolicyExpression:
        if self.operator in {"PREDICATE", "NOT"}:
            if self.predicate is None or self.operands:
                raise ValueError(f"{self.operator} requires predicate and no operands")
        elif self.predicate is not None:
            raise ValueError(f"{self.operator} does not accept predicate")
        elif self.operator in {"ALL", "ANY"} and not self.operands:
            raise ValueError(f"{self.operator} requires at least one operand")
        return self


class DecisionSelectedOptionRecommendation(SpecificationModel):
    operator: Literal["SELECTED_OPTION"]
    question_key: QuestionKey
    minimum_probability: float = Field(ge=0, le=1)


DecisionRecommendation = Annotated[
    DecisionPolicyExpression | DecisionSelectedOptionRecommendation,
    Field(discriminator="operator"),
]


class DecisionPolicy(SpecificationModel):
    version: Literal["decision-policy-v1"] = "decision-policy-v1"
    recommendations: dict[str, DecisionRecommendation] = Field(default_factory=dict, max_length=100)
    routes: dict[str, DecisionPolicyExpression] = Field(default_factory=dict, max_length=100)


def validate_decision_policy_semantics(policy: DecisionPolicy) -> None:
    for section_name, expressions in (
        ("recommendations", policy.recommendations),
        ("routes", policy.routes),
    ):
        for rule_name, expression in expressions.items():
            if isinstance(expression, DecisionSelectedOptionRecommendation):
                continue
            predicates = [expression.predicate] if expression.predicate is not None else expression.operands
            for predicate in predicates:
                if (
                    predicate.measure in {"noul", "confidence", "probability"}
                    and predicate.comparator == "EQ"
                    and predicate.threshold not in {0, 1}
                ):
                    raise ValueError(
                        f"decision_policy.{section_name}.{rule_name} uses EQ with an interior "
                        f"{predicate.measure} threshold; use GTE or LTE"
                    )


def validate_selected_option_policy_semantics(specification: DecisionSpecification) -> None:
    recommendations = specification.decision_policy.recommendations
    errors: list[str] = []
    for question_key, question in specification.questions.items():
        mapping = question.field_mapping
        if mapping is None or mapping.value_source != "SELECTED_OPTION":
            continue
        fallback_options = sorted(NON_ASSIGNABLE_CHOICE_OPTIONS.intersection(mapping.option_value_map))
        if fallback_options:
            errors.append(
                f"{question_key} SELECTED_OPTION field mapping must not assign fallback options: "
                + ", ".join(fallback_options)
            )
        recommendation_key = mapping.metadata_definition_key
        recommendation = recommendations.get(recommendation_key)
        if not isinstance(recommendation, DecisionSelectedOptionRecommendation):
            errors.append(
                f"{question_key} SELECTED_OPTION field mapping requires decision_policy.recommendations."
                f"{recommendation_key} with operator SELECTED_OPTION"
            )
        elif recommendation.question_key != question_key:
            errors.append(
                f"decision_policy.recommendations.{recommendation_key} must reference {question_key}"
            )
    if errors:
        raise ValueError("; ".join(errors))


class DecisionStateContract(SpecificationModel):
    builder_version: str = Field(min_length=1, max_length=100)
    required_paths: list[StatePath] = Field(min_length=1, max_length=100)

    @field_validator("required_paths")
    @classmethod
    def validate_required_paths(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("state paths must be unique")
        return value


class DecisionSpecification(SpecificationModel):
    schema_version: Literal["review-decision-specification-v1"] = "review-decision-specification-v1"
    decision_context: MatterDecisionContext = Field(default_factory=MatterDecisionContext)
    questions: dict[QuestionKey, DecisionQuestion] = Field(min_length=1, max_length=500)
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
        for expression in self.decision_policy.recommendations.values():
            if isinstance(expression, DecisionSelectedOptionRecommendation):
                self._validate_selected_option_recommendation(expression)
                continue
            self._validate_expression(expression)
        for expression in self.decision_policy.routes.values():
            self._validate_expression(expression)
        return self

    def _validate_selected_option_recommendation(
        self,
        recommendation: DecisionSelectedOptionRecommendation,
    ) -> None:
        question = self.questions.get(recommendation.question_key)
        if question is None:
            raise ValueError(f"policy references unknown question {recommendation.question_key}")
        if not isinstance(question, ChoiceDecisionQuestion):
            raise TypeError(
                f"SELECTED_OPTION recommendation requires a Choice question: {recommendation.question_key}"
            )
        if question.field_mapping is None or question.field_mapping.value_source != "SELECTED_OPTION":
            raise ValueError(
                "SELECTED_OPTION recommendation requires a SELECTED_OPTION field mapping: "
                f"{recommendation.question_key}"
            )

    def _validate_expression(self, expression: DecisionPolicyExpression) -> None:
        if expression.predicate is not None:
            self._validate_predicate(expression.predicate)
        for operand in expression.operands:
            self._validate_predicate(operand)

    def _validate_predicate(self, predicate: DecisionPredicate) -> None:
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


class DecisionCompilationWarning(SpecificationModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    question_key: QuestionKey | None = None


class DecisionCompilationOmission(SpecificationModel):
    subject: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=4000)
    source_refs: list[DecisionSourceReference] = Field(default_factory=list, max_length=100)


class DecisionSpecificationCompilationOutput(SpecificationModel):
    decision_specification: DecisionSpecification
    question_rationales: dict[QuestionKey, str] = Field(min_length=1, max_length=500)
    omissions: list[DecisionCompilationOmission] = Field(default_factory=list, max_length=500)
    warnings: list[DecisionCompilationWarning] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_question_rationales(self) -> DecisionSpecificationCompilationOutput:
        question_keys = set(self.decision_specification.questions)
        rationale_keys = set(self.question_rationales)
        if rationale_keys != question_keys:
            missing = sorted(question_keys - rationale_keys)
            extra = sorted(rationale_keys - question_keys)
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if extra:
                details.append("unknown: " + ", ".join(extra))
            raise ValueError("question_rationales must match question keys (" + "; ".join(details) + ")")
        if any(not rationale.strip() for rationale in self.question_rationales.values()):
            raise ValueError("question rationales must not be blank")
        return self


def validate_specification_for_task_version(
    specification: DecisionSpecification,
    task_version_id: uuid.UUID,
) -> None:
    validate_decision_policy_semantics(specification.decision_policy)
    validate_selected_option_policy_semantics(specification)
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
