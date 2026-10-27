# Production Security Deployment Plan

## Purpose

This document records the security and operational work required before Priv-View is deployed in a production environment. It is intentionally separate from the current private-network deployment. The current environment may remain simple while it is reachable only through the trusted private network; production must satisfy the controls below before it receives client or matter data.

This is a deployment gate and runbook outline, not a claim that the checked-in local `compose.yaml` is production-ready.

## Production assumptions

- Client and matter data may be confidential, privileged, personally identifiable, or subject to protective orders.
- PostgreSQL and MinIO are authoritative stores. OpenSearch is a rebuildable projection, but it can still contain sensitive document text, metadata, and vectors.
- Only the browser-facing web application should be reachable by ordinary users.
- Core API, Artifact API, embedding, classification, PostgreSQL, MinIO, OpenSearch, and workflow workers belong on private service networks.
- Administrators reach infrastructure through a VPN, bastion, or similarly controlled management path.
- Production configuration and credentials are separate from development and private-network credentials.

## Production launch blockers

Do not place a production deployment into service until all of these items are complete:

- [ ] A documented network diagram identifies public, application, data, and management trust zones.
- [ ] Public ingress terminates TLS with a valid certificate and redirects HTTP to HTTPS.
- [ ] PostgreSQL, MinIO, OpenSearch, and internal application services are not exposed directly to the public network.
- [ ] OpenSearch security is enabled with TLS and authenticated, least-privilege service access.
- [ ] MinIO uses TLS and dedicated application credentials rather than root credentials.
- [ ] Production secrets are generated independently and stored outside the repository and container images.
- [ ] PostgreSQL and object data have encrypted, automated backups with a tested restore procedure.
- [ ] OpenSearch has a documented snapshot or rebuild procedure and sufficient recovery capacity.
- [ ] At least one full disaster-recovery exercise has restored Core, Artifact, and workflow state into an isolated environment.
- [ ] Monitoring and alerts cover availability, capacity, certificate expiry, backup failure, authentication abuse, and durable workflow failure.
- [ ] Dependency, container-image, and deployment security reviews have no unresolved launch-blocking findings.

## Target network architecture

Use separate network boundaries even when all services initially run on one host:

| Zone | Services | Allowed inbound access |
| --- | --- | --- |
| Public ingress | Reverse proxy or load balancer | HTTPS from approved users or networks |
| Web/application | Next.js BFF, Core API | BFF from ingress; Core from BFF and approved internal clients |
| Internal services | Artifact, embedding, classification, workflow workers | Only the specific application services that call them |
| Data | PostgreSQL, MinIO API, OpenSearch | Only approved application and operations identities |
| Management | SSH, MinIO console, database administration, metrics | VPN or bastion with administrator authentication |

Production firewall rules should default to deny. Do not publish internal service ports merely for convenience. In particular:

- PostgreSQL `5432` must not be publicly reachable.
- MinIO API `9000` must be limited to Artifact Service and approved operations clients.
- MinIO console `9001` must be limited to the management network.
- OpenSearch `9200` and performance analyzer `9600` must be limited to application and management networks.
- Core, Artifact, embedding, and classification service ports must not bypass the public ingress policy.

If Docker Compose remains the first production orchestrator, use a production-specific Compose file or override. Do not edit the local file into a mixture of development and production behavior. Attach internal services to non-public Docker networks and publish only the intended ingress port.

## TLS and ingress

- Terminate browser TLS at a maintained reverse proxy or load balancer.
- Use TLS 1.2 or newer and automate certificate renewal.
- Set the public `APP_ORIGIN` to the exact HTTPS origin. Do not use a wildcard origin.
- Keep the Core API private behind the Next.js BFF unless a separately authenticated API exposure is explicitly required.
- Configure trusted proxy handling deliberately. Accept forwarded scheme, host, and client-address headers only from the known proxy.
- Set HSTS after HTTPS and certificate renewal have been validated.
- Add security response headers appropriate to the application, including `X-Content-Type-Options: nosniff`, a restrictive content security policy, and clickjacking protection.
- Restrict request body size and timeouts at ingress in addition to application-level upload limits.
- Rate-limit login and other abuse-sensitive endpoints by both account and source address. Preserve the application audit trail when the gateway rejects requests.

