import uuid
from types import SimpleNamespace

from app import privilege_adjudication_batches


def test_adjudication_batch_planner_partitions_family_packets(monkeypatch) -> None:
    review_run_id = uuid.uuid4()
    assessment_id = uuid.uuid4()
    review_run = SimpleNamespace(
        id=review_run_id,
        workflow_run_record_id=uuid.uuid4(),
        configuration_snapshot={"source_evidence_assessment_id": str(assessment_id)},
    )
    target = SimpleNamespace(provider="google", model="gemini-test")
    added = []

    class Database:
        @staticmethod
        def scalars(_statement):
            return []

        @staticmethod
        def add(value):
            added.append(value)

        @staticmethod
        def flush():
            for value in added:
                value.id = value.id or uuid.uuid4()

        @staticmethod
        def commit():
            return None

        @staticmethod
        def get(_model, _identifier):
            return None

    monkeypatch.setattr(privilege_adjudication_batches, "_review_run", lambda _db, _id: review_run)
    monkeypatch.setattr(
        privilege_adjudication_batches,
        "_prepare",
        lambda *_args, **_kwargs: SimpleNamespace(resolved_model="google:gemini-test"),
    )
    monkeypatch.setattr(
        privilege_adjudication_batches,
        "_model_packet_estimate",
        lambda _prepared: (2, 1_000),
    )
    monkeypatch.setattr(
        privilege_adjudication_batches,
        "resolve_batch_target",
        lambda _model: ("google:gemini-test", target),
    )
    first, second = uuid.uuid4(), uuid.uuid4()

    result = privilege_adjudication_batches.plan_privilege_adjudication_batches(
        Database(),  # type: ignore[arg-type]
        review_batch_run_id=review_run_id,
        documents=[
            {"matter_document_id": str(first), "observations_artifact_id": str(uuid.uuid4())},
            {"matter_document_id": str(second), "observations_artifact_id": str(uuid.uuid4())},
        ],
        actor_user_id=uuid.uuid4(),
        settings=SimpleNamespace(definition_assessment_provider_batch_size=3),  # type: ignore[arg-type]
    )

    assert len(result["provider_batch_ids"]) == 2
    assert result["realtime_anchor_document_ids"] == []
    assert [value.document_ids for value in added] == [[str(first)], [str(second)]]
    assert all(
        value.manifest["mode"]
        == privilege_adjudication_batches.PRIVILEGE_ADJUDICATION_BATCH_MODE
        for value in added
    )


def test_adjudication_batch_planner_keeps_deterministic_family_local(monkeypatch) -> None:
    review_run_id = uuid.uuid4()
    assessment_id = uuid.uuid4()
    review_run = SimpleNamespace(
        id=review_run_id,
        workflow_run_record_id=uuid.uuid4(),
        configuration_snapshot={"source_evidence_assessment_id": str(assessment_id)},
    )

    class Database:
        @staticmethod
        def scalars(_statement):
            return []

        @staticmethod
        def flush():
            return None

        @staticmethod
        def commit():
            return None

        @staticmethod
        def get(_model, _identifier):
            return None

    monkeypatch.setattr(privilege_adjudication_batches, "_review_run", lambda _db, _id: review_run)
    monkeypatch.setattr(
        privilege_adjudication_batches,
        "_prepare",
        lambda *_args, **_kwargs: SimpleNamespace(resolved_model="google:gemini-test"),
    )
    monkeypatch.setattr(privilege_adjudication_batches, "_model_packet_estimate", lambda _prepared: (0, 0))
    monkeypatch.setattr(
        privilege_adjudication_batches,
        "resolve_batch_target",
        lambda _model: ("google:gemini-test", SimpleNamespace(provider="google", model="gemini-test")),
    )
    anchor = uuid.uuid4()

    result = privilege_adjudication_batches.plan_privilege_adjudication_batches(
        Database(),  # type: ignore[arg-type]
        review_batch_run_id=review_run_id,
        documents=[{"matter_document_id": str(anchor), "observations_artifact_id": str(uuid.uuid4())}],
        actor_user_id=uuid.uuid4(),
        settings=SimpleNamespace(definition_assessment_provider_batch_size=100),  # type: ignore[arg-type]
    )

    assert result == {
        "provider_batch_ids": [],
        "realtime_anchor_document_ids": [str(anchor)],
    }
