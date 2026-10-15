from app.analysis_task_evidence import (
    MAX_EVIDENCE_PARAGRAPHS,
    build_conditional_evidence_questions,
    collect_conditional_evidence,
    select_evidence_question_keys,
)
from app.decision_engine import ChoiceDecisionAnswer, NoulDecisionAnswer
from app.decision_policy import evaluate_decision_policy
from app.decision_specifications import DecisionSpecification
from app.document_evidence import segment_paragraphs


def _specification(*, evidence_required: bool = True) -> DecisionSpecification:
    return DecisionSpecification.model_validate(
        {
            "questions": {
                "responsiveness.overall": {
                    "type": "choice",
                    "instructions": "Is this document responsive?",
                    "criteria": {
                        "responsive": "Within scope",
                        "not_responsive": "Outside scope",
                    },
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "heading": "Responsiveness",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "HIGHEST_CONFIDENCE_CHOICE"},
                    "evidence": {
                        "required": evidence_required,
                        "minimum_exists_probability": 0.7,
                    },
                }
            },
            "state_contract": {
                "builder_version": "document-review-state-v1",
                "required_paths": ["document.paragraphs"],
            },
            "decision_policy": {
                "recommendations": {
                    "responsive": {
                        "operator": "PREDICATE",
                        "predicate": {
                            "question_key": "responsiveness.overall",
                            "measure": "probability",
                            "option": "responsive",
                            "comparator": "GTE",
                            "threshold": 0.7,
                        },
                    }
                },
                "routes": {
                    "needs_review": {
                        "operator": "PREDICATE",
                        "predicate": {
                            "question_key": "responsiveness.overall",
                            "measure": "confidence",
                            "comparator": "LTE",
                            "threshold": 0.6,
                        },
                    }
                },
            },
        }
    )


def test_matched_recommendation_selects_conditional_evidence_question() -> None:
    specification = _specification()
    answers = {
        "responsiveness.overall": ChoiceDecisionAnswer(
            type="choice",
            choice="responsive",
            probabilities={"responsive": 0.9, "not_responsive": 0.1},
            confidence=0.9,
        )
    }
    recommendations, routes = evaluate_decision_policy(
        specification.decision_policy,
        answers,
        specification.questions,
    )

    selected = select_evidence_question_keys(
        specification,
        recommendations,
        routes,
    )

    assert selected == ("responsiveness.overall",)


def test_unmatched_outcome_does_not_request_evidence() -> None:
    specification = _specification()
    answers = {
        "responsiveness.overall": ChoiceDecisionAnswer(
            type="choice",
            choice="not_responsive",
            probabilities={"responsive": 0.1, "not_responsive": 0.9},
            confidence=0.9,
        )
    }
    recommendations, routes = evaluate_decision_policy(
        specification.decision_policy,
        answers,
        specification.questions,
    )
    selected = select_evidence_question_keys(
        specification,
        recommendations,
        routes,
    )

    questions, plans = build_conditional_evidence_questions(
        specification,
        segment_paragraphs("One paragraph."),
        selected,
    )

    assert selected == ()
    assert questions == {}
    assert plans == ()


def test_matched_review_route_selects_conditional_evidence_question() -> None:
    specification = _specification()
    answers = {
        "responsiveness.overall": ChoiceDecisionAnswer(
            type="choice",
            choice="not_responsive",
            probabilities={"responsive": 0.4, "not_responsive": 0.6},
            confidence=0.5,
        )
    }
    recommendations, routes = evaluate_decision_policy(
        specification.decision_policy,
        answers,
        specification.questions,
    )

    selected = select_evidence_question_keys(
        specification,
        recommendations,
        routes,
    )

    assert selected == ("responsiveness.overall",)


