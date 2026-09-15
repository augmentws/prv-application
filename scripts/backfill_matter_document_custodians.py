import argparse
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Custodian, Matter, MatterDocument, MatterDocumentCustodian
from artifact_service.database import ArtifactSessionLocal
from artifact_service.models import CollectionItemCustodian


@dataclass
class BackfillResult:
    documents_examined: int = 0
    relationships_added: int = 0
    documents_without_custodians: int = 0
    unknown_custodian_relationships: int = 0
    client_mismatch_relationships: int = 0


def backfill_matter_document_custodians(
    *,
    core_session_factory: Callable[[], Session] = SessionLocal,
    artifact_session_factory: Callable[[], Session] = ArtifactSessionLocal,
    batch_size: int = 1000,
) -> BackfillResult:
    result = BackfillResult()
    last_document_id = None
    while True:
        with core_session_factory() as core_db:
            statement = (
                select(MatterDocument.id, MatterDocument.collection_item_id, Matter.client_id)
                .join(Matter, Matter.id == MatterDocument.matter_id)
                .order_by(MatterDocument.id)
                .limit(batch_size)
            )
            if last_document_id is not None:
                statement = statement.where(MatterDocument.id > last_document_id)
            documents = list(core_db.execute(statement))
        if not documents:
            break

        document_by_item_id = {
            collection_item_id: (document_id, client_id)
            for document_id, collection_item_id, client_id in documents
        }
        with artifact_session_factory() as artifact_db:
            source_relationships = list(
                artifact_db.execute(
                    select(
                        CollectionItemCustodian.collection_item_id,
                        CollectionItemCustodian.custodian_id,
                        CollectionItemCustodian.relationship_type,
                    ).where(CollectionItemCustodian.collection_item_id.in_(document_by_item_id))
                )
            )

        represented_item_ids = {item_id for item_id, _, _ in source_relationships}
        result.documents_examined += len(documents)
        result.documents_without_custodians += len(document_by_item_id.keys() - represented_item_ids)
        referenced_custodian_ids = {custodian_id for _, custodian_id, _ in source_relationships}
        with core_session_factory() as core_db:
            custodian_clients = dict(
                core_db.execute(
                    select(Custodian.id, Custodian.client_id).where(Custodian.id.in_(referenced_custodian_ids))
                ).all()
            )
            existing_relationships = set(
                core_db.execute(
                    select(
                        MatterDocumentCustodian.matter_document_id,
                        MatterDocumentCustodian.custodian_id,
                    ).where(
                        MatterDocumentCustodian.matter_document_id.in_(
                            [document_id for document_id, _, _ in documents]
                        )
                    )
                )
            )
            for item_id, custodian_id, relationship_type in source_relationships:
                document_id, client_id = document_by_item_id[item_id]
                custodian_client_id = custodian_clients.get(custodian_id)
                if custodian_client_id is None:
                    result.unknown_custodian_relationships += 1
                    continue
                if custodian_client_id != client_id:
                    result.client_mismatch_relationships += 1
                    continue
                if (document_id, custodian_id) in existing_relationships:
                    continue
                core_db.add(
                    MatterDocumentCustodian(
                        matter_document_id=document_id,
                        custodian_id=custodian_id,
                        relationship_type=relationship_type,
                    )
                )
                existing_relationships.add((document_id, custodian_id))
                result.relationships_added += 1
            core_db.commit()
        last_document_id = documents[-1].id
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy custodian relationships for existing matter documents from Artifact into Core."
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    result = backfill_matter_document_custodians(batch_size=args.batch_size)
    print(
        "Matter-document custodian backfill complete: "
        f"documents={result.documents_examined}, "
        f"relationships_added={result.relationships_added}, "
        f"documents_without_custodians={result.documents_without_custodians}, "
        f"unknown_custodians={result.unknown_custodian_relationships}, "
        f"client_mismatches={result.client_mismatch_relationships}"
    )


if __name__ == "__main__":
    main()
