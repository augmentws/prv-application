import json
from unittest.mock import Mock

import pytest

from app.search.client import OpenSearchBulkError, OpenSearchClient, OpenSearchError


def _bulk_document_ids(content: str) -> list[str]:
    lines = content.strip().splitlines()
    return [next(iter(json.loads(lines[index]).values()))["_id"] for index in range(0, len(lines), 2)]


def test_bulk_recursively_halves_requests_rejected_as_too_large() -> None:
    client = object.__new__(OpenSearchClient)
    requests: list[list[str]] = []

    def request(_method: str, _path: str, **kwargs) -> dict:
        document_ids = _bulk_document_ids(kwargs["content"])
        requests.append(document_ids)
        if len(document_ids) > 2:
            raise OpenSearchError("OpenSearch returned 413: request entity too large", status_code=413)
        return {"errors": False}

    client._request = Mock(side_effect=request)
    client.bulk(
        "matter-documents",
        [("index", str(index), {"value": index}) for index in range(5)],
    )

    assert requests == [
        ["0", "1", "2", "3", "4"],
        ["0", "1"],
        ["2", "3", "4"],
        ["2"],
        ["3", "4"],
    ]


def test_bulk_reraises_413_for_one_oversized_document() -> None:
    client = object.__new__(OpenSearchClient)
    client._request = Mock(
        side_effect=OpenSearchError("OpenSearch returned 413: request entity too large", status_code=413)
    )

    with pytest.raises(OpenSearchBulkError, match="individual payload") as caught:
        client.bulk("matter-documents", [("index", "large-document", {"body": "too large"})])

    assert caught.value.failed_document_ids == ["large-document"]
    assert caught.value.retryable is False
    client._request.assert_called_once()


def test_bulk_skips_one_oversized_document_and_processes_remaining_siblings() -> None:
    client = object.__new__(OpenSearchClient)
    requests: list[list[str]] = []

    def request(_method: str, _path: str, **kwargs) -> dict:
        document_ids = _bulk_document_ids(kwargs["content"])
        requests.append(document_ids)
        if "large-document" in document_ids:
            raise OpenSearchError("OpenSearch returned 413: request entity too large", status_code=413)
        return {"errors": False}

    client._request = Mock(side_effect=request)

    with pytest.raises(OpenSearchBulkError) as caught:
        client.bulk(
            "matter-documents",
            [
                ("index", "before", {"value": 1}),
                ("index", "large-document", {"body": "too large"}),
                ("index", "after", {"value": 2}),
            ],
        )

    assert caught.value.failed_document_ids == ["large-document"]
    assert ["after"] in requests
