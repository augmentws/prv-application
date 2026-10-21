import pytest

from app.matter_entities import clean_metadata_email, extract_metadata_emails


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"john@example.com"', "john@example.com"),
        ("'john@example.com'", "john@example.com"),
        ("“john@example.com”", "john@example.com"),
        ('John Doe <"john@example.com">', "john@example.com"),
        ('john@example.com"', "john@example.com"),
        ('"john"@example.com', "john@example.com"),
        ("'john'@example.com", "john@example.com"),
        ("o'connor@example.com", "o'connor@example.com"),
    ],
)
def test_clean_metadata_email_removes_import_wrappers(raw: str, expected: str) -> None:
    assert clean_metadata_email(raw) == expected


def test_extract_metadata_emails_handles_multiple_and_rejects_invalid_values() -> None:
    assert extract_metadata_emails("one@example.com; 'two@example.com'") == [
        "one@example.com",
        "two@example.com",
    ]
    assert extract_metadata_emails("'not an email'") == []
