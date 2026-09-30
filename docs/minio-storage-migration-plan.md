# MinIO storage migration plan

## Objective

Move MinIO object data out of Docker Desktop's managed filesystem and onto a host-managed filesystem
without changing Artifact Service bucket names, object keys, or database records.

The immediate purpose is to remove the Docker VM inode pressure caused by the current object population.
The current Artifact database contains approximately 4.9 million content blobs. The MinIO object layout,
rather than PostgreSQL or an abandoned OpenSearch image, is therefore the principal consumer of Docker
filesystem inodes.

## Target design

Keep MinIO containerized, but replace its Compose named volume with an absolute host bind mount:

```text
Artifact Service -> http://minio:9000 -> MinIO container -> host-managed MinIO data directory
```

The proposed default host path is:

```text
/Users/petercharles/priv-view-data/minio
```

The path must be configurable through `PVR_MINIO_DATA_DIR`, must be outside the source repository, and
must not be in an automatically synchronized folder such as iCloud Drive. The existing MinIO endpoint,
credentials, bucket names, and opaque `v1/blobs/{content_blob_id}` keys remain unchanged. Consequently,
the Artifact database does not require a migration.

An external S3-compatible service remains the preferred longer-term production design. The same
object-level migration procedure can copy to that service, after which the Artifact Service storage
endpoint and credentials can be changed instead of using a host bind mount.

## Migration principles

- Copy through the S3 API with MinIO Client (`mc mirror`); do not copy MinIO's internal filesystem while
  MinIO is running.
- Use a temporary second MinIO instance backed by the destination bind mount.
- Perform the large initial mirror before the maintenance window.
- Stop every artifact writer for a final synchronization and cutover.
- Validate the destination before writes resume.
- Retain the source Docker volume until the new storage has passed an agreed observation period.
- Do not use `mc mirror --remove` during the initial migration. An unexpected alias or path error must
  not be able to delete destination objects.

MinIO documents `mc mirror` as supporting synchronization between filesystems, MinIO deployments, and
other S3-compatible services. Docker documents bind mounts as direct mappings from a host path into a
container:

- [MinIO Client `mc mirror`](https://docs.min.io/aistor/reference/cli/mc-mirror/)
- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

## Phase 0: restore migration headroom

The Docker VM currently has too little free inode capacity to conduct a reliable migration. Before
starting the copy:

1. Keep OpenSearch stopped unless it is needed for validation.
2. Increase the Docker Desktop virtual disk allocation enough to restore operational headroom.
3. Confirm that Docker can create a small container and that PostgreSQL can create temporary files.
4. Record Docker filesystem byte and inode use before migration.

Deleting more images is not expected to provide meaningful relief. The previously removed OpenSearch
data and image recovered only a small number of inodes compared with the approximately 4.9 million
content blobs.

**Exit criterion:** Docker and PostgreSQL can create temporary files, and there is enough free capacity
for the migration tools to operate throughout the copy.

## Phase 1: inventory and capacity gate

Start PostgreSQL, but do not start unnecessary processing workers. Record the authoritative payload
count and logical byte total from the Artifact database:

```sql
SELECT
    count(*) AS blob_count,
    sum(byte_length) AS logical_bytes,
    pg_size_pretty(sum(byte_length)) AS logical_size
FROM content_blob;
```

Also record:

- every bucket name referenced by `content_blob`;
- the source bucket object count and byte total reported through the S3 API;
- the size of MinIO system metadata;
- the destination filesystem's available bytes;
- a database backup taken before cutover.

The destination must fit the source's measured physical use plus at least 20% headroom. For a population
of millions of small objects, the database's sum of `byte_length` is not sufficient by itself because it
does not include filesystem and MinIO metadata overhead.

**Exit criterion:** capacity is sufficient, bucket inventory is recorded, and the pre-cutover database
backup has completed.

## Phase 2: prepare the destination

1. Create the host directory explicitly. Do not rely on Compose to silently create a misspelled path.
2. Verify that Docker Desktop is allowed to share the selected host path.
3. Restrict directory permissions to the account and runtime that operate MinIO.
4. Start a temporary destination MinIO using the exact source MinIO release, different host ports such
   as `9100` and `9101`, and the host directory mounted at `/data`.
5. Create MinIO Client aliases for the source and destination and verify them with read-only listing
   operations before starting the mirror.

The temporary destination exists only for the migration. It lets MinIO create its own correct on-disk
layout instead of depending on an unsupported raw copy of the source volume.

**Exit criterion:** the destination health endpoint responds, the expected empty bucket can be created,
and a test object can be uploaded, downloaded, checksum-verified, and removed.

## Phase 3: initial online mirror

Mirror every source bucket into a bucket with the same name on the temporary destination. The application
may continue operating during this pass, although pausing high-volume imports will shorten the final
maintenance window.

Operational requirements:

- run the mirror from a host process or dedicated migration container whose logs are retained;
- set a conservative concurrency level initially and watch source latency, destination latency, Docker
  inode use, host disk use, and errors;
- allow retries for transient object-read or object-write failures;
- do not treat the first pass as a consistent snapshot because objects may be added while it runs;
- do not change the Artifact Service endpoint during this phase.

With millions of objects, object count is likely to dominate elapsed time. Use the observed processing
rate from the first sustained interval to estimate the completion time instead of assuming throughput
from total bytes alone.

**Exit criterion:** the initial mirror completes without unresolved errors, and destination counts and
bytes are plausibly consistent with the source snapshot taken at the end of the pass.

## Phase 4: write freeze and final synchronization

Schedule a maintenance window and prevent all writes to Artifact storage:

1. Stop or otherwise block new uploads and imports.
2. Stop `workflow-worker`.
3. Stop `core-api` and `artifact-api`, unless a verified read-only mode is introduced before the
   migration.
4. Confirm there are no active import, text-processing, embedding, deletion, or matter-publication jobs.
5. Run a final `mc mirror --overwrite` pass from source to destination.
6. Preserve the mirror logs and record the completion time.

The source MinIO instance may remain running during the final mirror so that it can serve the copy, but
no application process may write to it.

**Exit criterion:** the final mirror has no unresolved errors and the source has remained write-quiescent
since the final pass began.

## Phase 5: destination verification

Perform verification before changing the production MinIO mount:

1. Compare source and destination bucket names.
2. Compare object counts and total logical bytes.
3. Confirm that every `content_blob.bucket_name` and `content_blob.storage_key` sampled from PostgreSQL
   exists at the destination.
4. Download a deterministic sample distributed across the key space and compare each object's SHA-256
   digest and byte length with `content_blob.sha256` and `content_blob.byte_length`.
5. Include native files, source containers, extracted text, OCR text, images, Parquet chunk sets, and
   vector sets in the sample when those artifact types exist.
6. Investigate every missing object or checksum mismatch. Do not proceed by accepting an unexplained
   discrepancy.

A full S3 listing of millions of objects will be slow. It should run once as a formal migration check,
while checksum verification may use a large deterministic sample unless policy requires reading every
object.

**Exit criterion:** counts and bytes reconcile, no required object is missing, and the checksum sample
has no mismatch.

## Phase 6: Compose cutover

Change the `minio` service in `compose.yaml` from the managed volume:

```yaml
volumes:
  - pvr_artifact_minio:/data
```

to an explicit bind mount:

```yaml
volumes:
  - type: bind
    source: ${PVR_MINIO_DATA_DIR}
    target: /data
    bind:
      create_host_path: false
```

Set the local environment value explicitly:

```text
PVR_MINIO_DATA_DIR=/Users/petercharles/priv-view-data/minio
```

Then:

1. Stop the temporary destination MinIO cleanly.
2. Stop the original `minio` service.
3. Start the normal `minio` service with the bind-mounted destination.
4. Confirm that its health endpoint, bucket inventory, and object reads are successful.
5. Start `artifact-api` and `core-api` while workers remain stopped.
6. Run read-only application smoke tests.
7. Perform one controlled upload/download/delete test against a disposable test artifact or bucket.
8. Start `workflow-worker` and resume imports only after validation succeeds.

The top-level `pvr_artifact_minio` volume declaration should remain temporarily so the old volume is easy
to restore during the rollback period.

**Exit criterion:** application reads and controlled writes succeed through the unchanged
`http://minio:9000` endpoint, and background processing resumes without storage errors.

## Rollback plan

Before normal writes resume, rollback is straightforward:

1. Stop application services and MinIO.
2. Restore `pvr_artifact_minio:/data` in `compose.yaml`.
3. Start the original MinIO and verify its health.
4. Restart the application services.

After normal writes resume on the destination, the old volume is no longer current. A later rollback
requires another write freeze and an object-level reverse mirror of all new destination objects before
the mount can be reverted. The rollback decision should therefore be made during the initial read-only
validation period whenever possible.

Do not delete the destination after rollback; retain both copies until the incident has been diagnosed.

## Phase 7: observation and cleanup

For the agreed observation period, monitor:

- MinIO health, request errors, and latency;
- Artifact API missing-object and checksum errors;
- failed imports and processing jobs;
- host filesystem capacity;
- Docker filesystem inode use;
- performance of representative large and small artifact reads.

After acceptance:

1. Take or confirm an independent backup of the new MinIO directory.
2. Remove the temporary migration service and migration-only credentials.
3. Remove the unused `pvr_artifact_minio` declaration from Compose.
4. Delete the old Docker volume only as a separately approved destructive operation.
5. Confirm that Docker filesystem inode use has fallen as expected.
6. Record the migration date, source and destination counts, verification results, and deletion date in
   the operational change record.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Docker runs out of inodes during migration | Increase Docker disk capacity first and keep OpenSearch stopped. |
| Live writes produce an inconsistent copy | Use an online first pass followed by a complete write freeze and final mirror. |
| Raw filesystem copy produces invalid MinIO state | Migrate through the S3 API into a running destination MinIO. |
| Wrong mirror alias or bucket damages data | Validate aliases with read-only listings and omit `--remove`. |
| Destination lacks capacity | Gate on measured physical source use plus at least 20% headroom. |
| Millions of small files perform poorly through a macOS bind mount | Benchmark during the online pass; move to external S3-compatible storage if latency is unacceptable. |
| Rollback loses post-cutover uploads | Validate before reopening writes; otherwise reverse-mirror new objects before rollback. |
| Old volume is deleted prematurely | Retain it through the observation period and require separate approval for deletion. |

## Completion criteria

The migration is complete only when:

- the normal MinIO service is using the configured host bind mount;
- source and destination inventories reconcile;
- the checksum verification sample has no mismatches;
- application read, upload, download, range-read, and background-processing tests pass;
- no storage-related errors occur during the observation period;
- an independent backup of the destination exists; and
- the old Docker volume has been deliberately retired or documented as a retained rollback copy.
