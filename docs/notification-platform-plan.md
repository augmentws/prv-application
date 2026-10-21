# Notification Platform Implementation Plan

Status: in-application foundation implemented; external delivery phases proposed.

## Implementation status

The Core notification registry, durable notification/audience/recipient records, scoped user/group/role/permission
audience resolution, recipient inbox APIs, application-shell badge, recent-items menu, and full notification center
are implemented. The first producers publish unified job completion/failure notifications to the initiating user and
agent action requests to users with the required matter-management permission. Agent decisions resolve the shared
notification without changing each recipient's read state. The browser uses bounded foreground polling in this
release.

Personal routing preferences, configured shared destinations, delivery-attempt records, Slack and Teams adapters,
digest delivery, and provider-native interactive actions remain future phases.

Related documents:

- `docs/chatbot-server-streaming-plan.md`
- `docs/first-pass-review-program-automation-plan.md`
- `docs/first-pass-review-program-ux-plan.md`
- `docs/role-permission-content-access-implementation-plan.md`
- `docs/unified-job-execution-platform-plan.md`
- `docs/job-outcome-manifest-platform-plan.md`
- `../../priv-view-architecture/core-application.md`
- `../../priv-view-architecture/platform-architecture.md`

## 1. Outcome

Add a generic notification platform that lets any Priv-View feature create durable, user-facing notifications. A
notification may be informational, such as a completed analysis task, or may direct a user to an authoritative
domain action, such as approving an agent action request.

The first release provides an in-application inbox with user-, group-, role-, and permission-based audiences. Matter
notifications are a filtered view of the same platform, not a separate subsystem. Later releases may deliver the
same notifications through Slack, Microsoft Teams, email, or other adapters without changing feature producers.

The platform must preserve these boundaries:

```text
authoritative domain mutation
    -> typed notification publication
    -> durable notification and recipient rows
    -> in-application inbox
    -> optional channel-specific delivery attempts
```

A domain object remains authoritative for the work and any required decision. A notification tells a person that the
work exists and gives them a safe route to it; it does not become a second task, job, approval, or audit system.

## 2. Terminology and boundaries

### 2.1 Domain events

A domain event records a meaningful application fact such as `analysis_task.completed`, `batch.ready`, or
`agent_action.requested`. It is application-facing and may be emitted transactionally through an outbox. A domain
event is not automatically suitable for display to a person.

### 2.2 Agent lifecycle events

The existing `agent_conversation_event` stream reports conversation and run changes such as a run starting, a
message being committed, an action request being created, or a run failing. It exists to keep mounted chat surfaces
synchronized and supports bounded replay and retention.

Do not use `agent_conversation_event` as the notification inbox. It has different authorization, retention, replay,
and presentation requirements. Most lifecycle events are too noisy to notify a person.

### 2.3 Notifications

A notification is durable, human-facing attention state. It has a controlled type, presentation, application
context, audience, and source reference. Each resolved recipient has independent read and dismissal state.

### 2.4 Delivery attempts

A delivery attempt is one effort to present a notification through a channel and destination. In-application inbox
availability is part of notification persistence. Slack, Teams, email, and future transports use durable delivery
attempts with provider-specific adapters and retries.

Delivery success is not domain success. Failure to send a Slack message must not change a completed job back to
failed, and clicking or reading a notification must not complete its referenced action.

## 3. Locked decisions

1. The platform is generic. Matter notifications use matter context and filters but do not have separate tables or
   producer APIs.
2. The Core database is authoritative for notification, audience, recipient, preference, and delivery-control
   state. Provider APIs are never authoritative.
3. Notifications are created only through a typed application service. Features and agent runtimes do not insert
   notification rows directly.
4. Notification types and templates are code-owned and versioned. An agent may supply bounded typed values but may
   not invent an audience, arbitrary external destination, action endpoint, or unbounded Slack/Teams message.
5. Notification publication and the corresponding Core domain mutation commit atomically when both occur in Core.
   A rolled-back mutation creates no notification.
6. Audience selectors are retained for explainability, and eligible users are resolved into durable per-user rows at
   publication time.
