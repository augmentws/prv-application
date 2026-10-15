# Role Permission and Content Access Implementation Plan

## 1. Purpose

Priv-View currently recognizes only `ADMIN` at tenant, client, and matter
scope. The database contains client and matter membership records, but there is
no general role catalog, permission catalog, user-to-role assignment model, or
access-group model. Most API routes therefore ask whether the caller is an
administrator instead of asking whether the caller may perform a particular
action on a particular resource.

This plan replaces that provisional model with two related controls:

1. Role-based permissions determine what a principal may do.
2. Direct user grants and access-group grants determine where the principal may
   do it.

The first implementation covers tenants, clients, collections, and matters.
Matter documents inherit matter access. Restricted document populations are a
later extension designed into, but not implemented by, this plan.

### Implementation status

The RBAC foundation, central evaluator, system-role and permission catalogs,
custom-role APIs, access groups, scoped grants, authorization revision,
transactional security audit events, legacy backfill, effective-access and
explanation APIs, and the tenant administration UI are implemented through
migration `0067_rbac_foundation`.

The application is in the compatibility stage: existing `ADMIN`, client
membership, and matter membership checks still work while explicit grants are
introduced. Phase 4 matter read and review enforcement is implemented: client
shell and matter lists are access-filtered, search and direct matter-document
reads require `document.view`, direct coding requires `document.review`, batch
review requires `batch.review` plus assignment access, and saved-search shares
and batch assignees are limited to users who can access the matter. Reviewers
load the exact collection item and native content through a matter-document
endpoint, so a matter grant does not confer general source-collection access.

Matter metadata reads now use `metadata.view`; schema mutations use
`metadata.manage`. Search status reads use `job.view`, and search recovery or
rebuild actions use `search.rebuild`. The review UI uses effective matter
permissions to separate Reviewer and Read Only behavior, and client navigation
hides collection and creation controls the caller lacks.

The remaining matter-administration routes, complete collection/Artifact
action delegation, workflow reauthorization, and eventual legacy-column
removal remain the later phases described below. Non-administrator roles are
now enforceable for the Phase 4 paths, but should not yet be treated as proof
that every administrative subsystem has completed permission cutover.

## 2. Governing decisions

The implementation follows these decisions:

- Permissions use stable, code-owned keys and describe individual actions.
- Roles are named permission bundles.
- Priv-View provides immutable system roles and permits tenant-owned custom
  roles.
- A role has no effect until it is bound to a user or access group at a resource
  scope.
- A user may have different roles in different clients, collections, and
  matters.
- Grants are allow-only. The initial model has no explicit deny rules.
- Effective permissions are the union of applicable direct and group grants.
- Tenant and client grants may be inherited by supported descendant resources.
- Access groups are tenant-owned and are not nested in the initial release.
- Direct user grants remain supported for exceptions and small teams.
- Core is the authority for identities, roles, permissions, groups, grants,
  and authorization decisions.
- Every security configuration change writes an append-only Core audit record
  in the same database transaction as the change. The security change rolls
  back if its audit record cannot be written.
- Artifact Service enforces the narrow authorization delegated by Core; it does
  not independently own users, roles, or groups.
- Backend authorization is final. Hiding a control in the browser is never the
  security boundary.
- Metadata groups, review-batch coding groups, and access groups are separate
  concepts. Use `AccessGroup` in code and database names even if the UI calls
  the feature **Groups**.

These decisions replace the current repository rule that granular permissions
are deferred and that phase one supports only `ADMIN`.

## 3. Scope

### 3.1 Included

- Permission and role catalogs.
- System roles and tenant-owned custom roles.
- Tenant-owned access groups and user membership.
- Direct-user and access-group role grants.
- Tenant, client, collection, and matter resource scopes.
- Central authorization evaluation and effective-access explanations.
- Route-by-route replacement of administrator-only checks.
- Authorization-aware list queries, search, background jobs, and Artifact
  delegation.
- Administration UI for roles, groups, members, and grants.
- Migration of current tenant roles and client/matter memberships.
- Auditing, tests, observability, and safe rollout.

### 3.2 Excluded from the first implementation

- Nested access groups.
- Explicit deny grants.
- Field-level security within a document.
- One access-control row per document.
- Cross-tenant groups or role assignments.
- External identity-provider group synchronization.
- Time-limited grants, approval workflows for grants, and segregation-of-duty
  policy engines.
- Changing metadata field presentation visibility. That is a separate
  matter-configuration concern, not authorization.

## 4. Current-state findings

The existing model has three role-bearing records:

- `app_user.tenant_role`
- `client_membership.role`
- `matter_membership.role`

Every field is constrained to `ADMIN`. `UserCreate.role` also accepts only
`ADMIN`, so every currently created tenant user is a tenant administrator.
Client and matter creation add an administrator membership for the creator,
but there are no membership-management APIs or screens.

Authorization is distributed through `can_admin_tenant`, `can_admin_client`,
`can_admin_matter`, `require_root_admin`, and route-local variants. List routes
generally assume tenant-wide visibility instead of filtering resources through
an access policy.

Artifact access is derived from tenant administration or client membership and
is represented as allowed tenant, client, and custodian identifiers. It has no
collection-level grant or action-level capability.

Core already has an `audit_record` table and a `record_audit` helper. Each row
records the tenant, nullable actor user, action key, target type and ID,
optional event-specific JSON details, and creation time. Existing routes use it
for many authentication, configuration, matter, processing, analysis, and
classification mutations. Coverage is currently opt-in, the details payload is
not standardized, and there is no audit-log query API or administration page.
The authorization implementation reuses this ledger rather than creating a
second security-history table.

