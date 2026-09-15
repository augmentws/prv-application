import base64
import csv
import hashlib
import io
import json
import mailbox
import zipfile
from email.message import EmailMessage
from pathlib import Path

import httpx

from scripts.import_client.adapters import emc2
from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.adapters.emc2 import Emc2Adapter
from scripts.import_client.adapters.enron_csv import EnronCsvAdapter
from scripts.import_client.api import OpenApiClient, _valid_json_unicode
from scripts.import_client.importer import BaseImporter
from scripts.import_client.models import CustodianSpec, ImportItem, SourceContainer

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_openapi_client_resolves_operation_ids_and_authenticates() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/auth/login":
            assert json.loads(request.content) == {
                "email": "admin@example.com",
                "password": "long-enough-password",
            }
            return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh"})
        assert request.headers["authorization"] == "Bearer access"
        return httpx.Response(
            201,
            json={
                "tenant_id": request.url.path.split("/")[3],
                "tenant_slug_snapshot": "root",
                "bucket_name": "pv-artifacts-root-abcdefghij",
                "status": "ACTIVE",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    api = OpenApiClient(
        "http://testserver",
        REPOSITORY_ROOT / "web" / "openapi.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        api.login("admin@example.com", "long-enough-password")
        response = api.ensure_storage("00000000-0000-0000-0000-000000000001", "root")
    finally:
        api.close()

    assert response["bucket_name"] == "pv-artifacts-root-abcdefghij"
    assert [request.method for request in requests] == ["POST", "POST"]


def test_openapi_client_reauthenticates_and_rewinds_upload_after_401() -> None:
    login_count = 0
    upload_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count
        if request.url.path == "/v1/auth/login":
            login_count += 1
            return httpx.Response(
                200,
                json={"access_token": f"access-{login_count}", "refresh_token": f"refresh-{login_count}"},
            )
        upload_requests.append(request)
        if len(upload_requests) == 1:
            return httpx.Response(401, json={"detail": "Invalid or expired token"})
        return httpx.Response(201, json={"artifact": {"id": "source-artifact"}})

    api = OpenApiClient(
        "http://testserver",
        REPOSITORY_ROOT / "web" / "openapi.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        api.login("admin@example.com", "long-enough-password")
        payload = io.BytesIO(b"complete container payload")
        response = api.upload_source_container(
            "collection-1",
            "source.csv",
            payload,
            "text/csv",
            "source.csv",
        )
    finally:
        api.close()

    assert response["artifact"]["id"] == "source-artifact"
    assert login_count == 2
    assert [request.headers["authorization"] for request in upload_requests] == [
        "Bearer access-1",
        "Bearer access-2",
    ]
    assert all(b"complete container payload" in request.content for request in upload_requests)


def test_upload_metadata_replaces_invalid_unicode_surrogates() -> None:
    payload = {
        "email": {
            "subject": "valid \ud800 text",
            "recipients": [{"display_name": "broken \udfff name"}],
        },
        "raw_metadata": {"nested": ["\ud800"]},
    }

    cleaned = _valid_json_unicode(payload)
    encoded = json.dumps(cleaned)

    assert "\\ud800" not in encoded
    assert "\\udfff" not in encoded
    assert cleaned["email"]["subject"] == "valid \ufffd text"
    assert cleaned["email"]["recipients"][0]["display_name"] == "broken \ufffd name"
    assert cleaned["raw_metadata"]["nested"] == ["\ufffd"]


def test_enron_adapter_parses_multiline_email_csv(tmp_path: Path) -> None:
    source = tmp_path / "top.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "message"])
        writer.writeheader()
        writer.writerow(
            {
                "file": "allen-p/_sent_mail/1.",
                "message": (
                    "Message-ID: <one@example>\n"
                    "Date: Mon, 14 May 2001 16:39:00 -0700\n"
                    "From: phillip.allen@enron.com\n"
                    "To: Tim Belden <tim.belden@enron.com>\n"
                    "Subject: Test\n\nHello\n"
                ),
            }
        )

    adapter = EnronCsvAdapter(source)
    container = next(iter(adapter.source_containers()))
    items = list(BaseImporter.iter_preprocessed_items(adapter, container))

    assert container.media_type == "text/csv"
    assert len(items) == 1
    assert items[0].record_type == "EMAIL"
    assert items[0].custodians[0].display_name == "allen-p"
    assert items[0].email is not None
    assert items[0].email.subject == "Test"
    assert items[0].email.recipients[0].email_address == "tim.belden@enron.com"


def test_enron_adapter_accepts_email_larger_than_default_csv_field_limit(tmp_path: Path) -> None:
    source = tmp_path / "enron.csv"
    body = "x" * (csv.field_size_limit() + 1)
    message = f"From: sender@example.com\nSubject: Large message\n\n{body}"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "message"])
        writer.writeheader()
        writer.writerow({"file": "allen-p/inbox/1.", "message": message})

    adapter = EnronCsvAdapter(source)
    container = next(iter(adapter.source_containers()))
    items = list(BaseImporter.iter_preprocessed_items(adapter, container))

    assert len(items) == 1
    assert items[0].content.decode("utf-8") == message


def test_enron_adapter_tolerates_malformed_recipient_headers(tmp_path: Path) -> None:
    source = tmp_path / "enron.csv"
    malformed_to = (
        "foo@enron.com, .casaudoumecq\\@enron.com, "
        "louise.kitchen\\@enron.com, .costa\\@enron.com"
    )
    message = f"From: sender@example.com\nTo: {malformed_to}\nSubject: Test\n\nBody"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "message"])
        writer.writeheader()
        writer.writerow({"file": "allen-p/inbox/2.", "message": message})

    adapter = EnronCsvAdapter(source)
    container = next(iter(adapter.source_containers()))
    items = list(BaseImporter.iter_preprocessed_items(adapter, container))

    assert len(items) == 1
    assert items[0].email is not None
    assert [recipient.email_address for recipient in items[0].email.recipients] == [
        "foo@enron.com",
        ".casaudoumecq\\@enron.com",
        "louise.kitchen\\@enron.com",
        ".costa\\@enron.com",
    ]