7. User, active access-group, exact-role, and effective-permission audiences are supported. Effective-permission
   targeting is preferred when the intent is "people authorized to perform this action." Exact-role targeting is
   appropriate when the role represents operational responsibility rather than mere capability.
8. Role audiences use the named active role at the selected scope. They include direct user grants and members of
   active groups holding that grant. They do not silently expand to unrelated administrators at broader scopes.
9. Permission audiences use the normal authorization evaluator for the referenced resource, including applicable
   inherited grants and active group membership. Authorization logic is not duplicated in notification code.
10. Recipient membership is a publication-time snapshot. A later grant change does not rewrite notification history.
    Current authorization is still checked before opening a referenced resource and before executing an action.
11. A notification may project whether its source action remains open, but the referenced domain object owns the
    action state and decision rules.
12. The first external-channel release uses links back to Priv-View. Slack- or Teams-native approvals are a separate
    release requiring verified identity binding, signed callbacks, execution-time authorization, and idempotency.
13. Read, seen, dismissed, delivery, and action-resolution states are separate. They must not be collapsed into one
    status column.
14. PostgreSQL `NOTIFY`, if used for live inbox updates, carries wake-up routing only. Clients always read committed
    notification state from tables.
15. Notification content is a summary, not an evidence transport. It must not contain document text, hidden model
    reasoning, raw tool arguments, credentials, secrets, or other unnecessary matter content.

## 4. Product behavior

### 4.1 Inbox surfaces

The React application adds one global notification control in the authenticated application shell:

- a badge showing the current user's unread count;
- a compact recent-notification panel; and
- a full notification center with `All`, `Unread`, and `Needs action` views.

The full notification center supports optional client and matter filters. A matter page may link to the same center
with its matter filter applied. This provides a matter-manager workspace without creating a second inbox model.

Each item displays:

- severity and notification category;
- title and safe summary;
- client and matter context when applicable;
- creation time;
- unread, dismissed, and action-required state;
- source status when it can be obtained safely; and
- a primary application link such as `Open job`, `Review request`, or `Open batch`.

Marking an item read is explicit or occurs when the item is opened. Dismissing an item removes it from the default
view but does not delete it, alter the source object, or erase delivery history. Action-required notifications remain
available in the `Needs action` view until the source action is resolved, even if they have been read.

### 4.2 Initial notification policy

The initial producers should be deliberately small:

| Event | Default recipients | Kind | Default priority |
| --- | --- | --- | --- |
| Analysis or processing job completed | Initiating user | Informational | Normal |
| Analysis or processing job failed | Initiating user; configured operational role if present | Informational | High |
| Agent action request created | Users selected by the action's exact authorization policy | Action required | High |
| Agent action request resolved | Existing recipients of the open notification | Resolution update | Normal |
| Review batch ready | Creating user or explicitly assigned user | Informational | Normal |

Do not initially notify every matter administrator whenever any successful task completes. Completion notices are
primarily for the initiator. Manager audiences are appropriate for failures, unowned work, explicit subscriptions,
or actions for which the manager is responsible.

Progress changes such as queued, starting, batch 4 of 20, retrying, or saving output are not notifications. Those
states remain in job views and lifecycle streams.

### 4.3 Noise controls

Every producer supplies an idempotency key. Repeated workflow execution, callback reconciliation, and transaction
retry must not create duplicate notification records for the same logical transition.

A type may also define a collapse key for display grouping. Collapse affects presentation only; it does not erase
the underlying durable notification or delivery history.

Action-required in-application notifications cannot be disabled by personal preferences while the user remains an
intended recipient. Users may control optional completion notifications and external delivery channels in a later
preferences release. Digest delivery is deferred until individual delivery is proven.

### 4.4 Review Workflow integration policy

Review Workflow Templates use the shared notification backbone for user attention, but not as the transport that
advances a workflow. The complete boundary is:

```text
authoritative child-task transition
    -> durable typed program event
    -> idempotent Review Program coordinator transition
    -> optional authoritative action or assignment
    -> optional typed notification publication
    -> global notification center and safe application route
```

The program event is machine-facing and retryable. The notification is a human-facing projection of authoritative
domain state, never its source. When the coordinator opens an action or reaches another notifiable state in Core,
that coordinator mutation and notification publication commit atomically. Reading, dismissing, delivering, or
failing to deliver the notification never changes task or program state.

