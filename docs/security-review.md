# Security Review

Date: 2026-10-19
Repository: `prv-application` (branch `main`)

## Scope and method

This review covers the Core API, Artifact Service, Classification Service, Embedding Service, the Next.js backend-for-frontend (BFF), and the compose deployment configuration.

Reviewed in full:

- Core authentication and token handling (`app/security.py`, `app/dependencies.py`, `app/routers/auth.py`, `app/config.py`)
- Authorization engine and grant scoping (`app/authorization.py`, `app/routers/clients.py`, `app/routers/tenants.py`)
- Artifact Service auth and delegation (`artifact_service/auth.py`, `artifact_service/api.py` access checks, `artifact_service/service.py`)
- BFF cookie, CSRF, and proxy code (`web/src/app/api/**`, `web/src/lib/server/core-api.ts`)
- Classification and embedding service auth, and classification model-package parsing (`classification_service/packaging.py`)
- Agent execution authorization (`app/agent_runtime.py`, `app/agent_tools.py`)
- CSV export writers (`app/search_exports.py`, `app/fact_exports.py`, `app/review_batch_comparisons.py`, `app/job_outcomes.py`)
- Compose deployment (`compose.yaml`, `.env.example`)

Swept across all Python modules with pattern searches for raw SQL construction, shell execution, `eval`/`exec`, pickle or YAML deserialization, disabled TLS verification, unsafe ZIP extraction, secret logging, and Core route handlers lacking a principal dependency.

Not covered:

- Line-by-line review of all roughly 195 Python modules and all frontend components
- Dependency vulnerability audit (`pipenv check` / `pip-audit`, `pnpm audit`)
- Dynamic testing or penetration testing against a running stack

## Summary

The core design is sound. Tenant isolation, grant scoping, token validation, and the BFF cookie and CSRF model are implemented carefully.

The most serious risks are configuration risks. Default secrets are hard-coded in both code and compose, nothing prevents them from reaching a non-development environment, and the compose file publishes internal services on all host interfaces. These risks depend on deployment, but they are easy to trigger by accident.

## Findings

### High

#### H1. Hard-coded secrets with no production guard; internal services exposed on all interfaces

**Status: remediated on `fix/security-defaults`.** `APP_ENV` now defaults to `production`. Each settings class rejects development or placeholder values for its secrets unless `APP_ENV=development` (`app/deployment_policy.py`). Each process loads its settings at startup. Compose requires the secrets from `.env` and binds published ports to `127.0.0.1`. Tests: `tests/test_deployment_policy.py`.

- Code defaults:
  - `JWT_SECRET`: `app/config.py:120`
  - `CLASSIFICATION_SERVICE_TOKEN`: `app/config.py:18`
  - `ARTIFACT_DELEGATION_SECRET`: `artifact_service/config.py`
  - `EMBEDDING_SERVICE_TOKEN`: `embedding_service/config.py:29`
- `app_env` is defined at `app/config.py:12` but never read, so nothing rejects default secrets outside development.
- `compose.yaml` hard-codes the same values at lines 79, 105, 121, 147, 152, 157, 188, 230, 235, and 259.
- `compose.yaml:114` publishes the Artifact Service as `8001:8001`, which Docker binds to all host interfaces. The standalone Artifact Service accepts any token signed with the delegation secret, and the token's claims scope access by tenant. Anyone who knows the default secret can mint a token and read any tenant's artifacts directly.
- The embedding service (`compose.yaml:87`, `8002`), classification service (`compose.yaml:137`, `8003`), PostgreSQL (`compose.yaml:9`, `5432`, `postgres:postgres`), and MinIO (`9000`/`9001`, `minioadmin`) are published the same way.

**Impact:** Full cross-tenant artifact read on any network that can reach the host, once the default secret is known.

**Recommendation:** Make `Settings` refuse to start when `app_env` is not `development` and any secret still equals its default. Bind published ports to `127.0.0.1:` in compose. Load real secrets from an untracked env file or a secret manager.

### Medium

#### M1. No refresh-token reuse detection

**Status: remediated on `fix/security-defaults`.** A rotated refresh token presented after `REFRESH_REUSE_GRACE_SECONDS` (default 30) revokes the session and writes an `auth.refresh_reuse_detected` audit record. Inside the window it is rejected without revoking, so a concurrent refresh from the same client is not treated as theft (`app/routers/auth.py`, `tests/test_refresh_reuse.py`).

- `app/routers/auth.py:122` (`refresh`)

A stale refresh token is rejected, but the session is not revoked. If an attacker holds a previous refresh token, rotation by the legitimate client invalidates it, but the attacker's copy is not treated as a reuse signal and the session stays active.

**Recommendation:** On a `jti` mismatch, revoke the session and write an audit record.

#### M2. No upload size cap

