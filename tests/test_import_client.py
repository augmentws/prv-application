import base64
import csv
import hashlib
import io
import json
import mailbox
import threading
import warnings
import zipfile
from email.message import EmailMessage
from pathlib import Path

import httpx

from scripts.import_client.adapters import emc2
from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.adapters.emc2 import Emc2Adapter
from scripts.import_client.adapters.enron_csv import EnronCsvAdapter
from scripts.import_client.adapters.jeb_bush_inventory import (
    JebBushInventoryAdapter,
    MissingInventorySourceWarning,
)
from scripts.import_client.api import OpenApiClient, _valid_json_unicode
from scripts.import_client.importer import BaseImporter
from scripts.import_client.models import CustodianSpec, EmailMetadata, EmailRecipient, ImportItem, SourceContainer

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


def test_jeb_bush_inventory_imports_eml_and_text_sidecars_only(
    tmp_path: Path,
) -> None:
    email_directory = tmp_path / "01 January 2003"
    attachment_directory = email_directory / "1315"
    attachment_directory.mkdir(parents=True)

    message = EmailMessage()
    message["Message-ID"] = "<1315@example>"
    message["From"] = "sender@example.com"
    message["To"] = "jeb@jeb.org"
    message["Subject"] = "Scotland"
    message.set_content("See attachment")
    message.add_attachment(
        b"native document",
        maintype="application",
        subtype="msword",
        filename="Scotland.doc",
    )
    eml_path = email_directory / "1315.eml"
    eml_path.write_bytes(message.as_bytes())

    doc_path = attachment_directory / "Scotland.doc"
    doc_path.write_bytes(b"native document")
    doc_sidecar = attachment_directory / "Scotland.doc.txt"
    doc_sidecar.write_text("Extracted Scotland text", encoding="utf-8")
    image_path = attachment_directory / "photo.jpg"
    image_path.write_bytes(b"image")
    (attachment_directory / "photo.jpg.txt").write_text(
        "stale image OCR", encoding="utf-8"
    )

    inventory = tmp_path / "inventory.csv"
    fieldnames = [
        "record_type",
        "eml_path",
        "eml_size_bytes",
        "eml_sha256",
        "attachment_count",
        "attachment_index",
        "attachment_original_name",
        "attachment_saved_path",
        "attachment_content_type",
        "attachment_size_bytes",
        "attachment_sha256",
    ]
    with inventory.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "record_type": "eml",
                "eml_path": "01 January 2003/1315.eml",
                "eml_size_bytes": eml_path.stat().st_size,
                "attachment_count": 2,
            }
        )
        writer.writerow(
            {
                "record_type": "attachment",
                "eml_path": "01 January 2003/1315.eml",
                "attachment_index": 1,
                "attachment_original_name": "Scotland.doc",
                "attachment_saved_path": "01 January 2003/1315/Scotland.doc",
                "attachment_content_type": "application/msword",
                "attachment_size_bytes": doc_path.stat().st_size,
                "attachment_sha256": "doc-hash",
            }
        )
        writer.writerow(
            {
                "record_type": "attachment",
                "eml_path": "01 January 2003/1315.eml",
                "attachment_index": 2,
                "attachment_original_name": "photo.jpg",
                "attachment_saved_path": "01 January 2003/1315/photo.jpg",
                "attachment_content_type": "image/jpeg",
                "attachment_size_bytes": image_path.stat().st_size,
                "attachment_sha256": "image-hash",
            }
        )

    adapter = JebBushInventoryAdapter(inventory)
    container = next(iter(adapter.source_containers()))
    items = list(BaseImporter.iter_preprocessed_items(adapter, container))

    assert [item.record_type for item in items] == ["EMAIL", "FILE"]
    assert items[0].email is not None
    assert items[0].email.subject == "Scotland"
    assert items[1].original_filename == "Scotland.doc.txt"
    assert items[1].content == b"Extracted Scotland text"
    assert items[1].media_type == "text/plain; charset=utf-8"
    assert items[1].parent_source_item_id == items[0].source_item_id
    assert items[1].family_id == items[0].family_id
    assert items[1].raw_metadata["source_attachment_filename"] == "Scotland.doc"
    assert items[1].raw_metadata["source_attachment_sha256"] == "doc-hash"


