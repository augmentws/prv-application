from __future__ import annotations

from typing import Any

from app.decision_engine import ChoiceDecisionAnswer, DecisionAnswer, NoulDecisionAnswer, ScoreDecisionAnswer
from app.decision_specifications import DecisionPolicy, DecisionPolicyExpression, DecisionPredicate


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


def evaluate_decision_policy(
    policy: DecisionPolicy,
    answers: dict[str, DecisionAnswer],
) -> tuple[dict[str, Any], dict[str, Any]]:
    recommendations = {
        key: {"matched": expression_matches(expression, answers), "policy_version": policy.version}
        for key, expression in policy.recommendations.items()
    }
    routes = {
        key: {"matched": expression_matches(expression, answers), "policy_version": policy.version}
        for key, expression in policy.routes.items()
    }
    return recommendations, routes
