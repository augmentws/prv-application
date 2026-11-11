from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from artifact_service.storage import S3BlobStorage


def test_s3_storage_uses_configured_connection_pool_size() -> None:
    settings = SimpleNamespace(
        storage_endpoint_url="http://minio.test:9000",
        storage_access_key="access-key",
        storage_secret_key="secret-key",
        storage_region="us-east-1",
        storage_max_pool_connections=48,
    )

    with (
        patch("artifact_service.storage.get_artifact_settings", return_value=settings),
        patch("artifact_service.storage.boto3.client") as client,
    ):
        S3BlobStorage()

    config = client.call_args.kwargs["config"]
    assert config.max_pool_connections == 48


def test_s3_storage_bulk_deletes_in_groups_of_one_thousand() -> None:
    settings = SimpleNamespace(
        storage_endpoint_url="http://minio.test:9000",
        storage_access_key="access-key",
        storage_secret_key="secret-key",
        storage_region="us-east-1",
        storage_max_pool_connections=48,
    )
    client = MagicMock()
    client.delete_objects.return_value = {}

    with (
        patch("artifact_service.storage.get_artifact_settings", return_value=settings),
        patch("artifact_service.storage.boto3.client", return_value=client),
    ):
        storage = S3BlobStorage()

    storage.delete_many("evidence", [f"objects/{index}" for index in range(1_001)])

    assert client.delete_objects.call_count == 2
    assert len(client.delete_objects.call_args_list[0].kwargs["Delete"]["Objects"]) == 1_000
    assert len(client.delete_objects.call_args_list[1].kwargs["Delete"]["Objects"]) == 1


def test_s3_storage_bulk_delete_raises_for_per_object_errors() -> None:
    settings = SimpleNamespace(
        storage_endpoint_url="http://minio.test:9000",
        storage_access_key="access-key",
        storage_secret_key="secret-key",
        storage_region="us-east-1",
        storage_max_pool_connections=48,
    )
    client = MagicMock()
    client.delete_objects.return_value = {
        "Errors": [{"Key": "objects/blocked", "Code": "AccessDenied"}]
    }

    with (
        patch("artifact_service.storage.get_artifact_settings", return_value=settings),
        patch("artifact_service.storage.boto3.client", return_value=client),
    ):
        storage = S3BlobStorage()

    with pytest.raises(RuntimeError, match="objects/blocked: AccessDenied"):
        storage.delete_many("evidence", ["objects/blocked"])
