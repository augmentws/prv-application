import uuid
from typing import Any

from sqlalchemy import exists, select

from app.models import JobArchive

MATTER_BULK_TAG_JOB_TYPE = "MATTER_BULK_TAG_JOB"
MATTER_TOPIC_JOB_TYPE = "MATTER_TOPIC_JOB"


def job_not_archived(job_type: str, job_id_column: Any):
    return ~exists(
        select(JobArchive.job_id).where(
            JobArchive.job_type == job_type,
            JobArchive.job_id == job_id_column,
        )
    )


def is_job_archived(db, *, job_type: str, job_id: uuid.UUID) -> bool:
    return db.get(JobArchive, (job_type, job_id)) is not None
