from pathlib import Path
from typing import Any

import pytest

from scripts.trec_qrels_import import (
    CREATE_DEFINITION,
    IMPORT_METADATA,
    LIST_DEFINITIONS,
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
    assert rows[0].sampling_probability == 0.5
    assert skipped == {304: 1}


def test_load_qrels_rejects_rows_without_five_columns(tmp_path: Path) -> None:
    source = tmp_path / "qrels.txt"
    source.write_text("304 0 document 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expected 5 columns"):
        load_qrels(source, {304})


class FakeApi:
    def __init__(self) -> None:
        self.definitions: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, operation_id: str, **kwargs: Any) -> Any:
        self.calls.append((operation_id, kwargs))
        if operation_id == LIST_DEFINITIONS:
            return self.definitions
        if operation_id == CREATE_DEFINITION:
            definition = {
                **kwargs["json_body"],
                "id": f"definition-{len(self.definitions) + 1}",
                "status": "ACTIVE",
                "value_source": "ASSERTED",
            }
            self.definitions.append(definition)
            return definition
        if operation_id == RESOLVE_SOURCE_IDS:
            source_ids = kwargs["json_body"]["source_item_ids"]
            return {
                "items": [
                    {
                        "source_item_id": source_id,
                        "collection_item_id": f"item-{index}",
                    }
                    for index, source_id in enumerate(source_ids)
                ],
                "missing_source_item_ids": [],
            }
        if operation_id == IMPORT_METADATA:
            count = len(kwargs["json_body"]["items"])
            return {
                "created_event_count": count,
                "existing_event_count": 0,
                "missing_collection_item_ids": [],
                "warnings": [],
            }
        raise AssertionError(f"Unexpected operation: {operation_id}")


def test_qrels_importer_creates_matter_fields_and_imports_values(
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
        matter_id="matter-1",
        collection_id="collection-1",
    )

    report = importer.run(source, qrels, skipped)

    assert [definition["key"] for definition in api.definitions] == [
        "trec_topic_304_judgment",
        "trec_topic_304_sampling_probability",
    ]
    assert report.imported_judgment_events == 2
    assert report.imported_probability_events == 2
    assert report.missing_collection_document_ids == []
    assert report.missing_matter_document_ids == []