Use encrypted connections on internal networks where the infrastructure or threat model requires them. OpenSearch and MinIO must use TLS in production. PostgreSQL TLS should be required when the database connection crosses a host or trust boundary.

## Secrets and service identities

- Set `APP_ENV=production` explicitly.
- Generate unique, high-entropy values for `JWT_SECRET`, `ARTIFACT_DELEGATION_SECRET`, `CLASSIFICATION_SERVICE_TOKEN`, `EMBEDDING_SERVICE_TOKEN`, database credentials, object-store credentials, bootstrap credentials, and third-party provider API keys.
- Store secrets in a deployment secret manager or protected runtime secret files. Never put secrets in Git, Dockerfiles, container-image layers, command history, tickets, or diagnostic bundles.
- Mount or inject only the secrets each service needs.
- Do not use the PostgreSQL superuser as the steady-state application identity. Create separate, least-privilege roles for Core, Artifact, migrations, backup, monitoring, and operations as the deployment matures.
- Do not use `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` as Artifact Service credentials. Create a dedicated service account and policy limited to the required tenant buckets and operations.
- Give Core, Artifact, classification, and embedding services distinct identities. Internal service tokens must not be accepted from browsers.
- Pin JWT issuer and audience values and keep access-token lifetimes short. Preserve refresh-token rotation and reuse detection.
- Rotate bootstrap credentials after initial provisioning and disable or tightly control emergency bootstrap paths.
- Establish a rotation procedure that supports overlapping credentials where necessary so rotation does not require unsafe downtime.
- Inventory every external model provider, its key owner, permitted data use, retention terms, and approved model endpoints before enabling it with production matter data.

## PostgreSQL

PostgreSQL contains authoritative Core data, Artifact metadata, authentication state, audit records, and DBOS durable workflow state.

Required controls:

- Run a supported PostgreSQL release with security updates applied on a defined maintenance cadence.
- Bind only to private interfaces and restrict access with both the host firewall and PostgreSQL access rules.
- Require strong authentication and TLS when accessed across hosts.
- Separate application and administrative roles; remove unnecessary schema and database privileges.
- Encrypt the underlying volume or managed-database storage and protect backups with separate encryption keys.
- Back up both `pvr-core` and `pvr-artifact`. A Core backup must include the `dbos` schema so durable workflows remain consistent.
- Enable point-in-time recovery through continuous WAL archiving or use an equivalent managed-service capability.
- Monitor connection count, pool saturation, locks, long transactions, storage growth, replication/backup lag, checkpoint pressure, and failed authentication.
- Size connection pools across the API and every worker as a combined total, leaving headroom for migrations, backup, and operations.
- Apply Alembic migrations as a controlled release step before starting application code that requires them. Record the Core and Artifact migration heads with the deployment.
- Test restore into an isolated environment and verify database consistency, application login, Artifact metadata access, and DBOS workflow visibility.

Do not treat a Docker volume as a backup. A volume protects data from container replacement, not host loss, corruption, operator error, or ransomware.

## MinIO and artifact storage

MinIO contains native files, extracted text, immutable derived artifacts, model inputs and outputs, and exports.

Required controls:

- Use TLS for the S3 API and console.
- Restrict the S3 API to Artifact Service and approved operations clients. Restrict the console to administrators on the management network.
- Configure dedicated least-privilege service accounts; reserve the root account for controlled recovery operations.
- Keep the MinIO license file read-only and limit host-file permissions.
- Store data on production-grade persistent storage with capacity monitoring and documented filesystem/mount requirements.
- Set an explicit restart policy and a meaningful health check.
- Enable object encryption at rest, preferably with an external KMS when available. Document key backup and recovery.
- Decide whether bucket versioning, object locking, retention rules, or replication are required by client contracts or litigation obligations. Do not enable irreversible retention without a reviewed lifecycle design.
- Back up or replicate object data independently of the database backup. A database-only restore cannot recover document bodies.
- Monitor disk capacity, drive health, failed requests, healing activity, credential use, certificate expiry, and replication/backup lag.
- Test an Artifact restore by retrieving and hash-verifying representative native, extracted-text, Parquet, and export artifacts.