The new implementation must therefore change both the data model and the
meaning of authorization checks. Adding role tables without replacing route
guards would not produce usable reviewer or read-only accounts.

## 5. Terminology

| Term | Meaning |
| --- | --- |
| Permission | One stable capability, such as `document.review` or `collection.publish`. |
| Role | A named set of permissions. |
| System role | A code-owned role shipped by Priv-View and immutable to tenants. |
| Custom role | A tenant-owned role whose permission membership may be managed by an authorized tenant administrator. |
| Access group | A tenant-owned collection of users used as an authorization subject. |
| Subject | The user or access group receiving a role grant. |
| Scope | The tenant, client, collection, or matter to which a grant applies. |
| Grant | A binding between one subject, one role, and one scope. |
| Effective permission | A permission obtained from all applicable direct and group grants after scope inheritance. |

Group membership alone grants no content access. A group must also receive at
least one scoped role grant.

## 6. Authorization relationship

```text
User -------------------------------+
  |                                  |
  +-- AccessGroupMember -> AccessGroup
                                     |
User or AccessGroup -----------------+
                  |
                  +-- AuthorizationGrant
                           |       |
                           |       +-- Resource scope
                           |
                           +-- Role -> RolePermission -> Permission
```

The evaluator answers a single question:

```text
Does this user have permission P on resource R?
```

It does not infer permission from UI routes or resource ownership labels.

## 7. Core data model

Add one Core migration after the current migration head. Use UUID primary keys,
normal Core timestamps, tenant indexes, status constraints, and audited command
services.

### 7.1 Permission

```text
permission
  id                  UUID primary key
  key                 VARCHAR(120) unique not null
  display_name        VARCHAR(200) not null
  description         TEXT not null
  category            VARCHAR(50) not null
  resource_type       VARCHAR(30) not null
  dangerous           BOOLEAN not null default false
  status              VARCHAR(20) not null
  created_at          TIMESTAMPTZ not null
  updated_at          TIMESTAMPTZ not null
```

Permission keys are seeded from a code-owned registry and are not created or
renamed through tenant APIs. Database rows provide foreign keys, descriptions,
administration displays, and migration stability. A startup or test-time
registry check must fail if code and database seed definitions diverge.

`resource_type` identifies the resource on which the action is evaluated. It
does not prevent a role containing that permission from being granted at an
ancestor scope.

### 7.2 Role

```text
role
  id                  UUID primary key
  tenant_id           UUID nullable, indexed
  key                 VARCHAR(100) not null
  display_name        VARCHAR(200) not null
  description         TEXT nullable
  role_kind           VARCHAR(20) not null
  status              VARCHAR(20) not null
  created_by_user_id  UUID nullable
  created_at          TIMESTAMPTZ not null
  updated_at          TIMESTAMPTZ not null
```

Rules:

- `role_kind` is `SYSTEM` or `CUSTOM`.
- A system role has `tenant_id = NULL` and is immutable through tenant APIs.
- A custom role belongs to exactly one tenant.
- Role keys are unique among system roles and unique within a tenant.
- A role referenced by a grant cannot be deleted. Archiving prevents
  new grants while preserving historical authorization records.
- A custom role cannot include permissions reserved for root administration.

### 7.3 Role permission

```text
role_permission
  role_id             UUID foreign key
  permission_id       UUID foreign key
  created_at          TIMESTAMPTZ not null
  primary key (role_id, permission_id)
```

Changing a custom role changes every grant of that role. The command
must record the prior and next permission sets in the audit ledger and advance
the tenant authorization revision.

### 7.4 Access group

```text
access_group
  id                  UUID primary key
  tenant_id           UUID not null, indexed
  key                 VARCHAR(100) not null
  display_name        VARCHAR(200) not null
  description         TEXT nullable
  group_kind          VARCHAR(20) not null
  external_reference VARCHAR(500) nullable
  status              VARCHAR(20) not null
  created_by_user_id  UUID not null
  created_at          TIMESTAMPTZ not null
  updated_at          TIMESTAMPTZ not null
```

`group_kind` begins with `LOCAL`. A future `EXTERNAL` kind may retain an
identity-provider group reference. Group keys are unique within a tenant.

Do not reuse `MetadataGroup`, rename existing metadata groups, or add an
authorization discriminator to them.

### 7.5 Access group member

```text
access_group_member
  access_group_id     UUID foreign key
  user_id             UUID foreign key
  added_by_user_id    UUID not null
  created_at          TIMESTAMPTZ not null
  primary key (access_group_id, user_id)
```

The user and group must belong to the same tenant. Suspended or archived users
remain in membership history but receive no effective permissions.

### 7.6 Authorization grant

```text
authorization_grant
  id                  UUID primary key
  tenant_id           UUID not null, indexed
  user_id             UUID nullable, indexed
  access_group_id     UUID nullable, indexed
  role_id             UUID not null, indexed
  scope_type          VARCHAR(20) not null
  tenant_scope_id     UUID nullable
  client_id           UUID nullable
  matter_id           UUID nullable
  collection_id       UUID nullable
  created_by_user_id  UUID not null
  updated_by_user_id  UUID not null
  created_at          TIMESTAMPTZ not null
  updated_at          TIMESTAMPTZ not null
```

Constraints:

- Exactly one of `user_id` and `access_group_id` is set.
- Exactly the scope column corresponding to `scope_type` is set.
- `scope_type` is `TENANT`, `CLIENT`, `COLLECTION`, or `MATTER`.
- Tenant, client, matter, subject, custom role, and collection ownership must
  resolve to the same tenant.
- Tenant, client, and matter columns use Core foreign keys.
- `collection_id` cannot have a cross-database foreign key. The grant command
  validates it through the Artifact gateway before commit and stores its
  tenant/client identity in the audit event.
- Unique indexes prevent duplicate grants for the same subject, role, and
  scope.
- The table contains current assignments only. Removing access deletes the
  grant through an audited command.

The subject and scope are immutable after creation. Changing the assigned role
updates `role_id`, `updated_by_user_id`, and `updated_at`. Moving access to a
different subject or resource deletes the old grant and creates a new one. The
audit ledger, rather than this table, retains the change history.

Use explicit nullable scope columns instead of an unconstrained generic
`resource_id`. This retains database integrity for every Core-owned resource
and makes scope queries indexable.

### 7.7 Authorization revision

Add `authorization_revision BIGINT NOT NULL DEFAULT 1` to `tenant`. Increment it
in the same transaction as any role-permission, group-membership, or grant
change. Include it in authorization cache keys and delegated Artifact claims as
decision provenance.

The first release should use request-scoped resolution with only a short-lived
process cache. The revision supports safe caching later without allowing a
removed grant to remain effective indefinitely.

## 8. Permission catalog

Permission names use lowercase dotted keys. They describe domain actions, not
pages or HTTP methods.

### 8.1 Tenant and identity

```text
tenant.view
tenant.configure
tenant.manage_users
tenant.manage_roles
tenant.manage_groups
tenant.view_audit
tenant.view_usage
```

### 8.2 Client and collection

```text
client.view
client.configure
client.manage_members
client.manage_custodians
collection.view
collection.create
collection.configure
collection.ingest
collection.process
collection.publish
collection.delete
```

### 8.3 Matter and documents

```text
matter.view
matter.configure
matter.manage_access
metadata.view
metadata.manage
document.view
document.review
document.view_history
document.bulk_code
document.import
```

### 8.4 Review and analysis

```text
batch.view
batch.create
batch.configure
batch.assign
batch.review
batch.run_analysis
batch.publish_analysis
matter_definition.view
matter_definition.manage
matter_definition.publish
analysis_task.view
analysis_task.manage
analysis_task.run
```

### 8.5 Processing and classification

```text
job.view
job.start
job.cancel
search.configure
search.rebuild
embedding.manage
classification.view
classification.train
classification.approve
classification.activate
classification.score
classification.publish
```

Before implementation, inventory every Core and Artifact route and assign one
read permission and, where applicable, one mutation permission. The catalog is
complete only when a test proves every non-public route has an authorization
declaration.

Root-only platform operations remain separately identified. A tenant custom
role must never acquire root tenant creation, system-agent management,
system-skill management, or platform-bootstrap permissions.

## 9. Initial system roles

Seed system roles through migration and a code-owned role manifest.

| System role | Intended use |
| --- | --- |
| Root Administrator | Platform bootstrap and root-only operations. Granted only by bootstrap or another authorized root administrator. |
| Tenant Administrator | All tenant, client, collection, matter, document, processing, and access-management permissions within one tenant. |
| Client Administrator | Client configuration plus descendant collection and matter administration. |
| Collection Manager | Collection viewing, ingestion, processing, publication, and permitted deletion. |
| Matter Administrator | Matter configuration, metadata, access, review, analysis, processing, and classification. |
| Reviewer | Matter/document viewing, assigned batch review, coding, and coding history. |
| Analysis Operator | Matter/document read plus analysis-task and batch-analysis execution without role or metadata-schema administration. |
| Classification Manager | Classification training, evaluation, approval, activation, scoring, and publication. |
| Read Only | Read-only access to the granted resource scope and its authorized descendants. |

The migration owns the exact permission matrix. Tests snapshot that matrix so a
later code change cannot silently broaden a system role.

Custom roles may be copied from a system role, but they receive their own
tenant-owned role record and explicit permission membership.

## 10. Scope inheritance and effective access

### 10.1 Inheritance

Apply the following resource ancestry:

```text
Tenant
  Client
    Collection
    Matter
      Matter Document
```

- A tenant grant may apply to all descendant clients, collections, matters,
  and matter documents when the role contains the requested permission.
- A tenant grant does not cross into a child tenant. Root platform
  administration uses the separately governed Root Administrator path.
- A client grant may apply to that client's collections and matters.
- A collection grant applies only to that collection and its source items and
  artifacts.
- A matter grant applies to that matter and its published matter documents.
- A collection grant does not provide matter access.
- A matter grant does not provide general access to the source collection.

### 10.2 Effective permission algorithm

For `authorize(user, permission_key, resource)`:

1. Require an active user and active tenant.
2. Resolve the resource and its tenant and ancestor scopes.
3. Reject cross-tenant resources before grant evaluation.
4. Resolve the user's active access-group IDs.
5. Load active direct or group grants at the resource or an applicable ancestor.
6. Join active roles and role permissions for `permission_key`.
7. Permit if at least one qualifying grant exists.
8. Otherwise deny and record a bounded authorization decision event.

Permission union is intentional. There is no most-specific override and no
deny precedence in the first version.

### 10.3 Root administration

Retain a narrowly defined root break-glass path for bootstrap and recovery.
Ordinary root-tenant users must still receive grants. `is_superuser` alone must
not bypass authorization unless the bootstrap/recovery policy explicitly says
so.

