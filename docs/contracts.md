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

The canonical endpoint is `POST /api/internal/v1/assistant` externally,
with `Authorization: Bearer <AI_PYTHON_INTERNAL_SECRET>`.
EdgeOne strips `/api` before invoking the FastAPI route, which remains
`POST /internal/v1/assistant`. The older `/internal/v1/turn` binding contract is
legacy-only and must not receive new assistant capabilities.
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
  "automationToken": "opaque-console-issued-automation-token",
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai",
  "history": []
}
```

`history` is adapter-owned, at most 12 user/assistant messages, each at most 2000
characters. The body cannot contain a Xiaomi session, Gateway credentials or arbitrary
provider URL. Automation tokens and service credentials never enter model messages or error details.

Responses preserve `requestId`, `conversationId`, `message`, `intent`, optional
`scenes`/`tool`, and internal `usage` with prompt/completion/total tokens and an
`estimated` flag. Public projection happens in the console. Scene status is derived
from the executor. Error responses contain a stable `code` and, when already known,
model `usage`; do not drop known usage when implementing failure settlement.

## Agent services → console tools

`POST /api/ai/tools`, `Authorization: Bearer <AI_TOOLS_INTERNAL_SECRET>`.
Maximum raw body: 32 KiB. Server-to-server only, not a replacement for browser APIs.

The canonical assistant uses a single automation-token envelope. The adapter sends the
opaque token in `X-Ai-User-Token` after the service bearer; its `authorize` request body
contains only `requestId`, `tool`, and `arguments`. The console decrypts the token,
re-derives the principal, resolves the bound home, and returns the trusted
`principalId`, `homeId`, and read-only `scopes` so the adapter can compare them with its
server-created turn before loading conversation state. Python forwards the same opaque
token only after a home capability is selected.

| Tool | Arguments | Current behavior |
|---|---|---|
| `authorize` | `{}` | `{ "ok": true, "principalId", "homeId", "scopes": ["ai:chat"] }` after fresh token authentication/home checks; adapter-only, not model-visible |
| `list_scenes` | `{}` | `{ "scenes": [{ "alias", "name", "description", "actionCount", "revision", "risk", "actionSummaries" }] }` |
| `get_home_status` | `{}` | Read-only normalized environment snapshot (below); requires `ai:chat` only |
| `get_device_status` | `{}` | Read-only per-room device on/off snapshot (below); requires `ai:chat` only |
| `activate_scene` | `{ "sceneId": "scene_<opaque-alias>", "revision": "rev_<sha256-prefix>" }` | Rejected by the canonical automation-token ingress until it receives a per-request console-issued action scope. The deprecated command router also rejects before dispatch. Do not enable physical writes until all operational gates pass. |

Scene discovery returns a content revision hash and normalized action summaries. Only
the deprecated command router currently receives the old activation schema, and it
rejects every write before dispatch. The canonical assistant does not register a scene
action capability. The automation-token tools ingress rejects direct activation without
a per-request console-issued action scope. Scene aliases, action summaries and revision
hashes are not authorization. Keep `AI_SCENE_EXECUTION_ENABLED` unset until the deployed
operational gates in `docs/TODO.md` pass and action registration moves to the canonical
assistant.
When enabled, a positive Xiaomi scene-run acknowledgment is reported as “request
submitted”; it is not a device-state readback and must not be rendered as confirmed
physical completion. A lost response or missing receipt is `AI_EXECUTION_STATUS_UNKNOWN`
and must not be retried automatically.

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
the tool. The model then receives a bounded projection of the exposure-filtered readings
alongside the original question and generates the final answer. The same typed snapshot
is returned as structured client data for the browser.

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
`Result.deviceStatus`; states are fetched only after the model selects the tool. The
model receives a bounded, sanitized projection of the exposed per-room device states
with the original question and generates the final answer. The same typed snapshot is
returned as structured client data for the browser.

The console executor refreshes the scene, validates alias/home/approval revision/risk,
claims a durable execution receipt, and returns only `status` and `message`. It does not
return real scene IDs, DIDs, raw Xiaomi records or credentials. Scope permission and an
idempotency key alone do not establish that an action is safe. The deployment execution
flag remains off until the operational gates in `docs/TODO.md` pass.

The existing `/api/xiaomi/control` and `/api/xiaomi/scenes/run` are **not** generic
LLM tools. They use different browser/session assumptions and expose raw device IDs.

### Phase 2 versioned home observation API

The canonical assistant uses the versioned automation-token endpoints for home reads:

- `POST /api/internal/assistant/v1/capabilities`
- `POST /api/internal/assistant/v1/tools:invoke`

Both require the console service Bearer plus `X-Ai-User-Token`. The console opens the
audience-bound `mijia-agent` token, re-derives principal/home context, and reads the
home-wide exposure record. The manifest endpoint remains available for discovery, but a
selected home-read tool uses a single `tools:invoke` request. Python bounds arguments
with its own versioned tool schema; the console rechecks current membership and exposure
and validates every requested filter before collecting data. Python never accepts remote
model schemas or descriptions.

The manifest reports `contextVersion: "1"`, an opaque `exposureRevision`, exposed room
names, exposed measurement types, exposed device kinds, and read capability availability.
Missing exposure records mean disabled with no rooms, metrics, devices, or capabilities.
Only after a home tool is selected does the agent invoke the console. Tool filters can
reduce disclosure, and every requested room, metric, kind, and state must remain within
the current exposure projection. The console fetches the current device inventory once
per invocation and reuses it for the selected collector. For `get_home_environment`, the
collector gathers all currently exposed environmental metrics in one pass, then the
console filters the sanitized snapshot by the requested rooms and metrics. Live values
still require the MIoT property batch reads performed within that collector.

`get_home_environment` accepts optional `rooms` and `metrics`; `get_device_status` accepts
optional `rooms`, `kinds`, and `states`. Results remain typed and sanitized. After the
tool returns, the model receives the original user question and a bounded projection of
the exposure-filtered measurements or states to generate its final answer. Exact repeated
home-read calls within one turn reuse the first result. The final answer and typed
snapshot are returned to the caller; Makers stores a generic summary in model history
for home-read turns, so later turns do not automatically receive past measurements.
Scene discovery and all writes remain outside this Phase 2 contract.

## Errors and cancellation

Common codes: `AI_INVALID_REQUEST` (400), `AI_UNAUTHENTICATED` (401),
`AI_SCOPE_FORBIDDEN`/`AI_HOME_FORBIDDEN`/`AI_PREVIEW_READ_ONLY` (403),
`AI_IDEMPOTENCY_CONFLICT`/`AI_REQUEST_IN_PROGRESS`/`AI_EXECUTION_STATUS_UNKNOWN`/`AI_SCENE_REVISION_CHANGED` (409), `AI_ACTION_LEDGER_UNAVAILABLE`/`AI_EXPOSURE_STORE_UNAVAILABLE` (503),
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
`AI_SCENE_EXECUTION_DISABLED` (403) — activation remains closed until the deployed
executor gates in `docs/TODO.md` pass. `GET /ai/command` returns an info summary. Every model call is
logged as JSONL (`AI_LLM_LOG_PATH`, stdout by default): request payload, bounded
response excerpt, usage, latency, `llm_call_failed` on error — no tokens, bindings,
gateway keys, or principal IDs ever appear.

The console `/api/ai/tools` accepts the token via the `X-Ai-User-Token` header
after the service bearer. Body uses `home` (name or ID) on direct automation calls;
the web adapter uses the token-bound home. The Python tool list for the canonical pipeline
matches the console contract: `list_scenes`, `get_home_status` and `get_device_status`
(read-only), `activate_scene` (disabled). After the console's phase-3 retirement, its legacy `/api/ai/command` route
returns `410 AI_COMMAND_RETIRED` and this repo's `POST /ai/command` is the only command
ingress; whether the console grows a thin Siri pass-through is a cutover decision.
