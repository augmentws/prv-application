# Question-answering throttling

Question-answering (Q&A) runs evaluate each document independently. A 1,000-document review batch therefore
creates approximately 1,000 provider requests; the review batch is an orchestration unit, not a provider Batch API
request.

## Controls

Q&A uses two orchestration limits and a separate provider policy:

| Setting | Default | Scope | Purpose |
| --- | ---: | --- | --- |
| `QA_BATCH_CONCURRENCY` | `2` | DBOS-global | Limits active Q&A Batch Set coordinators and active child review-batch workflows. Review Programs also use this value for each profile's child-launch window. |
| `QA_DOCUMENT_CONCURRENCY` | `12` | DBOS-global | Limits document evaluation workflows across every active Q&A batch and provider. |
| `TYPESAFE_CONCURRENCY` | `4` | Worker process | Limits simultaneous JEV requests in one worker process. |
| `TYPESAFE_REQUESTS_PER_MINUTE` | `15` | Worker process and model | Sliding-window JEV request limit. |
| `TYPESAFE_INPUT_TOKENS_PER_MINUTE` | `200000` | Worker process and model | Sliding-window estimated JEV input-token limit. |
| `OPENAI_DECISIONS_CONCURRENCY` | `4` | Worker process | Limits simultaneous OpenAI Decisions requests in one worker process. |
| `OPENAI_DECISIONS_REQUESTS_PER_MINUTE` | `60` | Worker process and model | Sliding-window OpenAI Decisions request limit. |
| `OPENAI_DECISIONS_INPUT_TOKENS_PER_MINUTE` | `500000` | Worker process and model | Sliding-window estimated OpenAI Decisions input-token limit. |

The Q&A document queue no longer derives its capacity from Matter Definition assessment concurrency or JEV
concurrency. Matter Definition assessment settings control only assessment workflows.

## Multiple workers

`QA_BATCH_CONCURRENCY` and `QA_DOCUMENT_CONCURRENCY` configure DBOS queues with `global_concurrency`. Workers
using the same DBOS system database, application, and queue names share those limits. Starting ten workers does
not multiply the configured Q&A batch or document concurrency by ten.

The decision-engine concurrency gates and RPM/TPM limiters are in process memory. Ten worker processes can
therefore provide up to ten independent provider limiters. Aggregate provider request rates can approach ten times
the configured per-process RPM/TPM limits when work is distributed across all workers, although the DBOS-global
`QA_DOCUMENT_CONCURRENCY` limit still caps simultaneous Q&A document workflows.

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
than the available share of `QA_DOCUMENT_CONCURRENCY` and `OPENAI_DECISIONS_CONCURRENCY` in the worker that
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
