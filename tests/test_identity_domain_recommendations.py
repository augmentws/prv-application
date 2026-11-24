import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.identity_domain_recommendations import (
    _domain_catalog,
    _publish_organization_proposals,
    decide_domain_recommendation,
)
from app.matter_entities import create_entity, merge_entities, reject_entity, unmerge_entity
from app.models import (
    Matter,
    MatterEntity,
    MatterEntityDomain,
    MatterIdentityAnalysis,
    MatterIdentityRecommendationRun,
    WorkflowRun,
)
from tests.test_agents_and_matter_definitions import create_tenant_context


def test_domain_catalog_excludes_personal_providers_and_uses_aggregate_names() -> None:
    catalog = _domain_catalog(
        {
            "identities": [
                {
                    "domain": "law.example",
                    "display_names": [{"value": "Example LLP", "observation_count": 4}],
                }
            ],
            "domains": [
                {
                    "domain": "gmail.com",
                    "identity_count": 10,
                    "document_count": 20,
                    "sender_count": 5,
                    "recipient_count": 8,
                },
                {
                    "domain": "LAW.EXAMPLE",
                    "identity_count": 2,
                    "document_count": 7,
                    "sender_count": 1,
                    "recipient_count": 6,
                },
            ],
        }
    )

    assert catalog == [
        {
            "domain": "law.example",
            "identity_count": 2,
            "document_count": 7,
            "sender_count": 1,
            "recipient_count": 6,
            "display_name_samples": ["Example LLP"],
        }
    ]