### 10.4 Resource visibility and action permission

Resource access and mutation are separate permissions. For example:

- `matter.view` makes the matter visible.
- `document.view` permits reading its documents.
- `document.review` permits writing reviewer coding.
- `metadata.manage` permits schema changes.

A list route must filter out inaccessible resources. It is insufficient to
authorize only the parent tenant and then return every child.

For direct-resource requests, return `404` when the user cannot see the
resource. Return `403` when the resource is visible but the requested action is
not permitted. Apply this consistently to Core and Artifact-backed resources.

## 11. Collection and matter security boundaries

Collection access governs source evidence before publication. Matter access
governs the matter-owned document relationship after publication.

Publishing collection documents to a matter requires both:

```text
collection.publish on the source collection
document.import on the destination matter
```

Once published, a user with `document.view` on the matter may view the matter
document without receiving general browse access to the source collection.
This is necessary because a matter may contain documents from several
collections with different collection teams.

The collection remains authoritative for native and derived artifacts, but the
Core `MatterDocument` link is the authorization bridge for a published copy.
Core must verify that link before delegating access to the exact collection
item or artifact needed by the document viewer.

Do not authorize a collection listing, arbitrary collection-item lookup, or
collection-wide artifact download merely because a user can view one published
matter document.

## 12. Artifact Service integration

### 12.1 Core remains the policy authority

Core resolves access-group memberships, grants, roles, and permissions. The
browser continues to call the BFF and never receives an Artifact service
credential.

Embedded Artifact calls use the same Core authorization service as ordinary
Core routes. Remote Artifact calls use short-lived delegation tokens minted
only after Core authorization.

### 12.2 Delegation claims

Replace broad `allowed_tenant_ids` and `allowed_clients` as the only content
authorization with explicit action and scope claims:

```text
actor user ID
tenant ID
authorization revision
allowed permission keys
client-wide collection scopes, when granted
explicit collection IDs, when granted directly
exact collection item or artifact IDs for matter-document reads
issued-at and short expiration
request or workflow correlation ID
```

Artifact validates the required permission and resource match on every route.
It must fail closed on unknown permissions, scope mismatches, malformed IDs, or
an expired token.

Core reauthorizes immediately before minting a remote delegation. Delegations
are operation-specific and expire within at most 60 seconds. Artifact cannot
treat the embedded authorization revision as proof that access was not removed
later; the token lifetime bounds that window. Long-running workflows obtain
a new delegation at each protected stage instead of retaining one token for the
run.

Avoid unbounded delegation tokens. A client-wide collection grant is encoded
as a client scope, not as every collection ID. A narrow matter-document
delegation contains only the exact item/artifact references required by that
request.

### 12.3 Collection grant validation

Because collections live in the Artifact database, Core grant creation must:

1. Authorize `client.manage_members` or the selected access-management
   permission on the owning client.
2. Resolve the collection through the Artifact gateway.
3. Verify tenant and client ownership.
4. Insert the Core grant and audit event.

Collection deletion does not leave stale grants. A successful deletion
workflow removes associated collection grants through an idempotent, audited
Core command. A periodic integrity check reports orphaned collection grant IDs.

## 13. Authorization service

Create an application service rather than distributing SQL joins through
routers.

Suggested interfaces:

```python
authorization.require(principal, "matter.view", ResourceRef.matter(matter_id))
authorization.can(principal, "document.review", ResourceRef.matter(matter_id))
authorization.filter_accessible(principal, "matter.view", Matter)
authorization.explain(principal, permission_key, resource_ref)
```

Requirements:

- One implementation is used by routers, command services, workflows, and
  embedded Artifact access.
- Resource resolution is code-owned and typed. Callers do not submit arbitrary
  table names.
- Query filters are composable SQL expressions for list endpoints.
- One request-scoped resolution cache avoids repeating group and role joins.
- Denials never include another tenant's resource details.
- An explanation includes the winning direct/group grant and ancestor scope,
  but is returned only to the subject or an authorized access administrator.
- Authorization decisions do not mutate domain state.

Keep compatibility wrappers named `can_admin_tenant`, `can_admin_client`, and
`can_admin_matter` only during migration. They must delegate to explicit
permissions and be removed after all call sites have migrated.

## 14. API contracts

### 14.1 Permission and role administration

```text
GET    /v1/permissions
GET    /v1/tenants/{tenant_id}/roles
POST   /v1/tenants/{tenant_id}/roles
GET    /v1/tenants/{tenant_id}/roles/{role_id}
PATCH  /v1/tenants/{tenant_id}/roles/{role_id}
PUT    /v1/tenants/{tenant_id}/roles/{role_id}/permissions
POST   /v1/tenants/{tenant_id}/roles/{role_id}/archive
```

System roles are returned with `role_kind = SYSTEM` and cannot be edited.
Permission replacement is atomic and requires the role row version to prevent
lost updates.

### 14.2 Access groups

```text
GET    /v1/tenants/{tenant_id}/access-groups
POST   /v1/tenants/{tenant_id}/access-groups
GET    /v1/tenants/{tenant_id}/access-groups/{group_id}
PATCH  /v1/tenants/{tenant_id}/access-groups/{group_id}
PUT    /v1/tenants/{tenant_id}/access-groups/{group_id}/members
POST   /v1/tenants/{tenant_id}/access-groups/{group_id}/archive
```

Member replacement is atomic, deduplicated, tenant-validated, row-versioned,
and audited. Later APIs may add incremental membership commands for external
directory synchronization.

