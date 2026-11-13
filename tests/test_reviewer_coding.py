import uuid
from types import SimpleNamespace

from app.models import (
    ReviewBatchReviewerDocumentRevision,
    ReviewBatchReviewerFieldRevision,
    ReviewBatchReviewerRevision,
)
from app.reviewer_coding import ReviewerFieldReplacement, append_reviewer_coding_revision


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_snapshots: list[tuple[type[object], ...]] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        self.flush_snapshots.append(tuple(type(value) for value in self.added))

    def execute(self, _statement: object) -> None:
        return None

    def get(self, _model: object, _identity: object) -> None:
        return None


def test_reviewer_revision_dependencies_are_flushed_parent_first() -> None:
    session = RecordingSession()
    coding = SimpleNamespace(
        id=uuid.uuid4(),
        review_batch_id=uuid.uuid4(),
        reviewer_user_id=uuid.uuid4(),
        revision=0,
    )
    definition = SimpleNamespace(
        id=uuid.uuid4(),
        display_name="Privilege",
        type="BOOLEAN",
        cardinality="SINGLE",
    )

    append_reviewer_coding_revision(
        session,  # type: ignore[arg-type]
        coding=coding,
        matter_document_id=uuid.uuid4(),
        status="COMPLETED",
        field_replacements=(
            ReviewerFieldReplacement(definition=definition, values=(False,)),  # type: ignore[arg-type]
        ),
        actor_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        matter_id=uuid.uuid4(),
        origin="IMPORT",
    )

    assert len(session.flush_snapshots) == 3
    assert ReviewBatchReviewerRevision in session.flush_snapshots[0]
    assert ReviewBatchReviewerDocumentRevision not in session.flush_snapshots[0]
    assert ReviewBatchReviewerDocumentRevision in session.flush_snapshots[1]
    assert ReviewBatchReviewerFieldRevision not in session.flush_snapshots[1]
    assert ReviewBatchReviewerFieldRevision in session.flush_snapshots[2]
