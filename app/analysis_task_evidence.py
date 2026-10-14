from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.decision_engine import ChoiceDecisionAnswer, DecisionAnswer, NoulDecisionAnswer
from app.decision_specifications import (
    ChoiceDecisionQuestion,
    DecisionAggregation,
    DecisionMappedValueRecommendation,
    DecisionPolicyExpression,
    DecisionQuestion,
    DecisionSelectedOptionRecommendation,
    DecisionSpecification,
    NoulCriteria,
    NoulDecisionQuestion,
)
from app.document_evidence import ParagraphMap

EVIDENCE_LOCALIZATION_VERSION = "jev-conditional-evidence-v2"
MAX_EVIDENCE_PARAGRAPHS = 255
MAX_DECISION_REQUEST_QUESTIONS = 500
MAX_STORED_EVIDENCE_CANDIDATES = 5


@dataclass(frozen=True)
class EvidenceQuestionPlan:
    question_key: str
    required: bool
    minimum_exists_probability: float
    exists_question_key: str | None
    location_question_key: str | None
    option_to_paragraph_id: dict[str, str]
    unavailable_reason: str | None = None


def _decision_question_context(question: DecisionQuestion) -> dict[str, Any]:
    return question.model_dump(
        mode="json",
        include={"type", "instructions", "criteria"},
        exclude_none=True,
    )


def _companion_questions(
    *,
    ordinal: int,
    question_key: str,
    question: DecisionQuestion,
    paragraph_map: ParagraphMap,
) -> tuple[EvidenceQuestionPlan, dict[str, DecisionQuestion]]:
    exists_key = f"internal_evidence.q{ordinal}.exists"
    location_key = f"internal_evidence.q{ordinal}.location"
    option_to_paragraph_id = {
        f"paragraph_{index}": paragraph.paragraph_id
        for index, paragraph in enumerate(paragraph_map.paragraphs, start=1)
    }
    location_criteria: dict[str, Any] = {
        option: {"paragraph_id": paragraph_id}
        for option, paragraph_id in option_to_paragraph_id.items()
    }
    location_criteria["no_support"] = {
        "meaning": "No document paragraph directly supports the frozen classification outcome"
    }
    context = _decision_question_context(question)
    shared_instruction = {
        "decision_question_key": question_key,
        "decision_question": context,
        "classification_outcome": (
            "The classification answer is frozen in classification.answers. Locate evidence for that outcome; "
            "do not reclassify the document."
        ),
        "evidence_scope": (
            "Use only document.paragraphs as evidence. Matter guidance explains the decision standard but is not "
            "document evidence. The selected paragraph must directly support the frozen classification outcome."
        ),
    }
    exists_question = NoulDecisionQuestion(
        type="noul",
        instructions={
            **shared_instruction,
            "task": (
                "Does at least one document paragraph directly support the correct answer to the decision question?"
            ),
        },
        criteria=NoulCriteria(
            true="At least one document paragraph directly supports the correct answer",
            false="No document paragraph directly supports the correct answer",
        ),
        source_refs=question.source_refs,
        aggregation=DecisionAggregation(operator="ANY_WINDOW"),
        evidence={"required": False},
    )
    location_question = ChoiceDecisionQuestion(
        type="choice",
        instructions={
            **shared_instruction,
            "task": (
                "Which document paragraph most directly supports the correct answer to the decision question? "
                "Select the option whose paragraph_id matches document.paragraphs, or select no_support when "
                "none of the document paragraphs directly supports the frozen classification outcome."
            ),
        },
        criteria=location_criteria,
        source_refs=question.source_refs,
        aggregation=DecisionAggregation(operator="MAX_PROBABILITY"),
        evidence={"required": False},
    )
    return (
        EvidenceQuestionPlan(
            question_key=question_key,
            required=True,
            minimum_exists_probability=question.evidence.minimum_exists_probability,
            exists_question_key=exists_key,
            location_question_key=location_key,
            option_to_paragraph_id=option_to_paragraph_id,
        ),
        {exists_key: exists_question, location_key: location_question},
    )


def _expression_question_keys(expression: DecisionPolicyExpression) -> set[str]:
    predicates = [expression.predicate] if expression.predicate is not None else expression.operands
    return {predicate.question_key for predicate in predicates}


def select_evidence_question_keys(
    specification: DecisionSpecification,
    recommendations: dict[str, Any],
    routes: dict[str, Any],
) -> tuple[str, ...]:
    """Select evidence only for actionable recommendations and matched review routes."""

    selected: set[str] = set()
    for rule_key, result in recommendations.items():
        if not isinstance(result, dict) or result.get("matched") is not True:
            continue
        question_key = result.get("question_key")
        if isinstance(question_key, str):
            selected.add(question_key)
            continue
        rule = specification.decision_policy.recommendations.get(rule_key)
        if isinstance(rule, DecisionSelectedOptionRecommendation):
            selected.add(rule.question_key)
        elif isinstance(rule, DecisionMappedValueRecommendation):
            selected.add(rule.predicate.question_key)
        elif isinstance(rule, DecisionPolicyExpression):
            selected.update(_expression_question_keys(rule))

    for route_key, result in routes.items():
        if not isinstance(result, dict) or result.get("matched") is not True:
            continue
        route = specification.decision_policy.routes.get(route_key)
        if route is not None:
            selected.update(_expression_question_keys(route))

    return tuple(
        key
        for key, question in specification.questions.items()
        if key in selected and question.evidence.required
    )


