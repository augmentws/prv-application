from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache
from typing import BinaryIO, Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from artifact_service.config import get_artifact_settings


class BlobStorage(Protocol):
    def ensure_bucket(self, bucket_name: str) -> None: ...

    def put_fileobj(self, bucket_name: str, storage_key: str, fileobj: BinaryIO, media_type: str) -> None: ...

    def open(self, bucket_name: str, storage_key: str) -> BinaryIO: ...


class S3BlobStorage:
    def __init__(self) -> None:
        settings = get_artifact_settings()
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint_url,
            aws_access_key_id=settings.storage_access_key,
            aws_secret_access_key=settings.storage_secret_key,
            region_name=settings.storage_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def ensure_bucket(self, bucket_name: str) -> None:
        try:
            self.client.head_bucket(Bucket=bucket_name)
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
        self.client.create_bucket(Bucket=bucket_name)

    def put_fileobj(self, bucket_name: str, storage_key: str, fileobj: BinaryIO, media_type: str) -> None:
        fileobj.seek(0)
        self.client.upload_fileobj(fileobj, bucket_name, storage_key, ExtraArgs={"ContentType": media_type})

    def open(self, bucket_name: str, storage_key: str) -> BinaryIO:
        response = self.client.get_object(Bucket=bucket_name, Key=storage_key)
        return response["Body"]


class MemoryBlobStorage:
    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, bytes]] = {}

    def ensure_bucket(self, bucket_name: str) -> None:
        self.buckets.setdefault(bucket_name, {})

    def put_fileobj(self, bucket_name: str, storage_key: str, fileobj: BinaryIO, media_type: str) -> None:
        del media_type
        fileobj.seek(0)
        self.buckets.setdefault(bucket_name, {})[storage_key] = fileobj.read()

    def open(self, bucket_name: str, storage_key: str) -> BinaryIO:
        from io import BytesIO

        try:
            return BytesIO(self.buckets[bucket_name][storage_key])
        except KeyError as exc:
            raise FileNotFoundError(storage_key) from exc


@lru_cache
def get_storage() -> BlobStorage:
    return S3BlobStorage()


def iter_file(fileobj: BinaryIO, chunk_size: int = 1024 * 1024) -> Generator[bytes, None, None]:
    try:
        while chunk := fileobj.read(chunk_size):
            yield chunk
    finally:
        fileobj.close()