def test_jeb_bush_inventory_skips_missing_eml_and_its_attachment_rows(
    tmp_path: Path,
) -> None:
    present_message = EmailMessage()
    present_message["Subject"] = "Present"
    present_message.set_content("Available email")
    present_path = tmp_path / "present.eml"
    present_path.write_bytes(present_message.as_bytes())

    missing_sidecar = tmp_path / "missing" / "attachment.doc.txt"
    missing_sidecar.parent.mkdir()
    missing_sidecar.write_text("orphaned extracted text", encoding="utf-8")

    inventory = tmp_path / "inventory.csv"
    fieldnames = [
        "record_type",
        "eml_path",
        "eml_size_bytes",
        "eml_sha256",
        "attachment_index",
        "attachment_original_name",
        "attachment_saved_path",
        "attachment_content_type",
        "attachment_size_bytes",
        "attachment_sha256",
    ]
    with inventory.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"record_type": "eml", "eml_path": "missing.eml"})
        writer.writerow(
            {
                "record_type": "attachment",
                "eml_path": "missing.eml",
                "attachment_index": 1,
                "attachment_original_name": "attachment.doc",
                "attachment_saved_path": "missing/attachment.doc",
                "attachment_content_type": "application/msword",
            }
        )
        writer.writerow({"record_type": "eml", "eml_path": "present.eml"})

    adapter = JebBushInventoryAdapter(inventory)
    container = next(iter(adapter.source_containers()))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        items = list(BaseImporter.iter_preprocessed_items(adapter, container))

    assert [item.original_filename for item in items] == ["present.eml"]
    assert len(caught) == 1
    assert issubclass(caught[0].category, MissingInventorySourceWarning)
    assert "missing.eml" in str(caught[0].message)
    assert "attachment rows" in str(caught[0].message)


