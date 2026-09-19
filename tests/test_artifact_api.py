import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from artifact_service.api import MAX_DATE_HISTOGRAM_BUCKETS, _fill_date_buckets
from artifact_service.config import get_artifact_settings
from artifact_service.schemas import TextProcessingRule
from artifact_service.text_processing import process_text


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_date_histogram_does_not_fill_extreme_empty_ranges() -> None:
    rows = [
        (datetime(100, 2, 1, tzinfo=timezone.utc), 46),
        (datetime(2099, 10, 1, tzinfo=timezone.utc), 2),
    ]

    buckets = _fill_date_buckets(rows, "month")

    assert MAX_DATE_HISTOGRAM_BUCKETS < 24_000
    assert [(bucket.start, bucket.count) for bucket in buckets] == rows


def test_individual_email_upload_with_source_container(client: TestClient, root_token: str) -> None:
    tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    client_response = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Dataset Client"},
    )
    assert client_response.status_code == 201, client_response.text
    client_id = client_response.json()["id"]

    custodian_response = client.post(
        f"/v1/clients/{client_id}/custodians",
        headers=auth(root_token),
        json={"display_name": "Phillip Allen", "email_addresses": ["phillip.allen@enron.com"]},
    )
    assert custodian_response.status_code == 201, custodian_response.text
    custodian_id = custodian_response.json()["id"]
    unused_custodian_response = client.post(
        f"/v1/clients/{client_id}/custodians",
        headers=auth(root_token),
        json={"display_name": "Unused Custodian", "email_addresses": ["unused@example.com"]},
    )
    assert unused_custodian_response.status_code == 201, unused_custodian_response.text

    storage_response = client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    assert storage_response.status_code == 201, storage_response.text
    bucket_name = storage_response.json()["bucket_name"]
    assert bucket_name.startswith("pv-artifacts-root-")
    assert len(bucket_name.rsplit("-", 1)[1]) == 10

    collection_response = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": "Enron Top CSV", "description": "Six-message sample"},
    )
    assert collection_response.status_code == 201, collection_response.text
    collection_id = collection_response.json()["id"]

    get_collection_response = client.get(
        f"/v1/collections/{collection_id}",
        headers=auth(root_token),
    )
    assert get_collection_response.status_code == 200, get_collection_response.text
    assert get_collection_response.json()["name"] == "Enron Top CSV"

    source_bytes = b'"file","message"\n"allen-p/_sent_mail/1.","Message-ID: <one@example>"\n'
    source_response = client.post(
        f"/v1/collections/{collection_id}/source-containers:upload",
        headers=auth(root_token),
        data={"original_source_path": "enron/top.csv"},
        files={"file": ("top.csv", source_bytes, "text/csv")},
    )
    assert source_response.status_code == 201, source_response.text
    source_artifact = source_response.json()["artifact"]
    assert source_artifact["role"] == "SOURCE_CONTAINER"

    email_bytes = (
        b"Message-ID: <one@example>\n"
        b"Date: Mon, 14 May 2001 16:39:00 -0700\n"
        b"From: phillip.allen@enron.com\n"
        b"To: tim.belden@enron.com\n"
        b"Subject: Test message\n\nHello\n"
    )
    metadata = {
        "source_item_id": "allen-p/_sent_mail/1.",
        "record_type": "EMAIL",
        "original_filename": "1.eml",
        "original_source_path": "allen-p/_sent_mail/1.",
        "custodian_ids": [custodian_id],
        "primary_custodian_id": custodian_id,
        "processing_status": "READY",
        "source_container_artifact_id": source_artifact["id"],
        "email": {
            "sender": "phillip.allen@enron.com",
            "subject": "Test message",
            "sent_at": "2001-05-14T23:39:00Z",
            "message_id": "<one@example>",
            "recipients": [
                {"recipient_type": "TO", "email_address": "tim.belden@enron.com"},
            ],
        },
        "raw_metadata": {"file": "allen-p/_sent_mail/1."},
        "unmapped_metadata": {},
    }
    item_response = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("1.eml", email_bytes, "message/rfc822")},
    )
    assert item_response.status_code == 201, item_response.text
    payload = item_response.json()
    assert payload["created"] is True
    assert payload["item"]["email"]["subject"] == "Test message"
    assert payload["item"]["file_date"] == "2001-05-14T23:39:00Z"
    assert payload["item"]["custodian_ids"] == [custodian_id]
    artifact_id = payload["item"]["native_artifact"]["id"]
    sha256 = payload["item"]["native_artifact"]["sha256"]

    artifacts_response = client.get(
        f"/v1/collection-items/{payload['item']['id']}/artifacts",
        headers=auth(root_token),
    )
    assert artifacts_response.status_code == 200, artifacts_response.text
    assert [(artifact["id"], artifact["role"]) for artifact in artifacts_response.json()] == [
        (artifact_id, "NATIVE")
    ]

    selection_response = client.post(
        f"/v1/collections/{collection_id}/selections",
        headers=auth(root_token),
        json={
            "request_id": str(uuid.uuid4()),
            "mode": "QUERY",
            "q": None,
            "custodian_ids": [],
            "file_extensions": [],
            "record_types": [],
            "processing_statuses": [],
            "item_ids": [],
        },
    )
    assert selection_response.status_code == 201, selection_response.text
    batch_response = client.get(
        f"/v1/collection-selections/{selection_response.json()['id']}/items",
        headers=auth(root_token),
    )
    assert batch_response.status_code == 200, batch_response.text
    assert batch_response.json()["items"] == [
        {
            "item_id": payload["item"]["id"],
            "custodian_ids": [custodian_id],
            "custodians": [{"custodian_id": custodian_id, "relationship_type": "PRIMARY"}],
        }
    ]

    custodian_summary_response = client.get(
        f"/v1/collections/{collection_id}/custodians",
        headers=auth(root_token),
    )
    assert custodian_summary_response.status_code == 200, custodian_summary_response.text
    assert custodian_summary_response.json() == [{"custodian_id": custodian_id, "item_count": 1}]

    content_response = client.get(f"/v1/artifacts/{artifact_id}/content", headers=auth(root_token))
    assert content_response.status_code == 200, content_response.text
    assert content_response.content == email_bytes
    assert content_response.headers["etag"] == sha256

    lineage_response = client.get(f"/v1/artifacts/{artifact_id}/lineage", headers=auth(root_token))
    assert lineage_response.status_code == 200, lineage_response.text
    assert lineage_response.json() == [
        {
            "artifact_id": artifact_id,
            "source_artifact_id": source_artifact["id"],
            "relationship": "EXTRACTED_FROM_CONTAINER",
            "created_at": lineage_response.json()[0]["created_at"],
        }
    ]

    query_response = client.get(
        f"/v1/collections/{collection_id}/items",
        headers=auth(root_token),
        params={"custodian_id": custodian_id, "record_type": "EMAIL", "sha256": sha256},
    )
    assert query_response.status_code == 200, query_response.text
    assert [item["source_item_id"] for item in query_response.json()] == ["allen-p/_sent_mail/1."]

    retry_response = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("1.eml", email_bytes, "message/rfc822")},
    )
    assert retry_response.status_code == 201, retry_response.text
    assert retry_response.json()["created"] is False
    assert retry_response.json()["item"]["id"] == payload["item"]["id"]

    conflict_response = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("1.eml", b"different bytes", "message/rfc822")},
    )
    assert conflict_response.status_code == 409


