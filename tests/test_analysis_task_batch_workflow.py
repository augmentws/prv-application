from app.analysis_tasks import MatterAnalysisTaskConflict, MatterAnalysisTaskInputTooLarge
from app.decision_engine import DecisionEngineProviderError
from app.workflows.analysis_task_batch import (
    BATCH_QUEUE,
    DOCUMENT_QUEUE,
    JEV_BATCH_QUEUE,
    JEV_DOCUMENT_QUEUE,
    OPENAI_BATCH_QUEUE,
    OPENAI_DOCUMENT_QUEUE,
    _batch_queue_for_engine,
    _document_queue_for_engine,
    _failure_code,
    _failure_is_retryable,
    _failure_message,
    _should_retry_document,
)


def test_document_queues_isolate_provider_backpressure() -> None:
    assert _batch_queue_for_engine("jev") is JEV_BATCH_QUEUE
    assert _batch_queue_for_engine("OPENAI-DECISIONS") is OPENAI_BATCH_QUEUE
    assert _batch_queue_for_engine("future-provider") is BATCH_QUEUE
    assert _document_queue_for_engine("jev") is JEV_DOCUMENT_QUEUE
    assert _document_queue_for_engine("OPENAI-DECISIONS") is OPENAI_DOCUMENT_QUEUE
    assert _document_queue_for_engine("future-provider") is DOCUMENT_QUEUE


def test_document_retry_policy_rejects_nonretryable_provider_errors() -> None:
    error = DecisionEngineProviderError(
        "maximum token limit exceeded",
        provider="typesafe",
        code="HTTP_400",
        retryable=False,
    )

    assert _should_retry_document(error) is False


def test_document_retry_policy_allows_retryable_provider_errors() -> None:
    error = DecisionEngineProviderError(
        "provider unavailable",
        provider="typesafe",
        code="HTTP_503",
        retryable=True,
    )

    assert _should_retry_document(error) is True


def test_document_retry_policy_rejects_local_conflicts() -> None:
    assert _should_retry_document(MatterAnalysisTaskConflict("invalid document state")) is False


def test_oversized_document_is_manually_retryable_without_immediate_step_retry() -> None:
    error = MatterAnalysisTaskInputTooLarge(
        actual_characters=150_000,
        maximum_characters=100_000,
    )

    assert _should_retry_document(error) is False
    assert _failure_is_retryable(error) is True
    assert _failure_code(error) == "ANALYSIS_DOCUMENT_INPUT_TOO_LARGE"
    assert _failure_message(error) == (
        "Document decision input is 150,000 characters, exceeding the configured single-request limit of "
        "100,000; increase ANALYSIS_TASK_DECISION_MAX_CHARACTERS and retry, or enable windowed evaluation"
    )


def test_document_failure_details_preserve_provider_reason_and_retryability() -> None:
    error = DecisionEngineProviderError(
        " provider temporarily unavailable\nrequest failed ",
        provider="openai-decisions",
        code="HTTP_503",
        retryable=True,
    )

    assert _failure_code(error) == "OPENAI_DECISIONS_HTTP_503"
    assert _failure_message(error) == "provider temporarily unavailable request failed"
    assert _should_retry_document(error) is True