def build_conditional_evidence_questions(
    specification: DecisionSpecification,
    paragraph_map: ParagraphMap,
    question_keys: tuple[str, ...],
) -> tuple[dict[str, DecisionQuestion], tuple[EvidenceQuestionPlan, ...]]:
    """Build a reduced second-pass request for selected classification outcomes."""

    selected = set(question_keys)
    required = [(key, question) for key, question in specification.questions.items() if key in selected]
    questions: dict[str, DecisionQuestion] = {}
    if not required:
        return questions, ()

    unavailable_reason: str | None = None
    if not paragraph_map.paragraphs:
        unavailable_reason = "no_document_paragraphs"
    elif len(paragraph_map.paragraphs) > MAX_EVIDENCE_PARAGRAPHS:
        unavailable_reason = "paragraph_limit_exceeded"
    elif 2 * len(required) > MAX_DECISION_REQUEST_QUESTIONS:
        unavailable_reason = "question_limit_exceeded"

    if unavailable_reason is not None:
        return questions, tuple(
            EvidenceQuestionPlan(
                question_key=key,
                required=True,
                minimum_exists_probability=question.evidence.minimum_exists_probability,
                exists_question_key=None,
                location_question_key=None,
                option_to_paragraph_id={},
                unavailable_reason=unavailable_reason,
            )
            for key, question in required
        )

    plans: list[EvidenceQuestionPlan] = []
    for ordinal, (key, question) in enumerate(required, start=1):
        plan, companions = _companion_questions(
            ordinal=ordinal,
            question_key=key,
            question=question,
            paragraph_map=paragraph_map,
        )
        collisions = set(questions).intersection(companions)
        if collisions:
            raise ValueError(f"Evidence question keys collide with specification questions: {sorted(collisions)}")
        questions.update(companions)
        plans.append(plan)
    return questions, tuple(plans)


def collect_conditional_evidence(
    plans: tuple[EvidenceQuestionPlan, ...],
    answers: dict[str, DecisionAnswer],
) -> tuple[dict[str, Any], bool]:
    """Validate second-pass evidence answers and retain compact, auditable paragraph references."""

    evidence: dict[str, Any] = {}
    complete = True
    for plan in plans:
        entry: dict[str, Any] = {
            "method": EVIDENCE_LOCALIZATION_VERSION,
            "required": plan.required,
            "minimum_exists_probability": plan.minimum_exists_probability,
            "paragraph_ids": [],
            "complete": False,
        }
        if plan.unavailable_reason is not None:
            entry["reason"] = plan.unavailable_reason
            evidence[plan.question_key] = entry
            complete = False
            continue

        exists = answers.get(plan.exists_question_key or "")
        location = answers.get(plan.location_question_key or "")
        if not isinstance(exists, NoulDecisionAnswer) or not isinstance(
            location, ChoiceDecisionAnswer
        ):
            entry["reason"] = "missing_or_invalid_provider_answer"
            evidence[plan.question_key] = entry
            complete = False
            continue

        entry["exists_probability"] = exists.noul
        ranked = sorted(
            (
                (plan.option_to_paragraph_id[option], probability)
                for option, probability in location.probabilities.items()
                if option in plan.option_to_paragraph_id
            ),
            key=lambda item: (-item[1], item[0]),
        )
        entry["candidate_paragraphs"] = [
            {"paragraph_id": paragraph_id, "probability": probability}
            for paragraph_id, probability in ranked[:MAX_STORED_EVIDENCE_CANDIDATES]
        ]
        selected_paragraph_id = plan.option_to_paragraph_id.get(location.choice)
        if selected_paragraph_id is not None:
            entry["selected_probability"] = location.probabilities[location.choice]

        if exists.noul < plan.minimum_exists_probability:
            entry["reason"] = "below_minimum_exists_probability"
        elif selected_paragraph_id is None:
            entry["reason"] = "no_supporting_paragraph_selected"
        else:
            entry["paragraph_ids"] = [selected_paragraph_id]
            entry["complete"] = True

        evidence[plan.question_key] = entry
        complete = complete and bool(entry["complete"])
    return evidence, complete


def primary_decision_answers(
    specification: DecisionSpecification,
    answers: dict[str, DecisionAnswer],
) -> dict[str, DecisionAnswer]:
    return {key: answers[key] for key in specification.questions if key in answers}