### 14.3 Grants

```text
GET    /v1/tenants/{tenant_id}/authorization-grants
POST   /v1/tenants/{tenant_id}/authorization-grants
PATCH  /v1/tenants/{tenant_id}/authorization-grants/{grant_id}
DELETE /v1/tenants/{tenant_id}/authorization-grants/{grant_id}
GET    /v1/authorization/effective
GET    /v1/authorization/explain
```

Grant list filters include subject, role, scope type, and resource.
Creation accepts exactly one subject and a typed scope object. The server
resolves all resource ownership; it never trusts a browser-supplied tenant ID
to establish ownership. Update may replace only the role; changing the subject
or scope requires deleting and recreating the grant.

The effective endpoint returns only the permission keys relevant to one
resolved resource. It is appropriate for rendering actions on a page. It does
not return every resource the user can access.

### 14.4 User creation

Remove the required `role = ADMIN` field from ordinary user creation. Creating
a user establishes identity and tenant ownership but grants no resource access
unless the same authorized command explicitly supplies initial group
memberships or grants.

The UI should encourage an initial group or role assignment and warn when a new
user has no effective access. It must not silently make every new user a tenant
administrator.

### 14.5 Audit log

```text
GET /v1/tenants/{tenant_id}/audit-records
```

The endpoint returns the tenant's complete audit history, including existing
domain actions and the new `authorization.*` actions. It is not limited to
authorization events.

The endpoint requires `tenant.view_audit` and always filters by the authorized
tenant. The Tenant Administrator system role receives this permission. Client,
collection, and matter administrator roles do not receive it merely because
they administer a descendant resource. Root administration uses its explicit
root-authorized tenant context.

The default request returns the 50 most recent matching records. Results sort
by `created_at DESC, id DESC` so equal timestamps have a stable order. The API
uses an opaque cursor to retrieve older records and accepts a maximum bounded
page size; it never uses an unbounded audit query.

The administration-page filters map directly to these query parameters:

```text
created_from       optional inclusive timestamp
created_to         optional exclusive timestamp
actor_user_id      optional exact user ID
system_actor       optional true to select records without an actor user
action             optional exact action key
action_prefix      optional action-key prefix
cursor             optional opaque continuation cursor
limit              optional, defaults to 50
```

A UI date selection is converted to the corresponding start and end timestamps
in the administrator's displayed timezone before the request is sent. User
filtering uses an authorized tenant-user selector and submits the stable user
ID. Action filtering may select an exact known action or enter a prefix such as
`authorization.`.

Each result includes the audit ID, creation time, actor user ID, actor display
information when the user is available, action key, target type, target ID, and
details. A null actor is displayed as **System**. Missing or archived users do
not hide the record; their stable actor ID remains available.

Target type and target ID filters remain supported for links from a role,
access-group, grant, model, job, or other resource history, even though they
are not required controls on the initial Audit Log page. There is no audit
create, update, or delete API exposed to administrators.

## 15. Route authorization inventory

Create a checked-in route-permission manifest. Each non-public API operation
declares:

```text
operation ID
resource resolver
required permission
whether list results require policy filtering
whether the operation starts durable work
whether Artifact delegation is required
```

CI compares the FastAPI route inventory with the manifest and fails for an
unclassified route. Authentication, health, and explicitly public operations
use documented exemptions.

Route conversion should split the current administrator checks rather than map
every route to a generic `matter.admin` permission. Read, review, configuration,
execution, approval, activation, cancellation, and publication are separate
capabilities.

## 16. Background jobs and durable workflows

Every job command must authorize the initiating action before creating the job.
The durable job stores:

```text
initiating user ID
required permission key
authorized resource type and ID
authorization revision at launch
role/grant explanation snapshot
```

Workers reauthorize the initiating user before externally visible mutations,
publication, promotion, activation, or destructive work. A removed grant may
therefore stop a queued job before it performs the protected action.

Read-only historical results remain governed by the caller's current access,
not the initiating user's former access. Job polling, artifact download,
failure diagnostics, and retry commands each require their own current
permission.

System-originated maintenance workflows use a code-owned service identity and
explicit service permission set. They must not impersonate a tenant
administrator.

## 17. Search and document access

Matter-level authorization is enforced before search execution. The server
injects the authorized matter boundary; browsers cannot provide or override an
authorization filter.

Document-level restrictions are deferred. The future design should use reusable
document access policies rather than user-by-document rows:

```text
document_access_policy
document_access_policy_group
matter_document.document_access_policy_id nullable
```

A null policy means the document inherits matter access. A restricted policy
requires both matter access and membership in an allowed access group.
OpenSearch would project policy identity or allowed group IDs and Core would
inject the caller's allowed policy filter into every keyword, semantic, hybrid,
facet, batch-selection, export, and scoring query. Direct document and artifact
routes would enforce the same rule from Core records.

Do not add document restrictions until this complete query and direct-read
enforcement can be implemented together.

## 18. Administration UI

Add tenant administration sections for:

- Roles
- Access groups
- Users
- Access assignments

### 18.1 Roles

The role page shows system and custom roles, permission categories, dangerous
permissions, and the number of current grants. Custom-role editing uses explicit
save and row-version conflict handling. System roles may be copied but not
edited.

### 18.2 Access groups

The group page manages the group name, description, status, members, and current
grants. Label the feature **Access groups** wherever confusion with metadata
groups is possible.

### 18.3 Resource access

