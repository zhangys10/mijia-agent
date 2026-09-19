# M1 deployment runbook

This runbook completes the remaining M1 read-only integration checks. It does not enable
physical scene execution or production cutover. Keep `AI_COMMAND_ENABLED=false`, keep the
console executor returning `AI_SCENE_EXECUTION_DISABLED`, and use only synthetic or approved
read-only requests.

## 1. Exit criteria

M1 is complete only when all of the following are recorded for a development environment:

- the console tool facade is deployed and revalidates principal and current-home access;
- the Makers adapter and Python ASGI function are deployed from `adapters/edgeone`;
- `/api` route stripping, internal Bearer authentication, Gateway access, Makers storage and
  cancellation are verified on the deployed runtime;
- console create/chat/list/delete works through the remote adapter with console
  `AI_QUOTA_ENABLED=false` as the temporary development contract;
- A/B principals cannot read each other's history or scene catalog;
- no Xiaomi credential, real scene ID, DID, internal secret or Authorization value appears in
  a response or captured application log;
- Preview returns the fixed mock without a model, quota reservation or device action;
- quota responses report `mode: "disabled"`; no quota enforcement or cost protection is claimed;
- no device action occurs.

Record the deployment IDs, timestamps, environment names, result of every verification row,
and rollback owner in the sign-off section. Never copy secret values into this document.

## 2. Configuration matrix

| Variable | Console | Makers adapter | Python Cloud Function |
|---|---|---|---|
| `XIAOMI_SESSION_SECRET`, `AI_PRINCIPAL_SECRET` | Set | Never | Never |
| `AI_AGENT_BASE_URL` | Leave unset until section 6 | Never | Never |
| `AI_QUOTA_ENABLED` | `false` for this development runbook | Never | Never |
| `AI_AGENT_INTERNAL_SECRET` | Sends | Verifies | Never |
| `MIJIA_CONSOLE_BASE_URL` | — | Set | Set |
| `AI_TOOLS_INTERNAL_SECRET` | Verifies | Sends | Sends |
| `AI_PYTHON_BASE_URL` | — | `https://<makers-host>/api` | — |
| `AI_PYTHON_INTERNAL_SECRET` | — | Sends | Verifies |
| `AI_GATEWAY_API_KEY`, `AI_GATEWAY_BASE_URL`, `AI_GATEWAY_MODEL` | Retire after cutover | — | Set explicitly |
| `AI_GATEWAY_ALLOWED_MODELS`, timeout/output limits | — | — | Optional policy |
| `AI_SCENE_APPROVED_IDS` | Set to the reviewed read-only catalog | — | — |
| `AI_ENVIRONMENT` | Set | Set | Set; do not rely on the production default |

Use distinct high-entropy secrets for each boundary and environment. Values stay in the
platform secret store or current terminal only. Disable request-body and Authorization-header
logging before sending a binding or user message.

Verified local state on 2026-09-18: the adapter directory is linked to Makers project
`makers-pqifa7c8qig3`. Production variable names include the Gateway credentials, all three
internal secrets, and `MIJIA_CONSOLE_BASE_URL`; `AI_PYTHON_BASE_URL` and `AI_ENVIRONMENT` were
missing. Recheck names without printing values before deployment because this state can change.

## 3. Deploy the console tool facade

1. Deploy the console branch containing `POST /api/ai/tools` to development.
2. Keep `AI_AGENT_BASE_URL` unset so the public chat path remains on its existing backend.
3. Configure `AI_TOOLS_INTERNAL_SECRET`, the session/principal secrets, and an empty or
   read-only `AI_SCENE_APPROVED_IDS` list.
4. From a trusted terminal, call `authorize` and `list_scenes` using a freshly minted internal
   binding. Do not save the request body.
5. Verify missing/wrong Bearer credentials return 401, a foreign home returns 403, and the
   catalog contains aliases and descriptions but no real IDs or Xiaomi session fields.
6. With `AI_ENVIRONMENT=preview`, submit a fully valid `activate_scene` request and verify 403
   `AI_PREVIEW_READ_ONLY`. Outside Preview it must still return 403
   `AI_SCENE_EXECUTION_DISABLED`.

## 4. Build and deploy the adapter plus Python

Run from the repository root:

```bash
npm run build --prefix adapters/edgeone
cd adapters/edgeone
edgeone makers link
edgeone makers env ls
edgeone makers deploy
```

Treat `edgeone makers env ls` output as sensitive even when checking only variable names. If a
value-oriented command is required, keep its output out of shared logs. Set variables with
`edgeone makers env set` or the console UI; never put values in this runbook or shell history.

The project root must be `adapters/edgeone` so `edgeone.json`, `agents/` and
`cloud-functions/` deploy together. EdgeOne runs `npm run build` from that directory; do not
configure `npm run build --prefix adapters/edgeone` as the project build command.

After deployment, verify:

```bash
curl -fsS "https://<makers-host>/api/healthz"
```

Expected response: `200 {"status":"ok"}`. External `/api/healthz` maps to FastAPI `/healthz`
because EdgeOne strips `/api`. Likewise, external `/api/internal/v1/turn` maps to
`/internal/v1/turn`. Verify the turn route rejects a missing or invalid
`AI_PYTHON_INTERNAL_SECRET`; do not place a real token directly in command history.