Every program-managed child execution declares a generic parent-managed notification policy and its parent run.
That policy suppresses the ordinary per-job `job.completed` notification and delegates meaningful attention to the
parent. It does not suppress the child's authoritative terminal state, audit history, failure diagnostics, or
durable program event. Standalone jobs keep the default job-notification behavior.

The first Review Program notification types are code-owned and schema-versioned:

| Type | Source authority | Kind | Primary route |
| --- | --- | --- | --- |
| `review_program.action_required` | Open `review_program_action` | Action required | Exact decision in the Review Program workspace |
| `review_program.review_assigned` | Review assignment or Review Batch | Action required | Assigned batch with a return route to the program |
| `review_program.attention_required` | Blocking program state | Action required | Failure or limit card in the program workspace |
| `review_program.completed` | Terminal Review Program run | Informational | Completion report |
| `review_program.stopped` | Stopped or canceled Review Program run | Informational | Program history |

Do not publish a notification for ordinary progress, each child completion, successful automatic retries,
reconciliation, per-document results, or automatic phase transitions. Those facts remain visible in the program
workspace, child job views, and activity history.

Action recipients come from the source action's exact authorization or explicit assignment policy. Completion and
stopped recipients use the program owner and any configured permitted audience. Template authors may select from
registered notification policies, but cannot invent notification types, audience selectors, route names, or
unbounded message content.

Resolving the source action updates the shared notification-resolution projection for all recipients without
changing their independent read or dismissal state. Opening a notification and resolving an action both recheck
current authorization. An empty audience for a required action is an operational error that leaves the program
waiting; it never falls back to an unauthorized user or silently advances.

## 5. Notification type registry

Add a code-owned registry, analogous to other manifest-backed platform configuration. A type specification contains:

```text
key                         analysis_task.completed
schema_version              1
category                    ANALYSIS
default_severity            SUCCESS
kind                        INFORMATIONAL
allowed_scope_types         MATTER
required_template_fields    task_name, run_id, completed_at
allowed_audience_kinds      USER, ROLE, PERMISSION
primary_route_builder       application-owned function
external_content_policy     SUMMARY_ONLY
default_external_policy     DISABLED
```

The registry validates every publication before rows are written. Templates produce a bounded plain-text title and
summary plus an application route descriptor. Templates do not render arbitrary HTML. Channel adapters render their
own escaped representation from the validated notification.

Unknown type keys, unsupported schema versions, missing template values, disallowed audience types, invalid source
references, and oversized values fail publication before commit.

The registry should initially live in application code. Tenant-authored templates, arbitrary webhooks, and a generic
rules language are explicitly out of scope.

## 6. Data model

Names below are proposed and may be adjusted to match model conventions during implementation.

### 6.1 `notification`

One immutable human-facing occurrence, with a small mutable resolution projection:

```text
id                          UUID primary key
tenant_id                   required tenant foreign key
scope_type                  TENANT | CLIENT | COLLECTION | MATTER
tenant_scope_id?            populated for tenant scope
client_id?                  populated for client scope and as parent context for collection scope
collection_id?              populated for collection scope
matter_id?                  populated for matter scope
type_key                    stable registry key
schema_version              positive integer
category                    bounded code
severity                    INFO | SUCCESS | WARNING | ERROR
kind                        INFORMATIONAL | ACTION_REQUIRED
title                       bounded plain text
summary                     bounded plain text
template_data               bounded JSON using an allowlisted schema
source_type                 bounded application resource type
source_id                   UUID or bounded stable source identifier
primary_route_name          allowlisted application route
primary_route_params        bounded JSON
created_by_kind             USER | SERVICE | WORKFLOW | SYSTEM
created_by_user_id?         user foreign key where applicable
idempotency_key             producer-supplied bounded key
occurred_at                 domain occurrence timestamp
created_at                  persistence timestamp
expires_at?                 optional inbox expiry
resolved_at?                projection of authoritative source resolution
resolution_code?            bounded non-authoritative presentation code
```

Constraints and indexes:

