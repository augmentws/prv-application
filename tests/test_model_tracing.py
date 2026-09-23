import json
import re

from app.config import Settings
from app.model_tracing import start_model_call_trace


def test_model_call_trace_records_request_validation_and_response(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        model_trace_enabled=True,
        model_trace_directory=str(tmp_path / "model-traces"),
    )
    trace = start_model_call_trace(
        settings,
        request_type="retrieval-planner",
        identifier="skill/run:123",
        request={"prompt": "matter content", "model_settings": {"temperature": 0}},
    )
    assert trace is not None
    trace.event("output_validation_failed", {"message": "queries is required", "output": {}})
    trace.complete({"output": {"queries": []}, "usage": {"requests": 2}})

    assert re.fullmatch(
        r"retrieval-planner-skill-run-123-\d{8}T\d{6}\.\d{6}Z\.json",
        trace.path.name,
    )
    assert trace.path.stat().st_mode & 0o777 == 0o600
    assert trace.path.parent.stat().st_mode & 0o777 == 0o700
    payload = json.loads(trace.path.read_text())
    assert payload["status"] == "COMPLETED"
    assert payload["request"]["prompt"] == "matter content"
    assert payload["events"][0]["type"] == "output_validation_failed"
    assert payload["response"]["usage"]["requests"] == 2


def test_model_call_trace_is_disabled_by_default(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        model_trace_enabled=False,
        model_trace_directory=str(tmp_path / "model-traces"),
    )
    trace = start_model_call_trace(
        settings,
        request_type="agent-turn",
        identifier="run-1",
        request={"prompt": "hello"},
    )
    assert trace is None
    assert not (tmp_path / "model-traces").exists()
