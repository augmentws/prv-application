from app.analysis_task_evidence import (
    MAX_EVIDENCE_PARAGRAPHS,
    build_same_request_evidence_questions,
    collect_same_request_evidence,
)
from app.decision_engine import ChoiceDecisionAnswer, NoulDecisionAnswer
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
        }
    )


def test_same_request_evidence_adds_exists_and_location_questions() -> None:
    specification = _specification()
    paragraph_map = segment_paragraphs("The contract concerns the investigation.\n\nUnrelated footer.")

    questions, plans = build_same_request_evidence_questions(specification, paragraph_map)

    assert set(questions) == {
        "responsiveness.overall",
        "internal_evidence.q1.exists",
        "internal_evidence.q1.location",
    }
    assert plans[0].option_to_paragraph_id == {"paragraph_1": "¶1", "paragraph_2": "¶2"}
    assert questions["internal_evidence.q1.location"].criteria == {
        "paragraph_1": {"paragraph_id": "¶1"},
        "paragraph_2": {"paragraph_id": "¶2"},
    }


def test_same_request_evidence_validates_threshold_and_selected_paragraph() -> None:
    specification = _specification()
    paragraph_map = segment_paragraphs("Relevant paragraph.\n\nSecondary paragraph.")
    _, plans = build_same_request_evidence_questions(specification, paragraph_map)
    answers = {
        "internal_evidence.q1.exists": NoulDecisionAnswer(type="noul", noul=0.92),
        "internal_evidence.q1.location": ChoiceDecisionAnswer(
            type="choice",
            choice="paragraph_1",
            confidence=0.8,
            probabilities={"paragraph_1": 0.9, "paragraph_2": 0.1},
        ),
    }

    evidence, complete = collect_same_request_evidence(plans, answers)

    assert complete is True
    assert evidence["responsiveness.overall"]["paragraph_ids"] == ["¶1"]
    assert evidence["responsiveness.overall"]["candidate_paragraphs"] == [
        {"paragraph_id": "¶1", "probability": 0.9},
        {"paragraph_id": "¶2", "probability": 0.1},
    ]

    answers["internal_evidence.q1.exists"] = NoulDecisionAnswer(type="noul", noul=0.69)
    evidence, complete = collect_same_request_evidence(plans, answers)
    assert complete is False
    assert evidence["responsiveness.overall"]["paragraph_ids"] == []
    assert evidence["responsiveness.overall"]["reason"] == "below_minimum_exists_probability"


def test_same_request_evidence_does_not_silently_truncate_long_documents() -> None:
    specification = _specification()
    paragraph_map = segment_paragraphs(
        "\n\n".join(f"Paragraph {index}." for index in range(MAX_EVIDENCE_PARAGRAPHS + 1))
    )

    questions, plans = build_same_request_evidence_questions(specification, paragraph_map)
    evidence, complete = collect_same_request_evidence(plans, {})

    assert set(questions) == {"responsiveness.overall"}
    assert complete is False
    assert evidence["responsiveness.overall"]["reason"] == "paragraph_limit_exceeded"


def test_optional_evidence_does_not_add_companion_questions() -> None:
    specification = _specification(evidence_required=False)

    questions, plans = build_same_request_evidence_questions(
        specification,
        segment_paragraphs("One paragraph."),
    )
    evidence, complete = collect_same_request_evidence(plans, {})

    assert set(questions) == {"responsiveness.overall"}
    assert plans == ()
    assert evidence == {}
    assert complete is True
