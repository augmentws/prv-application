from pathlib import Path

import pytest

from scripts.import_client.__main__ import dry_run
from scripts.import_client.adapters.edrm_enron_v2 import EdrmEnronV2Adapter
from scripts.import_client.importer import BaseImporter


def _write_document(package: Path, name: str, content: str) -> None:
    text_dir = package / "text_000"
    text_dir.mkdir(parents=True, exist_ok=True)
    (text_dir / name).write_text(content, encoding="utf-8")


def test_edrm_enron_v2_preserves_trec_ids_and_family_relationships(
    tmp_path: Path,
) -> None:
    package = tmp_path / "edrm-enron-v2_kean-s_xml_1of8.zip"
    message_id = "3.1008820.EJIZQLHKAFSFZR5YMM52V3M1LV5NWBHTB"
    _write_document(
        package,
        f"{message_id}.txt",
        "From: sender@example.com\nTo: recipient@example.com\nSubject: Test\n\nBody",
    )
    _write_document(package, f"{message_id}.10.txt", "Attachment ten")
    _write_document(package, f"{message_id}.2.txt", "Attachment two")

    adapter = EdrmEnronV2Adapter(tmp_path)
    container = next(iter(adapter.source_containers()))
    items = list(BaseImporter.iter_preprocessed_items(adapter, container))

    assert [item.source_item_id for item in items] == [
        message_id,
        f"{message_id}.2",
        f"{message_id}.10",
    ]
    assert items[0].record_type == "EMAIL"
    assert items[0].email is not None
    assert items[0].email.subject == "Test"
    assert items[0].parent_source_item_id is None
    assert items[1].record_type == "FILE"
    assert items[1].parent_source_item_id == message_id
    assert items[0].family_id == items[1].family_id == items[2].family_id
    assert items[0].custodians[0].display_name == "kean-s"
    assert items[0].unmapped_metadata == {}
    assert items[1].raw_metadata["attachment_ordinal"] == 2
    assert items[1].raw_metadata["trec_document_id"] == f"{message_id}.2"


def test_edrm_enron_v2_dry_run_counts_extracted_bytes(tmp_path: Path) -> None:
    package = tmp_path / "edrm-enron-v2_corman-s_xml.zip"
    content = "Subject: Example\n\nBody"
    _write_document(
        package,
        "3.1173584.AMF0JQN1FHSYBREAQH34QVK5FXZ1WTXOA.txt",
        content,
    )
    adapter = EdrmEnronV2Adapter(tmp_path)

    report = dry_run(
        adapter,
        None,
        include_attachments=True,
    )

    assert report["source_containers"] == 1
    assert report["source_container_bytes"] == len(content)
    assert report["items"] == 1
    assert report["record_types"] == {"EMAIL": 1}
    assert report["custodians"] == ["corman-s"]


def test_edrm_enron_v2_rejects_unknown_document_filename(tmp_path: Path) -> None:
    package = tmp_path / "edrm-enron-v2_corman-s_xml.zip"
    _write_document(package, "not-a-trec-id.txt", "content")
    adapter = EdrmEnronV2Adapter(tmp_path)
    container = next(iter(adapter.source_containers()))

    with pytest.raises(ValueError, match="Invalid EDRM Enron v2 document filename"):
        list(BaseImporter.iter_preprocessed_items(adapter, container))


def test_edrm_enron_v2_retains_attachment_when_parent_text_is_missing(
    tmp_path: Path,
) -> None:
    package = tmp_path / "edrm-enron-v2_love-p_xml.zip"
    message_id = "3.479016.OXCIPKCPHSFIBBUNXUAFWZPLM4BZFZB0B"
    _write_document(package, f"{message_id}.8.txt", "Orphan attachment text")
    adapter = EdrmEnronV2Adapter(tmp_path)
    container = next(iter(adapter.source_containers()))

    items = list(BaseImporter.iter_preprocessed_items(adapter, container))

    assert len(items) == 1
    assert items[0].record_type == "FILE"
    assert items[0].parent_source_item_id is None
    assert items[0].raw_metadata["parent_document_missing"] is True
