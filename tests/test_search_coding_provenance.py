import uuid
from types import SimpleNamespace
from unittest.mock import Mock

from app.search.coding_provenance import coded_by_labels, coded_by_tokens


def test_coded_by_tokens_distinguish_humans_agents_and_automated_sources() -> None:
    agent_id = uuid.uuid4()
    db = Mock()
    document_id = uuid.uuid4()
    db.execute.return_value.all.return_value = [
        (document_id, "HUMAN", "reviewer@example.com", None, None),
        (document_id, "HUMAN", "reviewer@example.com", None, None),
        (document_id, "AGENT", None, agent_id, None),
        (document_id, "AGENT", None, None, "analysis-task-promotion:promotion-1"),
        (document_id, "IMPORT", "importer@example.com", None, "import-job:job-1"),
    ]

    assert set(coded_by_tokens(db, document_id)) == {
        "agent:analysis-task-promotion",
        f"agent:{agent_id}",
        "user:reviewer@example.com",
    }


def test_coded_by_labels_show_human_email_and_agent_class() -> None:
    agent_id = uuid.uuid4()
    db = Mock()
    db.scalars.side_effect = [
        [
            SimpleNamespace(
                normalized_email="reviewer@example.com",
                display_name="Review User",
                email="Reviewer@Example.com",
            )
        ],
        [SimpleNamespace(id=agent_id, name="Privilege Agent")],
    ]

    labels = coded_by_labels(
        db,
        [
            "user:reviewer@example.com",
            f"agent:{agent_id}",
            "agent:analysis-task-promotion",
        ],
    )

    assert labels == {
        "user:reviewer@example.com": "Review User (Reviewer@Example.com)",
        f"agent:{agent_id}": "Privilege Agent (Agent)",
        "agent:analysis-task-promotion": "Jev analysis task (Agent)",
    }
