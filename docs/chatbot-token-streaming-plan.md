# Chatbot token-streaming implementation plan

## Status

This document describes the planned second release of chat streaming. It is not yet implemented.

The lifecycle-streaming release in [chatbot-server-streaming-plan.md](chatbot-server-streaming-plan.md) is the
prerequisite. It already supplies the durable matter cursor, PostgreSQL notification fan-out, authenticated SSE
endpoint, non-buffering Next.js proxy, reconnect and replay behavior, and polling fallback used by this plan.

The installed Pydantic AI version is `2.43.0`. It exposes `Agent.run_stream()` and
`StreamedRunResult.stream_text(delta=True)`, but the first delivery step must prove the correct API for the current
`str | DeferredToolRequests` output union and approval-required tool flow before production execution is changed.

## Outcome

Show visible assistant text progressively in Matter Definition Chat and Batch Chat while preserving the current
durability and security guarantees:

- never expose hidden reasoning, provider-internal events, tool arguments, or unapproved actions;
- keep the final `AgentMessage` as the authoritative assistant response;
- preserve DBOS retry behavior, Pydantic AI tool execution, deferred approvals, usage accounting, rate limiting, and
  model tracing;
- recover cleanly from worker, API, browser, and network interruption;
- prevent duplicate or concatenated output when an execution attempt is retried; and
- fall back to lifecycle-only behavior when a model or provider cannot stream safely.

“Token streaming” describes the user experience. Provider deltas may be coalesced into small frames before they are
persisted and delivered. The system must not perform one PostgreSQL transaction and notification for every provider
token.

## Non-goals

This release does not:

- stream chain-of-thought or hidden reasoning;
- make provisional text an authoritative chat message;
- change approval or authorization rules;
- stream structured assessment, embedding, clustering, or other background-job output;
- require every supported model to provide identical token boundaries; or
- remove the completed-message and lifecycle-event recovery path.

## Current execution boundary

Chat execution currently follows this path:

1. `prepare_agent_run()` marks the run and turn as running and constructs the pinned model request.
2. `execute_prepared_agent_run()` calls the generic `run_model()` helper.
3. `run_model()` calls `Agent.run()` and returns the final output, complete message history, usage, and invocation
   telemetry.
4. `persist_agent_run_outcome()` creates the authoritative assistant message or approval requests and publishes
   lifecycle events.

The streaming implementation should change only the chat execution path. Structured model tasks must continue using
the existing non-streaming `run_model()` contract.

## Design decisions

### Final messages remain authoritative

Partial output is a provisional rendering aid. It never appears as an `AgentMessage` and is never included in later
conversation history. On successful completion, the existing finalization transaction writes exactly one assistant
message. The browser replaces the provisional response with that durable message when it receives `message.created`.

The final output returned by Pydantic AI must exactly equal the text persisted in the final assistant message. A
completed provisional attempt whose assembled text differs from the final result is treated as replaced, not silently
accepted.

### Attempts isolate retries

Every real model execution receives a new output-attempt ID. All provisional events include both the logical agent-run
ID and physical attempt ID. If DBOS, Pydantic AI, or the provider retries after visible text was emitted, the old
attempt is marked `REPLACED` and the UI discards its text before displaying the replacement attempt.

An attempt-local monotonically increasing chunk sequence makes persistence idempotent. Repeating the same chunk
sequence is a no-op. A gap or conflicting duplicate fails the provisional stream without corrupting the final run.

### Persist small delta batches

Provider deltas are accumulated in memory and flushed when either threshold is reached:

- 50–100 milliseconds since the last flush; or
- 100–250 accumulated characters.

The exact defaults will be configuration values. A flush transaction locks the active attempt, verifies the next
chunk sequence, appends the visible text snapshot, and publishes one lifecycle event. This makes the user experience
continuous without creating database and notification traffic for every token.

### Streaming failure must not lose the answer

If provisional persistence or event delivery fails while the provider continues successfully, the worker may stop
publishing deltas and finish the model run through the existing outcome path. The final assistant message still
appears through `message.created`. Streaming degradation must not convert a valid completed answer into a failed run.