def test_collection_item_file_date_uses_email_parent_or_source_modified(
    client: TestClient,
    root_token: str,
) -> None:
    tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    client_id = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "File Date Client"},
    ).json()["id"]
    custodian_id = client.post(
        f"/v1/clients/{client_id}/custodians",
        headers=auth(root_token),
        json={"display_name": "Date Custodian"},
    ).json()["id"]
    client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    collection_id = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": "File Date Collection"},
    ).json()["id"]

    email_metadata = {
        "source_item_id": "email-1",
        "record_type": "EMAIL",
        "original_filename": "email-1.eml",
        "custodian_ids": [custodian_id],
        "source_modified_at": "2005-01-02T03:04:05Z",
        "email": {
            "sender": "sender@example.com",
            "subject": "Canonical date",
            "sent_at": "2001-05-14T23:39:00Z",
            "recipients": [],
        },
    }
    email = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(email_metadata)},
        files={"file": ("email-1.eml", b"Subject: Canonical date\n\nBody", "message/rfc822")},
    )
    assert email.status_code == 201, email.text
    assert email.json()["item"]["file_date"] == "2001-05-14T23:39:00Z"

    attachment_metadata = {
        "source_item_id": "email-1#attachment=1",
        "record_type": "FILE",
        "original_filename": "attachment.pdf",
        "custodian_ids": [custodian_id],
        "parent_collection_item_id": email.json()["item"]["id"],
        "source_modified_at": "2006-01-02T03:04:05Z",
    }
    attachment = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(attachment_metadata)},
        files={"file": ("attachment.pdf", b"attachment", "application/pdf")},
    )
    assert attachment.status_code == 201, attachment.text
    assert attachment.json()["item"]["file_date"] == "2001-05-14T23:39:00Z"

    file_metadata = {
        "source_item_id": "collected-file-1",
        "record_type": "FILE",
        "original_filename": "collected.txt",
        "custodian_ids": [custodian_id],
        "source_modified_at": "2007-06-07T08:09:10Z",
    }
    collected_file = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(file_metadata)},
        files={"file": ("collected.txt", b"collected", "text/plain")},
    )
    assert collected_file.status_code == 201, collected_file.text
    assert collected_file.json()["item"]["file_date"] == "2007-06-07T08:09:10Z"

    filtered = client.get(
        f"/v1/collections/{collection_id}/search",
        headers=auth(root_token),
        params={
            "file_date_from": "2007-01-01T00:00:00Z",
            "file_date_to": "2007-12-31T23:59:59Z",
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["source_item_id"] for item in filtered.json()["items"]] == ["collected-file-1"]

    histogram = client.get(
        f"/v1/collections/{collection_id}/date-histogram",
        headers=auth(root_token),
        params={"interval": "year"},
    )
    assert histogram.status_code == 200, histogram.text
    assert histogram.json()["interval"] == "year"
    assert [bucket["start"][:10] for bucket in histogram.json()["buckets"]] == [
        "2001-01-01",
        "2002-01-01",
        "2003-01-01",
        "2004-01-01",
        "2005-01-01",
        "2006-01-01",
        "2007-01-01",
    ]
    assert [bucket["count"] for bucket in histogram.json()["buckets"]] == [2, 0, 0, 0, 0, 0, 1]
    assert histogram.json()["missing_count"] == 0

    selection = client.post(
        f"/v1/collections/{collection_id}/selections",
        headers=auth(root_token),
        json={
            "request_id": str(uuid.uuid4()),
            "mode": "QUERY",
            "file_date_from": "2007-01-01T00:00:00Z",
            "file_date_to": "2007-12-31T23:59:59.999Z",
        },
    )
    assert selection.status_code == 201, selection.text
    assert selection.json()["total_count"] == 1
    batch = client.get(
        f"/v1/collection-selections/{selection.json()['id']}/items",
        headers=auth(root_token),
    )
    assert batch.status_code == 200, batch.text
    assert [item["item_id"] for item in batch.json()["items"]] == [collected_file.json()["item"]["id"]]


def test_collection_text_processing_profile_test_and_run(client: TestClient, root_token: str) -> None:
    tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    client_id = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Processing Client"},
    ).json()["id"]
    custodian_id = client.post(
        f"/v1/clients/{client_id}/custodians",
        headers=auth(root_token),
        json={"display_name": "Test Custodian"},
    ).json()["id"]
    assert client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    ).status_code == 201
    collection_id = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": "Processing Collection"},
    ).json()["id"]
    metadata = {
        "source_item_id": "message-1",
        "record_type": "FILE",
        "original_filename": "message.txt",
        "custodian_ids": [custodian_id],
        "processing_status": "READY",
    }
    upload = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("message.txt", b"Useful line\nCONFIDENTIAL FOOTER\n", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    item_id = upload.json()["item"]["id"]

    profile = client.get(
        f"/v1/collections/{collection_id}/text-processing/profile",
        headers=auth(root_token),
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["revision"] == 0
    assert [rule["id"] for rule in profile.json()["default_rules"]] == [
        "system-lotus-forward-envelope",
        "system-reply-history",
        "system-attachment-placeholder",
        "system-whitespace",
    ]

    rule = {
        "id": "remove-footer",
        "name": "Remove footer",
        "action": "REMOVE_LINE",
        "pattern": "^CONFIDENTIAL FOOTER$",
        "enabled": True,
        "case_sensitive": False,
    }
    save = client.put(
        f"/v1/collections/{collection_id}/text-processing/profile",
        headers=auth(root_token),
        json={"rules": [rule]},
    )
    assert save.status_code == 200, save.text
    assert save.json()["revision"] == 1

    preview = client.post(
        f"/v1/collections/{collection_id}/text-processing:test",
        headers=auth(root_token),
        json={"item_ids": [item_id], "rules": [rule]},
    )
    assert preview.status_code == 200, preview.text
    result = preview.json()["items"][0]
    assert result["normalized_text"] == "Useful line"
    assert result["changes"][-1] == {
        "rule_id": "remove-footer",
        "rule_name": "Remove footer",
        "match_count": 1,
    }

    run = client.post(
        f"/v1/collections/{collection_id}/text-processing/runs",
        headers=auth(root_token),
    )
    assert run.status_code == 202, run.text
    assert run.json()["status"] == "QUEUED"
    history = client.get(
        f"/v1/collections/{collection_id}/text-processing/runs",
        headers=auth(root_token),
    )
    assert history.status_code == 200, history.text
    assert [value["id"] for value in history.json()] == [run.json()["id"]]


def test_text_processing_removes_blocks_and_replaces_matches() -> None:
    rules = [
        TextProcessingRule(
            id="remove-banner",
            name="Remove banner",
            action="REMOVE_BLOCK",
            pattern="^BEGIN BANNER$",
            end_pattern="^END BANNER$",
        ),
        TextProcessingRule(
            id="redact-ticket",
            name="Normalize ticket",
            action="REPLACE",
            pattern=r"TICKET-\d+",
            replacement="TICKET",
        ),
    ]
    result = process_text("Hello\nBEGIN BANNER\nnoise\nEND BANNER\nTICKET-123", rules)
    assert result.text == "Hello\nTICKET"
    assert [change.rule_id for change in result.changes] == ["remove-banner", "redact-ticket"]


def test_default_text_processing_unwraps_lotus_forward_and_attachment_placeholder() -> None:
    source = """
---------------------- Forwarded by Phillip K Allen/HOU/ECT on 10/03/2000
04:30 PM ---------------------------

"George Richards" cbpres@austin.rr.com on 10/03/2000 06:35:56 AM
Please respond to cbpres@austin.rr.com
To: "Phillip Allen" pallen@enron.com
cc: "Larry Lewter" retwell@mail.sanmarcos.net
Subject: Westgate

Westgate

Enclosed are demographics on the Westgate site from Investor's Alliance.

Sincerely,

George W. Richards
President, Creekside Builders, LLC

- winmail.dat
"""
    result = process_text(source, [])
    assert result.text == (
        "Westgate\n\n"
        "Enclosed are demographics on the Westgate site from Investor's Alliance.\n\n"
        "Sincerely,\n\nGeorge W. Richards\nPresident, Creekside Builders, LLC"
    )
    assert [change.rule_id for change in result.changes] == [
        "system-lotus-forward-envelope",
        "system-attachment-placeholder",
    ]

    prior_version = process_text(source, [], processor_version="collection-text-v1")
    assert "Forwarded by Phillip K Allen" in prior_version.text
    assert "- winmail.dat" in prior_version.text


def test_default_text_processing_unwraps_minimal_lotus_from_header() -> None:
    source = """---------------------- Forwarded by Phillip K Allen/HOU/ECT on 09/28/2000
01:09 PM ---------------------------


    From:  Phillip K Allen                           09/28/2000 10:56 AM


Liane,

As we discussed yesterday, I am concerned about the San Juan monthly index.

Sincerely,

Phillip Allen
"""
    result = process_text(source, [])
    assert result.text == (
        "Liane,\n\n"
        "As we discussed yesterday, I am concerned about the San Juan monthly index.\n\n"
        "Sincerely,\n\nPhillip Allen"
    )
    assert [change.rule_id for change in result.changes] == ["system-lotus-forward-envelope"]

    prior_version = process_text(source, [], processor_version="collection-text-v2")
    assert "Forwarded by Phillip K Allen" in prior_version.text


def test_default_text_processing_skips_blank_lines_between_lotus_header_groups() -> None:
    source = """---------------------- Forwarded by Phillip K Allen/HOU/ECT on 10/04/2000
04:23 PM ---------------------------

Enron North America Corp.

From:  Airam Arteaga                           10/04/2000 12:23 PM

To: Phillip K Allen/HOU/ECT@ECT, Thomas A Martin/HOU/ECT@ECT
cc: Rita Hennessy/NA/Enron@Enron
Subject: Var, Reporting and Resources Meeting

Please plan to attend the below meeting:

Topic: Var, Reporting and Resources Meeting
"""
    result = process_text(source, [])
    assert result.text == (
        "Please plan to attend the below meeting:\n\n"
        "Topic: Var, Reporting and Resources Meeting"
    )
    assert [change.rule_id for change in result.changes] == ["system-lotus-forward-envelope"]

    prior_version = process_text(source, [], processor_version="collection-text-v3")
    assert prior_version.text.startswith("To: Phillip K Allen")


def test_default_text_processing_preserves_forwarded_subject_absent_from_body() -> None:
    source = """---------------------- Forwarded by Phillip K Allen/HOU/ECT on 09/26/2000
11:57 AM ---------------------------

"BS Stone" <bs_stone@yahoo.com> on 09/26/2000 04:47:40 AM
To: "jeff" <jeff@freeyellow.com>
cc: "Phillip K Allen" <Phillip.K.Allen@enron.com>
Subject: closing

Jeff,

Is the closing today?
"""
    result = process_text(source, [])
    assert result.text == "Subject: closing\n\nJeff,\n\nIs the closing today?"
    assert [change.rule_id for change in result.changes] == ["system-lotus-forward-envelope"]

    prior_version = process_text(source, [], processor_version="collection-text-v4")
    assert prior_version.text == "Jeff,\n\nIs the closing today?"


def test_default_text_processing_removes_headerless_calendar_forward_marker() -> None:
    source = """---------------------- Forwarded by Phillip K Allen/HOU/ECT on 09/25/2000
02:00 PM ---------------------------


    Invitation
Chairperson: Richard Burchfield
Sent by: Cindy Cicchetti

Start: 09/27/2000 01:00 PM
End: 09/27/2000 02:00 PM

Description: Gas Physical/Financial Positions - Room 2537
"""
    result = process_text(source, [])
    assert result.text == (
        "Invitation\n"
        "Chairperson: Richard Burchfield\n"
        "Sent by: Cindy Cicchetti\n\n"
        "Start: 09/27/2000 01:00 PM\n"
        "End: 09/27/2000 02:00 PM\n\n"
        "Description: Gas Physical/Financial Positions - Room 2537"
    )
    assert [change.rule_id for change in result.changes] == ["system-lotus-forward-envelope"]

    prior_version = process_text(source, [], processor_version="collection-text-v5")
    assert prior_version.text.startswith("---------------------- Forwarded by Phillip K Allen")


def test_default_text_processing_preserves_body_after_single_line_forward_marker() -> None:
    source = """---------------------- Forwarded by Phillip K Allen/HOU/ECT on 05/02/2001 05:26 AM ---------------------------

Ina Rangel
05/01/2001 12:24 PM
To: Phillip K Allen/HOU/ECT@ECT
cc:
Subject: Re: 2- SURVEY - PHILLIP ALLEN

-
Full Name: Phillip Allen

Login ID: pallen
"""
    result = process_text(source, [])
    assert result.text == (
        "Subject: Re: 2- SURVEY - PHILLIP ALLEN\n\n"
        "-\nFull Name: Phillip Allen\n\nLogin ID: pallen"
    )
    assert [change.rule_id for change in result.changes] == ["system-lotus-forward-envelope"]

    prior_version = process_text(source, [], processor_version="collection-text-v6")
    assert prior_version.text == ""


def test_upload_rejects_unknown_custodian(client: TestClient, root_token: str) -> None:
    tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    client_id = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "No Custodian Client"},
    ).json()["id"]
    client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    collection_id = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": "Collection"},
    ).json()["id"]
    metadata = {
        "source_item_id": "file-1",
        "record_type": "FILE",
        "original_filename": "file.txt",
        "custodian_ids": ["00000000-0000-0000-0000-000000000001"],
    }
    response = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("file.txt", b"content", "text/plain")},
    )
    assert response.status_code == 422


