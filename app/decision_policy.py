from __future__ import annotations

from typing import Any

from app.decision_engine import ChoiceDecisionAnswer, DecisionAnswer, NoulDecisionAnswer, ScoreDecisionAnswer
from app.decision_specifications import (
    DecisionPolicy,
    DecisionPolicyExpression,
    DecisionPredicate,
    DecisionQuestion,
    DecisionSelectedOptionRecommendation,
)


class DecisionPolicyError(ValueError):
    pass


def _measure(predicate: DecisionPredicate, answers: dict[str, DecisionAnswer]) -> float:
    answer = answers.get(predicate.question_key)
    if answer is None:
        raise DecisionPolicyError(f"Missing answer for policy question {predicate.question_key}")
    if predicate.measure == "noul":
        if not isinstance(answer, NoulDecisionAnswer):
            raise DecisionPolicyError(f"Policy expected a Noul answer for {predicate.question_key}")
        return answer.noul
    if predicate.measure == "confidence":
        if not isinstance(answer, (ChoiceDecisionAnswer, ScoreDecisionAnswer)):
            raise DecisionPolicyError(f"Policy expected provider confidence for {predicate.question_key}")
        return answer.confidence
    if predicate.measure == "score":
        if not isinstance(answer, ScoreDecisionAnswer):
            raise DecisionPolicyError(f"Policy expected a Score answer for {predicate.question_key}")
        return answer.score
    if not isinstance(answer, (ChoiceDecisionAnswer, ScoreDecisionAnswer)) or predicate.option is None:
        raise DecisionPolicyError(f"Policy expected option probabilities for {predicate.question_key}")
    try:
        return answer.probabilities[predicate.option]
    except KeyError as exc:
        raise DecisionPolicyError(
            f"Policy option {predicate.option} is absent from {predicate.question_key}"
        ) from exc


def _predicate_matches(predicate: DecisionPredicate, answers: dict[str, DecisionAnswer]) -> bool:
    value = _measure(predicate, answers)
    return {
        "GT": value > predicate.threshold,
        "GTE": value >= predicate.threshold,
        "LT": value < predicate.threshold,
        "LTE": value <= predicate.threshold,
        "EQ": value == predicate.threshold,
    }[predicate.comparator]


def expression_matches(expression: DecisionPolicyExpression, answers: dict[str, DecisionAnswer]) -> bool:
    if expression.operator == "PREDICATE":
        assert expression.predicate is not None
        return _predicate_matches(expression.predicate, answers)
    if expression.operator == "NOT":
        assert expression.predicate is not None
        return not _predicate_matches(expression.predicate, answers)
    matches = [_predicate_matches(predicate, answers) for predicate in expression.operands]
    return all(matches) if expression.operator == "ALL" else any(matches)


def _evaluate_selected_option_recommendation(
    recommendation: DecisionSelectedOptionRecommendation,
    answers: dict[str, DecisionAnswer],
    questions: dict[str, DecisionQuestion] | None,
    policy_version: str,
) -> dict[str, Any]:
    answer = answers.get(recommendation.question_key)
    if not isinstance(answer, ChoiceDecisionAnswer):
        raise DecisionPolicyError(
            f"SELECTED_OPTION recommendation expected a Choice answer for {recommendation.question_key}"
        )
    if questions is None:
        raise DecisionPolicyError("SELECTED_OPTION recommendations require the Decision Specification questions")
    question = questions.get(recommendation.question_key)
    if question is None or question.type != "choice" or question.field_mapping is None:
        raise DecisionPolicyError(
            f"SELECTED_OPTION recommendation has no Choice field mapping for {recommendation.question_key}"
        )
    mapping = question.field_mapping
    if mapping.value_source != "SELECTED_OPTION":
        raise DecisionPolicyError(
            f"SELECTED_OPTION recommendation has an incompatible field mapping for {recommendation.question_key}"
        )
    try:
        probability = answer.probabilities[answer.choice]
    except KeyError as exc:
        raise DecisionPolicyError(
            f"Selected option {answer.choice} has no probability for {recommendation.question_key}"
        ) from exc
    mapped = answer.choice in mapping.option_value_map
    meets_threshold = probability >= recommendation.minimum_probability
    matched = mapped and meets_threshold
    if not mapped:
        reason = "UNMAPPED_OPTION"
    elif not meets_threshold:
        reason = "BELOW_THRESHOLD"
    else:
        reason = "MATCHED"
    return {
        "matched": matched,
        "value": mapping.option_value_map.get(answer.choice) if matched else None,
        "selected_option": answer.choice,
        "probability": probability,
        "minimum_probability": recommendation.minimum_probability,
        "metadata_definition_key": mapping.metadata_definition_key,
        "question_key": recommendation.question_key,
        "reason": reason,
        "policy_version": policy_version,
    }


def evaluate_decision_policy(
    policy: DecisionPolicy,
    answers: dict[str, DecisionAnswer],
    questions: dict[str, DecisionQuestion] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    recommendations = {
        key: (
            _evaluate_selected_option_recommendation(expression, answers, questions, policy.version)
            if isinstance(expression, DecisionSelectedOptionRecommendation)
            else {"matched": expression_matches(expression, answers), "policy_version": policy.version}
        )
        for key, expression in policy.recommendations.items()
    }
    routes = {
        key: {"matched": expression_matches(expression, answers), "policy_version": policy.version}
        for key, expression in policy.routes.items()
    }
    return recommendations, routes
