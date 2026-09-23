# General assistant Phase 0

This repository now contains the architecture spike described in
`mijia-general-assistant-design.md`. It is deliberately separate from the deprecated
`mijia_agent` scene-router decisions.

## Implemented

- Provider-neutral messages, tool calls, usage, capability results, tool events, and canonical
  response outcomes under `src/mijia_assistant`.
- A per-request capability registry and a maximum-four-iteration engine with limits of eight
  reads, four reads per tool, one exclusive physical write, terminal write results, request
  deadlines, strict schemas, and bounded tool output.
- An OpenAI-compatible Makers adapter for repeated calls plus a streaming-delta normalizer that
  rejects malformed ordering and incomplete streams.
- A deterministic fake `get_weather` capability and local smoke matrix proving direct answers,
  clarification, a two-step weather loop, malformed-argument rejection, deadline handling, and
  rejection of unavailable physical writes.
- A canonical `POST /ai/assistant` automation-token ingress with no-store responses. The token
  stays in trusted server context and is forwarded only if the model selects a home read.
- Read-only console adapters for `get_home_environment` and `get_device_status`. Physical action
  capabilities are not registered.
- `mijia-agent-local-prod run --profile live-read` calls the canonical endpoint and renders
  outcome, answer, tool events, and normalized usage. It does not maintain legacy client-side
  history or fall back to `/ai/command`.
- The legacy router is disabled by default when configuration is loaded from the environment.
  `AI_LEGACY_ROUTER_ENABLED=true` is an explicit rollback flag and each request emits a
  `legacy_router_traffic` event.
- The EdgeOne build copies both Python packages into the Cloud Function.
- The canonical assistant uses `AI_ASSISTANT_MAX_OUTPUT_TOKENS` (default `512`) separately from
  the deprecated router's `AI_GATEWAY_MAX_OUTPUT_TOKENS` default of `256`.

## Local acceptance

```bash
.venv/bin/python -m mijia_agent.local_prod smoke --profile fake
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
npm test --prefix adapters/edgeone
```

`check` remains network-free and secret-redacted. `run --profile live-read` retains the existing
production acknowledgement, isolated child environment, owner-only token/cookie handling,
redirect refusal, visible request identifiers, and no automatic retry behavior.

## Runtime constraints and external validation

Phase 0 does not enable a write capability, so it cannot dispatch a Xiaomi action. The canonical
endpoint currently has process-local conversational context only; Makers `context.store` and the
two-projection `ConversationRepository` remain Phase 1 work.

The following checks require deployment credentials or infrastructure and are not represented as
local proof:

- live-read acceptance against the selected production `AI_GATEWAY_MODEL` and real exposed home
  reads;
- EdgeOne routing and cross-instance Makers storage behavior;
- multi-worker EdgeOne Blob `onlyIfNew` conflict behavior and strong reads;
- physical action claims or execution.

The Blob checks remain a prerequisite for registering `activate_scene`; neither the current
in-process idempotency store nor EdgeOne KV is accepted as a durable write claim.