Client, collection, and matter administration pages should have an **Access**
section listing direct users and groups, their roles, and whether the grant is
direct or inherited. Users with `matter.view` but without
`matter.manage_access` may not open this administration section.

### 18.4 Capability-driven application UI

Pages load effective permissions for their resource and use them to hide or
disable actions. Examples:

- A reviewer sees Search and Review but not Metadata configuration.
- An analysis operator may start an analysis run but cannot publish coding
  without `batch.publish_analysis`.
- A classification manager may train a model but activation remains hidden
  without `classification.activate`.

Direct navigation must still produce a server authorization failure. Component
tests should verify both visible and unavailable actions for representative
roles.

### 18.5 Audit Log

Add an **Audit Log** page to tenant administration. It is visible only to a
user with `tenant.view_audit`; direct navigation without that permission fails
authorization in Core as well as being absent from the navigation.

On first load, the page shows the 50 most recent tenant audit actions. The
read-only table contains:

```text
Date
User
Action
Target
```

**Date** uses the administrator's display timezone while retaining the exact
timestamp in the row details. **User** displays the current user name and email
when available, **System** for a null actor, or the retained actor ID when the
user record is unavailable. **Action** shows a human-readable label with the
stable action key available in row details. **Target** links to the resource
when the administrator still has permission to view it; otherwise it remains
plain text.

The filter bar provides:

- **Date**: a single date or date range.
- **User**: a searchable tenant-user selector, including **System**.
- **Action**: a searchable action selector that also accepts an action-key
  prefix.

Applying or clearing a filter resets pagination and requests the newest 50
matching records. An explicit **Load older** or next-page control uses the API
cursor; the browser does not fetch the entire ledger. Selecting a row opens a
read-only detail panel containing the exact timestamp, actor, action key,
target reference, and formatted bounded `details` JSON.

Role, access-group, and access-assignment screens link to this page with target
filters already applied. Filter state may be represented in the URL so an
administrator can share or revisit the same audit view, but the server always
revalidates tenant access and ignores unauthorized tenant identifiers.

## 19. Audit and observability

The existing Core `audit_record` ledger is the authoritative application audit
history, including authorization changes. `authorization_grant` and the other
authorization tables contain current state; they are not substitutes for the
audit ledger.

Every command that changes security state must insert its audit record through
`record_audit` before the shared Core transaction commits. The command service,
not the router or browser, owns both writes. If validation, authorization, the
domain write, or the audit insert fails, neither change commits. Do not enqueue
the audit write after commit and do not treat best-effort logging as sufficient.

Required action keys begin with `authorization.`:

```text
authorization.permission.catalog_updated
authorization.system_role.permissions_updated
authorization.role.created
authorization.role.updated
authorization.role.archived
authorization.role.permissions_replaced
authorization.access_group.created
authorization.access_group.updated
authorization.access_group.archived
authorization.access_group.members_replaced
authorization.grant.created
authorization.grant.role_changed
authorization.grant.removed
authorization.grant.migrated
authorization.bootstrap.used
```

User activation, suspension, archival, credential reset, and session
termination are also security changes and must continue to write their own
audited user or authentication action. Successful break-glass use is audited;
rejected authorization attempts use bounded security logs and metrics rather
than creating transactional policy audit rows.

Each authorization audit record includes, as applicable:

```text
actor user or service identity
tenant ID
authorization action
target type and target ID
subject type and subject ID
role ID
scope type and scope ID
before and after values
added and removed permission or member IDs
request or workflow correlation ID
authorization revision before and after the change
```

Audit details must be bounded. Membership and permission replacement commands
have explicit maximum change counts so their complete added and removed ID sets
fit in one audit record. Larger changes are submitted as separately audited
commands rather than truncating the security history.

Authorization audit records are append-only. Core exposes no update or delete
command for them, and the deployed database role should not receive permission
to alter or delete audit rows. Add an index over tenant, target type, target ID,
and creation time so administrators can inspect the complete history of a role,
group, or grant.

Reading the security audit log requires `tenant.view_audit`. Audit results are
always tenant-filtered unless the caller is using the explicit root
administration path.

Add metrics for authorization decision count and latency, denials by permission
key, Artifact delegation failures, route-manifest coverage, and shadow-mode
differences between legacy and new authorization.

Do not log JWTs, raw delegation tokens, passwords, document contents, or
unbounded group membership lists.

## 20. Migration and compatibility

Migration must preserve every current user's access before enforcing the new
model.

### 20.1 Seed and backfill

1. Seed the permission catalog and immutable system roles.
2. Create one tenant-scope Tenant Administrator grant for every active user
   whose current `tenant_role` is `ADMIN`.
3. Convert each `client_membership` to a client-scope Client Administrator
   grant.
4. Convert each `matter_membership` to a matter-scope Matter Administrator
   grant.
5. Deduplicate grants without removing the explicit narrower grants. They are
   useful if the broader tenant grant is later removed.
6. Bootstrap the root administrator's Root Administrator grant.
7. Set each tenant's authorization revision after backfill.

Backfill is idempotent and validated with counts and per-tenant summaries.
Seed and backfill transactions write the corresponding permission-catalog,
system-role, and migrated-grant audit actions with a system actor identity.

### 20.2 Compatibility period

During rollout:

- Continue writing legacy memberships where existing create flows expect them.
- Evaluate the new permission result in shadow mode beside legacy
  `can_admin_*` checks.
- Preserve the legacy allow decision while reporting mismatches.
- Correct route mappings and migration defects before new enforcement becomes
  authoritative.

