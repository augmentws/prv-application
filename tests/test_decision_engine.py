from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.decision_engine import (
    DecisionEngineNotRegistered,
    DecisionEngineRegistry,
    DecisionEnvelope,
    NoulDecisionAnswer,
)
from app.decision_specifications import DecisionSpecification


def test_decision_specification_enforces_primitive_uncertainty_semantics() -> None:
    specification = DecisionSpecification.model_validate(
        {
            "questions": {
                "privilege.legal_advice": {
                    "type": "noul",
                    "instructions": "Does the document request or provide legal advice?",
                    "criteria": {"true": "Legal advice is present", "false": "Legal advice is absent"},
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "heading": "Legal advice",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "ANY_WINDOW"},
                    "field_mapping": {
                        "metadata_definition_key": "privilege",
                        "value": True,
                        "uncertainty": {
                            "kind": "DERIVED_PROBABILITY",
                            "source": "MAPPED_BOOLEAN_PROBABILITY",
                        },
                    },
                }
            },
            "state_contract": {
                "builder_version": "document-review-state-v1",
                "required_paths": ["document.paragraphs"],
            },
        }
    )
    assert specification.questions["privilege.legal_advice"].type == "noul"

    invalid = specification.model_dump(mode="json")
    invalid["questions"]["privilege.legal_advice"]["field_mapping"]["uncertainty"] = {
        "kind": "PROVIDER_CONFIDENCE",
        "source": "PROVIDER_CONFIDENCE",
    }
    with pytest.raises(ValidationError, match="Noul field mappings cannot claim provider confidence"):
        DecisionSpecification.model_validate(invalid)


def test_decision_envelope_preserves_full_probabilities_and_noul_has_no_confidence() -> None:
    now = datetime.now(UTC)
    envelope = DecisionEnvelope.model_validate(
        {
            "answers": {
                "issue.classification": {
                    "type": "choice",
                    "choice": "responsive",
                    "probabilities": {"responsive": 0.82, "not_responsive": 0.18},
                    "confidence": 0.76,
                },
                "privilege.legal_advice": {"type": "noul", "noul": 0.91},
            },
            "provider": "jev",
            "model": "system-one",
            "started_at": now,
            "completed_at": now,
        }
    )
    choice = envelope.answers["issue.classification"]
    assert choice.type == "choice"
    assert choice.probabilities == {"responsive": 0.82, "not_responsive": 0.18}
    assert isinstance(envelope.answers["privilege.legal_advice"], NoulDecisionAnswer)

    with pytest.raises(ValidationError):
        NoulDecisionAnswer.model_validate({"type": "noul", "noul": 0.91, "confidence": 0.91})


def test_decision_engine_registry_requires_unique_registered_keys() -> None:
    class StubEngine:
        async def evaluate(self, request):
            raise NotImplementedError

    registry = DecisionEngineRegistry()
    engine = StubEngine()
    registry.register("JEV", engine)
    assert registry.get("jev") is engine
    assert registry.keys() == ("jev",)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("jev", engine)
    with pytest.raises(DecisionEngineNotRegistered):
        registry.get("missing")
