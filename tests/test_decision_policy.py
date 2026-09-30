import pytest

from app.decision_engine import ChoiceDecisionAnswer, NoulDecisionAnswer, ScoreDecisionAnswer
from app.decision_policy import DecisionPolicyError, evaluate_decision_policy
from app.decision_specifications import (
    DecisionPolicy,
    DecisionSpecification,
    validate_decision_policy_semantics,
    validate_selected_option_policy_semantics,
)


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


def test_continuous_policy_measure_rejects_equality_at_an_interior_threshold() -> None:
    invalid = DecisionPolicy.model_validate(
        {
            "recommendations": {
                "is_responsive": {
                    "operator": "PREDICATE",
                    "predicate": {
                        "question_key": "responsiveness.overall",
                        "measure": "probability",
                        "option": "responsive",
                        "comparator": "EQ",
                        "threshold": 0.7,
                    },
                }
            }
        }
    )
    with pytest.raises(ValueError, match="uses EQ with an interior probability threshold"):
        validate_decision_policy_semantics(invalid)

    valid = DecisionPolicy.model_validate(
        {
            "recommendations": {
                "is_responsive": {
                    "operator": "PREDICATE",
                    "predicate": {
                        "question_key": "responsiveness.overall",
                        "measure": "probability",
                        "option": "responsive",
                        "comparator": "GTE",
                        "threshold": 0.7,
                    },
                }
            }
        }
    )
    validate_decision_policy_semantics(valid)


def test_selected_option_recommendation_emits_mapped_issue_value() -> None:
    specification = DecisionSpecification.model_validate(
        {
            "questions": {
                "topic.primary_issue": {
                    "type": "choice",
                    "instructions": "Select the primary issue.",
                    "criteria": {
                        "issue_1_market_disruption": "Market disruption",
                        "issue_2_hurricane_losses": "Hurricane losses",
                        "unclear": "The issue cannot be determined",
                    },
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "heading": "Issues",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "MAX_PROBABILITY"},
                    "field_mapping": {
                        "metadata_definition_key": "issue_tag",
                        "value_source": "SELECTED_OPTION",
                        "value": None,
                        "option_value_map": {
                            "issue_1_market_disruption": "issue_1_market_disruption",
                            "issue_2_hurricane_losses": "issue_2_hurricane_losses",
                        },
                        "uncertainty": {
                            "kind": "SELECTED_PROBABILITY",
                            "source": "SELECTED_OPTION_PROBABILITY",
                        },
                    },
                }
            },
            "decision_policy": {
                "recommendations": {
                    "issue_tag": {
                        "operator": "SELECTED_OPTION",
                        "question_key": "topic.primary_issue",
                        "minimum_probability": 0.7,
                    }
                }
            },
            "state_contract": {
                "builder_version": "document-review-state-v1",
                "required_paths": ["document.text"],
            },
        }
    )
    validate_selected_option_policy_semantics(specification)
    answer = ChoiceDecisionAnswer(
        type="choice",
        choice="issue_2_hurricane_losses",
        confidence=0.9,
        probabilities={
            "issue_1_market_disruption": 0.04,
            "issue_2_hurricane_losses": 0.91,
            "unclear": 0.05,
        },
    )

    recommendations, _ = evaluate_decision_policy(
        specification.decision_policy,
        {"topic.primary_issue": answer},
        specification.questions,
    )

    assert recommendations["issue_tag"] == {
        "matched": True,
        "value": "issue_2_hurricane_losses",
        "selected_option": "issue_2_hurricane_losses",
        "probability": 0.91,
        "minimum_probability": 0.7,
        "metadata_definition_key": "issue_tag",
        "question_key": "topic.primary_issue",
        "reason": "MATCHED",
        "policy_version": "decision-policy-v1",
    }


@pytest.mark.parametrize(
    ("choice", "probability", "expected_reason"),
    [
        ("unclear", 0.9, "UNMAPPED_OPTION"),
        ("issue_2_hurricane_losses", 0.69, "BELOW_THRESHOLD"),
    ],
)
def test_selected_option_recommendation_fails_closed(
    choice: str,
    probability: float,
    expected_reason: str,
) -> None:
    specification = DecisionSpecification.model_validate(
        {
            "questions": {
                "topic.primary_issue": {
                    "type": "choice",
                    "instructions": "Select the primary issue.",
                    "criteria": {
                        "issue_2_hurricane_losses": "Hurricane losses",
                        "unclear": "The issue cannot be determined",
                    },
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "heading": "Issues",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "MAX_PROBABILITY"},
                    "field_mapping": {
                        "metadata_definition_key": "issue_tag",
                        "value_source": "SELECTED_OPTION",
                        "option_value_map": {
                            "issue_2_hurricane_losses": "issue_2_hurricane_losses"
                        },
                        "uncertainty": {
                            "kind": "SELECTED_PROBABILITY",
                            "source": "SELECTED_OPTION_PROBABILITY",
                        },
                    },
                }
            },
            "decision_policy": {
                "recommendations": {
                    "issue_tag": {
                        "operator": "SELECTED_OPTION",
                        "question_key": "topic.primary_issue",
                        "minimum_probability": 0.7,
                    }
                }
            },
            "state_contract": {
                "builder_version": "document-review-state-v1",
                "required_paths": ["document.text"],
            },
        }
    )
    other_probability = 1 - probability
    answer = ChoiceDecisionAnswer(
        type="choice",
        choice=choice,
        confidence=probability,
        probabilities={
            "issue_2_hurricane_losses": probability if choice == "issue_2_hurricane_losses" else other_probability,
            "unclear": probability if choice == "unclear" else other_probability,
        },
    )

    recommendations, _ = evaluate_decision_policy(
        specification.decision_policy,
        {"topic.primary_issue": answer},
        specification.questions,
    )

    assert recommendations["issue_tag"]["matched"] is False
    assert recommendations["issue_tag"]["value"] is None
    assert recommendations["issue_tag"]["reason"] == expected_reason
