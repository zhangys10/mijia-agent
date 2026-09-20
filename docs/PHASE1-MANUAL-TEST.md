# Phase 1 local manual test guide

Phase 1 is implemented and green locally. Use this to verify end-to-end from
Postman before any deployment. Everything below is local; activation stays
disabled (`AI_SCENE_EXECUTION_DISABLED`) by design.

## Prerequisites

- Python 3.11+ with repo deps (`mijia-agent/.venv` is already set up).
- Prod credentials via the EdgeOne CLI: `edgeone makers env pull` (or
  `edgeone makers dev`, which syncs them) writes the prod env — including
  `AI_GATEWAY_API_KEY/BASE_URL/MODEL/ALLOWED_MODELS`, `AI_TOOLS_INTERNAL_SECRET`,
  and `MIJIA_CONSOLE_BASE_URL` — into `adapters/edgeone/.env`. The
  gateway/model fields in the console automation-token UI are legacy BYOK;
  the agent ignores them, so no per-user key is needed.
- An automation token (`v1.…`) issued by the **prod** console settings UI
  (设置 → AI 自动化配置) — the token must be signed with the same
  `AI_AUTOMATION_TOKEN_SECRET` the prod console uses to decrypt it.

## 1. Generate an automation token

Either via the prod console UI (设置 → AI 自动化配置 → 自动化令牌；the BYOK
gateway/model fields in that form are legacy and ignored by the agent), or
offline with the console repo's script:

```bash
cd mijia-web-console
AI_AUTOMATION_TOKEN_SECRET="<prod secret>" \
XIAOMI_SESSION_SECRET="<console secret>" \
node --experimental-strip-types scripts/generate-automation-token.ts \
  --session '<xiaomi_session cookie value from the logged-in browser>' \
  --days 7 --out /tmp/automation-token.txt
# Postman header: Authorization: Bearer $(cat /tmp/automation-token.txt)
```

`--session` is the sealed `xiaomi_session` cookie value (DevTools →
Application → Cookies). The script uses the console's own token library, so
the output is identical to a UI-issued token; BYOK fields are placeholders.

## 2. Start the Python agent locally against the prod gateway + console

```bash
cd mijia-agent
set -a; source adapters/edgeone/.env; set +a
AI_ENVIRONMENT=production \
AI_LLM_LOG_PATH=/tmp/llm-calls.jsonl \
.venv/bin/uvicorn mijia_agent.app:create_app --factory --port 8000
```

Source the whole file: `Settings` validates `AI_PYTHON_INTERNAL_SECRET` and
`AI_TOOLS_INTERNAL_SECRET` at startup (each ≥ 32 chars, mutually distinct)
even if you only test `/ai/command` — a missing internal secret fails with
"Independent internal and tool secrets must each be at least 32 characters".

This uses the pulled prod values: model access goes through the real Makers
Gateway, and `/api/ai/tools` calls hit the prod console (which decrypts your
prod-issued token), so no local console server is needed. `AI_LLM_LOG_PATH`
writes one JSONL line per model call.

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

Open the deployed console's assistant panel in the browser and send a message:
it must behave exactly as before (it goes `/api/ai/chat` → prod adapter →
`POST /internal/v1/turn`, now with prompt parity plus `get_home_status`
read-only support). The prod adapter only gains these behaviors after the
Phase 1 branch is deployed — until then it reflects the current prod build.
