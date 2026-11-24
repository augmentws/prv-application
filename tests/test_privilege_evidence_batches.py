import uuid
from types import SimpleNamespace

from app.config import get_settings
from app.privilege_evidence_batches import (
    _estimate_prepared_document,
    plan_privilege_evidence_batches,
)


def test_privilege_batch_estimate_repeats_family_context_per_unit() -> None:
    prepared = SimpleNamespace(
        revision=SimpleNamespace(content_markdown="guidance"),
        catalog=SimpleNamespace(model_dump=lambda **_kwargs: {"criteria": ["privilege"]}),
        documents=(
            SimpleNamespace(
                source=SimpleNamespace(text="source text"),
                document_context=SimpleNamespace(as_model_input=lambda: [{"value": "family context"}]),
                plan=SimpleNamespace(extraction_units=(object(), object(), object())),
            ),
        ),
    )

    unit_count, estimated_bytes = _estimate_prepared_document(prepared)

    assert unit_count == 3
    assert estimated_bytes > 3 * 50_000


def test_provider_without_batch_adapter_falls_back_to_realtime() -> None:
    assessment_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    document_ids = [uuid.uuid4(), uuid.uuid4()]
    assessment = SimpleNamespace(
        id=assessment_id,
        review_batch_run_id=review_run_id,
        use_batching=True,
        configuration_snapshot={
            "evidence_first_orchestration": {
                "stages": {"EXTRACTION": {"resolved_model": "anthropic:claude-test"}}
            }
        },
    )

    class Database:
        @staticmethod
        def get(_model, value):
            if value == assessment_id:
                return assessment
            if isinstance(value, tuple) and value[0] == review_run_id:
                return SimpleNamespace(status="QUEUED")
            return None

    result = plan_privilege_evidence_batches(
        Database(),  # type: ignore[arg-type]
        assessment_id=assessment_id,
        document_ids=document_ids,
        actor_user_id=uuid.uuid4(),
        settings=get_settings(),
    )

    assert result == {
        "provider_batch_ids": [],
        "realtime_document_ids": [str(value) for value in document_ids],
    }