The current private-network MinIO data bind mount at `/home/petefc/augment.ws/prv-data/minio-data` provides persistence on that host but is not, by itself, production backup or high availability.

## OpenSearch

OpenSearch is rebuildable, but it holds sensitive search projections and must receive the same network and access protections as the authoritative stores.

Required controls:

- Enable the OpenSearch security plugin in production.
- Require TLS for client and node-to-node traffic.
- Create a least-privilege application identity rather than using anonymous access or a cluster administrator.
- Store `OPENSEARCH_USERNAME` and `OPENSEARCH_PASSWORD` securely and set `OPENSEARCH_VERIFY_TLS=true` with the appropriate trust chain.
- Use an environment-appropriate topology. A multi-node production cluster must distribute cluster-manager and data responsibilities across failure domains where practical.
- Configure replica counts to match the number of eligible data nodes. A one-node deployment cannot allocate replicas and will remain yellow if replicas are requested.
- Pin reviewed image versions and use a controlled upgrade and rollback procedure.
- Set the heap deliberately, normally no more than approximately half of available memory, while leaving capacity for the operating system and filesystem cache.
- If `bootstrap.memory_lock=true`, configure container `memlock` soft and hard limits as unlimited and verify `mlockall=true`; otherwise remove the misleading setting and manage swap explicitly.
- Set appropriate `nofile` limits and the host `vm.max_map_count` requirement.
- Monitor cluster health, unassigned shards, disk watermarks, heap pressure, garbage collection, thread-pool rejection, indexing/search latency, circuit breakers, and snapshot results.
- Maintain either tested snapshots or a tested complete rebuild procedure from PostgreSQL and Artifact data. Document expected rebuild time and required temporary capacity.

The current private-network instance uses a single OpenSearch node with the security plugin disabled, a 30 GiB heap, and a 64 KiB `memlock` limit. Those settings are acceptable only as a documented non-production exception. Production must not copy them unchanged.

## Application services and workers

- Run Core, Artifact, embedding, classification, BFF, and workflow worker services as non-root users with read-only root filesystems where feasible.
- Drop unnecessary Linux capabilities and prevent privilege escalation.
- Pin production images by immutable digest and generate a software bill of materials.
- Scan Python, JavaScript, operating-system, and container dependencies before release and continuously thereafter.
- Pin reviewed embedding model revisions, especially when `EMBEDDING_TRUST_REMOTE_CODE=true`. Prefer eliminating remote-code trust when the model permits it.
- Limit filesystem access for model caches, temporary uploads, traces, and exports.
- Keep `MODEL_TRACE_ENABLED=false` unless a time-limited, access-controlled diagnostic session is explicitly approved. Traces can contain matter content.
- Configure upload, model input, export, concurrency, and timeout limits according to tested capacity.
- Run only the intended number of workflow workers. Coordinate model-provider quotas across processes and prevent duplicate deployment groups from unintentionally consuming the same durable queue.
- Use graceful shutdown and allow in-flight durable steps to checkpoint or retry safely.
- Separate deployment health from job success: a healthy process can still have failed durable workflows requiring operator attention.

## Authentication, authorization, and tenant isolation

- Require individual administrator accounts; do not share the root administrator login.
- Add MFA or upstream identity-provider authentication before broad production use.
- Apply least-privilege roles and review tenant, client, collection, and matter grants periodically.
- Audit privileged operations, failed authentication, refresh-token reuse, role/grant changes, exports, artifact access, workflow administration, and destructive operations.
- Test tenant isolation at the API, database-query, Artifact delegation, search-filter, export, and UI layers.
- Define administrator offboarding, session revocation, credential rotation, and emergency access procedures.
- Set retention rules for sessions, audit records, exports, model traces, generated reports, and temporary artifacts.

## Backups, recovery, and continuity

Define recovery objectives before selecting backup frequency:

- Recovery point objective (RPO): maximum acceptable data loss.
- Recovery time objective (RTO): maximum acceptable service outage.

The recovery plan must cover these as one application state:

1. `pvr-core`, including the `dbos` schema.
2. `pvr-artifact`.
3. MinIO object data and encryption keys.
4. Deployment configuration, migration versions, and immutable image identifiers.
5. OpenSearch snapshots, or the tools and capacity required for a full rebuild.

