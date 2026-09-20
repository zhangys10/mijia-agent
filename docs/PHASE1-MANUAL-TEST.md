# Phase 1 local manual test guide

Phase 1 is implemented and green locally. Use this to verify end-to-end from
Postman before any deployment. Everything below is local; activation stays
disabled (`AI_SCENE_EXECUTION_DISABLED`) by design.

## Prerequisites

- Python 3.11+ with repo deps (`mijia-agent/.venv` is already set up).
- Console dev server: in `mijia-web-console`, copy `.env.example` → `.env.local`,
  make sure these are set:
  - `XIAOMI_SESSION_SECRET`, `AI_PRINCIPAL_SECRET` (each ≥ 32 chars, distinct),
  - `AI_AUTOMATION_TOKEN_SECRET` (≥ 32 chars) — the automation token encrypts with this,
  - `AI_TOOLS_INTERNAL_SECRET` (≥ 32 chars) — must match the agent's,
  - `AI_SCENE_APPROVED_IDS` — at least one approved scene ID for your home.
- Start the console: `npm run dev` (default `http://localhost:3000`).

## 1. Generate an automation token

1. Log into the console UI with your Xiaomi QR account.
2. Open the automation-token settings UI and generate a token
   (a `v1.<keyId>.<iv>.<ct>.<tag>` string). Optionally bind a home first.
3. Copy it — this is the Postman credential.

## 2. Start the Python agent locally

```bash
cd mijia-agent
AI_PYTHON_INTERNAL_SECRET="local-dev-internal-secret-0123456789" \
AI_TOOLS_INTERNAL_SECRET="local-dev-tools-secret-01234567890" \
AI_GATEWAY_API_KEY="<your makers gateway key>" \
AI_GATEWAY_BASE_URL="https://<your makers gateway>/v1" \
MIJIA_CONSOLE_BASE_URL="http://localhost:3000" \
AI_GATEWAY_MODEL="<allowed-model>" \
AI_GATEWAY_ALLOWED_MODELS="<allowed-model>" \
AI_ENVIRONMENT=development \
AI_LLM_LOG_PATH=/tmp/llm-calls.jsonl \
.venv/bin/uvicorn mijia_agent.app:create_app --factory --port 8000
```

`AI_ENVIRONMENT=development` permits the localhost console URL and preview
behavior stays off. `AI_LLM_LOG_PATH` writes one JSONL line per model call.

## 3. Exercise `POST /ai/command` from Postman

- `GET http://localhost:8000/ai/command` → info route, no auth needed.
- `POST http://localhost:8000/ai/command` with headers:
  - `Authorization: Bearer <token from step 1>`
  - `Idempotency-Key: postman-manual-0001` (any 16–128 chars)
  - body: `{ "text": "我回家了" }`

Expected:

| Body | Expected |
|---|---|
| `{ "text": "我回家了" }` | `200`, `intent: activate_scene`, `decisionSource: llm`, `execution` present, and `message` is the model's `replyMessage` |
| `{ "text": "我还没回家" }` | `200`, `status: not_understood`, `intent: none` |
| `{ "text": "今天天气怎么样" }` (no key needed) | `200`, `status: not_understood` |
| `{ "text": "家里温度多少" }` | chat-only today; `/ai/command` surfaces scenes, not environment (read-only status rides `/internal/v1/turn` web chat) |
| Repeat request 1 with the same `Idempotency-Key` | identical replayed response |
| Same key, different body | `409 IDEMPOTENCY_CONFLICT` |
| No/expired token | `401 AUTOMATION_TOKEN_*` |
| `{ "text": "我回家了", "home": "不存在的家" }` | `404 AI_HOME_NOT_FOUND` |
| Any scene-activation body with activation enabled | `403 AI_SCENE_EXECUTION_DISABLED` (M2 gate — expected) |

Verify the LLM log after each call:

```bash
tail -1 /tmp/llm-calls.jsonl | python3 -m json.tool
```

Each line has `event: llm_call` (request/response excerpt/usage/latency) or
`llm_call_failed` — and never the token, binding, or gateway key.

## 4. Cross-check the web chat path is untouched

With the console dev server up, open the assistant panel in the browser and
send a message: it must behave exactly as before (it goes
`/api/ai/chat` → adapter → `POST /internal/v1/turn`, now with prompt parity
plus `get_home_status` read-only support).
