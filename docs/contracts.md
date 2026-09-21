# Cross-repository contracts

## Existing public contract stays in the console

`POST /api/ai/chat`, `POST /api/ai/conversations`,
`DELETE /api/ai/conversations/:id`, and `GET /api/ai/quota` retain the Phase 0/5
Cookie authentication and body/response shapes. Browser principal overrides are
rejected. The existing console signs/validates conversation handles and owns quota
policy. Quota enforcement is deferred (M3): while console `AI_QUOTA_ENABLED=false`,
remote chat results need not carry `quota` and `GET /api/ai/quota` does not call the
adapter; the console returns a fixed principal-bound `mode: "disabled"` summary.
When quota is enabled, every successful chat result must carry a valid full summary
(`principalId`, `mode`, `limits`, `usage`, `remaining`, `resetAt`, `softLimit: true`),
and the adapter must serve the authenticated `POST /api/internal/quota` summary
route — that remains a future M3 adapter contract.

## Makers → Python

`POST /api/internal/v1/turn` externally, `Authorization: Bearer <AI_PYTHON_INTERNAL_SECRET>`.
EdgeOne strips `/api` before invoking the FastAPI route, which remains
`POST /internal/v1/turn`.
Maximum raw body: 64 KiB. Requests and nested history messages reject unknown fields.

```json
{
  "requestId": "req_example_000001",
  "conversationId": "conv_example",
  "principalId": "usr_example",
  "homeId": "example-home",
  "message": "查看可用场景",
  "idempotencyKey": "example-idempotency-0001",
  "scopes": ["ai:chat"],
  "sessionBinding": "opaque-console-issued-binding",
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai",
  "history": []
}
```

`history` is adapter-owned, at most 12 user/assistant messages, each at most 2000
characters. The body cannot contain a Xiaomi session, Gateway credentials or arbitrary
provider URL. Bindings and service credentials never enter model messages or error details.

Responses preserve `requestId`, `conversationId`, `message`, `intent`, optional
`scenes`/`tool`, and internal `usage` with prompt/completion/total tokens and an
`estimated` flag. Public projection happens in the console. Scene status is derived
from the executor. Error responses contain a stable `code` and, when already known,
model `usage`; do not drop known usage when implementing failure settlement.

## Agent services → console tools

`POST /api/ai/tools`, `Authorization: Bearer <AI_TOOLS_INTERNAL_SECRET>`.
Maximum raw body: 32 KiB. Server-to-server only, not a replacement for browser APIs.

The envelope contains `requestId`, `principalId`, `homeId`, `scopes`, `sessionBinding`,
optional `idempotencyKey`, `tool`, and `arguments`. The console verifies and decrypts
the binding, re-derives the principal from its session, and reloads current home access.

| Tool | Arguments | Current behavior |
|---|---|---|
| `authorize` | `{}` | `{ "ok": true }` after authentication/home checks; not model-visible |
| `list_scenes` | `{}` | `{ "scenes": [{ "alias", "name", "description", "actionCount" }] }` |
| `get_home_status` | `{}` | Read-only normalized environment snapshot (below); requires `ai:chat` only |
| `get_device_status` | `{}` | Read-only per-room device on/off snapshot (below); requires `ai:chat` only |
| `activate_scene` | `{ "sceneId": "scene_<opaque-alias>" }` | 403 `AI_SCENE_EXECUTION_DISABLED` until executor gate is complete |

`get_home_status` aggregates readings from any supported devices and returns a strict
sanitized object (never DIDs, raw property addresses, or Xiaomi records):

```json
{
  "capturedAt": "2026-09-20T08:00:00Z",
  "completeness": "complete | partial | empty",
  "groups": [
    {
      "metric": "temperature | humidity | co2 | formaldehyde | pm25 | pm10 | tvoc | pressure | battery",
      "label": "温度",
      "unit": "°C",
      "latest": {
        "value": 25.5,
        "unit": "°C",
        "sourceLabel": "客厅温湿度计",
        "roomName": "客厅",
        "capturedAt": "2026-09-20T08:00:00Z",
        "freshness": "fresh | stale"
      },
      "readings": [ { "..." : "same shape as latest" } ]
    }
  ],
  "warnings": ["部分设备读取失败"]
}
```

Python validates this shape strictly (`extra="forbid"`, bounded lists and strings) and
forwards it as `Result.homeStatus`. Readings are fetched only after the model selects
the tool; they never enter model messages, replies, or conversation history. The reply
text is a generic statement; the browser assistant renders the structured readings.

`get_device_status` answers "which lights/devices are on, by room" with the same
sanitization contract (never DIDs, model strings, raw property addresses, or Xiaomi
records). The console builds it from the same device sync pipeline and lighting model
as its home dashboard:

```json
{
  "capturedAt": "2026-09-21T08:00:00Z",
  "completeness": "complete | partial | empty",
  "poweredOn": 1,
  "rooms": [
    {
      "room": "客厅",
      "items": [
        { "name": "客厅吸顶灯", "kind": "light", "state": "on | off | unknown", "online": true }
      ]
    }
  ],
  "warnings": ["部分设备状态暂时不可用。"]
}
```