- unique `(tenant_id, type_key, idempotency_key)`;
- exactly one scope target consistent with `scope_type`;
- source and scope indexes supporting producer reconciliation and matter-filtered inbox reads;
- bounded title, summary, route parameters, and template data;
- no external URL supplied by a producer; and
- no cascade from source deletion that would erase notification history.

The route is an allowlisted application route plus parameters, not a free-form URL. The frontend route builder owns
the final relative URL and cannot be redirected to an arbitrary external origin.

### 6.2 `notification_audience`

The selectors requested by the publisher:

```text
id
notification_id
audience_kind               USER | ACCESS_GROUP | ROLE | PERMISSION
user_id?
access_group_id?
role_id?
permission_key?
scope_type
scope_id
created_at
```

Checks require exactly one selector value consistent with `audience_kind`. All referenced users, groups, roles, and
scopes must belong to the notification tenant. A role or permission must be valid for the selected resource type.

### 6.3 `notification_recipient`

One row per resolved user:

```text
id
notification_id
user_id
seen_at?
read_at?
dismissed_at?
created_at
```

Use a unique constraint on `(notification_id, user_id)`. Inactive users are excluded during audience resolution.
When one user matches several selectors, only one recipient row is created.

For explainability, add `notification_recipient_audience` with unique `(recipient_id, audience_id)` rather than
placing a single misleading reason on the recipient. This allows the UI or support tooling to explain that a user
was selected both directly and through a matter role.

### 6.4 `notification_delivery`

This table may be introduced with the foundation but remains unused until an external adapter is enabled:

```text
id
notification_id
recipient_id?               set for user-directed delivery
destination_id?             set for configured shared destinations
channel                     SLACK | TEAMS | EMAIL | future code
status                      PENDING | SENDING | DELIVERED | RETRY | FAILED | CANCELED
idempotency_key             stable adapter delivery key
attempt_count
next_attempt_at?
last_attempt_at?
delivered_at?
provider_message_id?
last_error_code?
last_error_summary?         redacted and bounded
created_at
updated_at
```

Do not store access tokens, provider response bodies, or unbounded exception strings in this table. Connection
credentials belong to the integration secret store.

### 6.5 Preferences and destinations

Personal `notification_preference` and configured `notification_destination` records are deferred from the first
slice. Their future contracts should support:

- type or category selection;
- minimum severity;
- immediate versus digest delivery;
- user-directed versus shared-channel destination;
- tenant, client, or matter scope;
- active/disabled status; and
- a content policy defining whether matter names or other context may leave Priv-View.

The absence of a preference never prevents an action-required item from appearing in the in-application inbox.

## 7. Publication and audience resolution

### 7.1 Service contract

Add one application service, conceptually:

```python
publish_notification(
    db,
    *,
    type_key="analysis_task.completed",
    schema_version=1,
    scope=ResourceRef.matter(matter_id),
    source=NotificationSource("matter_analysis_task_run", run_id),
    audiences=(
        UserAudience(initiated_by_user_id),
        RoleAudience(role_id, exact_scope=ResourceRef.matter(matter_id)),
    ),
    template_data={"task_name": task.name, "run_id": str(run.id)},
    idempotency_key=f"matter-analysis-task-run:{run.id}:completed",
)
```

The service:

1. Validates the registered notification type and template values.
2. Resolves and verifies the tenant and scope.
3. Validates the source belongs to the same tenant and permitted scope.
4. Validates each selector and resolves active users.
5. Applies the selector-specific authorization rules.
6. Creates the notification, audience, recipient, and recipient-audience rows.
7. Issues a small PostgreSQL wake-up after inserts when live inbox updates are enabled.
8. Returns the existing notification on an idempotent retry.

The caller owns `commit()`. The publication service must not commit independently. This keeps the domain transition
and notification atomic.

### 7.2 User audience

A direct user must be active, belong to the notification tenant, and be authorized for the notification scope unless
the registered type explicitly represents an access or account-administration event that is safe before access is
granted.

### 7.3 Access-group audience

Resolve only active group members and validate that the group belongs to the same tenant. A group audience is an
operational recipient list; it does not grant resource access. Each member must independently be authorized for the
scope and source before becoming a recipient.

### 7.4 Role audience

