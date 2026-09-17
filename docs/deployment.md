# Deployment and rollback

## Configuration ownership

| Variable | Console | Makers adapter | Python |
|---|---|---|---|
| `XIAOMI_SESSION_SECRET`, `AI_PRINCIPAL_SECRET` | Yes | Never | Never |
| `AI_QUOTA_*`, KV binding | Yes | No | No |
| `AI_AGENT_BASE_URL` | New adapter's HTTPS origin | No | No |
| `AI_AGENT_INTERNAL_SECRET` | Sends | Verifies | No |
| `MIJIA_CONSOLE_BASE_URL` | No | Uses | Uses |
| `AI_TOOLS_INTERNAL_SECRET` | Verifies | Sends | Sends |
| `AI_PYTHON_BASE_URL` | No | Uses | No |
| `AI_PYTHON_INTERNAL_SECRET` | No | Sends | Verifies |
| `AI_GATEWAY_API_KEY`, `AI_GATEWAY_BASE_URL`, `AI_GATEWAY_MODEL` | Retire after cutover | Not needed | Yes |
| `AI_GATEWAY_ALLOWED_MODELS` | — | — | Optional; defaults to configured model only |
| `AI_GATEWAY_TIMEOUT_MS` / `AI_GATEWAY_MAX_OUTPUT_TOKENS` | — | — | Defaults 5000 / 256 |
| `AI_SCENE_APPROVED_IDS` | Yes | No | No |
| `AI_ENVIRONMENT` | Set preview checks | development/preview/production | Required policy; default production |

Use distinct secrets across service boundaries and environments. Python validates the
Python/tool secrets are at least 32 characters and distinct. Configure the same separation
for the console→Makers secret operationally. Use high-entropy generated values; do not
commit them. Gateway credentials are injected explicitly into the Python host; Makers
environment variables do not magically propagate across a network boundary.

Python service URLs require HTTPS; development permits HTTP only on localhost/127.0.0.1.
Clients do not follow redirects carrying Authorization. Disable body/header logging in
proxies and tracing; access logs should not capture bindings or user text.

## Sequence

1. Review/apply `integration/mijia-web-console.patch` against pinned PR #31 head in a clean
   branch, or use the companion PR. Keep its activation gate closed.
2. Deploy Python with the documented variables. The included Dockerfile is a non-root
   container, but no cloud host is selected or deployed by this extraction.
3. Set the new Makers project root to `adapters/edgeone`; link the correct development
   project and verify Agents capability, routing, `context.store`, cancellation and configuration.
4. Test console→adapter→Python→Gateway and Python→console discovery with fake/low-risk data.
5. Set console `AI_AGENT_BASE_URL` to the new Makers origin. Keep `AI_COMMAND_ENABLED=false`
   and old BYOK UI closed. Public Web Chat paths stay unchanged.
6. Complete execution/state/quota gates in TODO.md before any production cutover.

The project's prior contract used `agents.framework=openai-agents-sdk`, `dir=agents`,
`timeout=60`, file routes and the `Makers-Conversation-Id` header. The standalone adapter
config carries that baseline forward; it has not been validated as a fresh Makers project.
The Tencent live documentation fetch was unavailable during extraction, so no new claim
of native Python support, scheduler APIs or standalone build behavior is made.

## Rollback

Unset `AI_AGENT_BASE_URL` to restore the console's same-project Agent route. Keep the
old implementation until parity/cutover is complete. Do not serve both writers for the
same command namespace. If a command is uncertain, inspect its executor receipt before
retrying on either backend. Quota remains in the console and is not reset by rollback.

New Makers project histories are separate; no automatic cross-project memory migration
is promised. Principal rotation changes user IDs and requires an explicit quota/history plan.
