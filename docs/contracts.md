# Cross-repository contracts

## Existing public contract stays in the console

`POST /api/ai/chat`, `POST /api/ai/conversations`,
`DELETE /api/ai/conversations/:id`, and `GET /api/ai/quota` retain the Phase 0/5
Cookie authentication and body/response shapes. Browser principal overrides are
rejected. The existing console signs/validates conversation handles and owns quotas.

## Makers → Python

`POST /internal/v1/turn`, `Authorization: Bearer <AI_PYTHON_INTERNAL_SECRET>`.
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
| `activate_scene` | `{ "sceneId": "scene_<opaque-alias>" }` | 403 `AI_SCENE_EXECUTION_DISABLED` until executor gate is complete |

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