Resolve direct user grants and active-group grants assigning the exact active role at the explicitly named scope.
The selector does not include users merely because a different broader role gives them equivalent permissions. This
keeps "matter managers" an operational responsibility assignment rather than an accidental synonym for every root,
tenant, or client administrator.

Resolved users must still have the basic view permission for the notification scope.

### 7.5 Permission audience

Use `user_can()` with the selected `ResourceRef` and permission key. This includes applicable direct and group grants
and the existing narrowly defined administrative compatibility paths. Use this selector when the notification asks
someone to perform an operation and any authorized person may act.

The permission must match the action endpoint's actual authorization rule. Do not create a parallel
`can_receive_approval_notification` rule that can drift from execution authorization.

### 7.6 Empty audiences

Publication with no eligible recipient is valid only for notification types explicitly marked `allow_empty`. For an
action-required type, an empty resolved audience is an operational error. The source work remains pending, the
failure is logged and measured, and the producer follows its defined fallback or escalation policy. It must not
silently send the action to an unauthorized administrator.

### 7.7 Fan-out bounds

Audience resolution and recipient creation are synchronous in the first release so the recipient snapshot commits
atomically with the domain transition. Configure and enforce a maximum recipient count per publication. Exceeding
the limit fails before any notification rows are written; it must not create a partial audience.

The first release is intended for user-, matter-, and ordinary client-scoped audiences. Large tenant broadcasts need
a separately designed durable fan-out workflow that freezes the audience before processing it in chunks. Do not
quietly turn synchronous publication into an unbounded tenant-user scan.

## 8. Actions and resolution

The notification platform never executes arbitrary actions. A notification's primary route opens an existing
authorized UI backed by its normal domain endpoint.

For an `AgentActionRequest`:

1. Creating the pending request publishes one `agent_action.requested` notification in the same transaction.
2. The audience is derived from the same authorization requirement used by the decision endpoint.
3. Selecting `Review request` opens the existing action-request UI.
4. Approval or rejection is submitted to the existing decision endpoint, which locks the request, checks current
   authorization, records the decision, and writes its audit record.
5. The successful decision marks the notification's resolution projection with a stable resolution code.
6. All recipients see that the request is resolved; reading or dismissing remains per-recipient.

If the underlying action is resolved through another surface, reconciliation applies the same notification
resolution projection. A stale notification can never authorize a stale or forbidden action: the source endpoint
must still reject resolved, conflicting, or unauthorized requests.

The initial implementation supports one primary route per notification. Multiple generic command buttons and
notification-owned approval state are out of scope.

## 9. API contract

Add an authenticated user-scoped router. Proposed endpoints:

```text
GET  /v1/notifications
     ?state=ALL|UNREAD|ACTION_REQUIRED
     [&matter_id={matter_id}]
     [&client_id={client_id}]
     [&category={category}]
     [&cursor={opaque_cursor}]
     [&limit={bounded_limit}]

GET  /v1/notifications/counts

POST /v1/notifications/{notification_id}/read
POST /v1/notifications/{notification_id}/dismiss
POST /v1/notifications/mark-read
```

Rules:

- List and mutation queries always join through the current user's `notification_recipient` row.
- IDs supplied by another tenant or recipient return not found rather than revealing existence.
- Pagination uses a stable opaque cursor over `(created_at, id)` and a deterministic descending order.
- Counts distinguish unread from unresolved action-required items.
- Bulk mark-read accepts a bounded ID list and changes only the authenticated user's rows.
- Dismiss is idempotent. A later source-resolution update does not clear dismissal.
- API responses return allowlisted route descriptors, never provider credentials or internal delivery errors.

The generated OpenAPI client remains the frontend contract. Do not manually add notification types to generated
TypeScript output.

### 9.1 Live updates

The first slice may use bounded foreground polling if that is the smallest reliable delivery. The preferred live
update is a user-scoped SSE wake-up stream built on one PostgreSQL listener per API process and local bounded fan-out,
following the proven agent-event hub shape without reusing the agent event table.

The wake-up payload contains only tenant and user routing IDs. On wake-up the browser invalidates notification list
and count queries. No notification content is trusted from PostgreSQL `NOTIFY`.

