import pytest

from app.decision_engine import ChoiceDecisionAnswer, NoulDecisionAnswer, ScoreDecisionAnswer
from app.decision_policy import DecisionPolicyError, evaluate_decision_policy
from app.decision_specifications import DecisionPolicy


def test_bounded_decision_policy_uses_native_primitive_semantics() -> None:
    policy = DecisionPolicy.model_validate(
        {
            "recommendations": {
                "potentially_privileged": {
                    "operator": "ALL",
                    "operands": [
                        {
                            "question_key": "privilege.legal_advice",
                            "measure": "noul",
                            "comparator": "GTE",
                            "threshold": 0.7,
                        },
                        {
                            "question_key": "privilege.relationship",
                            "measure": "probability",
                            "option": "attorney_client",
                            "comparator": "GTE",
                            "threshold": 0.6,
                        },
                    ],
                }
            },
            "routes": {
                "human_review": {
                    "operator": "PREDICATE",
                    "predicate": {
                        "question_key": "privilege.strength",
                        "measure": "confidence",
                        "comparator": "LT",
                        "threshold": 0.8,
                    },
                }
            },
        }
    )
    answers = {
        "privilege.legal_advice": NoulDecisionAnswer(type="noul", noul=0.92),
        "privilege.relationship": ChoiceDecisionAnswer(
            type="choice",
            choice="attorney_client",
            confidence=0.9,
            probabilities={"attorney_client": 0.87, "other": 0.13},
        ),
        "privilege.strength": ScoreDecisionAnswer(
            type="score",
            score=1.8,
            confidence=0.72,
            legend={"0": "none", "1": "weak", "2": "strong"},
            probabilities={"0": 0.02, "1": 0.16, "2": 0.82},
        ),
    }

    recommendations, routes = evaluate_decision_policy(policy, answers)

    assert recommendations["potentially_privileged"]["matched"] is True
    assert routes["human_review"]["matched"] is True


def test_decision_policy_fails_closed_when_an_answer_is_missing() -> None:
    policy = DecisionPolicy.model_validate(
        {
            "routes": {
                "human_review": {
                    "operator": "PREDICATE",
                    "predicate": {
                        "question_key": "privilege.legal_advice",
                        "measure": "noul",
                        "comparator": "GTE",
                        "threshold": 0.7,
                    },
                }
            }
        }
    )
    with pytest.raises(DecisionPolicyError, match="Missing answer"):
        evaluate_decision_policy(policy, {})
