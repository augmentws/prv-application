from pathlib import Path
from typing import Any

from scripts.trec_matter_publish import (
    CREATE_DOCUMENT_IMPORT,
    CollectionIdentity,
    build_manifest,
    publish_manifest,
)
from scripts.trec_qrels_import import load_qrels


def _document_id(number: int) -> str:
    return f"3.{number}.ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789"


def test_build_manifest_includes_all_coded_and_deterministic_uncoded_sample(
    tmp_path: Path,
) -> None:
    source = tmp_path / "qrels.txt"
    source.write_text(
        f"304 0 {_document_id(1)} 1 0.25\n304 0 {_document_id(2)} 0 0.50\n304 0 {_document_id(3)} -1 0.75\n",
        encoding="utf-8",
    )
    qrels, skipped = load_qrels(source, {304})
    identities = [
        CollectionIdentity(item_id=f"item-{number}", source_item_id=_document_id(number)) for number in range(1, 9)
    ]

    first = build_manifest(
        identities=iter(identities),
        qrels=qrels,
        skipped=skipped,
        source=source,
        topic=304,
        collection_id="collection-1",
        matter_id="matter-1",
        uncoded_count=3,
        seed="repeatable-seed",
    )
    second = build_manifest(
        identities=iter(reversed(identities)),
        qrels=qrels,
        skipped=skipped,
        source=source,
        topic=304,
        collection_id="collection-1",
        matter_id="matter-1",
        uncoded_count=3,
        seed="repeatable-seed",
    )

    assert first == second
    assert first["selected_count"] == 5
    assert first["qrels"]["coded_count"] == 2
    assert first["qrels"]["unassessed_rows"] == 1
    coded = [item for item in first["items"] if item["partition"] == "coded"]
    uncoded = [item for item in first["items"] if item["partition"] == "uncoded_random"]
    assert {item["source_item_id"] for item in coded} == {
        _document_id(1),
        _document_id(2),
    }
    assert all("sampling_probability" not in item for item in coded)
    assert len(uncoded) == 3
    assert not {item["source_item_id"] for item in uncoded} & {
        _document_id(1),
        _document_id(2),
    }


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, operation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((operation_id, kwargs))
        return {"id": "import-job-1", "status": "QUEUED"}


def test_publish_manifest_creates_one_explicit_matter_import() -> None:
    manifest = {
        "topic": 304,
        "matter_id": "matter-1",
        "collection_id": "collection-1",
        "qrels": {"coded_count": 2},
        "sampling": {"uncoded_selected_count": 1},
        "items": [
            {"collection_item_id": "item-1"},
            {"collection_item_id": "item-2"},
            {"collection_item_id": "item-3"},
        ],
    }
    api = FakeApi()

    result = publish_manifest(api, manifest, "a" * 64)  # type: ignore[arg-type]

    assert result["id"] == "import-job-1"
    assert api.calls[0][0] == CREATE_DOCUMENT_IMPORT
    assert api.calls[0][1]["path_params"] == {"matter_id": "matter-1"}
    assert api.calls[0][1]["json_body"]["selection"] == {
        "mode": "EXPLICIT",
        "item_ids": ["item-1", "item-2", "item-3"],
    }