Provider or model execution failures continue to fail the run normally.

## Persistence model

Add an `agent_run_output_attempt` table in a new Core Alembic migration. Suggested columns:

- `id` UUID primary key, used as the attempt ID;
- `agent_run_id` UUID foreign key;
- `attempt_number` integer unique within the run;
- `status`: `ACTIVE`, `COMPLETED`, `REPLACED`, or `FAILED`;
- `text_snapshot` text containing the currently assembled visible output;
- `last_chunk_sequence` integer;
- `started_at`, `completed_at`, `replaced_at`, and `created_at` timestamps; and
- optional bounded failure metadata that never contains document text or hidden model content.

Constraints and indexes:

- unique `(agent_run_id, attempt_number)`;
- at most one `ACTIVE` attempt per agent run;
- non-negative chunk sequence;
- index `(agent_run_id, status)`; and
- delete attempts with their owning agent run.

The lifecycle event log remains the ordered replay record. The attempt row supplies a compact current snapshot for
first hydration and reconnect recovery without replaying the entire retained delta history.

Provisional text should use a shorter configurable retention period than authoritative messages. Cleanup must not
delete an active attempt. Completed and replaced snapshots may be deleted after the final message and event-retention
windows make them unnecessary.

## Event contract

Add these schema-version-1 events to the existing matter-scoped SSE stream.

### `run.output.started`

```json
{
  "schema_version": 1,
  "matter_sequence": 101,
  "conversation_id": "...",
  "run_id": "...",
  "attempt_id": "...",
  "attempt_number": 1
}
```

### `run.output.delta`

```json
{
  "schema_version": 1,
  "matter_sequence": 102,
  "conversation_id": "...",
  "run_id": "...",
  "attempt_id": "...",
  "chunk_sequence": 1,
  "delta": "Visible assistant text only"
}
```

### `run.output.completed`

```json
{
  "schema_version": 1,
  "matter_sequence": 110,
  "conversation_id": "...",
  "run_id": "...",
  "attempt_id": "...",
  "last_chunk_sequence": 9
}
```

### `run.output.replaced`

```json
{
  "schema_version": 1,
  "matter_sequence": 111,
  "conversation_id": "...",
  "run_id": "...",
  "attempt_id": "...",
  "replacement_attempt_id": "..."
}
```

Event payload limits continue to apply. `delta` is the only event property that may contain user-visible generated
text. No event contains instructions, message history, reasoning, tool-call arguments, document excerpts added by a
tool, or provider diagnostic payloads.

## Backend implementation

### 1. Pydantic AI compatibility spike

Build an isolated executable test using the same agent output union, tools, rate-limit capability, message history,
and deferred-tool-results path as production. Determine whether the safe integration point is:

- `Agent.run_stream()` plus `stream_text(delta=True)`; or
- the lower-level Pydantic AI event stream filtered strictly to visible text-part deltas.

The spike must prove:

- normal text answers stream incrementally;
- tool calls can precede the final text answer;
- `DeferredToolRequests` does not leak serialized tool data into visible output;
- output validation or provider retries do not merge two attempts;
- the final result, all messages, usage, and invocation metadata remain available; and
- cancellation closes the provider stream promptly.

Do not select an API based only on a simple text-only model fixture.

### 2. Streaming model executor

Add a chat-specific streaming model executor beside `run_model()`. It should accept an async visible-delta callback
and otherwise return the existing `ModelRunEnvelope` contract. Preserve:

- prompt assembly and pinned model selection;
- Pydantic AI capabilities and rate-limit hooks;
- usage limits;
- trace creation, completion, and failure;
- provider identity and invocation telemetry;
- complete message-history serialization; and
- current `ModelExecutionError` classification.

The callback must receive visible deltas only. Generic structured-model callers continue using `run_model()`.

### 3. Attempt coordinator

Add a coordinator used by the DBOS worker to:

- create and commit the output attempt before the first delta;
- coalesce provider deltas;
- persist idempotent, ordered batches;
- mark an earlier active attempt replaced before a retry begins;
- mark the current attempt completed or failed;
- disable further delta publication after a provisional-stream failure while allowing final model execution to
  continue; and
