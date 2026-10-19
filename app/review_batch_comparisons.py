import csv
import io
import itertools
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.csv_safety import spreadsheet_safe_row
from app.document_metadata import event_value
from app.models import (
    MatterDocument,
    MetadataDefinition,
    ReviewBatchDocument,
    ReviewBatchReviewerDocument,
    ReviewBatchReviewerValue,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
)
from app.schemas import (
    ReviewBatchCodingComparisonRead,
    ReviewBatchComparisonClassAgreementRead,
    ReviewBatchComparisonDocumentRead,
    ReviewBatchComparisonDocumentSourceRead,
    ReviewBatchComparisonMatrixCellRead,
    ReviewBatchComparisonRequest,
    ReviewBatchComparisonSourceOptionRead,
    ReviewBatchComparisonSourceSummaryRead,
    ReviewBatchComparisonSourceValueCountRead,
    ReviewBatchComparisonValueCountRead,
    ReviewBatchMultiSourceAgreementRead,
    ReviewBatchMultiSourceClassAgreementRead,
    ReviewBatchPairwiseAgreementRead,
)

COMPARISON_CHUNK_SIZE = 2_000
MAX_RETURNED_VALUE_COUNTS = 200
NO_VALUE_KEY = "__NO_VALUE__"


def _value_key(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _value_labels(definition: MetadataDefinition) -> dict[str, str]:
    labels = {
        str(item.get("key")): str(item.get("label") or item.get("key"))
        for item in definition.allowed_values or []
        if item.get("key") is not None
    }
    if definition.type == "BOOLEAN":
        labels.update({"true": "Yes", "false": "No"})
    labels[NO_VALUE_KEY] = "No value"
    return labels


@dataclass(frozen=True)
class SourceValue:
    state: str
    value_keys: tuple[str, ...]
    top_confidence: float | None


@dataclass(frozen=True)
class ComparisonDocument:
    matter_document_id: uuid.UUID
    collection_item_id: uuid.UUID
    sequence_number: int
    agreement_status: str
    sources: dict[uuid.UUID, SourceValue]


@dataclass
class PairAccumulator:
    compared: int = 0
    exact: int = 0
    partial: int = 0
    disagreement: int = 0
    incomplete: int = 0
    jaccard_sum: float = 0.0
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)
    class_counts: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0, 0]))


@dataclass
class MultiSourceAccumulator:
    compared: int = 0
    incomplete: int = 0
    any_counts: Counter[str] = field(default_factory=Counter)
    all_counts: Counter[str] = field(default_factory=Counter)
    source_counts: dict[uuid.UUID, Counter[str]] = field(default_factory=dict)


def _source_state(status: str | None, has_values: bool) -> str:
    if status == "FAILED":
        return "FAILED"
    if status == "SKIPPED":
        return "SKIPPED"
    if status in {"QUEUED", "IN_PROGRESS"}:
        return "INCOMPLETE"
    if status == "COMPLETED" or has_values:
        return "EVALUATED"
    return "NOT_EVALUATED"


def _agreement_status(sources: dict[uuid.UUID, SourceValue]) -> str:
    if any(source.state != "EVALUATED" for source in sources.values()):
        return "INCOMPLETE"
    sets = [set(source.value_keys) for source in sources.values()]
    if all(values == sets[0] for values in sets[1:]):
        return "EXACT"
    if any(left & right for left, right in itertools.combinations(sets, 2)):
        return "PARTIAL"
    return "DISAGREEMENT"