def test_jeb_bush_inventory_honors_optional_skip_csv(
    tmp_path: Path,
    capsys,
) -> None:
    skipped_message = EmailMessage()
    skipped_message["Subject"] = "Skipped"
    skipped_message.set_content("Do not import")
    (tmp_path / "skipped.eml").write_bytes(skipped_message.as_bytes())

    present_message = EmailMessage()
    present_message["Subject"] = "Present"
    present_message.set_content("Import this email")
    (tmp_path / "present.eml").write_bytes(present_message.as_bytes())

    skipped_family_sidecar = tmp_path / "skipped" / "family.doc.txt"
    skipped_family_sidecar.parent.mkdir()
    skipped_family_sidecar.write_text("skipped family text", encoding="utf-8")
    skipped_attachment_sidecar = tmp_path / "present" / "skip.doc.txt"
    skipped_attachment_sidecar.parent.mkdir()
    skipped_attachment_sidecar.write_text("skipped attachment text", encoding="utf-8")

    (tmp_path / "skip.csv").write_text(
        "./skipped.eml\npresent\\skip.doc\n",
        encoding="utf-8",
    )
    inventory = tmp_path / "inventory.csv"
    fieldnames = [
        "record_type",
        "eml_path",
        "eml_size_bytes",
        "eml_sha256",
        "attachment_index",
        "attachment_original_name",
        "attachment_saved_path",
        "attachment_content_type",
        "attachment_size_bytes",
        "attachment_sha256",
    ]
    with inventory.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"record_type": "eml", "eml_path": "skipped.eml"})
        writer.writerow(
            {
                "record_type": "attachment",
                "eml_path": "skipped.eml",
                "attachment_original_name": "family.doc",
                "attachment_saved_path": "skipped/family.doc",
                "attachment_content_type": "application/msword",
            }
        )
        writer.writerow({"record_type": "eml", "eml_path": "present.eml"})
        writer.writerow(
            {
                "record_type": "attachment",
                "eml_path": "present.eml",
                "attachment_original_name": "skip.doc",
                "attachment_saved_path": "present/skip.doc",
                "attachment_content_type": "application/msword",
            }
        )

    adapter = JebBushInventoryAdapter(inventory)
    container = next(iter(adapter.source_containers()))
    items = list(BaseImporter.iter_preprocessed_items(adapter, container))
    stderr = capsys.readouterr().err

    assert adapter.skip_paths == {"skipped.eml", "present/skip.doc"}
    assert [item.original_filename for item in items] == ["present.eml"]
    assert stderr.splitlines() == [
        "Skipped by skip.csv: skipped.eml",
        "Skipped by skip.csv: present/skip.doc",
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


class ParallelContainerAdapter(DatasetAdapter):
    dataset_name = "parallel-test"
    default_collection_name = "Parallel test"

    def __init__(self, source: Path) -> None:
        self.source = source

    def source_containers(self):
        yield SourceContainer("source", self.source, "text/plain", "source.txt")

    def custom_items(self, source_container):
        custodian = CustodianSpec("Custodian")
        yield ImportItem("source", "first", "FILE", "first.txt", b"first", (custodian,))
        yield ImportItem("source", "second", "FILE", "second.txt", b"second", (custodian,))


class DirtyEmailContainerAdapter(DatasetAdapter):
    dataset_name = "dirty-email-test"
    default_collection_name = "Dirty email test"

    def __init__(self, source: Path) -> None:
        self.source = source

    def source_containers(self):
        yield SourceContainer("source", self.source, "message/rfc822", "dirty.eml")

    def custom_items(self, source_container):
        yield ImportItem(
            source_container_key="source",
            source_item_id="dirty-email",
            record_type="EMAIL",
            original_filename="dirty.eml",
            content=b"Subject: original dirty headers\n\nBody",
            custodians=(CustodianSpec("Custodian"),),
            media_type="message/rfc822",
            email=EmailMetadata(
                sender="s" * 4001,
                subject=("Before\x00After" + ("x" * 10000)),
                message_id="m" * 1001,
                recipients=(
                    EmailRecipient(
                        recipient_type="TO",
                        display_name="d" * 501,
                        email_address="e" * 501,
                    ),
                ),
            ),
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


class ConcurrentFakeApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.barrier = threading.Barrier(2)
        self.lock = threading.Lock()
        self.active_uploads = 0
        self.maximum_active_uploads = 0

    def upload_item(self, collection_id, filename, content, media_type, metadata):
        with self.lock:
            self.active_uploads += 1
            self.maximum_active_uploads = max(
                self.maximum_active_uploads,
                self.active_uploads,
            )
        self.barrier.wait(timeout=2)
        with self.lock:
            self.item_metadata.append(json.loads(json.dumps(metadata)))
            item_id = f"item-{len(self.item_metadata)}"
            self.active_uploads -= 1
        return {"created": True, "item": {"id": item_id}}


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
        workers=4,
    )

    report = importer.run(SingleContainerAdapter(source))

    assert report.items_created == 2
    assert report.bytes_submitted == 11
    assert api.item_metadata[0]["source_container_artifact_id"] == "source-artifact"
    assert api.item_metadata[1]["parent_collection_item_id"] == "item-1"


def test_base_importer_uploads_independent_items_concurrently(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"container")
    api = ConcurrentFakeApi()
    importer = BaseImporter(
        api,  # type: ignore[arg-type]
        tenant_id="tenant",
        tenant_slug="root",
        client_id="client",
        collection_name="Collection",
        workers=2,
    )

    report = importer.run(ParallelContainerAdapter(source))

    assert report.items_created == 2
    assert report.bytes_submitted == 11
    assert api.maximum_active_uploads == 2


def test_base_importer_warns_and_uploads_dirty_email_metadata(tmp_path: Path) -> None:
    source = tmp_path / "dirty.eml"
    source.write_bytes(b"container")
    api = FakeApi()
    progress: list[str] = []
    importer = BaseImporter(
        api,  # type: ignore[arg-type]
        tenant_id="tenant",
        tenant_slug="root",
        client_id="client",
        collection_name="Collection",
        progress=progress.append,
    )

    report = importer.run(DirtyEmailContainerAdapter(source))

    assert report.failures == []
    assert report.items_created == 1
    payload = api.item_metadata[0]
    email = payload["email"]
    assert len(email["sender"]) == 4000
    assert len(email["subject"]) == 10000
    assert "\x00" not in email["subject"]
    assert email["message_id"] is None
    assert len(email["recipients"][0]["display_name"]) == 500
    assert len(email["recipients"][0]["email_address"]) == 500
    recorded_warnings = payload["raw_metadata"]["email_metadata_warnings"]
    assert len(recorded_warnings) == 6
    assert any("email.subject contained 1 NUL character" in message for message in progress)
    assert any("email.message_id exceeded 1000 characters" in message for message in progress)
