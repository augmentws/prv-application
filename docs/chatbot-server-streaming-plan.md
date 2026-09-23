# Chatbot server-streaming conversion plan

## Outcome

Replace the Matter Definition chatbot's four polling loops with one authenticated server-sent events (SSE)
connection per active conversation. The first release streams durable lifecycle changes immediately. A second release
adds user-visible model text deltas after the provider and DBOS retry behavior has been validated.

The database remains authoritative. Streaming is a delivery mechanism, not a second source of state.

## Current state

`matter-definition-panel.tsx` currently polls:

- conversations every 2 seconds;
- messages every 1.5 seconds;
- runs every 1.5 seconds; and
- action requests every 1.5 seconds.

Agent work executes in a separate DBOS worker. The API and worker therefore cannot rely on an in-memory event bus.
The generic Next.js Core API proxy also buffers upstream responses with `arrayBuffer()` and always requests JSON,
so it cannot carry an event stream without a dedicated streaming path.

## Scope

### First release: lifecycle streaming

Stream these committed state changes:

- `conversation.updated`
- `turn.created` and `turn.updated`
- `run.created` and `run.updated`
- `message.created`
- `action_request.created` and `action_request.updated`
- `heartbeat`

The assistant message appears when it has been durably committed, as it does today. This release removes polling
without changing model execution semantics.

### Second release: text streaming

Stream only user-visible assistant text deltas. Do not stream hidden reasoning, chain-of-thought, provider-internal
events, or unapproved tool arguments. Tool and approval activity remains represented by structured lifecycle events.

## Architecture

### 1. Durable conversation event log

Add an `agent_conversation_event` table with:

- a globally ordered `BIGINT` event ID;
- tenant, matter, and conversation IDs;
- optional turn, run, message, and action-request IDs;
- event type;
- attempt ID where applicable;
- a versioned JSON payload; and
- creation timestamp.

Indexes should support `(conversation_id, id)` replay and retention cleanup. Event payloads must contain only data
the conversation's authorized reader may see.

Create one event publisher service. It inserts the event in the same database transaction as the domain mutation
and issues PostgreSQL `NOTIFY` in that transaction. PostgreSQL delivers the notification only after commit. A missed
notification is harmless because reconnecting clients replay rows from the event table.

The existing conversation, turn, run, message, and action-request tables remain the source of truth. The event log
is an ordered delivery record and must not be used to reconstruct billing or legal/audit state.

### 2. Publish events at every mutation boundary

Instrument the existing mutation paths rather than inferring changes by polling:

- conversation creation;
- turn creation and queueing;
- run preparation and transition to `RUNNING`;
- completed run persistence;
- approval-request creation;
- approval decisions and resumed runs; and
- run failure.

Events and their corresponding state changes must commit atomically. A rolled-back mutation must publish no event.
Use stable event schema versions so frontend and backend releases can overlap safely.

### 3. Authenticated SSE endpoint

Add:

```text
GET /v1/agent-conversations/{conversation_id}/events
```

The endpoint must:

- perform the same matter-admin authorization as the existing conversation endpoints;
- accept `Last-Event-ID` and an optional `after` cursor;
- replay committed events after the cursor before waiting for notifications;
- use a dedicated async PostgreSQL connection for `LISTEN`, rather than holding a normal request session open;
- emit a heartbeat every 15–20 seconds;
- stop promptly on client disconnect;
- bound replay page size and payload size; and
- return `text/event-stream`, `Cache-Control: no-store`, and proxy-buffering-disabled headers.

Each event includes its durable ID, event type, schema version, and JSON data. On an expired cursor, return a
`snapshot.required` event so the client refetches current state and resumes from the newest cursor.

### 4. Stream-safe Next.js proxy

Do not send SSE through the current catch-all proxy behavior, which consumes the entire upstream body with
`arrayBuffer()`. Add a dedicated route for conversation events, or a carefully isolated streaming branch in the
proxy.

The streaming proxy must:

- refresh the access token once before opening the upstream stream when necessary;
- pass the upstream `ReadableStream` through without buffering;
- preserve `text/event-stream`, cache, and connection headers;
- forward the cursor;
- propagate browser disconnect/abort upstream; and
- never attempt token refresh in the middle of an established stream.

Use the existing cookie authentication boundary. SSE is a safe `GET`; no CSRF mutation exception is needed.

### 5. Frontend stream hook

Create a `useAgentConversationStream(conversationId)` hook. Keep the existing queries for initial hydration and
recovery, then open one stream for the selected conversation.

For the first implementation, events may invalidate the exact React Query caches for conversations, messages,
runs, and action requests. Once the contract is stable, include complete read-model payloads in events and patch
those caches directly to avoid follow-up GET requests.

The hook must:

- remember the last event ID;
- reconnect with bounded exponential backoff and jitter;
- ignore duplicate event IDs;
- reconcile optimistic user messages by stable IDs;
- expose `connecting`, `live`, `reconnecting`, and `offline` states; and
- perform a full snapshot refetch after cursor expiry or an event schema it does not understand.

Remove the four chat polling intervals only after the stream is live. Keep a feature-flagged polling fallback for
the initial rollout.

### 6. Text-delta streaming spike

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

## Delivery sequence

1. Define the versioned event contract, retention policy, migration, and publisher service.
2. Add atomic lifecycle-event publication to all agent mutation paths.
3. Implement replay plus PostgreSQL `LISTEN/NOTIFY` in the FastAPI SSE endpoint.
4. Add the non-buffering Next.js streaming proxy and abort propagation.
5. Add the React stream hook, connection-state UI, and cache reconciliation.
6. Roll out lifecycle streaming behind `AGENT_STREAMING_ENABLED`; retain polling as fallback.
7. Measure correctness and delivery latency, then remove normal chat polling.
8. Complete the provider/DBOS streaming spike and add batched visible-text deltas behind a second flag.
9. Generalize the event infrastructure for assessment, batch, import, embedding, and topic-job progress if desired.

## Testing

### Backend

- authorization and tenant/matter isolation;
- monotonic event ordering and duplicate-free cursor replay;
- no event on transaction rollback;
- replay after a missed notification or API restart;
- worker-to-API delivery across separate processes;
- heartbeat and disconnect cleanup;
- cursor expiry and snapshot recovery; and
- retry attempt isolation for provisional output.

### Next.js proxy

- response bytes arrive before the upstream response closes;
- the proxy does not call `arrayBuffer()` for SSE;
- authentication refresh occurs before connection establishment;
- headers and `Last-Event-ID` are preserved; and
- browser abort closes the upstream request.

### Frontend

- lifecycle events update the correct query caches;
- optimistic messages are not duplicated;
- approval requests appear and resolve without polling;
- reconnect resumes from the last event ID;
- unknown/expired events trigger snapshot recovery; and
- polling fallback works when the feature flag is disabled.

### End to end

Run the API and DBOS worker as separate processes, submit a turn, observe `QUEUED → RUNNING → COMPLETED` and the
assistant message over one stream, then repeat with an approval request, a worker failure, and a reconnect.

## Observability and operations

Track active SSE connections, reconnect rate, event delivery latency, replay depth, expired cursors, event-log size,
and text-delta batching rate. Log conversation and event IDs, but never log document text or model output solely for
stream diagnostics.

Set explicit proxy idle timeouts above the heartbeat interval. Add retention cleanup for old delivery events; keep
final messages, runs, model invocations, usage accounting, and audit records under their existing retention rules.

## Acceptance criteria

- An active chat uses one SSE connection and makes no periodic messages/runs/actions polling requests.
- State committed by the DBOS worker appears without depending on API-process memory.
- Reconnect after API restart or transient network loss produces no missing or duplicate visible messages.
- Approval workflows and completed-conversation follow-up turns continue to work.
- The final rendered assistant message exactly matches the durable `AgentMessage` record.
- Usage accounting and model traces remain unchanged by lifecycle streaming.
- No hidden reasoning is exposed.
- The feature can be disabled without a database rollback.
