from pathlib import Path
from typing import Any

import pytest

from scripts.batch_coding_import import (
    GET_BATCH,
    GET_CURRENT_USER,
    GET_REVIEWER_CODING,
    LIST_BATCH_DOCUMENTS,
    REPLACE_VALUES,
)
from scripts.trec_qrels_import import (
    RESOLVE_SOURCE_IDS,
    TrecQrelsImporter,
    load_qrels,
)


def test_load_qrels_selects_topics_and_skips_unassessed(tmp_path: Path) -> None:
    source = tmp_path / "qrels.txt"
    source.write_text(
        "301 0 3.1007403.DKQYPWMOI55PSMBZWFTW5SDVR0GPGHLIB 1 0.25\n"
        "304 0 3.1008820.EJIZQLHKAFSFZR5YMM52V3M1LV5NWBHTB 0 0.5\n"
        "304 0 3.1008820.EJIZQLHKAFSFZR5YMM52V3M1LV5NWBHTB.1 -1 0.5\n",
        encoding="utf-8",
    )

    rows, skipped = load_qrels(source, {304})

    assert len(rows) == 1
    assert rows[0].topic == 304
    assert rows[0].relevance == 0
    assert not hasattr(rows[0], "sampling_probability")
    assert skipped == {304: 1}


def test_load_qrels_rejects_rows_without_five_columns(tmp_path: Path) -> None:
    source = tmp_path / "qrels.txt"
    source.write_text("304 0 document 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expected 5 columns"):
        load_qrels(source, {304})


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.matter_id = "10000000-0000-0000-0000-000000000001"
        self.batch_id = "20000000-0000-0000-0000-000000000001"
        self.collection_id = "30000000-0000-0000-0000-000000000001"
        self.definition_id = "40000000-0000-0000-0000-000000000001"
        self.reviewer_id = "50000000-0000-0000-0000-000000000001"
        self.coding_id = "60000000-0000-0000-0000-000000000001"
        self.source_to_collection_item: dict[str, str] = {}

    def request(self, operation_id: str, **kwargs: Any) -> Any:
        self.calls.append((operation_id, kwargs))
        if operation_id == GET_CURRENT_USER:
            return {"id": self.reviewer_id, "email": "importer@example.com"}
        if operation_id == GET_BATCH:
            return {
                "id": self.batch_id,
                "name": "TREC privilege evaluation",
                "status": "READY",
                "assigned_user": {
                    "id": self.reviewer_id,
                    "email": "importer@example.com",
                },
                "coding_groups": [
                    {
                        "fields": [
                            {
                                "metadata_definition_id": self.definition_id,
                                "definition_snapshot": {
                                    "key": "trec_priv_coding",
                                    "type": "BOOLEAN",
                                    "cardinality": "SINGLE",
                                },
                            }
                        ]
                    }
                ],
            }
        if operation_id == RESOLVE_SOURCE_IDS:
            source_ids = kwargs["json_body"]["source_item_ids"]
            self.source_to_collection_item = {
                source_id: f"70000000-0000-0000-0000-{index:012d}"
                for index, source_id in enumerate(source_ids, start=1)
            }
            return {
                "items": [
                    {
                        "source_item_id": source_id,
                        "collection_item_id": self.source_to_collection_item[source_id],
                    }
                    for source_id in source_ids
                ],
                "missing_source_item_ids": [],
            }
        if operation_id == LIST_BATCH_DOCUMENTS:
            return [
                {
                    "matter_document_id": f"80000000-0000-0000-0000-{index:012d}",
                    "collection_item_id": collection_item_id,
                }
                for index, collection_item_id in enumerate(
                    self.source_to_collection_item.values(), start=1
                )
            ]
        if operation_id == GET_REVIEWER_CODING:
            return {"id": self.coding_id}
        if operation_id == REPLACE_VALUES:
            return []
        raise AssertionError(f"Unexpected operation: {operation_id}")


def test_qrels_importer_writes_importing_users_reviewer_coding(
    tmp_path: Path,
) -> None:
    source = tmp_path / "qrels.txt"
    source.write_text(
        "304 0 3.1007403.DKQYPWMOI55PSMBZWFTW5SDVR0GPGHLIB 1 0.25\n"
        "304 0 3.1008820.EJIZQLHKAFSFZR5YMM52V3M1LV5NWBHTB 0 0.5\n",
        encoding="utf-8",
    )
    qrels, skipped = load_qrels(source, {304})
    api = FakeApi()
    importer = TrecQrelsImporter(
        api,  # type: ignore[arg-type]
        matter_id=api.matter_id,
        collection_id=api.collection_id,
        batch_id=api.batch_id,
    )

    plan, report = importer.run(qrels, skipped)

    assert plan.reviewer["id"] == api.reviewer_id
    assert report.reviewer_user_id == api.reviewer_id
    assert report.imported_documents == 2
    coding_imports = [
        kwargs
        for operation, kwargs in api.calls
        if operation == REPLACE_VALUES
    ]
    assert [call["json_body"]["fields"][0]["values"][0] for call in coding_imports] == [
        True,
        False,
    ]
    assert all(
        call["json_body"]["fields"][0]["metadata_definition_id"]
        == api.definition_id
        for call in coding_imports
    )
    assert all(call["json_body"]["origin"] == "IMPORT" for call in coding_imports)