def test_conditional_evidence_adds_only_exists_and_location_questions() -> None:
    specification = _specification()
    paragraph_map = segment_paragraphs("The contract concerns the investigation.\n\nUnrelated footer.")

    questions, plans = build_conditional_evidence_questions(
        specification,
        paragraph_map,
        ("responsiveness.overall",),
    )

    assert set(questions) == {
        "internal_evidence.q1.exists",
        "internal_evidence.q1.location",
    }
    assert plans[0].option_to_paragraph_id == {"paragraph_1": "¶1", "paragraph_2": "¶2"}
    assert questions["internal_evidence.q1.location"].criteria == {
        "paragraph_1": {"paragraph_id": "¶1"},
        "paragraph_2": {"paragraph_id": "¶2"},
        "no_support": {
            "meaning": "No document paragraph directly supports the frozen classification outcome"
        },
    }


def test_conditional_evidence_validates_threshold_and_selected_paragraph() -> None:
    specification = _specification()
    paragraph_map = segment_paragraphs("Relevant paragraph.\n\nSecondary paragraph.")
    _, plans = build_conditional_evidence_questions(
        specification,
        paragraph_map,
        ("responsiveness.overall",),
    )
    answers = {
        "internal_evidence.q1.exists": NoulDecisionAnswer(type="noul", noul=0.92),
        "internal_evidence.q1.location": ChoiceDecisionAnswer(
            type="choice",
            choice="paragraph_1",
            confidence=0.8,
            probabilities={"paragraph_1": 0.9, "paragraph_2": 0.1, "no_support": 0.0},
        ),
    }

    evidence, complete = collect_conditional_evidence(plans, answers)

    assert complete is True
    assert evidence["responsiveness.overall"]["paragraph_ids"] == ["¶1"]
    assert evidence["responsiveness.overall"]["candidate_paragraphs"] == [
        {"paragraph_id": "¶1", "probability": 0.9},
        {"paragraph_id": "¶2", "probability": 0.1},
    ]

    answers["internal_evidence.q1.exists"] = NoulDecisionAnswer(type="noul", noul=0.69)
    evidence, complete = collect_conditional_evidence(plans, answers)
    assert complete is False
    assert evidence["responsiveness.overall"]["paragraph_ids"] == []
    assert evidence["responsiveness.overall"]["reason"] == "below_minimum_exists_probability"


def test_conditional_evidence_allows_no_support_for_multiple_paragraphs() -> None:
    specification = _specification()
    paragraph_map = segment_paragraphs("First paragraph.\n\nSecond paragraph.")
    _, plans = build_conditional_evidence_questions(
        specification,
        paragraph_map,
        ("responsiveness.overall",),
    )
    answers = {
        "internal_evidence.q1.exists": NoulDecisionAnswer(type="noul", noul=0.9),
        "internal_evidence.q1.location": ChoiceDecisionAnswer(
            type="choice",
            choice="no_support",
            confidence=0.75,
            probabilities={"paragraph_1": 0.1, "paragraph_2": 0.15, "no_support": 0.75},
        ),
    }

    evidence, complete = collect_conditional_evidence(plans, answers)

    assert complete is False
    assert evidence["responsiveness.overall"]["paragraph_ids"] == []
    assert evidence["responsiveness.overall"]["reason"] == "no_supporting_paragraph_selected"


def test_conditional_evidence_does_not_silently_truncate_long_documents() -> None:
    specification = _specification()
    paragraph_map = segment_paragraphs(
        "\n\n".join(f"Paragraph {index}." for index in range(MAX_EVIDENCE_PARAGRAPHS + 1))
    )

    questions, plans = build_conditional_evidence_questions(
        specification,
        paragraph_map,
        ("responsiveness.overall",),
    )
    evidence, complete = collect_conditional_evidence(plans, {})

    assert questions == {}
    assert complete is False
    assert evidence["responsiveness.overall"]["reason"] == "paragraph_limit_exceeded"


def test_optional_evidence_does_not_add_companion_questions() -> None:
    specification = _specification(evidence_required=False)
    selected = select_evidence_question_keys(
        specification,
        {"responsive": {"matched": True}},
        {"needs_review": {"matched": False}},
    )

    questions, plans = build_conditional_evidence_questions(
        specification,
        segment_paragraphs("One paragraph."),
        selected,
    )
    evidence, complete = collect_conditional_evidence(plans, {})

    assert selected == ()
    assert questions == {}
    assert plans == ()
    assert evidence == {}
    assert complete is True
