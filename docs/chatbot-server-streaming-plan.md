# Chatbot server-streaming conversion plan

## Implementation status

The lifecycle-streaming release described below is implemented for Matter Definition Chat and Batch Chat. Durable lifecycle events, matter-scoped replay cursors, PostgreSQL notification fan-out, authenticated SSE, the non-buffering Next.js proxy, React Query invalidation, reconnect, rotation, and polling fallback are active behind `AGENT_STREAMING_ENABLED` and `NEXT_PUBLIC_AGENT_STREAMING_ENABLED`. Token-by-token model output remains the separate second release and is specified in [chatbot-token-streaming-plan.md](chatbot-token-streaming-plan.md).

## Outcome

Replace the polling loops used by both Matter Definition Chat and Batch Chat with one authenticated server-sent
events (SSE) connection per mounted chat surface. The stream is scoped to the matter, workflow type, and optional
review batch so it can deliver conversation-list changes as well as lifecycle changes for the selected conversation.

The first release streams durable lifecycle changes immediately. A second release adds user-visible model text
deltas after provider and DBOS retry behavior has been validated.

The database remains authoritative. PostgreSQL notifications and API-process fan-out are wake-up mechanisms, not
sources of state. Normal operation uses no periodic chat polling, but an automatic polling fallback remains available
during rollout and prolonged stream failure.

## Current state

Matter Definition Chat currently polls:

- conversations every 2 seconds;
- messages every 1.5 seconds;
- runs every 1.5 seconds; and
- action requests every 1.5 seconds.

Batch Chat polls conversations every 2 seconds and messages and runs every 1.5 seconds. Both chat surfaces are in
scope for the lifecycle-streaming release.

Agent work executes in a separate DBOS worker. The API and worker therefore cannot depend on an in-memory event bus
for correctness. The generic Next.js Core API proxy also buffers upstream responses with `arrayBuffer()` and always
requests JSON, so it cannot carry an event stream without a dedicated streaming route.

## Scope

### First release: lifecycle streaming

Persist these lifecycle event types:

- `conversation.created` and `conversation.updated`;
- `turn.created` and `turn.updated`;
- `run.created` and `run.updated`;
- `message.created`; and
- `action_request.created` and `action_request.updated`.

The assistant message appears when it has been durably committed, as it does today. This release removes normal
polling without changing model execution semantics.

The stream also uses non-durable control frames:

- `stream.ready` establishes the initial high-water cursor;
- `stream.checkpoint` advances the resume position after the server has scanned through a high-water cursor;
- `snapshot.required` tells the client that its cursor cannot be replayed safely;
- `stream.rotate` asks the client to reconnect through the authenticated proxy; and
- heartbeat comments keep intermediaries from considering an idle connection dead.

Control frames and heartbeats are never inserted into the event log and do not allocate a new sequence.
`stream.ready` and `stream.checkpoint` may carry an existing matter high-water sequence that the client can safely use
as its next resume cursor.

### Second release: text streaming

Stream only user-visible assistant text deltas. Do not stream hidden reasoning, chain-of-thought, provider-internal
events, or unapproved tool arguments. Tool and approval activity remains represented by structured lifecycle events.

## Architecture

### 1. Durable conversation event log and cursor

Add an `agent_conversation_event` table with:

- an internal primary key that is not used as the replay cursor;
- tenant, matter, conversation, workflow type, and optional review-batch IDs;
- a transactionally allocated `matter_sequence`;
- optional turn, run, message, and action-request IDs;
- event type and schema version;
- attempt ID where applicable;
- a bounded JSON payload containing only data an authorized reader may see; and
- creation timestamp.

Add a unique constraint on `(matter_id, matter_sequence)` and indexes supporting matter/workflow/batch replay,
conversation lookup, and retention cleanup.

Do not use a PostgreSQL identity or sequence value as the delivery cursor. Sequence allocation is not commit ordered:
one transaction can allocate a lower value and commit after a transaction with a higher value, causing cursor-based
replay to skip the late commit permanently.

Instead, add an `agent_conversation_event_cursor` row per matter. The publisher locks that row, increments its
transactional counter, and writes the event with the resulting `matter_sequence` in the same transaction as the
domain mutation. A rollback therefore rolls back both the mutation and cursor increment. The cursor row also records
the newest sequence and the oldest sequence still available after retention cleanup. Creating the cursor row must be
race safe, using an insert-on-conflict followed by `SELECT ... FOR UPDATE`.