Durable stream replay is unnecessary because the inbox tables are authoritative: first connection, reconnect, queue
overflow, or listener restart causes a fresh bounded snapshot and count read. The endpoint must still use the
subscribe-before-snapshot handshake to avoid a hydration gap and must rotate connections periodically to revalidate
authentication.

## 10. External delivery architecture

### 10.1 Adapter interface

Each provider implements a narrow interface receiving:

- notification ID and delivery idempotency key;
- validated, channel-neutral notification presentation;
- configured destination reference;
- safe Priv-View deep link; and
- content-policy result.

The adapter returns a normalized provider message ID or a classified retryable/permanent error. Feature producers do
not import Slack or Teams clients.

### 10.2 Delivery dispatcher

After commit, a delivery dispatcher claims pending rows using the repository's established durable-job conventions.
It records attempts, applies bounded exponential backoff with jitter, and uses the same idempotency key on ambiguous
provider retries. A permanent external-delivery failure leaves the in-app notification intact.

Do not start or restart a worker as part of implementation or deployment validation. Worker startup remains a
user-operated deployment step under the repository instructions.

### 10.3 Destination types

Support these separately:

- user-directed destinations, which require a verified mapping from Priv-View user to provider identity; and
- shared destinations, which are explicitly configured for a tenant, client, or matter.

A role audience does not imply a Slack channel. Audience resolution decides which Priv-View users should receive an
item; routing preferences and destination configuration decide whether and where an external copy is sent.

### 10.4 Interactive provider actions

Slack or Teams buttons that execute a decision are deferred. Before enabling them, the platform must provide:

- verified provider-subject-to-Priv-View-user identity bindings;
- request-signature verification and replay prevention;
- one-time or idempotent callback handling;
- current tenant, matter, and action authorization;
- a visible confirmation or conflict response; and
- the same audit record produced by an in-application decision.

## 11. Authorization, privacy, and isolation

1. Every notification and related row carries or derives a single tenant. Cross-tenant audiences and destinations
   are rejected before persistence.
2. Scope validation follows the existing `ResourceRef` and authorization model.
3. A recipient snapshot records who was selected; it does not preserve access to a resource after revocation.
4. Inbox reads suppress matter-, collection-, or client-scoped content the user can no longer view. The service should
   batch authorization checks for page scope IDs instead of issuing one query per row.
5. The referenced resource and every action endpoint perform current authorization independently.
6. Shared-channel destinations must be explicitly approved for the same scope. A tenant-wide channel is not assumed
   safe for matter names or matter-specific summaries.
7. External templates default to less detail than the in-app summary and link back to Priv-View.
8. Notification template data is bounded, schema validated, and redacted from ordinary logs.
9. Provider credentials are never stored in notification tables, API responses, audit details, or error summaries.
10. User-generated Markdown, HTML, model output, and imported evidence are escaped or summarized before rendering.

Publication itself does not need one audit row per informational notification. Changes to external destinations,
notification policy, or interactive action decisions are auditable. Notification and delivery rows provide
operational traceability but do not replace the application audit log.

## 12. Retention and deletion

Use independent policies for:

- notification presentation history;
- per-recipient read and dismissal state;
- external delivery attempts; and
- authoritative source and audit history.

Deleting expired notification presentation rows must not delete source jobs, action decisions, or audit records.
Action-required notifications are not eligible for ordinary expiry while their source action is unresolved. Failed
delivery diagnostics may have a shorter retention period than in-app history.

Tenant deletion follows the existing tenant lifecycle. User deletion or deactivation must preserve necessary
historical notification provenance without continuing future deliveries.

## 13. Backend implementation phases

### Phase 1: foundation and in-app inbox

1. Add the notification type registry and bounded template validation.
2. Add `Notification`, `NotificationAudience`, `NotificationRecipient`, and
   `NotificationRecipientAudience` models and the corresponding Core migration.
3. Implement transactional publication, idempotency, and audience resolution.
4. Implement list, counts, read, dismiss, and bounded bulk-read APIs.
5. Add audit-safe structured logging and basic metrics.
6. Add backend unit and API tests.
7. Regenerate the checked-in OpenAPI document and TypeScript client.