Do not use `legacy OR new` indefinitely. It is a temporary migration mode with
an explicit removal phase.

### 20.3 Cutover and cleanup

After shadow comparison is clean:

1. Make the new authorization service authoritative.
2. Stop writing `tenant_role`, `client_membership`, and `matter_membership`.
3. Remove compatibility checks and update durable workflow reauthorization.
4. Remove the legacy columns and tables in a later migration after one release
   of stable operation and a verified backup.
5. Update repository architecture rules and user documentation.

## 21. Implementation phases

### Phase 0: Permission inventory and governing updates

- Update `AGENTS.md` authorization rules to adopt this model.
- Create the route-permission manifest covering Core and Artifact operations.
- Finalize the permission registry and system-role matrix.
- Identify every list query requiring authorization filtering.
- Identify every durable workflow requiring launch and execution-time checks.

Exit criteria:

- Every current non-public route is classified.
- System-role permission snapshots receive product approval.
- Collection-to-matter publication semantics are recorded as an architectural
  decision.

### Phase 1: Database foundation

- Add permission, role, role-permission, access-group, group-member, grant, and
  authorization-revision schema.
- Seed permissions and system roles.
- Backfill legacy administrator grants.
- Add model and migration tests, including upgrade and downgrade coverage.
- Add the authorization audit event catalog, transactional command helpers, and
  audit lookup indexes.

Exit criteria:

- Backfill preserves every legacy allow relationship.
- Cross-tenant rows fail database or command validation.
- System role and permission registries match their database seeds.

### Phase 2: Central authorization service

- Implement typed resource references and ancestry resolution.
- Implement `require`, `can`, `filter_accessible`, and `explain`.
- Add request-scoped caching keyed by authorization revision.
- Convert compatibility `can_admin_*` helpers to call the new service in
  shadow mode.
- Add route-manifest CI coverage.

Exit criteria:

- Direct and group grants produce identical decisions when configured
  identically.
- Tenant and client inheritance is covered by positive and negative tests.
- Shadow evaluation records mismatches without changing production behavior.

### Phase 3: Role, group, and grant APIs

- Add permission discovery and role administration APIs.
- Add access-group and membership APIs.
- Add grant creation, role-update, removal, effective-access, and explanation
  APIs.
- Add the tenant-filtered, read-only audit API over the existing complete
  application audit ledger, with newest-first cursor pagination and Date,
  User, and Action filters.
- Change user creation so ordinary users receive no implicit administrator
  role.
- Add optimistic concurrency and full audit coverage.

Exit criteria:

- A tenant administrator can create a reviewer, place the user in a group, and
  grant that group a matter role without direct database changes.
- Privilege-escalation attempts through custom roles or cross-tenant IDs fail.
- Every successful security mutation has exactly one matching audit record, and
  forced audit-write failures roll back the mutation.

### Phase 4: Matter read and review authorization

Status: implemented.

- Convert matter listing and detail routes to `matter.view`.
- Convert search and direct document reads to `document.view`.
- Convert reviewer coding to `document.review`.
- Convert batch review to `batch.review` and assignment-aware checks.
- Filter shared-search user candidates through matter access.
- Add the Reviewer and Read Only UI experiences.

Exit criteria:

- A reviewer can search, read, and code only granted matters.
- A read-only user cannot write coding or start jobs.
- Matter lists, direct URLs, search results, and artifact reads agree on access.

### Phase 5: Matter administration and processing authorization

- Convert metadata, matter definition, analysis-task, batch-management,
  embedding, search-index, bulk-tag, classification, and promotion operations
  to their explicit permissions.
- Add launch and execution-time workflow authorization snapshots.
- Separate viewing status from starting, canceling, approving, activating, or
  publishing work.

Exit criteria:

- Representative custom roles can operate one subsystem without receiving
  unrelated matter-administrator capabilities.
- Removing a permission stops queued protected mutations and blocks new ones.

### Phase 6: Collection access and Artifact delegation

- Add collection-scope grant validation through the Artifact gateway.
- Replace broad Artifact principal derivation with action-aware scope claims.
- Enforce collection permissions on list, item, ingestion, processing,
  deletion, and publication operations.
- Require both source collection and destination matter permission for
  publication.
- Add narrow matter-document artifact delegation.

Exit criteria:

- Collection-only users cannot access matters.
- Matter reviewers can read published matter documents without browsing the
  source collection.
- Direct collection and artifact URLs cannot bypass Core grants.

### Phase 7: Administration UI

- Add role, permission, access-group, membership, and grant screens.
- Add Access sections to client, collection, and matter administration.
- Load effective capabilities on operational pages.
- Hide or disable actions consistently and show useful access-denied states.
- Add an effective-access explanation view for administrators.
- Add the admin-only tenant Audit Log page showing the newest 50 matching
  actions, Date/User/Action filters, cursor navigation to older records, and
  filtered history links from roles, access groups, and grants.

Exit criteria:

- All common authorization tasks are possible without database access.
- A permitted tenant administrator can filter and page through the Audit Log,
  while a user without `tenant.view_audit` cannot see or directly open it.
- UI route guards and action controls match API decisions in component and
  end-to-end tests.

### Phase 8: Enforcement cutover and legacy removal

- Review shadow-decision metrics and resolve all unexplained differences.
- Make permission authorization authoritative.
- Stop legacy membership writes.
- Remove compatibility helpers and update documentation.
- Remove legacy role columns and membership tables after the stabilization
  window.

Exit criteria:

- No production route depends on `tenant_role`, `ClientMembership`,
  `MatterMembership`, or `can_admin_*`.