- publish output events atomically with attempt-state updates.

Use a new SQLAlchemy session for each flush. Do not keep one database transaction open for the duration of a provider
request.

### 4. DBOS retry behavior

Generating an attempt ID and starting an attempt must occur at a durable boundary. A replay of a completed DBOS step
must return the same recorded result rather than starting a second provider stream. A genuine new provider execution
must receive a new attempt number and replace any earlier active attempt.

The implementation must explicitly test worker termination after:

- attempt creation but before the first delta;
- a committed delta;
- provider completion but before outcome persistence; and
- final message persistence.

### 5. Outcome finalization

Extend `persist_agent_run_outcome()` so its existing transaction:

- validates the completed attempt against the final text result;
- marks the attempt completed;
- writes the final assistant message exactly once;
- publishes `message.created` and final lifecycle state; and
- leaves approval-only outcomes without a provisional assistant message.

If an approval request follows a partially emitted text attempt, replace that attempt before exposing the approval
request. Provisional explanatory text must not become conversation history unless it is also the authoritative final
assistant output.

### 6. Snapshot API

Expose the latest provisional attempt through the existing authorized run read model or a narrowly scoped read
endpoint. It must include only:

- run ID;
- attempt ID and attempt number;
- status;
- last chunk sequence; and
- current visible text snapshot.

Initial stream hydration already refreshes runs. Including the snapshot in `AgentRunRead` is preferred if the payload
remains bounded; otherwise add a conversation-scoped active-output endpoint. Authorization must remain in FastAPI.

## Frontend implementation

Extend `useAgentChatStream()` with a provisional-output store keyed by run ID and attempt ID.

Event handling:

- `run.output.started`: create or replace the provisional assistant bubble;
- `run.output.delta`: append only the next chunk sequence and ignore exact duplicates;
- `run.output.completed`: mark the bubble complete but provisional;
- `run.output.replaced`: discard the matching attempt immediately;
- `message.created`: refresh the authoritative messages and remove provisional output for that run; and
- run failure: remove or visibly fail the provisional bubble without presenting it as a saved message.

Hydration and reconnect:

- load the latest active snapshot before declaring the stream live;
- buffer lifecycle frames received during hydration using the existing race-free protocol;
- reconcile snapshot chunk sequence with replayed deltas;
- recover from an expired event cursor using the snapshot; and
- never concatenate snapshots or deltas from different attempt IDs.

Rendering:

- show provisional text in a normal assistant bubble with a subtle streaming indicator;
- preserve whitespace and Markdown behavior without reparsing the full document more often than necessary;
- keep automatic scrolling only when the user is already near the bottom;
- do not trap the user at the bottom when they scroll up;
- replace the provisional bubble without visible duplication when the durable message arrives; and
- expose an accessible status such as “Assistant is responding” without announcing every token to screen readers.

Matter Definition Chat and Batch Chat must share this behavior through the common hook rather than implementing
separate stream parsers.

## Configuration and rollout

Add independent flags:

```text
AGENT_TEXT_STREAMING_ENABLED=false
NEXT_PUBLIC_AGENT_TEXT_STREAMING_ENABLED=false
```

Suggested tuning settings:

```text
AGENT_TEXT_STREAM_FLUSH_MILLISECONDS=75
AGENT_TEXT_STREAM_FLUSH_CHARACTERS=160
AGENT_TEXT_STREAM_SNAPSHOT_RETENTION_HOURS=24
AGENT_TEXT_STREAM_MAX_SNAPSHOT_CHARACTERS=<bounded value aligned with agent output limits>
```

Lifecycle streaming remains enabled independently. When text streaming is disabled or unsupported, the user sees the
current running state followed by the complete durable assistant message.

Roll out in this order:

1. local Pydantic AI fixtures;
2. one supported development provider and model;
3. Matter Definition Chat in development;
4. Batch Chat in development;
5. provider/model compatibility matrix;
6. production canary for administrators;
7. broader enablement after retry, latency, and database-load metrics are acceptable.

Unsupported providers or models use lifecycle-only behavior rather than failing the chat turn.