Create one event publisher service and require every publishing path to use it. After inserting an event, issue a
small PostgreSQL `NOTIFY` in the same transaction. Notifications contain only routing information such as the matter
ID and newest sequence; clients always read event contents from the durable table. PostgreSQL delivers the
notification only after commit.

The existing conversation, turn, run, message, action-request, billing, and audit tables remain authoritative. The
event log is an ordered delivery record and is not used to reconstruct legal, billing, or audit state.

### 2. Publish events at every mutation boundary

Instrument the existing mutation paths rather than inferring changes by polling:

- conversation creation and rename;
- turn, user-message, and run creation and queueing;
- run preparation and transition to `RUNNING`;
- completed run and assistant-message persistence;
- approval-request creation;
- approval decisions and resumed-run creation; and
- run failure.

Events and their corresponding state changes must commit atomically. A rolled-back mutation publishes no event.
Multi-record transitions may publish multiple events with consecutive matter sequences, but consumers must also be
correct if they invalidate the affected read models once for the whole group. Use stable event schema versions so
frontend and backend releases can overlap safely.

All cache inserts and patches must be idempotent by resource ID. A lifecycle event can arrive before the HTTP
response that created the same message or run, so blindly appending mutation results would create duplicates.

### 3. PostgreSQL notification hub

Do not reserve one PostgreSQL `LISTEN` connection per browser stream. Each API process owns one dedicated async
PostgreSQL listener connection and fans wake-ups out to its local SSE subscribers through bounded in-memory queues.
Every API process listens to the same notification channel, so clients connected to any process receive wake-ups.

This in-memory fan-out is safe because it carries no authoritative event data. Each subscriber queries committed
rows after its durable cursor. Queue overflow, listener restart, or ambiguous delivery closes the affected stream or
emits `snapshot.required`; reconnect and replay recover from the event table.

Add the required async PostgreSQL dependency and manage the listener as an application lifespan resource rather than
mixing it with the synchronous request SQLAlchemy session. Define explicit queue bounds, listener reconnect backoff,
and a database connection budget. When the listener reconnects, wake all subscribers so they query for missed rows.
A lightweight high-water check on heartbeat also prevents a silently lost notification from leaving an otherwise
healthy SSE connection stale indefinitely.

### 4. Authenticated matter-scoped SSE endpoint

Add:

```text
GET /v1/matters/{matter_id}/agent-events
    ?workflow_type=MATTER_DEFINITION_SETUP|BATCH_CHAT
    [&review_batch_id={review_batch_id}]
    [&after={matter_sequence}]
```

The endpoint performs the same matter-admin authorization as the existing conversation endpoints. Batch Chat scope
also validates that the requested review batch belongs to the matter. A stream emits only events matching its
authorized workflow and batch scope, while the cursor remains the matter-wide sequence. Gaps caused by other
workflows are expected and safe because new matching events can only receive a higher matter sequence.

The endpoint must:

- accept `Last-Event-ID` and the explicit `after` query cursor, rejecting conflicting values;
- establish the process-hub subscription before reading the high-water mark or replaying rows;
- replay committed matching events after the cursor in bounded pages;
- after every wake-up, query the durable log for rows after the current cursor rather than trusting notification
  contents;
- emit events in `matter_sequence` order and ignore already-delivered sequences;
- emit `stream.checkpoint` after scanning safely through a captured matter high-water sequence, including when no
  matching lifecycle events were found;
- bound replay page size, total replay work, and event payload size;
- stop promptly on browser disconnect and remove its local subscription;
- emit a heartbeat comment every 15–20 seconds;
- compare the cursor to the matter's retained-sequence floor and emit `snapshot.required` when it has expired;
- periodically compare against the durable high-water mark to recover from a missed notification; and
- return `text/event-stream`, `Cache-Control: no-cache, no-store, no-transform`, and `X-Accel-Buffering: no`.

#### Race-free first hydration

An initial client without a cursor must not replay the entire retained history and must not create a gap between its
initial GET requests and the live stream:

1. The endpoint subscribes to the notification hub.
2. It reads the current matter high-water sequence.
3. It emits `stream.ready` with that sequence and begins watching for higher sequences.
4. The frontend starts or refreshes the conversations, messages, runs, and action-request queries after receiving
   `stream.ready`, while buffering subsequent lifecycle events.
5. After hydration completes, the frontend applies the buffered events or invalidates the affected caches.

An event committed before the high-water read is included in the subsequent database snapshot. An event committed
afterward is buffered from the stream. This closes the initial hydration race without replaying historical events.

#### Race-free reconnect replay