def iter_comparison_documents(
    db: Session,
    *,
    batch_id: uuid.UUID,
    definition_id: uuid.UUID,
    source_options: list[ReviewBatchComparisonSourceOptionRead],
) -> Iterator[ComparisonDocument]:
    run_ids = [source.id for source in source_options if source.source_type == "RUN"]
    reviewer_ids = [source.id for source in source_options if source.source_type == "REVIEWER"]
    source_ids = [source.id for source in source_options]
    last_sequence = 0
    while True:
        documents = db.execute(
            select(
                ReviewBatchDocument.matter_document_id,
                ReviewBatchDocument.sequence_number,
                MatterDocument.collection_item_id,
            )
            .join(MatterDocument, MatterDocument.id == ReviewBatchDocument.matter_document_id)
            .where(
                ReviewBatchDocument.review_batch_id == batch_id,
                ReviewBatchDocument.sequence_number > last_sequence,
            )
            .order_by(ReviewBatchDocument.sequence_number)
            .limit(COMPARISON_CHUNK_SIZE)
        ).all()
        if not documents:
            return
        document_ids = [row.matter_document_id for row in documents]
        statuses = {
            (row.review_batch_run_id, row.matter_document_id): row.status
            for row in db.scalars(
                select(ReviewBatchRunDocument).where(
                    ReviewBatchRunDocument.review_batch_run_id.in_(run_ids),
                    ReviewBatchRunDocument.matter_document_id.in_(document_ids),
                )
            )
        }
        statuses.update(
            {
                (row.reviewer_coding_id, row.matter_document_id): row.status
                for row in db.scalars(
                    select(ReviewBatchReviewerDocument).where(
                        ReviewBatchReviewerDocument.reviewer_coding_id.in_(reviewer_ids),
                        ReviewBatchReviewerDocument.matter_document_id.in_(document_ids),
                    )
                )
            }
        )
        values: dict[tuple[uuid.UUID, uuid.UUID], list[tuple[int, str, float | None]]] = defaultdict(list)
        for row in db.scalars(
            select(ReviewBatchRunValue).where(
                ReviewBatchRunValue.review_batch_run_id.in_(run_ids),
                ReviewBatchRunValue.matter_document_id.in_(document_ids),
                ReviewBatchRunValue.metadata_definition_id == definition_id,
            )
        ):
            values[(row.review_batch_run_id, row.matter_document_id)].append(
                (row.value_ordinal, _value_key(event_value(row)), row.confidence)
            )
        for row in db.scalars(
            select(ReviewBatchReviewerValue).where(
                ReviewBatchReviewerValue.reviewer_coding_id.in_(reviewer_ids),
                ReviewBatchReviewerValue.matter_document_id.in_(document_ids),
                ReviewBatchReviewerValue.metadata_definition_id == definition_id,
            )
        ):
            values[(row.reviewer_coding_id, row.matter_document_id)].append(
                (row.value_ordinal, _value_key(event_value(row)), None)
            )
        for document in documents:
            source_values: dict[uuid.UUID, SourceValue] = {}
            for source_id in source_ids:
                rows = sorted(values.get((source_id, document.matter_document_id), []))
                unique_keys = tuple(dict.fromkeys(row[1] for row in rows))
                confidences = [row[2] for row in rows if row[2] is not None]
                source_values[source_id] = SourceValue(
                    state=_source_state(statuses.get((source_id, document.matter_document_id)), bool(rows)),
                    value_keys=unique_keys,
                    top_confidence=max(confidences) if confidences else None,
                )
            yield ComparisonDocument(
                matter_document_id=document.matter_document_id,
                collection_item_id=document.collection_item_id,
                sequence_number=document.sequence_number,
                agreement_status=_agreement_status(source_values),
                sources=source_values,
            )
        last_sequence = documents[-1].sequence_number


def _matches_filter(status: str, row_filter: str) -> bool:
    if row_filter == "ALL":
        return True
    if row_filter == "AGREEMENT":
        return status == "EXACT"
    if row_filter == "DISAGREEMENT":
        return status in {"PARTIAL", "DISAGREEMENT"}
    return status == "INCOMPLETE"