def test_emc2_adapter_extracts_mbox_messages_attachments_and_zip_files(tmp_path: Path) -> None:
    custodian_directory = tmp_path / "custodians" / "Benson, Hal"
    custodian_directory.mkdir(parents=True)
    mbox_path = custodian_directory / "hbenson.mbox"
    message = EmailMessage()
    message["Message-ID"] = "<message@example>"
    message["From"] = "hal@example.com"
    message["To"] = "sarah@example.com"
    message["Subject"] = "Report"
    message["Date"] = "Mon, 23 Oct 1995 10:00:00 -0400"
    message.set_content("Attached")
    message.add_attachment(b"evidence", maintype="text", subtype="plain", filename="evidence.txt")
    box = mailbox.mbox(mbox_path)
    box.add(message)
    box.flush()
    box.close()

    doj_directory = tmp_path / "custodians" / "US-DOJ"
    doj_directory.mkdir(parents=True)
    zip_path = doj_directory / "edocs.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("edocs/hacker_chatlog.txt", "chat")
        archive.writestr("__MACOSX/edocs/._hacker_chatlog.txt", "metadata")

    adapter = Emc2Adapter(tmp_path)
    containers = list(adapter.source_containers())
    items = [
        item
        for container in containers
        for item in BaseImporter.iter_preprocessed_items(adapter, container)
    ]

    assert [container.key for container in containers] == [
        "custodians/Benson, Hal/hbenson.mbox",
        "custodians/US-DOJ/edocs.zip",
    ]
    assert [item.record_type for item in items] == ["EMAIL", "FILE", "CHAT"]
    assert items[1].content == b"evidence"
    assert items[1].parent_source_item_id == items[0].source_item_id
    assert items[1].family_id == items[0].family_id
    assert items[2].custodians[0].display_name == "US-DOJ"


