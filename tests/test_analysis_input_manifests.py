import io
import uuid
from dataclasses import replace

import pyarrow.parquet as pq
import pytest

from app.analysis_input_manifests import (
    ANALYSIS_INPUT_MANIFEST_SCHEMA,
    AnalysisInputContract,
    AnalysisInputDocument,
    AnalysisInputExtensionField,
    AnalysisInputManifestError,
    prepare_analysis_input_manifest,
)


def _document(ordinal: int, *, document_id: uuid.UUID | None = None) -> AnalysisInputDocument:
    return AnalysisInputDocument(
        matter_document_id=document_id or uuid.uuid4(),
        collection_item_id=uuid.uuid4(),
        source_collection_id=uuid.uuid4(),
        scope_ordinal=ordinal,
        resolved_inputs={"document": {"file_date": "2026-10-15T00:00:00Z"}},
        extensions={"family_id": None},
    )


def _contract() -> AnalysisInputContract:
    return AnalysisInputContract(
        key="test.document-input",
        version=1,
        resolver_version="test-resolver-v1",
        required_input_paths=["document.file_date"],
        extension_fields=[AnalysisInputExtensionField(name="family_id", type="STRING")],
    )


def test_prepare_analysis_input_manifest_is_ordered_and_self_describing() -> None:
    scope_id = uuid.uuid4()
    second = _document(2)
    first = _document(1)

    prepared = prepare_analysis_input_manifest(
        [second, first],
        contract=_contract(),
        scope_type="REVIEW_BATCH",
        scope_id=scope_id,
    )

    parquet = pq.ParquetFile(io.BytesIO(prepared.content))
    rows = parquet.read().to_pylist()
    metadata = parquet.schema_arrow.metadata or {}
    assert [row["matter_document_id"] for row in rows] == [
        str(first.matter_document_id),
        str(second.matter_document_id),
    ]
    assert all(len(row["input_content_hash"]) == 64 for row in rows)
    assert metadata[b"pvr.schema"].decode() == ANALYSIS_INPUT_MANIFEST_SCHEMA
    assert metadata[b"pvr.contract_hash"].decode() == prepared.contract_hash
    assert metadata[b"pvr.scope_id"].decode() == str(scope_id)
    assert prepared.item_count == 2
    assert prepared.byte_length == len(prepared.content)


def test_prepare_analysis_input_manifest_rejects_duplicate_documents() -> None:
    document_id = uuid.uuid4()
    with pytest.raises(AnalysisInputManifestError, match="only once"):
        prepare_analysis_input_manifest(
            [_document(1, document_id=document_id), _document(2, document_id=document_id)],
            contract=_contract(),
            scope_type="MATTER",
            scope_id=uuid.uuid4(),
        )


def test_prepare_analysis_input_manifest_requires_declared_extensions() -> None:
    document = _document(1)
    invalid = AnalysisInputDocument(
        matter_document_id=document.matter_document_id,
        collection_item_id=document.collection_item_id,
        source_collection_id=document.source_collection_id,
        scope_ordinal=1,
    )
    with pytest.raises(AnalysisInputManifestError, match="extension fields"):
        prepare_analysis_input_manifest(
            [invalid],
            contract=_contract(),
            scope_type="MATTER",
            scope_id=uuid.uuid4(),
        )


def test_ready_rows_require_declared_inputs_but_missing_rows_preserve_population() -> None:
    contract = AnalysisInputContract(
        key="test.document-text",
        version=1,
        resolver_version="test-resolver-v1",
        required_input_paths=["document.text"],
        source_artifact_required=True,
        extension_fields=[AnalysisInputExtensionField(name="family_id", type="STRING")],
    )
    ready_without_required_path = replace(
        _document(1),
        source_artifact_id=uuid.uuid4(),
        source_content_hash="0" * 64,
    )
    with pytest.raises(AnalysisInputManifestError, match="required input path document.text"):
        prepare_analysis_input_manifest(
            [ready_without_required_path],
            contract=contract,
            scope_type="MATTER",
            scope_id=uuid.uuid4(),
        )

    missing = replace(
        _document(1),
        input_status="MISSING",
        failure_code="SOURCE_TEXT_MISSING",
    )
    prepared = prepare_analysis_input_manifest(
        [missing],
        contract=contract,
        scope_type="MATTER",
        scope_id=uuid.uuid4(),
    )
    assert pq.read_table(io.BytesIO(prepared.content)).to_pylist()[0]["input_status"] == "MISSING"