def build_coding_comparison(
    db: Session,
    *,
    batch_id: uuid.UUID,
    definition: MetadataDefinition,
    request: ReviewBatchComparisonRequest,
    source_options: list[ReviewBatchComparisonSourceOptionRead],
    document_count: int,
) -> ReviewBatchCodingComparisonRead:
    labels = _value_labels(definition)
    source_ids = request.source_ids
    state_counts = {source_id: Counter() for source_id in source_ids}
    value_counts = {source_id: Counter() for source_id in source_ids}
    pair_accumulators = {pair: PairAccumulator() for pair in itertools.combinations(source_ids, 2)}
    multi_source = MultiSourceAccumulator(source_counts={source_id: Counter() for source_id in source_ids})
    filtered_count = 0
    page_documents: list[ReviewBatchComparisonDocumentRead] = []

    for document in iter_comparison_documents(
        db,
        batch_id=batch_id,
        definition_id=definition.id,
        source_options=source_options,
    ):
        for source_id, source in document.sources.items():
            state_counts[source_id][source.state] += 1
            if source.state == "EVALUATED":
                if source.value_keys:
                    value_counts[source_id].update(source.value_keys)
                else:
                    value_counts[source_id][NO_VALUE_KEY] += 1

        for (left_id, right_id), accumulator in pair_accumulators.items():
            left = document.sources[left_id]
            right = document.sources[right_id]
            if left.state != "EVALUATED" or right.state != "EVALUATED":
                accumulator.incomplete += 1
                continue
            left_values = set(left.value_keys)
            right_values = set(right.value_keys)
            union = left_values | right_values
            intersection = left_values & right_values
            accumulator.compared += 1
            accumulator.jaccard_sum += (len(intersection) / len(union)) if union else 1.0
            if left_values == right_values:
                accumulator.exact += 1
            elif intersection:
                accumulator.partial += 1
            else:
                accumulator.disagreement += 1
            if definition.cardinality == "SINGLE" and definition.type in {"ENUM", "BOOLEAN"}:
                left_key = next(iter(left_values), NO_VALUE_KEY)
                right_key = next(iter(right_values), NO_VALUE_KEY)
                accumulator.confusion[(left_key, right_key)] += 1
            else:
                for key in union:
                    counts = accumulator.class_counts[key]
                    if key in left_values and key in right_values:
                        counts[0] += 1
                    elif key in left_values:
                        counts[1] += 1
                    else:
                        counts[2] += 1

        selected_source_values = [document.sources[source_id] for source_id in source_ids]
        if all(source.state == "EVALUATED" for source in selected_source_values):
            multi_source.compared += 1
            selected_value_sets = [set(source.value_keys) for source in selected_source_values]
            any_values = set().union(*selected_value_sets)
            all_values = set.intersection(*selected_value_sets) if selected_value_sets else set()
            multi_source.any_counts.update(any_values)
            multi_source.all_counts.update(all_values)
            for source_id, values_for_source in zip(source_ids, selected_value_sets, strict=True):
                multi_source.source_counts[source_id].update(values_for_source)
        else:
            multi_source.incomplete += 1

        if not _matches_filter(document.agreement_status, request.row_filter):
            continue
        row_index = filtered_count
        filtered_count += 1
        if row_index < request.offset or len(page_documents) >= request.limit:
            continue
        page_documents.append(
            ReviewBatchComparisonDocumentRead(
                matter_document_id=document.matter_document_id,
                collection_item_id=document.collection_item_id,
                sequence_number=document.sequence_number,
                agreement_status=document.agreement_status,
                sources=[
                    ReviewBatchComparisonDocumentSourceRead(
                        source_id=source_id,
                        state=document.sources[source_id].state,
                        value_keys=list(document.sources[source_id].value_keys),
                        display_values=[labels.get(key, key) for key in document.sources[source_id].value_keys],
                        top_confidence=document.sources[source_id].top_confidence,
                    )
                    for source_id in source_ids
                ],
            )
        )

    summaries = []
    for source_id in source_ids:
        ordered_counts = sorted(value_counts[source_id].items(), key=lambda item: (-item[1], labels.get(item[0], item[0])))
        summaries.append(
            ReviewBatchComparisonSourceSummaryRead(
                source_id=source_id,
                evaluated_document_count=state_counts[source_id]["EVALUATED"],
                no_value_document_count=value_counts[source_id][NO_VALUE_KEY],
                skipped_document_count=state_counts[source_id]["SKIPPED"],
                failed_document_count=state_counts[source_id]["FAILED"],
                incomplete_document_count=state_counts[source_id]["INCOMPLETE"],
                not_evaluated_document_count=state_counts[source_id]["NOT_EVALUATED"],
                value_counts=[
                    ReviewBatchComparisonValueCountRead(
                        value_key=key,
                        display_label=labels.get(key, key),
                        document_count=count,
                    )
                    for key, count in ordered_counts[:MAX_RETURNED_VALUE_COUNTS]
                ],
                value_counts_truncated=len(ordered_counts) > MAX_RETURNED_VALUE_COUNTS,
            )
        )

    pairwise = []
    class_order = [str(item.get("key")) for item in definition.allowed_values or [] if item.get("key") is not None]
    multi_source_observed_classes = set(multi_source.any_counts)
    multi_source_ordered_classes = class_order + sorted(multi_source_observed_classes - set(class_order))
    multi_source_agreement = ReviewBatchMultiSourceAgreementRead(
        compared_document_count=multi_source.compared,
        incomplete_document_count=multi_source.incomplete,
        class_agreement=[
            ReviewBatchMultiSourceClassAgreementRead(
                value_key=key,
                display_label=labels.get(key, key),
                source_counts=[
                    ReviewBatchComparisonSourceValueCountRead(
                        source_id=source_id,
                        document_count=multi_source.source_counts[source_id][key],
                    )
                    for source_id in source_ids
                ],
                all_selected_count=multi_source.all_counts[key],
                any_selected_count=multi_source.any_counts[key],
                jaccard=(
                    multi_source.all_counts[key] / multi_source.any_counts[key]
                    if multi_source.any_counts[key]
                    else None
                ),
            )
            for key in multi_source_ordered_classes
        ],
    )
    for (left_id, right_id), accumulator in pair_accumulators.items():
        observed_classes = set(accumulator.class_counts)
        ordered_classes = class_order + sorted(observed_classes - set(class_order))
        pairwise.append(
            ReviewBatchPairwiseAgreementRead(
                left_source_id=left_id,
                right_source_id=right_id,
                compared_document_count=accumulator.compared,
                exact_match_count=accumulator.exact,
                partial_match_count=accumulator.partial,
                disagreement_count=accumulator.disagreement,
                incomplete_document_count=accumulator.incomplete,
                exact_agreement=(accumulator.exact / accumulator.compared) if accumulator.compared else None,
                mean_jaccard=(accumulator.jaccard_sum / accumulator.compared) if accumulator.compared else None,
                confusion_matrix=[
                    ReviewBatchComparisonMatrixCellRead(
                        left_value=left_value,
                        right_value=right_value,
                        document_count=count,
                    )
                    for (left_value, right_value), count in sorted(accumulator.confusion.items())
                ],
                class_agreement=[
                    ReviewBatchComparisonClassAgreementRead(
                        value_key=key,
                        display_label=labels.get(key, key),
                        both_selected_count=accumulator.class_counts[key][0],
                        left_only_count=accumulator.class_counts[key][1],
                        right_only_count=accumulator.class_counts[key][2],
                        both_not_selected_count=max(
                            0,
                            accumulator.compared - sum(accumulator.class_counts[key]),
                        ),
                        jaccard=(
                            accumulator.class_counts[key][0] / sum(accumulator.class_counts[key])
                            if sum(accumulator.class_counts[key])
                            else None
                        ),
                    )
                    for key in ordered_classes
                ],
            )
        )

    return ReviewBatchCodingComparisonRead(
        review_batch_id=batch_id,
        metadata_definition_id=definition.id,
        field_display_name=definition.display_name,
        cardinality=definition.cardinality,
        document_count=document_count,
        filtered_document_count=filtered_count,
        offset=request.offset,
        limit=request.limit,
        sources=source_options,
        source_summaries=summaries,
        multi_source_agreement=multi_source_agreement,
        pairwise_agreements=pairwise,
        documents=page_documents,
    )


