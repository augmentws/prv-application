from scripts.assessment_model_baseline import _schema_report


def test_google_baseline_reports_lossy_schema_admission() -> None:
    report = _schema_report("google:gemini-test")

    realtime_paths = {
        adjustment["path"]
        for adjustment in report["realtime_adjustments"]
        if adjustment["action"] == "REMOVED"
    }
    batch_paths = {
        adjustment["path"]
        for adjustment in report["batch_adjustments"]
        if adjustment["action"] == "REMOVED"
    }

    assert "$.$defs.paragraph_ids.maxItems" in realtime_paths
    assert "$.$defs.paragraph_ids.uniqueItems" in realtime_paths
    assert "$.$defs.paragraph_ids.items.pattern" in realtime_paths
    assert "$.$defs" in batch_paths
    assert "$.additionalProperties" in batch_paths


def test_non_google_baseline_preserves_full_schema() -> None:
    report = _schema_report("openai:gpt-test")

    assert report["realtime_adjustments"] == []
    assert "batch_adjustments" not in report