def test_accepting_domain_recommendation_publishes_confirmed_law_firm(
    client: TestClient,
    root_token: str,
    monkeypatch,
) -> None:
    tenant_id, token, matter_id = create_tenant_context(client, root_token)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["id"])
    with TestingSessionLocal() as db:
        matter = db.get(Matter, uuid.UUID(matter_id))
        assert matter is not None
        analysis = MatterIdentityAnalysis(
            tenant_id=uuid.UUID(tenant_id),
            matter_id=matter.id,
            status="COMPLETED",
            workflow_id=f"identity-analysis:{uuid.uuid4()}:attempt:1",
            total_document_count=10,
            processed_document_count=10,
            documents_with_participants_count=8,
            identity_count=6,
            domain_count=2,
            invalid_participant_count=0,
            result_artifact_id=uuid.uuid4(),
            result_content_hash="a" * 64,
            created_by_user_id=user_id,
        )
        db.add(analysis)
        db.flush()
        run = MatterIdentityRecommendationRun(
            tenant_id=uuid.UUID(tenant_id),
            matter_id=matter.id,
            identity_analysis_id=analysis.id,
            status="COMPLETED",
            workflow_id=f"identity-domain-review:{uuid.uuid4()}:attempt:1",
            domain_count=1,
            processed_domain_count=1,
            recommendation_count=1,
            result_artifact_id=uuid.uuid4(),
            result_content_hash="b" * 64,
            created_by_user_id=user_id,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        monkeypatch.setattr(
            "app.identity_domain_recommendations.recommendation_result",
            lambda *_args, **_kwargs: {
                "recommendations": [
                    {
                        "domain": "mail.example-law.com",
                        "canonical_domain": "example-law.com",
                        "organization_name": "Example Law LLP",
                        "organization_category": "LAW_FIRM",
                    }
                ]
            },
        )
        decision = decide_domain_recommendation(
            db,
            matter=matter,
            run=run,
            domain="mail.example-law.com",
            decision="ACCEPTED",
            actor_user_id=user_id,
        )
        db.commit()

        entity = db.get(MatterEntity, decision.entity_id)
        assert entity is not None
        assert entity.entity_type == "ORGANIZATION"
        assert entity.organization_category == "LAW_FIRM"
        assert entity.status == "CONFIRMED"
        mapping = db.scalar(select(MatterEntityDomain).where(MatterEntityDomain.entity_id == entity.id))
        assert mapping is not None
        assert mapping.normalized_domain == "example-law.com"

        target = create_entity(
            db,
            matter=matter,
            entity_type="ORGANIZATION",
            display_name="Example Law",
            actor_user_id=user_id,
        )
        db.flush()
        merge_entities(
            db,
            source=entity,
            target=target,
            source_lock_version=entity.lock_version,
            actor_user_id=user_id,
        )
        db.flush()
        assert target.organization_category == "LAW_FIRM"
        assert db.get(MatterEntityDomain, mapping.id).entity_id == target.id

        unmerge_entity(
            db,
            variant=entity,
            lock_version=entity.lock_version,
            actor_user_id=user_id,
        )
        db.flush()
        assert entity.organization_category == "LAW_FIRM"
        assert target.organization_category is None
        assert db.get(MatterEntityDomain, mapping.id).entity_id == entity.id

        workflow = WorkflowRun(
            tenant_id=uuid.UUID(tenant_id),
            client_id=matter.client_id,
            matter_id=matter.id,
            job_type="IDENTITY_DOMAIN_RECOMMENDATION",
            job_id=run.id,
            attempt_number=1,
            workflow_key="identity_domain_recommendation_v1",
            code_version="3",
            dbos_workflow_id=f"identity-domain-proposal-test:{uuid.uuid4()}",
            status="RUNNING",
            initiated_by_user_id=user_id,
        )
        db.add(workflow)
        db.flush()
        recommendations = [
            {
                "domain": "mail.acme.example",
                "canonical_domain": "acme.example",
                "organization_name": "Acme Corporation",
                "organization_category": "CORPORATION",
                "confidence": 0.91,
            },
            {
                "domain": "acme-legal.example",
                "canonical_domain": "acme-legal.example",
                "organization_name": "ACME CORPORATION",
                "organization_category": "CORPORATION",
                "confidence": 0.88,
            },
        ]
        summary = _publish_organization_proposals(
            db,
            run=run,
            workflow_id=workflow.id,
            recommendations=recommendations,
            matter=matter,
        )
        db.flush()
        proposal = db.get(MatterEntity, uuid.UUID(recommendations[0]["proposed_entity_id"]))
        assert summary["proposed_entity_count"] == 1
        assert recommendations[0]["publication_status"] == "PROPOSED"
        assert recommendations[1]["publication_status"] == "ALREADY_PROPOSED"
        assert recommendations[1]["proposed_entity_id"] == recommendations[0]["proposed_entity_id"]
        assert proposal is not None
        assert proposal.status == "PROPOSED"
        assert proposal.organization_category == "CORPORATION"
        assert proposal.created_by_workflow_run_id == workflow.id
        proposal_domains = set(
            db.scalars(select(MatterEntityDomain.normalized_domain).where(MatterEntityDomain.entity_id == proposal.id))
        )
        assert proposal_domains == {"acme.example", "acme-legal.example"}

        reject_entity(
            db,
            entity=proposal,
            lock_version=proposal.lock_version,
            actor_user_id=user_id,
            reason="Not the intended organization",
        )
        db.flush()
        assert db.scalar(select(MatterEntityDomain).where(MatterEntityDomain.entity_id == proposal.id)) is None

        existing_law_firm = create_entity(
            db,
            matter=matter,
            entity_type="ORGANIZATION",
            display_name="Kirkland & Ellis",
            status="PROPOSED",
            actor_type="AGENT",
            workflow_run_id=workflow.id,
        )
        db.flush()
        existing_recommendation = [{
            "domain": "kirkland.com",
            "canonical_domain": "kirkland.com",
            "organization_name": "Kirkland & Ellis",
            "organization_category": "LAW_FIRM",
            "confidence": 0.97,
        }]
        existing_summary = _publish_organization_proposals(
            db,
            run=run,
            workflow_id=workflow.id,
            recommendations=existing_recommendation,
            matter=matter,
        )
        db.flush()
        assert existing_summary["proposed_entity_count"] == 0
        assert existing_recommendation[0]["proposed_entity_id"] == str(existing_law_firm.id)
        assert existing_law_firm.organization_category == "LAW_FIRM"
        assert db.scalar(
            select(MatterEntityDomain.normalized_domain).where(MatterEntityDomain.entity_id == existing_law_firm.id)
        ) == "kirkland.com"