For a client with a valid cursor, the endpoint subscribes first and then repeatedly queries all matching rows after
the cursor until caught up to a captured high-water mark. It then enters the notification loop. An event committed
between subscription and replay is therefore either included in replay or produces a queued wake-up; it cannot fall
between a final query and `LISTEN` registration.

If the cursor is below the retained floor, the endpoint emits `snapshot.required` with the current high-water cursor.
The client performs the first-hydration sequence again and does not reuse the expired cursor.

### 5. Authorization lifetime

Authentication and matter authorization are checked before the stream begins, but a long-lived stream must not
outlive authorization indefinitely. Set a maximum stream lifetime no longer than the access-token lifetime and also
bounded by a short membership-revalidation window. Before the deadline, emit `stream.rotate` and close cleanly. The
frontend reconnects through the Next.js proxy, which refreshes the token if necessary, and FastAPI re-authorizes the
matter and batch scope.

Permission removal is therefore bounded by the revalidation window. Deployments needing faster revocation may close
matching streams when membership changes, but forced rotation remains the correctness backstop.

### 6. Stream-safe Next.js proxy

Do not send SSE through the current catch-all proxy, which consumes the upstream body with `arrayBuffer()`. Add a
dedicated route for the agent event stream.

The streaming proxy must:

- refresh the access token once before opening the upstream stream when necessary;
- request `text/event-stream` and pass the upstream `ReadableStream` through without buffering;
- forward `Last-Event-ID` or the explicit cursor;
- propagate browser disconnect through an `AbortController` to the upstream request;
- never attempt token refresh in the middle of an established stream;
- preserve `Content-Type`, `Cache-Control`, and `X-Accel-Buffering`; and
- disable response transformation or compression that would buffer small frames.

Do not copy hop-by-hop or invalid streaming headers such as `Connection`, `Keep-Alive`, `Transfer-Encoding`, or a
buffered `Content-Length`. Use the existing same-origin cookie authentication boundary. SSE is a safe `GET`; no CSRF
mutation exception is needed.

### 7. Frontend stream hook

Create a `useAgentChatStream({ matterId, workflowType, reviewBatchId })` hook and use it in Matter Definition Chat and
Batch Chat. Use a fetch-based SSE parser rather than native `EventSource`; the hook needs explicit response-status
handling, `AbortController`, cursor control, schema recovery, and bounded reconnect behavior.

The hook must:

- keep the last delivered matter sequence for the active subscription;
- accept a `stream.ready` or `stream.checkpoint` high-water sequence as the resume cursor only after all earlier
  matching events have been applied;
- reconnect with bounded exponential backoff and jitter;
- ignore duplicate or older sequences;
- follow the `stream.ready` buffering and hydration protocol;
- deduplicate mutation responses and stream events by resource ID;
- expose `connecting`, `live`, `reconnecting`, `fallback`, and `offline` states;
- close the previous stream immediately when matter, workflow, batch, or selected surface changes;
- handle `stream.rotate` by reconnecting normally through the proxy;
- perform full snapshot hydration after cursor expiry or an unsupported schema version; and
- batch or debounce React Query invalidations generated by adjacent lifecycle events.

For the first implementation, lifecycle events may invalidate the exact conversation, message, run, and
action-request queries. Once the contract is stable, complete read-model payloads may patch those caches directly to
avoid follow-up GET requests.

Remove polling intervals only while the stream is healthy. Behind `AGENT_STREAMING_ENABLED`, activate polling when
the stream endpoint is unavailable, the browser is offline, or reconnects exceed a defined failure/time threshold.
Stop fallback polling after a successful race-free hydration on a live stream. When the feature flag is disabled,
retain the existing polling behavior without requiring a database rollback.

### 8. Text-delta streaming spike

Before changing production execution, verify that the configured Pydantic AI/provider path can stream the current
result union (`str | DeferredToolRequests`) while preserving tool approvals, usage accounting, model tracing, and
DBOS retries.

If supported, introduce:

- `run.output.started` with a unique attempt ID;
- `run.output.delta` containing only visible text;
- `run.output.completed`; and
- `run.output.replaced` when a retry supersedes a partial attempt.

Batch deltas by time or character count to avoid one database write per token. Persist the final assistant message
exactly once through the existing outcome path. The UI treats deltas as provisional and replaces them with the
durable final message when `message.created` arrives.

If a connection drops, the UI may replay retained chunks or show the latest provisional snapshot, but the committed
assistant message is always the recovery boundary. Token chunks can have shorter retention than lifecycle events.
Retention cleanup must update the matter's retained-sequence floor transactionally so an expired cursor always
produces `snapshot.required` rather than a partial replay.

