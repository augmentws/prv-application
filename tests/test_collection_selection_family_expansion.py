import uuid

from conftest import ArtifactTestingSessionLocal

from artifact_service.models import ClientCollection, CollectionItem
from artifact_service.schemas import CollectionSelectionCreate
from artifact_service.selections import create_collection_selection, get_collection_selection_batch


def _item(
    *,
    collection: ClientCollection,
    source_item_id: str,
    filename: str,
    family_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
) -> CollectionItem:
    return CollectionItem(
        tenant_id=collection.tenant_id,
        client_id=collection.client_id,
        collection_id=collection.id,
        source_item_id=source_item_id,
        record_type="EMAIL" if parent_id is None else "FILE",
        original_filename=filename,
        original_extension=".eml" if parent_id is None else ".pdf",
        original_source_path=None,
        source_created_at=None,
        source_modified_at=None,
        file_date=None,
        family_id=family_id,
        parent_collection_item_id=parent_id,
        processing_status="READY",
        raw_metadata={},
        unmapped_metadata={},
    )


def test_collection_selection_expands_complete_families_by_default_and_can_opt_out() -> None:
    with ArtifactTestingSessionLocal() as db:
        collection = ClientCollection(
            tenant_id=uuid.uuid4(),
            client_id=uuid.uuid4(),
            name="Family selection",
            description=None,
            status="OPEN",
            created_by_user_id=uuid.uuid4(),
            active_text_processing_run_id=None,
        )
        db.add(collection)
        db.flush()
        family_id = uuid.uuid4()
        parent = _item(
            collection=collection,
            source_item_id="parent",
            filename="message.eml",
            family_id=family_id,
        )
        db.add(parent)
        db.flush()
        matching_child = _item(
            collection=collection,
            source_item_id="attachment-one",
            filename="responsive-contract.pdf",
            family_id=family_id,
            parent_id=parent.id,
        )
        sibling = _item(
            collection=collection,
            source_item_id="attachment-two",
            filename="schedule.pdf",
            family_id=family_id,
            parent_id=parent.id,
        )
        unrelated = _item(
            collection=collection,
            source_item_id="unrelated",
            filename="unrelated.eml",
        )
        db.add_all([matching_child, sibling, unrelated])
        db.flush()

        expanded = create_collection_selection(
            db,
            collection,
            CollectionSelectionCreate(request_id=uuid.uuid4(), q="responsive-contract"),
        )
        seed_only = create_collection_selection(
            db,
            collection,
            CollectionSelectionCreate(
                request_id=uuid.uuid4(),
                q="responsive-contract",
                include_complete_families=False,
            ),
        )
        db.commit()

        assert expanded.total_count == 3
        assert set(get_collection_selection_batch(db, expanded, offset=0, limit=10)) == {
            parent.id,
            matching_child.id,
            sibling.id,
        }
        assert expanded.selection["family_expansion"] == {
            "policy": "EXPAND_COMPLETE_FAMILIES_IN_COLLECTION",
            "seed_document_count": 1,
            "added_document_count": 2,
            "final_document_count": 3,
        }
        assert seed_only.total_count == 1
        assert get_collection_selection_batch(db, seed_only, offset=0, limit=10) == [matching_child.id]
        assert seed_only.selection["family_expansion"]["policy"] == "SEED_DOCUMENTS_ONLY"
