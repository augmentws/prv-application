import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.artifact_gateway import CollectionItemRelationshipSnapshot
from app.routers import matter_imports as router


def test_child_family_resolves_parent_siblings_and_matter_availability(monkeypatch) -> None:
    matter_id = uuid.uuid4()
    current_document_id = uuid.uuid4()
    parent_document_id = uuid.uuid4()
    sibling_document_id = uuid.uuid4()
    parent_item_id = uuid.uuid4()
    current_item_id = uuid.uuid4()
    sibling_item_id = uuid.uuid4()
    unavailable_item_id = uuid.uuid4()
    family_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    client_id = uuid.uuid4()
    user_id = uuid.uuid4()

    matter = SimpleNamespace(id=matter_id, client_id=client_id, client=SimpleNamespace(tenant_id=tenant_id))
    current_document = SimpleNamespace(
        id=current_document_id,
        matter_id=matter_id,
        collection_item_id=current_item_id,
    )
    monkeypatch.setattr(router, "_matter", lambda *_args, **_kwargs: matter)
    monkeypatch.setattr(router, "_matter_document", lambda *_args, **_kwargs: current_document)

    child_relationship = CollectionItemRelationshipSnapshot(
        item_id=current_item_id,
        family_id=family_id,
        parent_item_id=parent_item_id,
        parent_source_item_id="parent-source",
        parent_identifier="PARENT-001",
        child_item_ids=[],
        child_source_item_ids=[],
        child_identifiers=[],
    )
    parent_relationship = CollectionItemRelationshipSnapshot(
        item_id=parent_item_id,
        family_id=family_id,
        parent_item_id=None,
        parent_source_item_id=None,
        parent_identifier=None,
        child_item_ids=[current_item_id, sibling_item_id, unavailable_item_id],
        child_source_item_ids=["child-current", "child-sibling", "child-unavailable"],
        child_identifiers=["ATT-001", "ATT-002", "ATT-003"],
    )

    def relationships(*, item_ids, **_kwargs):
        if item_ids == [current_item_id]:
            return {current_item_id: child_relationship}
        assert item_ids == [parent_item_id]
        return {parent_item_id: parent_relationship}

    monkeypatch.setattr(router, "get_collection_item_relationship_snapshots", relationships)
    monkeypatch.setattr(
        router,
        "get_collection_item_analysis_input_snapshots",
        lambda **_kwargs: {
            parent_item_id: SimpleNamespace(
                original_filename="parent.eml", email_subject="Legal questions", record_type="EMAIL"
            ),
            current_item_id: SimpleNamespace(
                original_filename="questions.doc", email_subject=None, record_type="FILE"
            ),
            sibling_item_id: SimpleNamespace(
                original_filename="notes.doc", email_subject=None, record_type="FILE"
            ),
            unavailable_item_id: SimpleNamespace(
                original_filename="outside.doc", email_subject=None, record_type="FILE"
            ),
        },
    )

    db = MagicMock()
    db.scalars.return_value = [
        SimpleNamespace(id=parent_document_id, collection_item_id=parent_item_id),
        SimpleNamespace(id=current_document_id, collection_item_id=current_item_id),
        SimpleNamespace(id=sibling_document_id, collection_item_id=sibling_item_id),
    ]
    principal = SimpleNamespace(user=SimpleNamespace(id=user_id))

    result = router.get_matter_document_family(
        matter_id=matter_id,
        document_id=current_document_id,
        principal=principal,
        db=db,
    )

    assert result.current_role == "ATTACHMENT"
    assert result.parent is not None
    assert result.parent.matter_document_id == parent_document_id
    assert result.parent.display_name == "Legal questions"
    assert [child.identifier for child in result.children] == ["ATT-001", "ATT-002", "ATT-003"]
    assert result.children[0].is_current is True
    assert result.children[1].matter_document_id == sibling_document_id
    assert result.children[2].available_in_matter is False
    assert result.unavailable_member_count == 1


def test_standalone_family_does_not_report_relatives(monkeypatch) -> None:
    matter_id = uuid.uuid4()
    document_id = uuid.uuid4()
    item_id = uuid.uuid4()
    family_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    client_id = uuid.uuid4()
    matter = SimpleNamespace(id=matter_id, client_id=client_id, client=SimpleNamespace(tenant_id=tenant_id))
    document = SimpleNamespace(id=document_id, matter_id=matter_id, collection_item_id=item_id)
    monkeypatch.setattr(router, "_matter", lambda *_args, **_kwargs: matter)
    monkeypatch.setattr(router, "_matter_document", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(
        router,
        "get_collection_item_relationship_snapshots",
        lambda **_kwargs: {
            item_id: CollectionItemRelationshipSnapshot(
                item_id=item_id,
                family_id=family_id,
                parent_item_id=None,
                parent_source_item_id=None,
                parent_identifier=None,
                child_item_ids=[],
                child_source_item_ids=[],
                child_identifiers=[],
            )
        },
    )
    monkeypatch.setattr(
        router,
        "get_collection_item_analysis_input_snapshots",
        lambda **_kwargs: {
            item_id: SimpleNamespace(
                original_filename="standalone.eml", email_subject="Standalone", record_type="EMAIL"
            )
        },
    )

    result = router.get_matter_document_family(
        matter_id=matter_id,
        document_id=document_id,
        principal=SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4())),
        db=MagicMock(),
    )

    assert result.current_role == "STANDALONE"
    assert result.parent is None
    assert result.children == []
    assert result.unavailable_member_count == 0