From the deployed Python function, make one bounded, non-tool Gateway request and confirm the
configured `AI_GATEWAY_MODEL` is available. A successful model check in another EdgeOne
project is not evidence for this Cloud Function. Confirm the response and logs contain no
Gateway credential or upstream headers.

## 5. Quota deferral: use disabled mode for development

Set console `AI_QUOTA_ENABLED=false` for this M1 development integration. In remote mode the
console then:

- accepts the raw quota-less Python result forwarded by the adapter;
- returns a principal-bound quota summary with `mode: "disabled"`, null limits/usage/remaining,
  `resetAt: null` and `softLimit: true`; and
- never reads/writes the console quota KV or calls adapter `POST /api/internal/quota`.

This is an explicit development exception, not a quota implementation. There are no request or
Token limits, no usage ledger, no application `AI_QUOTA_EXCEEDED`, and no protection against
shared Gateway cost. Keep smoke tests bounded and do not use this mode for production traffic.
The adapter-owned reserve/commit/release, chat summaries, and authenticated quota-summary route
remain M3 production gates. Never work around that future contract by running a second console
ledger in remote mode.

Before section 6, verify the console branch includes remote disabled-mode support and its tests.
An enabled remote mode still rejects a successful adapter result without `quota` (502) by design.

## 6. Exercise console remote mode

After section 5 is configured:

1. Set the development console's `AI_AGENT_BASE_URL` to the adapter's HTTPS origin, set
   `AI_QUOTA_ENABLED=false`, and redeploy. Non-loopback HTTP origins must be rejected.
2. Keep `AI_COMMAND_ENABLED=false` and the physical executor disabled.
3. With logged-in principal A, create a conversation, chat to list scenes, reuse the handle,
   read quota, delete the conversation, then verify the same handle starts with empty Agent
   memory. Both chat and quota API must report disabled quota with null counters.
4. With principal B, verify A's handle is rejected and A's catalog/history cannot be read.
5. Verify a request without a client idempotency key receives only read-only scope.
6. Verify the public response contains only opaque IDs, safe text, scene display metadata and
   disabled quota fields. Inspect configured redacted logs for the same boundary.
7. Verify chat produces one Agent turn only, `GET /api/ai/quota` makes no adapter request, and
   no local or remote quota ledger is read or mutated. Do not claim usage accounting.

Use browser DevTools or a terminal with an ephemeral `COOKIE` variable. Do not paste a real
`xiaomi_session`, binding or internal Bearer value into tickets, CI variables, this file, or
recorded shell history.

## 7. Validate stop propagation

Start a turn whose Gateway call remains in flight, then call the deployed `/ai-home/stop`
route with the same `Makers-Conversation-Id`. Verify the runtime abort reaches Python and the
public result maps to 499 `AI_AGENT_CANCELLED`. Confirm a second conversation continues
normally and no partial assistant message is stored as a successful turn.

This disabled-quota M1 check records no model usage in an application ledger. When M3 restores
quota, cancellation before model usage may release a reservation, known usage must be settled,
and an indeterminate post-dispatch outcome must be conservatively settled. This check does not
authorize a device tool.

## 8. Verification record

| Check | Expected | Result/evidence |
|---|---|---|
| Console `authorize` | 200 for valid binding only | |
| Console `list_scenes` | aliases only; no credentials/real IDs | |
| Console Preview chat | fixed mock; no Agent/quota call | |
| Console `activate_scene` | Preview read-only or execution disabled | |
| `/api/healthz` route stripping | 200 from FastAPI `/healthz` | |
| Turn ingress authentication | invalid Bearer rejected | |
| Gateway model from deployed Python | bounded non-tool turn succeeds | |
| Adapter storage isolation | A/B histories remain isolated | |
| Disabled quota summary | chat/quota API report `mode: disabled` with null counters | |
| Remote create/chat/list/delete | public contract passes | |
| Disabled quota isolation | no console KV or adapter quota-route/ledger access | |
| Stop during Gateway call | 499 `AI_AGENT_CANCELLED` | |
| Response/log redaction | no protected values | |

## 9. Rollback

Unset `AI_AGENT_BASE_URL` and redeploy the console to restore its same-project route. Keep only
one active writer for a command namespace; never alternate backends to retry a request with an
uncertain result. New-project conversation history is separate and is not migrated by rollback.
Disabled mode has no quota ledger to copy or reset. If the local route should also remain quota
disabled during rollback, keep `AI_QUOTA_ENABLED=false`; otherwise restore a verified local KV
configuration deliberately before setting it to `true`.

Physical activation remains blocked. If a future command times out after dispatch, inspect its
durable executor receipt before any retry; absence of a chat response is not evidence that a
physical action did not occur.

## 10. Sign-off

| Field | Value |
|---|---|
| Environment / Makers project | |
| Console deployment / commit | |
| Adapter deployment / commit | |
| Python build / commit | |
| Verification timestamp | |
| Reviewer | |
| Console `AI_QUOTA_ENABLED=false` set (quota deferred to M3) | |
| Rollback owner and command tested | |
| M1 accepted / remaining blockers | |