## Delivery sequence

1. Define the versioned event and control-frame contracts, retention floor, migration, and transactional publisher.
2. Add atomic lifecycle-event publication to every agent mutation path and test concurrent commit ordering.
3. Add the per-process PostgreSQL notification hub with reconnect and bounded subscriber queues.
4. Implement the matter-scoped endpoint, race-free hydration, replay, cursor-expiry, rotation, and disconnect cleanup.
5. Add the non-buffering Next.js streaming proxy and abort propagation.
6. Add the shared React stream hook, connection-state UI, idempotent cache reconciliation, and automatic fallback.
7. Roll out lifecycle streaming behind `AGENT_STREAMING_ENABLED` in both Matter Definition Chat and Batch Chat.
8. Measure correctness and delivery latency, then disable normal polling while each stream is healthy.
9. Complete the provider/DBOS streaming spike and add batched visible-text deltas behind a second flag.
10. Generalize the event infrastructure for assessment, import, embedding, topic, and other job progress if desired.

## Testing

### Backend and event ordering

- authorization and tenant/matter/batch isolation;
- concurrent transactions where a transaction started first commits last, proving cursors follow commit serialization;
- monotonic matter-sequence allocation and duplicate-free replay;
- no event or cursor increment on transaction rollback;
- listener registration concurrent with event commit, proving there is no replay-to-listen gap;
- replay after a missed notification, listener restart, or API restart;
- worker-to-API delivery across separate processes;
- bounded subscriber-queue overflow and recovery;
- heartbeat, stream rotation, reauthorization, and disconnect cleanup;
- cursor retention-floor expiry and snapshot recovery; and
- retry attempt isolation for provisional output.

### Next.js proxy

- response bytes arrive before the upstream response closes;
- the proxy never calls `arrayBuffer()` for SSE;
- authentication refresh occurs before connection establishment;
- `Content-Type`, cache, buffering, and cursor headers are preserved;
- hop-by-hop and buffered content-length headers are absent; and
- browser abort closes the upstream request.

### Frontend

- first hydration buffers concurrent lifecycle events without losing or duplicating state;
- lifecycle events update the correct Matter Definition and Batch Chat caches;
- mutation responses and stream events do not duplicate messages or runs;
- conversation creation and rename update the conversation picker without polling;
- approval requests appear and resolve without polling;
- reconnect resumes from the last matter sequence;
- unknown schemas and expired cursors trigger snapshot recovery;
- stream rotation reconnects and reauthorizes;
- repeated stream failure activates polling and a healthy stream disables it again; and
- switching chat scope aborts the previous stream.

### End to end

Run the API and DBOS worker as separate processes. For both chat surfaces, submit a turn and observe
`QUEUED → RUNNING → COMPLETED` plus the assistant message over one stream. Repeat with an approval request, a worker
failure, an API-listener restart, a browser reconnect, token rotation, and a temporarily unavailable stream endpoint.

## Observability and operations

Track active SSE connections, notification-hub health, local subscriber count and queue overflows, PostgreSQL
listener reconnects, client reconnect rate, fallback activation, event delivery latency, replay depth, expired
cursors, event-log size, and text-delta batching rate. Log matter, conversation, and event-sequence IDs, but never log
document text or model output solely for stream diagnostics.

Set explicit proxy and load-balancer idle timeouts above the heartbeat interval. Define the maximum stream lifetime,
subscriber queue size, replay limits, event payload limit, retention duration, and cleanup schedule in configuration.
Keep final messages, runs, model invocations, usage accounting, and audit records under their existing retention rules.

## Acceptance criteria

- Each mounted Matter Definition or Batch Chat surface uses one scoped SSE connection.
- A healthy stream makes no periodic conversation, message, run, or action-request polling requests.
- Conversation creation and rename are visible through the stream, including changes made by another authorized client.
- State committed by the DBOS worker appears without depending on API-process memory for correctness.
- Concurrent commits, API restart, listener restart, transient network loss, and cursor replay produce no missing or
  duplicate visible messages.
- An initial page load cannot lose an event between snapshot hydration and stream establishment.
- Approval workflows and completed-conversation follow-up turns continue to work.
- The final rendered assistant message exactly matches the durable `AgentMessage` record.
- Usage accounting and model traces remain unchanged by lifecycle streaming.
- Streams periodically reconnect and reauthorize; permission removal is bounded by the configured window.
- No hidden reasoning is exposed.
- Repeated stream failure automatically restores polling until streaming recovers.
- The feature can be disabled without a database rollback.
