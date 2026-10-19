import pytest

from app.csv_safety import neutralize_formula, spreadsheet_safe_row


@pytest.mark.parametrize("value", ["=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)"])
def test_formula_triggers_are_prefixed_with_an_apostrophe(value: str) -> None:
    assert neutralize_formula(value) == f"'{value}"


@pytest.mark.parametrize("value", ["", "plain text", "Name = value", "1-2", "email@example.com", " =leading space"])
def test_ordinary_text_is_left_unchanged(value: str) -> None:
    assert neutralize_formula(value) == value


def test_spreadsheet_safe_row_neutralizes_only_text_cells() -> None:
    row = {"message": "=cmd|' /C calc'!A0", "attempt_number": 2, "ordinal": -1, "payload": None}

    assert spreadsheet_safe_row(row) == {
        "message": "'=cmd|' /C calc'!A0",
        "attempt_number": 2,
        "ordinal": -1,
        "payload": None,
    }
