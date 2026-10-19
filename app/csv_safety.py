from typing import Any

# Characters that make a spreadsheet treat a cell as a formula, which can run commands or fetch data.
FORMULA_PREFIXES = ("=", "+", "-", "@")


def neutralize_formula(value: str) -> str:
    """Prefix a leading formula trigger with an apostrophe so spreadsheets show it as text."""

    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def spreadsheet_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a CSV row with every text cell neutralized; non-text cells are unchanged."""

    return {key: neutralize_formula(value) if isinstance(value, str) else value for key, value in row.items()}