def _export_cell(source: SourceValue, labels: dict[str, str]) -> str:
    if source.state != "EVALUATED":
        return f"[{source.state.replace('_', ' ')}]"
    if not source.value_keys:
        return "[NO VALUE]"
    return " | ".join(labels.get(key, key) for key in source.value_keys)


def export_coding_comparison(
    db: Session,
    *,
    batch_id: uuid.UUID,
    definition: MetadataDefinition,
    source_options: list[ReviewBatchComparisonSourceOptionRead],
    row_filter: str,
    export_format: str,
) -> tuple[bytes, str, str]:
    labels = _value_labels(definition)
    source_ids = [source.id for source in source_options]
    seen_labels: Counter[str] = Counter()
    columns: list[str] = []
    for source in source_options:
        seen_labels[source.label] += 1
        columns.append(
            source.label if seen_labels[source.label] == 1 else f"{source.label} ({str(source.id)[:8]})"
        )
    fieldnames = ["matter_document_id", "collection_item_id", "batch_sequence", "agreement_status", *columns]

    def rows() -> Iterator[dict[str, Any]]:
        for document in iter_comparison_documents(
            db,
            batch_id=batch_id,
            definition_id=definition.id,
            source_options=source_options,
        ):
            if not _matches_filter(document.agreement_status, row_filter):
                continue
            row: dict[str, Any] = {
                "matter_document_id": str(document.matter_document_id),
                "collection_item_id": str(document.collection_item_id),
                "batch_sequence": document.sequence_number,
                "agreement_status": document.agreement_status,
            }
            for source_id, column in zip(source_ids, columns, strict=True):
                row[column] = _export_cell(document.sources[source_id], labels)
            yield row

    safe_field = "".join(
        character.lower() if character.isalnum() else "-" for character in definition.display_name
    ).strip("-")
    if export_format == "csv":
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(spreadsheet_safe_row(row) for row in rows())
        return output.getvalue().encode("utf-8"), "text/csv; charset=utf-8", f"batch-coding-{safe_field}.csv"

    schema = pa.schema(
        [
            pa.field("matter_document_id", pa.string()),
            pa.field("collection_item_id", pa.string()),
            pa.field("batch_sequence", pa.int64()),
            pa.field("agreement_status", pa.string()),
            *(pa.field(column, pa.string()) for column in columns),
        ],
        metadata={
            b"priv-view.field_id": str(definition.id).encode(),
            b"priv-view.field_name": definition.display_name.encode(),
            b"priv-view.source_columns": json.dumps(
                {column: str(source_id) for column, source_id in zip(columns, source_ids, strict=True)},
                sort_keys=True,
            ).encode(),
        },
    )
    output = io.BytesIO()
    writer = pq.ParquetWriter(output, schema, compression="zstd")
    batch: list[dict[str, Any]] = []
    try:
        for row in rows():
            batch.append(row)
            if len(batch) >= COMPARISON_CHUNK_SIZE:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                batch.clear()
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=schema))
    finally:
        writer.close()
    return output.getvalue(), "application/vnd.apache.parquet", f"batch-coding-{safe_field}.parquet"
