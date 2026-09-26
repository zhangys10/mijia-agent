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
`POST /internal/v1/assistant`.
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
Successful canonical assistant responses also include `historyAnswer`: a bounded,
redacted assistant message produced by the Python conversation layer. The Makers
adapter stores the original user turn and this projection as model history. It must
not substitute the display `message` or infer redaction from a tool name.
For each model call, Python presents that history as bounded reference data within
the current user turn; only the latest user text is an active request for tools.

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
| `list_scenes` | `{}` | `{ "scenes": [{ "alias", "name", "description", "actionCount", "revision", "actionSummaries" }] }` |
| `get_home_status` | `{}` | Read-only normalized environment snapshot (below); requires `ai:chat` only |
| `get_device_status` | `{}` | Read-only per-room device on/off snapshot (below); requires `ai:chat` only |
| `activate_scene` | `{ "sceneId": "scene_<opaque-alias>", "revision": "rev_<sha256-prefix>" }` | Not registered by the canonical assistant. Do not enable physical writes until all operational gates pass. |

Scene discovery returns a content revision hash and normalized action summaries. The
console exposes enabled manual scenes through individual approval or a confirmed
home-level approval bypass; no static low-risk scene classification is applied. Bypass
also covers future enabled scenes and scene edits in that home. The deprecated command
router rejects every write before dispatch. The canonical assistant does not register a
scene action capability. The automation-token tools ingress rejects direct activation
without a per-request console-issued action scope. Scene aliases, action summaries and
revision hashes are not authorization. Keep `AI_SCENE_EXECUTION_ENABLED` unset until the deployed
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
home-wide exposure record. For a home question, the model first selects the local
`discover_home_exposure` tool. Python fetches the manifest, then offers only home-read
tools constrained by that manifest. A selected read uses one `tools:invoke` request.
Python bounds arguments with its own versioned tool schema; the console rechecks current
membership and exposure and validates every requested filter before collecting data.
Python never accepts remote model schemas or descriptions.

The manifest reports `contextVersion: "1"`, an opaque `exposureRevision`, exposed room
names, exposed measurement types, exposed device kinds, per-room measurement and device-kind
lists, and read capability availability.
Missing exposure records mean disabled with no rooms, metrics, devices, or capabilities.
After the discovery call, the agent invokes the console for a home read only when the model
selects a listed capability. Tool filters can
reduce disclosure, and every requested room, metric, kind, and state must remain within
the current exposure projection. The console fetches the current device inventory once
per invocation and reuses it for the selected collector. For `get_home_environment`, the
console intersects requested filters with current exposure before collecting. The
collector batches MIoT property reads for those selected room/metric pairs, then the
console filters the sanitized snapshot again before returning it.

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

The retired direct `/ai/command` ingress and its command-router contract have been removed.
Assistant requests must use the authenticated console → Makers → Python flow documented
above. Siri/automation entrypoints must go through the console's authenticated public
contract; Python is not a public endpoint.
