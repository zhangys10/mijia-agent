# Deployment and rollback

## Configuration ownership

| Variable | Console | Makers adapter | Python |
|---|---|---|---|
| `XIAOMI_SESSION_SECRET`, `AI_PRINCIPAL_SECRET` | Yes | Never | Never |
| `AI_QUOTA_*`, KV binding | Yes (`AI_QUOTA_ENABLED=false` defers enforcement in remote mode too) | No | No |
| `AI_AGENT_BASE_URL` | New adapter's HTTPS origin | No | No |
| `AI_AGENT_INTERNAL_SECRET` | Sends | Verifies | No |
| `MIJIA_CONSOLE_BASE_URL` | No | Uses | Uses |
| `AI_TOOLS_INTERNAL_SECRET` | Verifies | Sends | Sends |
| `AI_PYTHON_BASE_URL` | No | Uses (`https://<makers-host>/api`) | No |
| `AI_PYTHON_INTERNAL_SECRET` | No | Sends | Verifies |
| `AI_GATEWAY_API_KEY`, `AI_GATEWAY_BASE_URL`, `AI_GATEWAY_MODEL` | Retire after cutover | Not needed | Yes |
| `AI_GATEWAY_ALLOWED_MODELS` | — | — | Optional; defaults to configured model only |
| `AI_GATEWAY_TIMEOUT_MS` / `AI_GATEWAY_MAX_OUTPUT_TOKENS` | — | — | Defaults 5000 / 256 |
| `AI_ENVIRONMENT` | Set preview checks | development/preview/production | Required policy; default production |

Use distinct secrets across service boundaries and environments. Python validates the
Python/tool secrets are at least 32 characters and distinct. Configure the same separation
for the console→Makers secret operationally. Use high-entropy generated values; do not
commit them. Gateway credentials are injected explicitly into the Python Cloud Function;
Makers Agent environment variables do not automatically become Cloud Function variables.

Python service URLs require HTTPS; development permits HTTP only on localhost/127.0.0.1.
Clients do not follow redirects carrying Authorization. Disable body/header logging in
proxies and tracing; access logs should not capture bindings or user text.

## Sequence

For a step-by-step development deployment with per-step verification and rollback records, see
the [M1 deployment runbook](./m1-deployment-runbook.md).

1. Review/apply `integration/mijia-web-console.patch` against pinned PR #31 head in a clean
   branch, then apply `integration/mijia-web-console-usage-settlement.patch`; or use the updated
   companion PR. Keep its activation gate closed.
2. Run `npm run build --prefix adapters/edgeone` to sync `src/mijia_agent` into
   `adapters/edgeone/cloud-functions/api/mijia_agent`.
3. Set the new Makers project root to `adapters/edgeone`; link the correct development
   project and deploy the Agents plus Cloud Functions configuration. The root must keep
   `edgeone.json`, `agents/`, and `cloud-functions/` together. Configure Python's
   environment variables on `cloud-functions/api`, including Gateway credentials.
4. Verify Agents capability, routing, `context.store`, cancellation, `/api/healthz`,
   and `/api/internal/v1/turn` ingress controls.
5. Test console→adapter→Python→Gateway and Python→console discovery with fake/low-risk data.
6. Set console `AI_AGENT_BASE_URL` to the new Makers origin. Keep the console's legacy
   `/api/ai/command` route retired (phase 3: it answers `410 AI_COMMAND_RETIRED`) and old
   BYOK UI closed. Public Web Chat paths stay unchanged. For the quota-deferred
   development cutover, also set console `AI_QUOTA_ENABLED=false`: the console synthesizes
   disabled quota summaries and requires no adapter quota surface. Do not claim cost
   protection in this mode.
7. Complete the durable-execution and quota workstreams in
   [steward-report-alignment.md](./steward-report-alignment.md) before any production cutover.

## EdgeOne Cloud Functions

The Python service is deployed through `cloud-functions/api/index.py` as an ASGI
FastAPI application. External routes are `/api/healthz` and `/api/internal/v1/turn`;
EdgeOne strips `/api` before dispatch, so the Python routes stay unchanged. Set the
adapter's `AI_PYTHON_BASE_URL` to `https://<makers-host>/api`.

`cloud-functions/requirements.txt` pins the direct runtime dependencies
`fastapi==0.141.1` and `httpx==0.28.1`. The generated
`cloud-functions/api/mijia_agent` mirror is ignored by Git and must be rebuilt after
Python source changes. The included Dockerfile remains a local standalone/fallback
host; it is not the EdgeOne deployment path.

Use `npm run build` as EdgeOne's custom build command. EdgeOne detects
`adapters/edgeone/package.json` and executes the command from that directory; adding
`--prefix adapters/edgeone` there doubles the path. The generated Python builder output
is expected at `.edgeone/cloud-functions/api-python/api/`, and the runtime imports the
auxiliary package as `api.mijia_agent`.

The project's prior contract used `agents.framework=openai-agents-sdk`, `dir=agents`,
`timeout=60`, file routes and the `Makers-Conversation-Id` header. The standalone adapter
config carries that baseline forward. The EdgeOne Cloud Functions file routing, ASGI
entry, dependency merging, and build exclusions follow Tencent's current documentation.

## Rollback

Unset `AI_AGENT_BASE_URL` to restore the console's same-project Agent route. Keep the
old implementation until parity/cutover is complete. Do not serve both writers for the
same command namespace. If a command is uncertain, inspect its executor receipt before
retrying on either backend. Quota remains a console-owned policy: disabled mode has no
ledger to reset; restoring enforcement requires a verified local KV configuration.

New Makers project histories are separate; no automatic cross-project memory migration
is promised. Principal rotation changes user IDs and requires an explicit quota/history plan.
