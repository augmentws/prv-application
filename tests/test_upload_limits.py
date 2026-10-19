import io

import pytest
from fastapi import HTTPException, UploadFile

from artifact_service.config import get_artifact_settings
from artifact_service.service import stage_upload

ONE_MEBIBYTE = 1024 * 1024


@pytest.fixture
def small_upload_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARTIFACT_UPLOAD_MAX_BYTES", str(ONE_MEBIBYTE))
    get_artifact_settings.cache_clear()
    yield ONE_MEBIBYTE
    monkeypatch.undo()
    get_artifact_settings.cache_clear()


def test_upload_at_the_limit_is_staged(small_upload_limit: int) -> None:
    upload = UploadFile(file=io.BytesIO(b"x" * small_upload_limit), filename="exact.bin")

    staged, _, byte_length = stage_upload(upload)

    assert byte_length == small_upload_limit
    staged.close()


def test_upload_over_the_limit_is_rejected_with_413(small_upload_limit: int) -> None:
    upload = UploadFile(file=io.BytesIO(b"x" * (small_upload_limit + 1)), filename="too-large.bin")

    with pytest.raises(HTTPException) as rejected:
        stage_upload(upload)

    assert rejected.value.status_code == 413