**Status: remediated on `fix/security-defaults`.** `ARTIFACT_UPLOAD_MAX_BYTES` (default 2 GB) is enforced while the upload is read, and the Core proxy rejects an oversized `Content-Length` before forwarding (`artifact_service/service.py`, `app/routers/artifact_proxy.py`, `tests/test_upload_limits.py`). The BFF also stopped buffering mutation bodies in memory. It streams them to Core and refreshes the access token before forwarding, so a streamed body is never retried (`web/src/app/api/core/[...path]/route.ts`, `route.mutation.test.ts`). The BFF buffering was not in the original review and was found while fixing this item.

- `artifact_service/service.py:51` (`stage_upload`)

The upload is streamed into a spooled temporary file with no byte limit. Any authenticated user can exhaust disk. The BFF proxy also forwards request bodies without a limit.

**Recommendation:** Enforce a maximum size while reading, and add a body-size limit at the proxy.

#### M3. CSV formula injection not neutralized in two exports

**Status: remediated on `fix/security-defaults`.** The coding comparison and job outcome CSV exports now neutralize text cells that start with `=`, `+`, `-`, or `@`, using the shared helper in `app/csv_safety.py` (`tests/test_csv_safety.py`). XLSX output is unchanged, because its cells are stored as strings and cannot run formulas.

Search and fact exports escape cells beginning with `=`, `+`, `-`, or `@` when `spreadsheet_safe` is true, which is the default. Two writers do not escape at all:

- `app/review_batch_comparisons.py:447` (`_export_cell`) and `:499` (writer), the coding comparison export
- `app/job_outcomes.py:925-931`, the job outcome export, including the `message` column

Document text can be attacker-controlled, so a crafted value such as `=HYPERLINK(...)` could execute when an administrator opens the file.

**Recommendation:** Apply the same guard in both writers.

#### M4. Agent execution authorization bypasses the RBAC model

- `app/agent_runtime.py:176-199` (`_require_actor_access`)

Execution-time re-authorization checks only legacy `tenant_role == ADMIN` and matter `ADMIN` membership, not `can()`. It fails closed today. It will drift from the explicit-permission model that the project requires for new access decisions, and users granted a matter-scoped permission cannot run agents at all.

**Recommendation:** Route this check through `can()` with the relevant permission key.

#### M5. Login throttling is per account only

- `app/config.py:125-126` (`login_max_failed_attempts`, `login_lockout_minutes`)

Five failed attempts lock an account for 15 minutes. There is no per-IP throttle, so an attacker can repeatedly lock out any known email address. This is a deliberate trade-off, but it is a denial-of-service path.

**Recommendation:** Add a per-IP limiter at the BFF or gateway.

### Low

- **L1. Local model traces contain matter content.** `.model-traces/` holds about 1.4 GB of agent request payloads, which can include privileged document content. It is gitignored, but unencrypted on disk. Add retention and purge, or confirm `MODEL_TRACE_ENABLED` is off.
- **L2. `ensure_storage` is callable by any tenant user,** not only administrators (`artifact_service/api.py:396`). It is idempotent, so impact is limited.
- **L3. Downloads lack `X-Content-Type-Options: nosniff`.** The attachment disposition largely mitigates this.
- **L4. Default database and object-store credentials** in config and compose (`postgres`, `minioadmin`). Deployment aspect covered under H1.

## Verified controls

- **Tokens:** HS256 is pinned. Issuer, audience, and required claims are checked. Access tokens are validated against live session state on every request, so logout takes effect immediately. Refresh rotation stores only a hash of the current `jti` (`app/security.py`, `app/dependencies.py`).
- **Passwords:** Hashed with Argon2 through `pwdlib`. A dummy hash is verified for unknown users to prevent timing-based account enumeration. A 12-character minimum is enforced (`app/schemas.py`).
- **Service tokens:** The classification and embedding services compare tokens in constant time, and every classification route requires one.
- **Authorization:** Tenant boundaries are enforced in `can()` (`app/authorization.py`). Grant scopes do not cross sibling client, matter, or collection boundaries. Core routes require a principal; only `/login` and `/refresh` do not, as intended.
- **Artifact access:** Every artifact route calls `require_client`. Content is served as an attachment with percent-encoded filenames.
- **Injection:** SQL is parameterized throughout. Search field names are allowlisted against metadata definitions before reaching OpenSearch. `pg_notify` uses bound parameters.
- **Model packages:** ZIP archives are checked for absolute paths, `..` segments, and size limits before loading. NPZ loading uses `allow_pickle=False`. Nothing uses pickle or unsafe YAML loading.
- **BFF:** Access and refresh tokens are HttpOnly. The refresh cookie is `SameSite=strict`. Mutations require both a same-origin `Origin` header and a matching CSRF token. Login is same-origin checked. `.env` files are gitignored.

## Recommended order

1. **H1**: production secret guard and loopback-only port bindings. Required before any shared deployment.
2. **M3**: CSV escaping in the two unguarded writers. Small and contained.
3. **M1** and **M2**: refresh-token reuse revocation, and upload size limits.
4. **M4** and **M5**: route agent authorization through `can()`, and add per-IP login throttling.
5. Run the dependency audits listed under "Not covered".
