from app.model_batching import GoogleBatchProvider, resolve_batch_target


def test_batch_provider_resolution_is_capability_based() -> None:
    resolved = resolve_batch_target("google:gemini-3.5-flash-lite")

    assert resolved is not None
    adapter, target = resolved
    assert isinstance(adapter, GoogleBatchProvider)
    assert target.provider == "google"
    assert target.model == "gemini-3.5-flash-lite"


def test_models_without_a_batch_adapter_remain_eligible_for_realtime_execution() -> None:
    assert resolve_batch_target("openai:gpt-5.4-mini") is None
    assert resolve_batch_target("anthropic:claude-sonnet-4-6") is None