def test_emc2_adapter_repairs_a_known_malformed_base64_attachment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custodian_directory = tmp_path / "custodians" / "Benson, Hal"
    custodian_directory.mkdir(parents=True)
    mbox_path = custodian_directory / "hbenson.mbox"
    expected = b"decoded attachment text"
    encoded = base64.b64encode(expected).decode("ascii")
    malformed = encoded[:5] + "A" + encoded[5:]
    encoded_sha256 = hashlib.sha256(malformed.encode("ascii")).hexdigest()
    monkeypatch.setitem(emc2._KNOWN_BASE64_REPAIRS, encoded_sha256, 5)

    raw_message = (
        "From: hal@example.com\n"
        "To: sarah@example.com\n"
        "Subject: Report\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="boundary"\n'
        "\n"
        "--boundary\n"
        "Content-Type: text/plain\n"
        "\n"
        "Attached\n"
        "--boundary\n"
        'Content-Type: text/plain; name="evidence.txt"\n'
        "Content-Transfer-Encoding: base64\n"
        'Content-Disposition: attachment; filename="evidence.txt"\n'
        "\n"
        f"{malformed}\n"
        "--boundary--\n"
    )
    box = mailbox.mbox(mbox_path)
    box.add(mailbox.mboxMessage(raw_message))
    box.flush()
    box.close()

    adapter = Emc2Adapter(tmp_path)
    container = next(iter(adapter.source_containers()))
    items = list(BaseImporter.iter_preprocessed_items(adapter, container))

    assert items[1].content == expected
    assert items[1].raw_metadata["mime_transfer_repair"] == {
        "kind": "KNOWN_EMC2_MALFORMED_BASE64",
        "encoded_payload_sha256": encoded_sha256,
        "removed_character_offset": 5,
    }


class SingleContainerAdapter(DatasetAdapter):
    dataset_name = "test"
    default_collection_name = "Test"

    def __init__(self, source: Path) -> None:
        self.source = source

    def source_containers(self):
        yield SourceContainer("source", self.source, "text/plain", "source.txt")

    def custom_items(self, source_container):
        custodian = CustodianSpec("Custodian")
        yield ImportItem("source", "parent", "FILE", "parent.eml", b"parent", (custodian,))
        yield ImportItem(
            "source",
            "child",
            "FILE",
            "child.txt",
            b"child",
            (custodian,),
            parent_source_item_id="parent",
        )


class FakeApi:
    def __init__(self) -> None:
        self.item_metadata = []

    def ensure_storage(self, tenant_id, tenant_slug):
        return {"tenant_id": tenant_id, "tenant_slug_snapshot": tenant_slug}

    def list_collections(self, tenant_id, client_id):
        return []

    def create_collection(self, tenant_id, client_id, name, description):
        return {"id": "collection", "name": name, "status": "OPEN"}

    def list_custodians(self, client_id):
        return []

    def create_custodian(self, client_id, display_name, email_addresses, external_reference):
        return {"id": "custodian", "display_name": display_name, "status": "ACTIVE"}

    def upload_source_container(self, collection_id, filename, content, media_type, original_source_path):
        assert content.read() == b"container"
        return {"artifact": {"id": "source-artifact"}}

    def upload_item(self, collection_id, filename, content, media_type, metadata):
        self.item_metadata.append(json.loads(json.dumps(metadata)))
        return {"created": True, "item": {"id": f"item-{len(self.item_metadata)}"}}


def test_base_importer_links_children_to_uploaded_parent(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"container")
    api = FakeApi()
    importer = BaseImporter(
        api,  # type: ignore[arg-type]
        tenant_id="tenant",
        tenant_slug="root",
        client_id="client",
        collection_name="Collection",
    )

    report = importer.run(SingleContainerAdapter(source))

    assert report.items_created == 2
    assert report.bytes_submitted == 11
    assert api.item_metadata[0]["source_container_artifact_id"] == "source-artifact"
    assert api.item_metadata[1]["parent_collection_item_id"] == "item-1"
