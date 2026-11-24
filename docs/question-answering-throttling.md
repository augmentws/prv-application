# Question-answering throttling

The current throttling controls describe the deployed execution model. The broader work for capability-aware window
sizing, replay measurement, and operator ETA is documented in the
[Question-Answering Batch Performance Improvement Plan](question-answering-performance-plan.md).

Question-answering (Q&A) runs evaluate each document independently. A 1,000-document review batch therefore
creates at least approximately 1,000 provider requests; the review batch is an orchestration unit, not a provider
Batch API request. Inline evidence normally avoids a second request for supported documents, while long JEV documents
still use one request per planned source window.

## Controls

Q&A uses two orchestration limits and a separate provider policy:

| Setting | Default | Scope | Purpose |
| --- | ---: | --- | --- |
| `QA_BATCH_CONCURRENCY` | `2` | DBOS-global per queue | Limits active Q&A coordinators separately in the JEV, OpenAI, and legacy fallback queues. Review Programs also use this value for each profile's child-launch window. |
| `QA_DOCUMENT_CONCURRENCY` | `12` | Configuration guard / legacy DBOS queue | Caps the sum of configured provider-specific capacities and remains the fallback queue capacity. |
| `QA_JEV_DOCUMENT_CONCURRENCY` | `8` | DBOS-global | Limits JEV document workflows without occupying OpenAI slots. |
| `QA_OPENAI_DOCUMENT_CONCURRENCY` | `4` | DBOS-global | Limits OpenAI Decisions document workflows without waiting behind JEV work. |
| `QA_INLINE_EVIDENCE_ENABLED` | `true` | Q&A document | Combines classification and typed evidence companions in one provider request when the contract is supported. |
| `QA_INLINE_EVIDENCE_MAX_STATIC_CHARACTERS` | `12000` | Non-windowed providers | Falls back to conditional evidence localization when inline evidence questions would add unsafe static request overhead. JEV filters paragraph choices per source window. |
| `TYPESAFE_CONCURRENCY` | `4` | Worker process | Limits simultaneous JEV requests in one worker process. |
| `TYPESAFE_REQUESTS_PER_MINUTE` | `15` | Worker process and model | Sliding-window JEV request limit. |
| `TYPESAFE_INPUT_TOKENS_PER_MINUTE` | `200000` | Worker process and model | Sliding-window estimated JEV input-token limit. |
| `TYPESAFE_REQUEST_TARGET_CHARACTERS` | `60000` | JEV request | Document-window requests are planned below this approximate serialized request size. When both text and paragraph representations are present, they are windowed together and share this budget. A provider `max_tokens_exceeded` response triggers progressively smaller document-only windows. |
| `TYPESAFE_REQUEST_TARGET_TOKENS` | `8000` | JEV request | Token-aware ceiling applied in addition to the character target. The worker uses the model tokenizer when known and a documented fallback encoding otherwise, so OCR text, control-heavy text, and other token-dense documents are partitioned before the provider rejects them. |
| `TYPESAFE_MAX_DOCUMENT_WINDOWS` | `128` | Document | Safety limit on the total number of JEV document windows, including provider-triggered refinement. |
| `OPENAI_DECISIONS_CONCURRENCY` | `4` | Worker process | Limits simultaneous OpenAI Decisions requests in one worker process. |
| `OPENAI_DECISIONS_REQUESTS_PER_MINUTE` | `60` | Worker process and model | Sliding-window OpenAI Decisions request limit. |
| `OPENAI_DECISIONS_INPUT_TOKENS_PER_MINUTE` | `500000` | Worker process and model | Sliding-window estimated OpenAI Decisions input-token limit. |

The Q&A document queue no longer derives its capacity from Matter Definition assessment concurrency or JEV
concurrency. Matter Definition assessment settings control only assessment workflows.

## Multiple workers

The Q&A batch and provider-specific document settings configure DBOS queues with `global_concurrency`. Workers using
the same DBOS system database, application, and queue names share those limits. Starting ten workers does not multiply
the configured Q&A batch or document concurrency by ten.

The decision-engine concurrency gates and RPM/TPM limiters are in process memory. Ten worker processes can
therefore provide up to ten independent provider limiters. Aggregate provider request rates can approach ten times
the configured per-process RPM/TPM limits when work is distributed across all workers, although the DBOS-global
sum of `QA_JEV_DOCUMENT_CONCURRENCY` and `QA_OPENAI_DOCUMENT_CONCURRENCY` still caps simultaneous known-provider
Q&A document workflows.

Production deployments must account for this distinction:

- Set the DBOS-global limits for total application capacity.
- Divide an account-wide provider quota among the maximum worker count when setting per-process RPM/TPM values.
- Use identical Q&A queue settings on every API and workflow-worker process.
- If strict account-wide provider quotas are required, use a distributed provider rate limiter. The current in-memory
  provider limiter is not an account-wide enforcement mechanism.

For example, with four workers and an account-wide JEV quota of 120 requests per minute, configure
`TYPESAFE_REQUESTS_PER_MINUTE=30` on each worker, subject to the provider's actual quota and the desired safety
margin.

## Effective concurrency

The effective number of provider calls is bounded by every applicable layer. For OpenAI Decisions, it is no more
than `QA_OPENAI_DOCUMENT_CONCURRENCY` and `OPENAI_DECISIONS_CONCURRENCY` in the worker that
claimed the document. RPM and estimated-TPM limits may reduce throughput further. JEV follows the corresponding
TypeSafe limits.

Provider concurrency is a ceiling, not a target. Request latency, retry backoff, token limits, document preparation,
and database work can all produce lower throughput.

## Applying changes

These settings are read when the API and workflow worker import their queue and decision-engine modules. Restart
the affected processes after changing them. Do not change queue concurrency on only some workers; deploy one
consistent configuration across the DBOS application.

Docker Compose passes all Q&A orchestration and provider throttle settings through from `.env`. Local processes
started with Pipenv read the same names directly from `.env`.
