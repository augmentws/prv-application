import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.decision_engine import (
    DecisionEngineNotRegistered,
    DecisionEngineRegistry,
    DecisionEnvelope,
    DecisionRequest,
    NoulDecisionAnswer,
)
from app.decision_execution import execute_decision_request
from app.decision_specifications import DecisionSpecification, validate_selected_option_policy_semantics
from app.openai_decision_engine import OpenAIDecisionsEngine


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


def test_selected_option_mapping_requires_choice_probability_and_known_options() -> None:
    base = {
        "questions": {
            "topic.primary_issue": {
                "type": "choice",
                "instructions": "Select the primary issue.",
                "criteria": {"issue_one": "Issue one", "unclear": "Unclear"},
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
                    "option_value_map": {"issue_one": "issue_one"},
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
    specification = DecisionSpecification.model_validate(base)
    assert specification.questions["topic.primary_issue"].field_mapping is not None
    validate_selected_option_policy_semantics(specification)

    missing_recommendation = specification.model_copy(deep=True)
    missing_recommendation.decision_policy.recommendations = {}
    with pytest.raises(ValueError, match="requires decision_policy.recommendations.issue_tag"):
        validate_selected_option_policy_semantics(missing_recommendation)

    mapped_fallback = specification.model_copy(deep=True)
    mapping = mapped_fallback.questions["topic.primary_issue"].field_mapping
    assert mapping is not None
    mapping.option_value_map["unclear"] = "unclear"
    with pytest.raises(ValueError, match="must not assign fallback options: unclear"):
        validate_selected_option_policy_semantics(mapped_fallback)

    invalid = specification.model_dump(mode="json")
    invalid["questions"]["topic.primary_issue"]["field_mapping"]["option_value_map"] = {
        "missing_option": "issue_one"
    }
    with pytest.raises(ValidationError, match="unknown Choice options"):
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


def test_decision_execution_produces_shared_invocation_telemetry() -> None:
    now = datetime.now(UTC)
    envelope = DecisionEnvelope.model_validate(
        {
            "answers": {"privilege.legal_advice": {"type": "noul", "noul": 0.91}},
            "provider": "typesafe",
            "model": "jev-2026-09-01",
            "provider_request_id": "req_123",
            "usage": {"request_count": 2, "input_tokens": 50, "output_tokens": 4},
            "latency_ms": 125,
            "started_at": now,
            "completed_at": now,
            "attempts": 2,
        }
    )

    class StubEngine:
        async def evaluate(self, request):
            return envelope

    specification = DecisionSpecification.model_validate(
        {
            "questions": {
                "privilege.legal_advice": {
                    "type": "noul",
                    "instructions": "Does the document request or provide legal advice?",
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "heading": "Legal advice",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "ANY_WINDOW"},
                }
            },
            "state_contract": {
                "builder_version": "document-review-state-v1",
                "required_paths": ["document.paragraphs"],
            },
        }
    )
    registry = DecisionEngineRegistry()
    registry.register("jev", StubEngine())
    execution = asyncio.run(
        execute_decision_request(
            DecisionRequest(
                state={"document": {"paragraphs": []}},
                questions=specification.questions,
                model_key="jev-latest",
                run_id="run-123",
            ),
            engine_key="jev",
            registry=registry,
            settings=Settings(model_trace_enabled=False),
        )
    )

    assert execution.decision is envelope
    assert len(execution.model_configuration_hash) == 64
    assert execution.invocations[0].provider == "typesafe"
    assert execution.invocations[0].request_count == 2
    assert execution.invocations[0].input_tokens == 50


def test_openai_decisions_adapter_maps_provider_primitives_to_internal_answers() -> None:
    specification = DecisionSpecification.model_validate(
        {
            "questions": {
                "review.responsive": {
                    "type": "noul",
                    "instructions": "Is the document responsive?",
                    "criteria": {"true": "It concerns the investigation.", "false": "It does not."},
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "MAX_PROBABILITY"},
                },
                "review.disposition": {
                    "type": "choice",
                    "instructions": "Choose the disposition.",
                    "criteria": {"responsive": "Responsive", "not_responsive": "Not responsive"},
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "HIGHEST_CONFIDENCE_CHOICE"},
                },
                "review.priority": {
                    "type": "score",
                    "instructions": "Score review priority.",
                    "criteria": ["Low", "Medium", "High"],
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "MAX_PROBABILITY"},
                },
            },
            "state_contract": {
                "builder_version": "document-review-state-v1",
                "required_paths": ["document.text"],
            },
        }
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"x-request-id": "req_decision_123"},
            json={
                "model": "gpt-6-luna",
                "answers": [
                    {"type": "predicate", "name": "question_0", "probability": 0.91},
                    {
                        "type": "choice",
                        "name": "question_1",
                        "choice": "responsive",
                        "probabilities": [
                            {"value": "responsive", "probability": 0.84},
                            {"value": "not_responsive", "probability": 0.16},
                        ],
                        "confidence": 0.77,
                    },
                    {
                        "type": "score",
                        "name": "question_2",
                        "score": 1.7,
                        "probabilities": [
                            {"value": 0, "label": "0", "probability": 0.05},
                            {"value": 1, "label": "1", "probability": 0.2},
                            {"value": 2, "label": "2", "probability": 0.75},
                        ],
                        "confidence": 0.7,
                    },
                ],
                "usage": {"input_tokens": 123, "output_tokens": 0, "total_tokens": 123},
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    engine = OpenAIDecisionsEngine(
        Settings(openai_api_key="test-key"),
        client_factory=client_factory,
    )
    envelope = asyncio.run(
        engine.evaluate(
            DecisionRequest(
                state={"document": {"text": "Investigation notes"}},
                questions=specification.questions,
                model_key="gpt-6-luna",
                idempotency_key="decision-idempotency-key",
            )
        )
    )

    assert captured["model"] == "gpt-6-luna"
    assert json.loads(captured["input"])["document"]["text"] == "Investigation notes"
    assert [question["type"] for question in captured["questions"]] == ["predicate", "choice", "score"]
    assert captured["questions"][1]["choices"][0]["value"] == "responsive"
    assert envelope.provider == "openai"
    assert envelope.provider_request_id == "req_decision_123"
    assert envelope.answers["review.responsive"].model_dump() == {"type": "noul", "noul": 0.91}
    assert envelope.answers["review.disposition"].model_dump()["probabilities"] == {
        "responsive": 0.84,
        "not_responsive": 0.16,
    }
    assert envelope.answers["review.priority"].model_dump()["score"] == 1.7
    assert envelope.usage.input_tokens == 123


def test_openai_decisions_adapter_requires_key_and_supported_model() -> None:
    request = DecisionRequest.model_validate(
        {
            "state": {"document": {"text": "text"}},
            "questions": {
                "review.responsive": {
                    "type": "noul",
                    "instructions": "Is it responsive?",
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "MAX_PROBABILITY"},
                }
            },
            "model_key": "gpt-6-luna",
        }
    )
    with pytest.raises(Exception, match="OPENAI_API_KEY"):
        asyncio.run(OpenAIDecisionsEngine(Settings(openai_api_key=None)).evaluate(request))

    unsupported = request.model_copy(update={"model_key": "not-supported"})
    with pytest.raises(Exception, match="does not support"):
        asyncio.run(OpenAIDecisionsEngine(Settings(openai_api_key="test-key")).evaluate(unsupported))
