import uuid
from io import BytesIO

from conftest import ArtifactTestingSessionLocal
from sqlalchemy import func, select

from artifact_service.models import ContentBlob, TenantStorage
from artifact_service.service import get_or_create_blob
from artifact_service.storage import MemoryBlobStorage


def test_get_or_create_blob_reuses_winner_after_concurrent_digest_insert() -> None:
    tenant_id = uuid.uuid4()
    bucket_name = "race-bucket"
    digest = "a" * 64
    winner_id = uuid.uuid4()

    class RacingStorage(MemoryBlobStorage):
        inserted_winner = False

        def put_fileobj(self, bucket_name: str, storage_key: str, fileobj: BytesIO, media_type: str) -> None:
            super().put_fileobj(bucket_name, storage_key, fileobj, media_type)
            if self.inserted_winner:
                return
            self.inserted_winner = True
            self.buckets[bucket_name]["v1/blobs/winner"] = b"content"
            with ArtifactTestingSessionLocal() as winner_db:
                winner_db.add(
                    ContentBlob(
                        id=winner_id,
                        tenant_id=tenant_id,
                        sha256=digest,
                        bucket_name=bucket_name,
                        storage_key="v1/blobs/winner",
                        byte_length=7,
                        media_type=media_type,
                    )
                )
                winner_db.commit()

    storage = RacingStorage()
    storage.ensure_bucket(bucket_name)
    with ArtifactTestingSessionLocal() as db:
        tenant_storage = TenantStorage(
            tenant_id=tenant_id,
            tenant_slug_snapshot="race-tenant",
            bucket_name=bucket_name,
            status="ACTIVE",
        )
        db.add(tenant_storage)
        db.commit()

        blob = get_or_create_blob(
            db,
            storage,
            tenant_storage,
            tenant_id,
            BytesIO(b"content"),
            digest,
            7,
            "text/plain",
        )
        db.commit()

        assert blob.id == winner_id
        assert db.scalar(select(func.count()).select_from(ContentBlob)) == 1
        assert set(storage.buckets[bucket_name]) == {"v1/blobs/winner"}