## Observability

Measure:

- time to first visible delta;
- delta batches and characters per run;
- flush transaction latency and failures;
- active, completed, failed, and replaced attempts;
- duplicate and out-of-order chunks;
- provisional-to-final text mismatches;
- reconnect snapshot recovery;
- runs that fell back to final-message-only delivery;
- provider/model streaming support and failure rate; and
- event-log and provisional-snapshot storage growth.

Logs may contain IDs, counts, timings, statuses, and sequence numbers. Do not log generated text, document content,
prompts, reasoning, or tool arguments solely for streaming diagnostics.

## Testing

### Model execution

- plain text streams and returns the same final envelope as non-streaming execution;
- several provider deltas are coalesced into ordered persisted batches;
- rate limiting, usage limits, tracing, and invocation telemetry remain correct;
- tools may execute before final visible text;
- approval-required tool output does not appear as text;
- structured or hidden model events are filtered out;
- cancellation closes the model stream; and
- unsupported streaming falls back to the non-streaming execution path.

### Persistence and retry safety

- chunk sequences are monotonic and duplicate flushes are idempotent;
- concurrent flushes cannot reorder text;
- rollback writes neither the snapshot update nor lifecycle event;
- a new attempt replaces the old attempt;
- worker restart produces no duplicated prefix;
- cleanup retains active attempts; and
- final-message persistence remains exactly once.

### SSE and replay

- output events preserve matter/workflow/batch isolation;
- reconnect replays missed deltas in matter-sequence order;
- first hydration combines the latest snapshot with subsequent deltas without a gap;
- cursor expiry recovers from the snapshot; and
- browser abort closes the upstream stream while worker execution continues safely.

### Frontend

- the first delta creates one provisional assistant bubble;
- sequential chunks append once;
- duplicate chunks are ignored;
- a replacement attempt removes the old text;
- the final message replaces the provisional bubble without duplication;
- approval and failure states remove inappropriate provisional output;
- reconnect restores the active response;
- changing chat scope clears the old provisional state;
- scrolling behaves correctly during long output; and
- assistive technology receives status changes rather than token-by-token announcements.

### End to end

Run the API and DBOS worker as separate processes and test both chat surfaces with:

- a normal multi-paragraph answer;
- a tool call followed by an answer;
- an approval request and resumed run;
- a provider retry after partial output;
- worker termination during output;
- API restart during output;
- browser disconnect and reconnect;
- token rotation;
- text streaming disabled; and
- a provider/model that does not support streaming.

For every successful case, the rendered final text must exactly match the durable `AgentMessage`.

## Delivery sequence

1. Commit, migrate, and validate lifecycle streaming in a separate-process environment.
2. Complete the Pydantic AI compatibility spike and record the chosen event API.
3. Add the output-attempt migration, models, cleanup, and read schema.
4. Implement the idempotent attempt coordinator and batched delta publisher.
5. Add the chat-specific streaming model executor without changing structured model execution.
6. Integrate attempt lifecycle with DBOS retries and authoritative outcome persistence.
7. Extend the shared frontend hook and both chat renderers.
8. Add feature flags, model/provider fallback, metrics, and operational documentation.
9. Run backend, frontend, migration, and separate-process end-to-end tests.
10. Canary the feature, compare provisional text to final messages, then enable it by default only after the mismatch
    and duplicate rates remain zero.

## Acceptance criteria

- Visible assistant text begins before the provider completes a normal text response.
- Only user-visible assistant text is streamed.
- Tool arguments, hidden reasoning, and internal provider events never reach the browser.
- A retry never concatenates two attempts or leaves stale provisional text displayed.
- Reconnect and cursor expiry recover the latest active visible output.
- The final assistant message is persisted exactly once and exactly matches the final rendered response.
- Approval-required tools and resumed runs retain their current semantics.
- Usage accounting, model tracing, rate limiting, and audit behavior remain correct.
- Streaming can be disabled independently without a migration rollback.
- Unsupported providers and transient streaming failures degrade to complete-message delivery rather than failing a
  valid chat response.
- Database and notification load remain bounded by configured delta batching.