- The route manifest has complete CI coverage.
- Existing administrators and newly configured non-admin users pass migration
  acceptance tests.

### Phase 9: Restricted document access

This is a later, separately approved phase.

- Add reusable document access policies bound to access groups.
- Inject policy filters into all search modes and derived document selections.
- Enforce the same policy on direct reads, artifacts, batches, exports,
  analysis, classification, and background jobs.
- Reindex affected matter documents when policies change.

Do not begin this phase until query-time and direct-read enforcement can ship
together.

## 22. Test strategy

### 22.1 Model and command tests

- System roles are immutable.
- Custom roles cannot acquire root-only permissions.
- Group members, roles, resources, and grants cannot cross tenants.
- Exactly one grant subject and one scope are required.
- Duplicate grants are rejected idempotently.
- Archived roles and groups do not create new effective access.
- Authorization revision increments atomically with every policy change.
- Every role, permission membership, access-group membership, and grant
  mutation writes the expected authorization audit action.
- The audit record and security mutation commit atomically.
- A forced audit insertion failure leaves both security state and authorization
  revision unchanged.
- Grant deletion retains its complete history in the append-only audit ledger.
- Audit queries require `tenant.view_audit` and never return another tenant's
  records.
- The default audit query returns at most the newest 50 records in stable
  `created_at DESC, id DESC` order.
- Date, actor-user, exact-action, and action-prefix filters compose without
  weakening tenant isolation.
- Null actors are returned as system events, and missing or archived users do
  not remove historical audit records.
- Audit cursors neither duplicate nor skip records when multiple rows share a
  creation timestamp.

### 22.2 Evaluator matrix

Cover direct and group grants at every scope with:

- exact-scope allow;
- inherited allow;
- unrelated-client denial;
- unrelated-matter denial;
- missing-permission denial;
- suspended-user denial;
- removed-grant denial;
- union of multiple roles;
- cross-tenant denial;
- root bootstrap behavior.

### 22.3 Route coverage

- CI fails when a new non-public route lacks a manifest entry.
- List routes return only authorized resources.
- Direct inaccessible resources return the chosen `404` behavior.
- Visible resources with insufficient action permission return `403`.
- Browser capability responses never substitute for backend enforcement.

### 22.4 Artifact and workflow tests

- Delegation tokens cannot broaden their action or resource scope.
- Expired delegations fail closed, and long-running work refreshes delegation
  after authorization changes rather than retaining a run-wide token.
- Matter-document delegation cannot list the source collection.
- Publication requires both source and destination permissions.
- Jobs reauthorize protected mutations after grant removal.
- Historical result reads use the requesting user's current permissions.

### 22.5 Migration tests

- Upgrade and downgrade work from a production-shaped legacy database.
- Backfill is idempotent.
- Every legacy administrator retains equivalent access.
- Explicit client and matter memberships remain represented after broader
  tenant grants are removed.
- New users are not administrators by default.

## 23. Security review checklist

Before enforcement cutover, verify:

- No route trusts tenant, client, collection, matter, user, or group ownership
  supplied by the browser.
- No list endpoint authorizes only the parent and leaks unauthorized children.
- No background worker treats a recorded initiator as permanent authorization.
- No Artifact delegation permits a broader action than Core approved.
- No search, facet, saved-search, random-sample, batch, export, analysis, or
  classification path bypasses content access filters.
- No custom role can modify its own role or grant unless the caller already has
  the independent role-management permission.
- The last Tenant Administrator cannot accidentally remove the tenant's only
  access-management path without an explicit guarded operation.
- Root break-glass use is narrow, audited, and observable.

## 24. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| A route remains protected only by authentication | Route-permission manifest and CI comparison with FastAPI operations. |
| Existing administrators lose access during migration | Idempotent legacy backfill, shadow evaluation, and delayed cutover. |
| New custom roles accidentally gain broad authority | Code-owned permissions, root-only permission restrictions, previews, and matrix tests. |
| Group or grant changes remain cached | Tenant authorization revision and short-lived/request-scoped caches. |
| Artifact tokens become too large | Encode ancestor scopes and exact narrow references rather than enumerating descendants. |
| Collection and matter access become unintentionally coupled | Treat publication as entry into the matter security boundary and use narrow artifact delegation. |
| Polymorphic collection grants lose referential integrity | Validate through Artifact at mutation time and run an orphan integrity check. |
| Document ACLs degrade search correctness or performance | Defer them until one policy model covers database, OpenSearch, direct reads, and jobs. |
| “Group” terminology confuses authorization with metadata layout | Use `AccessGroup` internally and label administration screens **Access groups**. |

## 25. Definition of done

The initial role and content-access implementation is complete when:

- Users can exist without administrator access.
- System and custom roles map to stable permissions.
- Users and access groups can receive scoped roles on clients, collections, and
  matters.
- Resource lists, direct reads, mutations, search, jobs, and artifacts enforce
  the same effective access.
- A matter reviewer can review granted content without receiving configuration
  or collection-administration privileges.
- Collection access and published matter-document access remain separate.
- Administrators can manage roles, groups, members, and grants through the UI.
- Authorized tenant administrators can inspect the most recent 50 audit
  actions, filter them by Date, User, and Action, load older records, and view
  event details without changing the ledger.
- Every protected route has a tested permission declaration.
- Existing administrators retain access through the migration.
- Legacy `ADMIN` columns, membership checks, and `can_admin_*` dependencies are
  retired after the stabilization window.