def test_collection_search_returns_results_before_requested_disjunctive_facets(
    client: TestClient,
    root_token: str,
) -> None:
    tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    client_id = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Search Client"},
    ).json()["id"]
    custodian_ids = []
    for name in ["Alice Adams", "Bob Baker"]:
        response = client.post(
            f"/v1/clients/{client_id}/custodians",
            headers=auth(root_token),
            json={"display_name": name},
        )
        assert response.status_code == 201, response.text
        custodian_ids.append(response.json()["id"])
    client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    collection_id = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": "Search Collection"},
    ).json()["id"]

    items = [
        ("budget-pdf", "budget.pdf", "/finance/annual", [custodian_ids[0]], "READY"),
        ("legal-notes", "notes.txt", "/legal/notes", [custodian_ids[1]], "NOT_PROCESSED"),
        ("budget-data", "data.csv", "/finance/budget-data", custodian_ids, "READY"),
    ]
    for source_id, filename, source_path, item_custodians, processing_status in items:
        metadata = {
            "source_item_id": source_id,
            "record_type": "FILE",
            "original_filename": filename,
            "original_source_path": source_path,
            "custodian_ids": item_custodians,
            "processing_status": processing_status,
        }
        response = client.post(
            f"/v1/collections/{collection_id}/items:upload",
            headers=auth(root_token),
            data={"metadata": json.dumps(metadata)},
            files={"file": (filename, source_id.encode(), "application/octet-stream")},
        )
        assert response.status_code == 201, response.text

    extension_response = client.get(
        f"/v1/collections/{collection_id}/search",
        headers=auth(root_token),
        params=[("q", "budget"), ("extension", "pdf")],
    )
    assert extension_response.status_code == 200, extension_response.text
    extension_payload = extension_response.json()
    assert extension_payload["total"] == 1
    assert [item["source_item_id"] for item in extension_payload["items"]] == ["budget-pdf"]
    assert "facets" not in extension_payload

    extension_facet = client.get(
        f"/v1/collections/{collection_id}/search/facets/file_extensions",
        headers=auth(root_token),
        params=[("q", "budget"), ("extension", "pdf")],
    )
    assert extension_facet.status_code == 200, extension_facet.text
    assert {entry["value"]: entry["count"] for entry in extension_facet.json()} == {
        "csv": 1,
        "pdf": 1,
    }
    custodian_facet = client.get(
        f"/v1/collections/{collection_id}/search/facets/custodians",
        headers=auth(root_token),
        params=[("q", "budget"), ("extension", "pdf")],
    )
    assert custodian_facet.status_code == 200, custodian_facet.text
    assert custodian_facet.json() == [
        {"value": custodian_ids[0], "count": 1}
    ]

    custodian_response = client.get(
        f"/v1/collections/{collection_id}/search",
        headers=auth(root_token),
        params=[("q", "budget"), ("custodian_id", custodian_ids[1])],
    )
    assert custodian_response.status_code == 200, custodian_response.text
    custodian_payload = custodian_response.json()
    assert custodian_payload["total"] == 1
    assert [item["source_item_id"] for item in custodian_payload["items"]] == ["budget-data"]
    selected_custodian_facet = client.get(
        f"/v1/collections/{collection_id}/search/facets/custodians",
        headers=auth(root_token),
        params=[("q", "budget"), ("custodian_id", custodian_ids[1])],
    )
    assert selected_custodian_facet.status_code == 200, selected_custodian_facet.text
    assert {entry["value"]: entry["count"] for entry in selected_custodian_facet.json()} == {
        custodian_ids[0]: 2,
        custodian_ids[1]: 1,
    }

    path_response = client.get(
        f"/v1/collections/{collection_id}/search",
        headers=auth(root_token),
        params={"q": "/legal/notes"},
    )
    assert path_response.status_code == 200, path_response.text
    assert [item["source_item_id"] for item in path_response.json()["items"]] == ["legal-notes"]