Devices without a readable power property (locks, sensors) report `state: "unknown"`
— never a guessed value. Python validates the shape strictly and forwards it as
`Result.deviceStatus`; states are fetched only after the model selects the tool and
never enter model messages, replies, or conversation history. The reply text is a
generic statement; the browser assistant renders the per-room device card.

The future executor must refresh the scene, validate alias/home/approval revision/risk,
claim a durable execution receipt, and return only `status` and `message`. It must not
return real scene IDs, DIDs, raw Xiaomi records or credentials. Scope permission and
an idempotency key alone do not establish that an action is safe.

The existing `/api/xiaomi/control` and `/api/xiaomi/scenes/run` are **not** generic
LLM tools. They use different browser/session assumptions and expose raw device IDs.

## Errors and cancellation

Common codes: `AI_INVALID_REQUEST` (400), `AI_UNAUTHENTICATED` (401),
`AI_SCOPE_FORBIDDEN`/`AI_HOME_FORBIDDEN`/`AI_PREVIEW_READ_ONLY` (403),
`AI_IDEMPOTENCY_CONFLICT`/`AI_REQUEST_IN_PROGRESS`/`AI_EXECUTION_STATUS_UNKNOWN` (409),
`AI_GATEWAY_RATE_LIMITED` (429), Gateway/scene/agent failures (502), store unavailable
(503), and Gateway/scene timeouts (504).

All agent responses use `Cache-Control: no-store`. Never expose an upstream exception,
validation input dump, Authorization header or sealed binding. Existing console error
mapping requires the new uncertain/disabled codes to be added before cutover.

The new adapter stop route uses the platform conversation header, authenticated
principal/home/binding envelope, and `conversation_id` equal to the current platform
conversation. Cancellation aborts the HTTP call; it cannot undo an already dispatched
device action. No successful physical cancellation is implied.

## Postman/Siri → Python (`POST /ai/command`, Phase 1)

New direct ingress replacing the console's `/api/ai/command` for external clients.
`Authorization: Bearer <console-issued automation token>` (`v1.…`, ≤ 8192 chars).
The token is opaque to Python: the console decrypts it in `/api/ai/tools`, re-derives
the principal, and resolves the home. Python never opens it, logs it, or uses its BYOK
provider fields. Optional `Idempotency-Key` header (16–128 chars) becomes mandatory
once an action is selected; replay returns the completed response, same-key/different-body
conflicts return 409, concurrent duplicates return 202 processing (process-local store,
same soft boundary the console had — not durable).

```json
{ "text": "我回家了", "home": "我的家", "locale": "zh-CN", "timezone": "Asia/Shanghai",
  "conversationId": "conv_example", "history": [
    { "role": "user", "content": "…" },
    { "role": "assistant", "content": "…" }
  ] }
```

`home` accepts a home ID, exact name, or substring; omitted means the token-bound home,
then the account's first home. `history` is at most 32 messages; each is trimmed to 300
chars. Success responses mirror the console `AiCommandResponse`:

```json
{ "requestId": "req_…", "conversationId": "conv_…", "conversationReset": false,
  "turnIndex": 1, "status": "completed", "intent": "activate_scene",
  "sceneId": "scene_<opaque-alias>", "sceneName": "回家模式",
  "message": "好的，已开启回家模式", "execution": { "status": "success", "succeeded": 1,
  "failed": 0 }, "decisionSource": "llm", "llmOutput": "…" }
```

Executor status always wins over model text. Public error codes: `LLM_TIMEOUT` (504),
`LLM_PROVIDER_ERROR` (502), `MI_CLOUD_ERROR` (502), `DEVICE_TIMEOUT` (504),
`AUTOMATION_TOKEN_EXPIRED`/`AUTOMATION_TOKEN_INVALID` (401), `AI_HOME_NOT_FOUND` (404),
`IDEMPOTENCY_CONFLICT` (409), `INVALID_REQUEST` (400), `UNAUTHORIZED` (401), and
`AI_SCENE_EXECUTION_DISABLED` (403) — activation remains closed until the durable
executor claim (M2). `GET /ai/command` returns an info summary. Every model call is
logged as JSONL (`AI_LLM_LOG_PATH`, stdout by default): request payload, bounded
response excerpt, usage, latency, `llm_call_failed` on error — no tokens, bindings,
gateway keys, or principal IDs ever appear.

The console `/api/ai/tools` accepts the token via the new `X-Ai-User-Token` header
after the service bearer; the `sessionBinding` envelope path is unchanged. Body uses
`home` (name or ID) on the token path. The Python tool list for both pipelines matches
the console contract: `list_scenes`, `get_home_status` and `get_device_status`
(read-only), `activate_scene` (disabled). After the console's phase-3 retirement, its legacy `/api/ai/command` route
returns `410 AI_COMMAND_RETIRED` and this repo's `POST /ai/command` is the only command
ingress; whether the console grows a thin Siri pass-through is a cutover decision.
