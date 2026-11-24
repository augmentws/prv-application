import json
import uuid
from types import SimpleNamespace

import pyarrow.parquet as pq

from app.coding_comparisons import _agreement, _mapped_values, _reviewer_states, _summary, _write_parquet
from app.schemas import MatterCodingComparisonCreate
from artifact_service.workflow_artifact_types import ArtifactType
from artifact_service.workflow_artifacts import COLUMNAR_ARTIFACT_TYPES


def test_cross_field_mapping_normalizes_boolean_and_enum_values() -> None:
    state, values = _mapped_values(["true"], {"true": "PRIVILEGED", "false": "NOT_PRIVILEGED"})

    assert state == "EVALUATED"
    assert values == ["PRIVILEGED"]
    assert _mapped_values(["priv"], {"priv": "PRIVILEGED"}) == ("EVALUATED", ["PRIVILEGED"])


def test_unmapped_values_are_not_silently_treated_as_negative() -> None:
    assert _mapped_values(["unexpected"], {"true": "PRIVILEGED"}) == ("UNMAPPED", [])


def test_reviewer_boolean_values_are_loaded_from_pinned_coding_and_mapped(monkeypatch) -> None:
    privileged_id = uuid.uuid4()
    not_privileged_id = uuid.uuid4()
    coding_id = uuid.uuid4()

    def fake_resolve(*_args, **kwargs):
        assert kwargs["reviewer_coding_id"] == coding_id
        assert kwargs["revision"] == 25
        return SimpleNamespace(
            documents=(
                SimpleNamespace(matter_document_id=privileged_id, status="COMPLETED"),
                SimpleNamespace(matter_document_id=not_privileged_id, status="COMPLETED"),
            ),
            values=(
                SimpleNamespace(matter_document_id=privileged_id, value_boolean=True),
                SimpleNamespace(matter_document_id=not_privileged_id, value_boolean=False),
            ),
        )

    monkeypatch.setattr("app.coding_comparisons.resolve_reviewer_coding_as_of", fake_resolve)

    states = _reviewer_states(
        object(),
        {privileged_id, not_privileged_id},
        {
            "metadata_definition_id": str(uuid.uuid4()),
            "reviewer_codings": [{"id": str(coding_id), "revision": 25}],
            "value_mapping": {"true": "privileged", "false": "not_privileged"},
        },
    )

    assert states[privileged_id] == {
        "state": "EVALUATED",
        "raw_value_keys": ["true"],
        "value_keys": ["privileged"],
    }
    assert states[not_privileged_id] == {
        "state": "EVALUATED",
        "raw_value_keys": ["false"],
        "value_keys": ["not_privileged"],
    }


def test_incomplete_source_state_excludes_document_from_agreement() -> None:
    sources = [
        {"state": "EVALUATED", "value_keys": ["PRIVILEGED"]},
        {"state": "MISSING", "value_keys": []},
    ]

    assert _agreement(sources) == "INCOMPLETE"


def test_summary_keeps_conflicts_and_missing_values_visible() -> None:
    sources = [
        {"source_id": "qa", "label": "Q&A", "source_type": "QA_RUN_GROUP"},
        {"source_id": "human", "label": "Human", "source_type": "MATTER_FIELD"},
    ]
    rows = [
        {
            "agreement_status": "INCOMPLETE",
            "sources": [
                {"source_id": "qa", "state": "CONFLICTED", "value_keys": []},
                {"source_id": "human", "state": "MISSING", "value_keys": []},
            ],
        }
    ]

    summary = _summary(rows, sources)

    assert summary["agreement_counts"] == {"INCOMPLETE": 1}
    assert summary["sources"][0]["state_counts"] == {"CONFLICTED": 1}
    assert summary["sources"][1]["state_counts"] == {"MISSING": 1}
    assert summary["multi_source"]["compared_document_count"] == 0
    assert summary["multi_source"]["incomplete_document_count"] == 1


