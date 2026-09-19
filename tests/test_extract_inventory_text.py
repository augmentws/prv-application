from __future__ import annotations

import csv
from pathlib import Path

from scripts.extract_inventory_text import Job, Settings, iter_jobs, process_job


def _settings(root: Path, *, ocr: bool = False) -> Settings:
    return Settings(
        root=root,
        overwrite=False,
        ocr=ocr,
        ocr_language="eng",
        ocr_max_pages=10,
        timeout=10,
        tika_jar=None,
    )


def _job(path: str, content_type: str) -> Job:
    return Job(
        record_type="attachment",
        parent_eml_path="message.eml",
        source_relative_path=path,
        source_content_type=content_type,
        inventory_size="",
        inventory_sha256="",
    )


def test_extracts_text_and_html_sidecars(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("First line\r\nSecond line", encoding="utf-8")
    (tmp_path / "page.html").write_text(
        "<p>Visible</p><script>hidden()</script><p>Text</p>", encoding="utf-8"
    )

    text_result = process_job(_job("note.txt", "text/plain"), _settings(tmp_path))
    html_result = process_job(_job("page.html", "text/html"), _settings(tmp_path))

    assert text_result["status"] == "extracted"
    assert html_result["status"] == "extracted"
    assert (tmp_path / "note.txt.txt").read_text(encoding="utf-8") == (
        "First line\nSecond line\n"
    )
    html_text = (tmp_path / "page.html.txt").read_text(encoding="utf-8")
    assert "Visible" in html_text
    assert "Text" in html_text
    assert "hidden" not in html_text


def test_skips_standalone_images_even_when_ocr_is_enabled(tmp_path: Path) -> None:
    (tmp_path / "photo.jpg").write_bytes(b"not-an-image")

    result = process_job(_job("photo.jpg", "image/jpeg"), _settings(tmp_path, ocr=True))

    assert result["status"] == "skipped_image"
    assert not (tmp_path / "photo.jpg.txt").exists()


def test_pdf_extension_takes_precedence_over_incorrect_image_mime(tmp_path: Path) -> None:
    (tmp_path / "scan.pdf").write_bytes(b"not-a-pdf")

    result = process_job(_job("scan.pdf", "image/tiff"), _settings(tmp_path))

    assert result["status"] == "error"
    assert result["extractor"] == "pdf"
    assert result["status"] != "skipped_image"


def test_inventory_defaults_to_attachments_and_can_include_eml(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.csv"
    fieldnames = [
        "record_type",
        "eml_path",
        "eml_size_bytes",
        "eml_sha256",
        "attachment_saved_path",
        "attachment_content_type",
        "attachment_size_bytes",
        "attachment_sha256",
    ]
    with inventory.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "record_type": "eml",
                "eml_path": "message.eml",
                "eml_size_bytes": "100",
            }
        )
        writer.writerow(
            {
                "record_type": "attachment",
                "eml_path": "message.eml",
                "attachment_saved_path": "message/note.txt",
                "attachment_content_type": "text/plain",
                "attachment_size_bytes": "10",
            }
        )

    attachment_jobs = list(iter_jobs(inventory, include_eml=False, limit=None))
    all_jobs = list(iter_jobs(inventory, include_eml=True, limit=None))

    assert [job.record_type for job in attachment_jobs] == ["attachment"]
    assert [job.record_type for job in all_jobs] == ["eml", "attachment"]
