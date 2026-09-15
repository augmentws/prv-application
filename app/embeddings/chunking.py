import hashlib
import re
from dataclasses import dataclass
from itertools import pairwise


@dataclass(frozen=True)
class TextChunk:
    chunk_id: str
    ordinal: int
    char_start: int
    char_end: int
    text: str


def _nonempty_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _split_long_span(text: str, start: int, end: int, max_characters: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > max_characters:
        boundary = text.rfind(" ", cursor, cursor + max_characters + 1)
        if boundary <= cursor:
            boundary = cursor + max_characters
        span = _nonempty_span(text, cursor, boundary)
        if span:
            spans.append(span)
        cursor = boundary
    span = _nonempty_span(text, cursor, end)
    if span:
        spans.append(span)
    return spans


def _semantic_units(text: str, max_characters: int) -> list[tuple[int, int]]:
    boundaries = [0]
    boundary_pattern = re.compile(r"(?<=[.!?])(?:[\"')\]]*)\s+|\n\s*\n+")
    boundaries.extend(match.end() for match in boundary_pattern.finditer(text))
    boundaries.append(len(text))
    units: list[tuple[int, int]] = []
    for start, end in pairwise(boundaries):
        span = _nonempty_span(text, start, end)
        if span is None:
            continue
        if span[1] - span[0] > max_characters:
            units.extend(_split_long_span(text, span[0], span[1], max_characters))
        else:
            units.append(span)
    return units


def semantic_chunks(
    text: str,
    *,
    source_hash: str,
    target_characters: int,
    max_characters: int,
    overlap_characters: int,
) -> list[TextChunk]:
    if target_characters > max_characters:
        raise ValueError("Chunk target characters must not exceed chunk maximum characters")
    units = _semantic_units(text, max_characters)
    chunks: list[TextChunk] = []
    current: list[tuple[int, int]] = []
    added_since_emit = False

    def emit() -> None:
        nonlocal current, added_since_emit
        if not current:
            return
        start, end = current[0][0], current[-1][1]
        value = text[start:end].strip()
        if value:
            ordinal = len(chunks)
            identity = f"{source_hash}:{ordinal}:{start}:{end}".encode()
            chunks.append(
                TextChunk(
                    chunk_id=hashlib.sha256(identity).hexdigest(),
                    ordinal=ordinal,
                    char_start=start,
                    char_end=end,
                    text=value,
                )
            )
        retained: list[tuple[int, int]] = []
        for unit in reversed(current):
            if end - unit[0] > overlap_characters:
                break
            retained.insert(0, unit)
        current = retained
        added_since_emit = False

    for unit in units:
        if current and unit[1] - current[0][0] > max_characters:
            emit()
            if current and unit[1] - current[0][0] > max_characters:
                current = []
        current.append(unit)
        added_since_emit = True
        if current[-1][1] - current[0][0] >= target_characters:
            emit()
    if current and added_since_emit:
        final_span = (current[0][0], current[-1][1])
        if not chunks or (chunks[-1].char_start, chunks[-1].char_end) != final_span:
            emit()
    return chunks