def test_summary_provides_batch_statistics_for_canonical_values() -> None:
    sources = [
        {"source_id": "qa", "label": "Q&A", "source_type": "QA_RUN_GROUP"},
        {"source_id": "human", "label": "Human", "source_type": "MATTER_FIELD"},
    ]
    rows = [
        {
            "agreement_status": "EXACT",
            "sources": [
                {"source_id": "qa", "state": "EVALUATED", "value_keys": ["PRIVILEGED"]},
                {"source_id": "human", "state": "EVALUATED", "value_keys": ["PRIVILEGED"]},
            ],
        },
        {
            "agreement_status": "DISAGREEMENT",
            "sources": [
                {"source_id": "qa", "state": "EVALUATED", "value_keys": []},
                {"source_id": "human", "state": "EVALUATED", "value_keys": ["PRIVILEGED"]},
            ],
        },
    ]

    summary = _summary(rows, sources)

    assert summary["sources"][0]["evaluated_document_count"] == 2
    assert summary["sources"][0]["no_value_document_count"] == 1
    assert summary["pairwise"][0]["exact_agreement"] == 0.5
    assert summary["pairwise"][0]["mean_jaccard"] == 0.5
    assert summary["pairwise"][0]["confusion_matrix"] == [
        {"left_value": "PRIVILEGED", "right_value": "PRIVILEGED", "document_count": 1},
        {"left_value": "__NO_VALUE__", "right_value": "PRIVILEGED", "document_count": 1},
    ]
    assert summary["multi_source"]["class_agreement"] == [
        {
            "value_key": "PRIVILEGED",
            "source_counts": [
                {"source_id": "qa", "document_count": 1},
                {"source_id": "human", "document_count": 2},
            ],
            "all_selected_count": 1,
            "any_selected_count": 2,
            "jaccard": 0.5,
        }
    ]


def test_result_matrix_is_columnar_artifact_with_no_core_row_model() -> None:
    comparison_id = uuid.uuid4()
    document_id = uuid.uuid4()
    item_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    stream = _write_parquet(
        [
            {
                "ordinal": 1,
                "matter_document_id": document_id,
                "collection_item_id": item_id,
                "batch_ids": [batch_id],
                "agreement_status": "EXACT",
                "sources": [
                    {
                        "source_id": "qa",
                        "state": "EVALUATED",
                        "raw_value_keys": ["true"],
                        "value_keys": ["PRIVILEGED"],
                    }
                ],
            }
        ],
        kind="result",
        comparison_id=comparison_id,
    )
    try:
        table = pq.read_table(stream)
    finally:
        stream.close()

    row = table.to_pylist()[0]
    assert row["matter_document_id"] == str(document_id)
    assert json.loads(row["batch_ids_json"]) == [str(batch_id)]
    assert json.loads(row["sources_json"])[0]["value_keys"] == ["PRIVILEGED"]
    assert ArtifactType.CODING_COMPARISON_POPULATION in COLUMNAR_ARTIFACT_TYPES
    assert ArtifactType.CODING_COMPARISON_RESULT in COLUMNAR_ARTIFACT_TYPES


def test_create_contract_requires_distinct_sources_and_allows_cross_field_mapping() -> None:
    payload = MatterCodingComparisonCreate.model_validate(
        {
            "name": "Privilege comparison",
            "review_batch_ids": [str(uuid.uuid4())],
            "idempotency_key": "request-1",
            "sources": [
                {
                    "source_id": "qa",
                    "source_type": "QA_RUN_GROUP",
                    "label": "Q&A",
                    "metadata_definition_id": str(uuid.uuid4()),
                    "run_ids": [str(uuid.uuid4())],
                    "value_mapping": {"true": "PRIVILEGED", "false": "NOT_PRIVILEGED"},
                },
                {
                    "source_id": "human",
                    "source_type": "MATTER_FIELD",
                    "label": "Human privilege coding",
                    "metadata_definition_id": str(uuid.uuid4()),
                    "value_mapping": {"privileged": "PRIVILEGED"},
                },
            ],
        }
    )

    assert payload.sources[0].metadata_definition_id != payload.sources[1].metadata_definition_id


def test_create_contract_pins_reviewer_coding_revision() -> None:
    coding_id = uuid.uuid4()
    payload = MatterCodingComparisonCreate.model_validate(
        {
            "name": "Imported reviewer comparison",
            "review_batch_ids": [str(uuid.uuid4())],
            "idempotency_key": "request-reviewer-1",
            "sources": [
                {
                    "source_id": "reviewer",
                    "source_type": "REVIEWER_CODING_GROUP",
                    "label": "Reviewer One",
                    "metadata_definition_id": str(uuid.uuid4()),
                    "reviewer_codings": [{"id": str(coding_id), "revision": 34822}],
                    "value_mapping": {"true": "privileged", "false": "not_privileged"},
                },
                {
                    "source_id": "matter",
                    "source_type": "MATTER_FIELD",
                    "label": "Matter privilege",
                    "metadata_definition_id": str(uuid.uuid4()),
                },
            ],
        }
    )

    assert payload.sources[0].reviewer_codings[0].id == coding_id
    assert payload.sources[0].reviewer_codings[0].revision == 34822