Use encrypted, versioned backups in a separate failure domain with restricted delete privileges. Monitor backup completion and age. Perform scheduled restore tests; a successful backup command without a verified restore is not sufficient.

Document recovery ordering. In general, restore compatible PostgreSQL databases and MinIO state first, validate Artifact references, restore or rebuild OpenSearch, start APIs, then start exactly one intended workflow-worker deployment and reconcile durable jobs.

## Logging, monitoring, and incident response

- Centralize logs with access controls and retention appropriate for matter confidentiality.
- Redact passwords, tokens, API keys, authorization headers, signed URLs, document bodies, prompts, model responses, and unnecessary personal information.
- Correlate requests, durable workflow IDs, job IDs, tenant IDs, and matter IDs without logging sensitive content.
- Alert on service unavailability, database connection failures, storage exhaustion, OpenSearch red/yellow states beyond policy, repeated workflow failures, authentication spikes, backup failure, and certificate expiry.
- Synchronize host clocks and retain enough audit evidence to reconstruct security-sensitive actions.
- Maintain an incident runbook covering credential compromise, tenant-isolation failure, malicious upload, lost host, object corruption, database corruption, and external model-provider exposure.
- Define who may declare, investigate, contain, and communicate an incident. Preserve evidence without broadly copying matter data.

## Deployment and change control

- Maintain separate production configuration with no development credentials, placeholder values, or insecure service flags.
- Validate rendered deployment configuration in CI without printing secrets.
- Require review for network, authentication, authorization, migration, retention, encryption, and backup changes.
- Deploy from immutable, signed artifacts where supported; record source revision and image digest.
- Run migrations and smoke tests as explicit release stages.
- Test rollback compatibility before deployment. Database rollback must never rely on destructive schema reversal without a verified data-preservation plan.
- Keep production diagnostics and support bundles encrypted and access-controlled. Review them for secrets and matter content before sharing.

## Pre-production validation

Complete and retain evidence for the following tests:

- [ ] An external network scan finds only the intended HTTPS ingress.
- [ ] Direct access to PostgreSQL, MinIO, OpenSearch, and internal APIs is denied from the public/user network.
- [ ] TLS certificate, protocol, redirect, HSTS, and security-header checks pass.
- [ ] Default, development, and placeholder credentials are rejected.
- [ ] MinIO application credentials cannot perform root or unrelated-bucket operations.
- [ ] OpenSearch application credentials cannot perform cluster-administration operations.
- [ ] Cross-tenant API, Artifact, search, export, and browser access tests fail closed.
- [ ] Login throttling, account lockout, session revocation, and refresh-token reuse detection behave as designed.
- [ ] Oversized uploads and requests are rejected without exhausting application resources.
- [ ] Dependency and image scans are reviewed and exceptions have owners and expiry dates.
- [ ] PostgreSQL point-in-time recovery is demonstrated.
- [ ] MinIO objects are restored and content hashes are verified.
- [ ] OpenSearch is restored or rebuilt and search behavior is verified.
- [ ] A workflow running at interruption is reconciled correctly after recovery.
- [ ] Monitoring detects a deliberately failed backup, stopped service, low-disk condition, and failed workflow.

## Current private-network deployment notes

These observations are recorded so they are not accidentally treated as production defaults:

- MinIO currently publishes `9000` and `9001` on all host interfaces and has `restart=no`.
- MinIO data is persisted through the host bind mount `/home/petefc/augment.ws/prv-data/minio-data`.
- OpenSearch currently publishes `9200` and `9600` on all host interfaces and uses `restart=unless-stopped`.
- OpenSearch data is persisted in the Docker volume `prv-application_pvr_opensearch`.
- OpenSearch currently has a 64 KiB `memlock` limit even though memory locking is requested.
- The live containers were created with settings that differ from the current checked-in `compose.yaml`, which now binds published ports to loopback.
- The current environment is intentionally being kept simple because it is on a trusted private network. These exceptions must be revisited rather than copied into production.

## Ownership record

Before production work begins, assign an owner and target date to every unchecked launch blocker. Record the selected hosting architecture, threat model, RPO/RTO, data residency requirements, client contractual requirements, and any accepted exceptions in the deployment change record. Every exception must identify its compensating control, approver, and expiration or review date.