def test_standalone_artifact_api_uses_scoped_delegation(artifact_api_client: TestClient) -> None:
    tenant_id = uuid.uuid4()
    client_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    settings = get_artifact_settings()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": settings.delegation_issuer,
            "aud": settings.delegation_audience,
            "sub": str(actor_id),
            "tenants": [str(tenant_id)],
            "clients": [[str(tenant_id), str(client_id)]],
            "custodians": [],
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.delegation_secret,
        algorithm="HS256",
    )
    headers = auth(token)

    unauthorized = artifact_api_client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        json={"tenant_slug": "standalone"},
    )
    assert unauthorized.status_code == 401

    storage_response = artifact_api_client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=headers,
        json={"tenant_slug": "standalone"},
    )
    assert storage_response.status_code == 201, storage_response.text

    collection_response = artifact_api_client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=headers,
        json={"name": "Standalone Collection"},
    )
    assert collection_response.status_code == 201, collection_response.text

    deletion_response = artifact_api_client.post(
        f"/v1/internal/collections/{collection_response.json()['id']}/deletions",
        headers=headers,
    )
    assert deletion_response.status_code == 202, deletion_response.text
    deletion = deletion_response.json()
    assert deletion["status"] == "QUEUED"

    failed_response = artifact_api_client.post(
        f"/v1/internal/collection-deletions/{deletion['id']}/failure",
        headers=headers,
        json={"message": "dispatch unavailable"},
    )
    assert failed_response.status_code == 200, failed_response.text
    assert failed_response.json()["status"] == "FAILED"

    retry_response = artifact_api_client.post(
        f"/v1/internal/collection-deletions/{deletion['id']}/retry",
        headers=headers,
    )
    assert retry_response.status_code == 202, retry_response.text
    assert retry_response.json()["status"] == "QUEUED"
    assert retry_response.json()["attempt_count"] == 2