Exit criterion: a feature can atomically publish one typed matter notification to a user or scoped audience, and
only resolved recipients can see and manage their own inbox state.

### Phase 2: frontend and live invalidation

1. Add the application-shell badge and recent-notification panel.
2. Add the full notification center and matter/client filters.
3. Add unread and needs-action query state with accessible empty, loading, and error states.
4. Add bounded polling or the user-scoped SSE wake-up stream.
5. Ensure route descriptors map only to known application routes.

Exit criterion: a recipient sees a newly published item promptly, can open its source, mark it read, dismiss it, and
distinguish an unresolved action from an unread informational message.

### Phase 3: initial producers

1. Publish analysis/job completion and failure notifications from terminal state transitions.
2. Publish and resolve agent-action-request notifications in the same transactions as request and decision state.
3. Add review-batch-ready notification for the creating or assigned user.
4. Add a generic parent-managed notification policy for child executions so an orchestrating workflow can suppress
   per-child completion noise without suppressing authoritative terminal events.
5. Register Review Program action, assignment, attention, completion, and stopped notification types and safe route
   descriptors.
6. Verify retries and reconciliation use stable idempotency keys.
7. Tune default recipient policies from actual usage rather than enabling every potential producer.

Exit criterion: duplicate workflow execution cannot create duplicate inbox items, action resolution is consistent
across all recipients, and no progress-only lifecycle event appears as a notification.

### Phase 4: preferences and configured destinations

1. Add personal category/channel preferences.
2. Add tenant-, client-, and matter-scoped destinations with explicit content policies.
3. Add administrative APIs and UI using existing configuration permissions or new narrowly scoped permissions if
   the implementation proves they are required.
4. Audit destination and policy changes.

Exit criterion: external routing can be configured without changing notification producers or broadening resource
access.

### Phase 5: Slack and Teams adapters

1. Add durable delivery rows and dispatcher integration.
2. Implement one provider adapter at a time behind a feature flag.
3. Add verified user identity mappings for direct messages.
4. Add provider-specific rendering and content-policy tests.
5. Add retry, dead-letter visibility, and administrator diagnostics.
6. Implement the second provider only after delivery semantics are stable.

Exit criterion: provider outages and ambiguous retries neither duplicate logical deliveries nor affect domain or
in-app notification state.

Interactive provider approvals remain a separate explicitly approved phase.

## 14. Testing strategy

### 14.1 Publication and data integrity

- Domain rollback also rolls back the notification and recipients.
- The same idempotency key returns one logical notification under serial and concurrent retry.
- Type, template, route, source, scope, and payload bounds are enforced.
- Cross-tenant user, group, role, permission, source, and destination references fail closed.
- One user matching multiple selectors receives one item with every audience reason retained.
- Inactive users and inactive groups are excluded.
- An action-required type rejects or explicitly escalates an empty audience.

### 14.2 Audience semantics

- Direct-user selection requires tenant and scope eligibility.
- Group selection includes only active, independently authorized members.
- Exact-role selection includes direct and group grants at that exact scope but does not sweep in broader
  administrative roles.
- Permission selection matches `user_can()` for tenant, client, collection, and matter resources.
- Custom roles work without notification-specific code.

### 14.3 API and authorization

- A user sees only their recipient rows.
- Another recipient's notification ID returns not found.
- Matter and client filters cannot expose another tenant's counts or records.
- Read, dismiss, and bulk-read operations are idempotent and user scoped.
- Revoked resource access suppresses sensitive notification content and never permits opening or acting on the
  source.
- Cursor pagination remains stable when newer notifications arrive.

### 14.4 Actions

- Creating an action request creates one open notification.
- Approval, rejection, or external resolution updates the resolution projection for every recipient.
- A stale link cannot repeat or bypass the domain decision.
- Reading and dismissal never mutate authoritative action state.
- Concurrent decisions preserve the existing domain conflict behavior.

### 14.5 Orchestrated workflow integration

- A child terminal transition emits one durable program event even when its generic job notification is suppressed.
- The coordinator advances from the program event and authoritative child state, never from a notification row.
- Parent-managed child completions do not create per-child inbox items.
- One logical open action, assignment, blocking failure, completion, or stop produces at most one typed program
  notification across retries and reconciliation.
