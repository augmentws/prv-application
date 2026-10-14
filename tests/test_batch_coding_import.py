import csv
import uuid
from pathlib import Path
from typing import Any

import pytest

from scripts.batch_coding_import import (
    GET_REVIEWER_CODING,
    GET_BATCH,
    GET_CURRENT_USER,
    LIST_BATCH_DOCUMENTS,
    REPLACE_VALUES,
    SKIP_DOCUMENT,
    BatchCodingImporter,
    load_coding_rows,
)


def batch_fixture() -> dict[str, Any]:
    return {
        "id": "batch-1",
        "name": "Imported review",
        "status": "READY",
        "assigned_user": {"id": "user-1", "email": "reviewer@example.com"},
        "coding_groups": [{
            "display_name": "Review coding",
            "fields": [
                {
                    "metadata_definition_id": "11111111-1111-1111-1111-111111111111",
                    "definition_snapshot": {
                        "key": "responsiveness",
                        "display_name": "Responsiveness",
                        "type": "ENUM",
                        "cardinality": "SINGLE",
                        "allowed_values": [
                            {"key": "responsive", "label": "Responsive", "active": True},
                            {"key": "not_responsive", "label": "Not responsive", "active": True},
                        ],
                    },
                },
                {
                    "metadata_definition_id": "22222222-2222-2222-2222-222222222222",
                    "definition_snapshot": {
                        "key": "issue_coding",
                        "display_name": "Issue Coding",
                        "type": "ENUM",
                        "cardinality": "MULTIPLE",
                        "allowed_values": [
                            {"key": "topic_401", "label": "Topic 401", "active": True},
                            {"key": "topic_402", "label": "Topic 402", "active": True},
                        ],
                    },
                },
            ],
        }],
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_load_coding_rows_resolves_field_names_enum_labels_and_skips(tmp_path: Path) -> None:
    document_one = str(uuid.uuid4())
    document_two = str(uuid.uuid4())
    source = tmp_path / "coding.csv"
    write_csv(source, [
        {
            "matter_document_id": document_one,
            "Responsiveness": "Responsive",
            "issue_coding": '["Topic 401","topic_402"]',
            "_review_status": "COMPLETED",
        },
        {
            "matter_document_id": document_two,
            "Responsiveness": "",
            "issue_coding": "",
            "_review_status": "SKIPPED",
        },
    ])

    rows = load_coding_rows(source, batch_fixture())

    assert rows[0].document_id == document_one
    assert rows[0].fields == (
        {"metadata_definition_id": "11111111-1111-1111-1111-111111111111", "values": ["responsive"]},
        {
            "metadata_definition_id": "22222222-2222-2222-2222-222222222222",
            "values": ["topic_401", "topic_402"],
        },
    )
    assert rows[1].review_status == "SKIPPED"


def test_load_coding_rows_rejects_duplicate_documents_and_values_on_skips(tmp_path: Path) -> None:
    document_id = str(uuid.uuid4())
    source = tmp_path / "coding.csv"
    write_csv(source, [
        {
            "matter_document_id": document_id,
            "responsiveness": "Responsive",
            "_review_status": "SKIPPED",
        },
    ])
    with pytest.raises(ValueError, match="skipped documents cannot contain coding values"):
        load_coding_rows(source, batch_fixture())

    write_csv(source, [
        {"matter_document_id": document_id, "responsiveness": "Responsive"},
        {"matter_document_id": document_id, "responsiveness": "Not responsive"},
    ])
    with pytest.raises(ValueError, match="duplicate matter_document_id"):
        load_coding_rows(source, batch_fixture())


class FakeApi:
    def __init__(self, batch: dict[str, Any], document_ids: set[str]) -> None:
        self.batch = batch
        self.document_ids = document_ids
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, operation_id: str, **kwargs: Any) -> Any:
        self.calls.append((operation_id, kwargs))
        if operation_id == GET_CURRENT_USER:
            return {"id": "user-1", "email": "reviewer@example.com"}
        if operation_id == GET_BATCH:
            return self.batch
        if operation_id == LIST_BATCH_DOCUMENTS:
            return [
                {"matter_document_id": document_id}
                for document_id in kwargs["params"]["document_id"]
                if document_id in self.document_ids
            ]
        if operation_id == GET_REVIEWER_CODING:
            return {"id": "coding-1", "reviewer_user_id": "user-1", "revision": 0}
        if operation_id in {REPLACE_VALUES, SKIP_DOCUMENT}:
            return []
        raise AssertionError(f"Unexpected operation: {operation_id}")


def test_importer_validates_membership_and_updates_canonical_reviewer_coding(tmp_path: Path) -> None:
    document_one = str(uuid.uuid4())
    document_two = str(uuid.uuid4())
    source = tmp_path / "coding.csv"
    write_csv(source, [
        {"matter_document_id": document_one, "responsiveness": "Responsive"},
        {"matter_document_id": document_two, "responsiveness": "Not responsive"},
    ])
    api = FakeApi(batch_fixture(), {document_one, document_two})
    importer = BatchCodingImporter(api, matter_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()))  # type: ignore[arg-type]
    reviewer, batch = importer.load_scope()
    rows = load_coding_rows(source, batch)
    importer.validate_membership(rows)

    report = importer.import_rows(
        reviewer,
        rows,
        continue_on_error=False,
    )

    assert report.reviewer_coding_id == "coding-1"
    assert report.imported_documents == 2
    assert [operation for operation, _ in api.calls].count(REPLACE_VALUES) == 2
    assert any(
        operation == GET_REVIEWER_CODING
        for operation, kwargs in api.calls
    )
    assert api.calls[-1][0] == REPLACE_VALUES


def test_importer_rejects_documents_outside_batch(tmp_path: Path) -> None:
    document_id = str(uuid.uuid4())
    source = tmp_path / "coding.csv"
    write_csv(source, [{"matter_document_id": document_id, "responsiveness": "Responsive"}])
    api = FakeApi(batch_fixture(), set())
    importer = BatchCodingImporter(api, matter_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()))  # type: ignore[arg-type]
    _, batch = importer.load_scope()
    rows = load_coding_rows(source, batch)

    with pytest.raises(ValueError, match="not in this batch"):
        importer.validate_membership(rows)
    assert not any(operation == GET_REVIEWER_CODING for operation, _ in api.calls)
