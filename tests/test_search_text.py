import uuid
from email.message import EmailMessage

from app.artifact_gateway import SearchTextArtifact, _search_text_candidate
from app.search.text import extract_search_text


def test_extracts_plain_email_body_without_attachment_text() -> None:
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "recipient@example.com"
    message["Subject"] = "Quarterly update"
    message.set_content("The confidential project is Juniper.")
    message.add_attachment(b"attachment words", maintype="text", subtype="plain", filename="notes.txt")

    body = extract_search_text(
        message.as_bytes(),
        role="NATIVE",
        media_type="message/rfc822",
        filename="message.eml",
        record_type="EMAIL",
    )

    assert body == "The confidential project is Juniper."


def test_uses_html_when_email_has_no_plain_body() -> None:
    message = EmailMessage()
    message["Subject"] = "HTML only"
    message.set_content("<p>Project <strong>Maple</strong></p><script>ignored()</script>", subtype="html")

    body = extract_search_text(
        message.as_bytes(),
        role="NATIVE",
        media_type="message/rfc822",
        filename="message.eml",
        record_type="EMAIL",
    )

    assert body == "Project Maple"


def test_native_binary_requires_a_derived_text_artifact() -> None:
    assert (
        extract_search_text(
            b"not searchable",
            role="NATIVE",
            media_type="application/pdf",
            filename="document.pdf",
            record_type="FILE",
        )
        is None
    )


def test_search_text_artifact_precedence_is_extracted_then_ocr_then_native() -> None:
    native = SearchTextArtifact(uuid.uuid4(), "NATIVE", "message.eml", "message/rfc822")
    ocr = SearchTextArtifact(uuid.uuid4(), "OCR_TEXT", "message-ocr.txt", "text/plain")
    extracted = SearchTextArtifact(uuid.uuid4(), "EXTRACTED_TEXT", "message.txt", "text/plain")

    assert _search_text_candidate([native, ocr, extracted], record_type="EMAIL") == extracted
    assert _search_text_candidate([native, ocr], record_type="EMAIL") == ocr
    assert _search_text_candidate([native], record_type="EMAIL") == native