- A notification publication failure rolls back and retries the coordinator transition that attempted to publish
  it, but does not roll back the already committed child transition; atomicity is enforced at each documented
  transaction boundary.
- An empty action audience leaves the Review Program waiting and exposes an operational error.
- Review Program routes reopen the exact current state, reject stale action attempts, and reauthorize the user.
- Resolving a program action updates every recipient's resolution projection without changing read or dismissal.
- Notification presentation contains no document evidence, model reasoning, raw task payload, or arbitrary link.

### 14.6 Live updates and frontend

- Initial hydration has no fetch/subscribe gap.
- Reconnect, lost wake-up, listener restart, and local queue overflow recover through snapshot reads.
- Badge counts match list state after read, dismiss, and resolution changes.
- Keyboard and screen-reader users can identify unread and action-required items.
- Unknown route names and malformed parameters fail safely.

### 14.7 External delivery

- Provider requests contain only allowed fields for the configured content policy.
- Retry uses a stable delivery idempotency key.
- Retryable and permanent errors are classified without storing secrets or response bodies.
- Provider failure leaves the in-app item and source domain state unchanged.
- Disabled or deleted destinations stop unclaimed delivery without deleting history.

## 15. Observability and operations

Track at minimum:

- notifications published by type, severity, and scope;
- resolved recipient count and empty-audience failures;
- idempotent duplicate publications avoided;
- unread and unresolved-action age distributions;
- live-update connection count and invalidation failures;
- delivery attempts, latency, retry depth, and permanent failures by provider;
- redaction/template validation failures; and
- notifications suppressed because current resource access was revoked.
- parent-managed child notifications suppressed versus consolidated parent notifications published; and
- Review Program event-to-attention latency, empty action audiences, and duplicate publications avoided.

Logs use notification, source, tenant, and delivery IDs but omit title, summary, template data, provider payloads, and
credentials. Alerts should cover an aging action-required backlog, repeated empty audiences, and sustained external
delivery failure.

## 16. Migration and deployment

The foundation requires one new Core Alembic migration created from the then-current migration head. It adds the
notification, audience, recipient, and recipient-audience tables plus their constraints and indexes. Delivery,
preference, and destination tables may ship in later migrations when their features are implemented.

The implementation handoff must name the actual migration file and provide the exact command:

```bash
pipenv run alembic upgrade head
```

No data backfill is required for the first release. Existing agent lifecycle events and completed jobs are not
converted into historical notifications.

Deploy the schema and read APIs before enabling producers. Deploy the frontend before or with the first producers.
External adapters remain disabled until destinations, credentials, content policy, and dispatcher monitoring are in
place. No background worker is started or restarted by implementation or migration steps.

## 17. Explicitly out of scope for the first release

- Treating every domain or agent lifecycle event as a notification.
- Arbitrary agent-authored notification text or audiences.
- Notification-owned jobs, approvals, assignments, or audit truth.
- Slack- or Teams-native approvals.
- Tenant-authored templates or notification rules code.
- Arbitrary webhooks or user-supplied callback URLs.
- Evidence excerpts, document bodies, hidden reasoning, or raw tool arguments in notifications.
- Mobile push, browser push, SMS, and digest scheduling.
- Historical backfill from existing jobs or lifecycle event tables.

## 18. MVP acceptance criteria

The notification foundation is complete when:

1. Any authorized application feature can publish a registered notification in its existing Core transaction.
2. Publication supports direct users, active groups, exact roles, and effective permissions at an explicit scope.
3. Recipient resolution is tenant safe, deduplicated, explainable, and idempotent.
4. The initiating user receives one completion or failure item for an enabled job type.
5. Every eligible approver receives an action-required item for an agent action request, and resolving the request
   resolves the item for all recipients without changing their read state.
6. The global notification center and matter filter show only the authenticated user's currently viewable content.
7. Read, dismiss, unread-count, needs-action-count, pagination, and deep links work across reloads.
8. Workflow retries, callback reconciliation, and concurrent publication do not create duplicates.
9. Notification summaries contain no unapproved matter evidence, secrets, or arbitrary URLs.
10. The schema and producer design can add Slack or Teams delivery without changing feature publication contracts.
