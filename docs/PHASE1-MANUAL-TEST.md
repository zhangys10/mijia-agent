# Local agent with production services

This guide runs the Python agent locally while using the real production Makers Gateway
and production console tool facade. It is a **live production integration check**, not a
unit test: it incurs real model cost, reads real account/home data, and follows whatever
physical-device policy production currently enforces.

The workflow does not add an execution bypass. Until M2's durable executor/idempotency
work is complete, production scene activation is expected to return
`AI_SCENE_EXECUTION_DISABLED`.

## Prerequisites

- Python 3.11+ and the repo installed for development (`pip install --no-deps -e .`).
- Production variables pulled with `edgeone makers env pull` (or `edgeone makers dev`) into
  the ignored `adapters/edgeone/.env`.
- A sealed `xiaomi_session` cookie copied from the logged-in production console
  (DevTools → Application → Cookies). The CLI turns it into an automation token by
  delegating to the console repo's offline generator — the Python process never
  implements Xiaomi session decryption.

The pulled env must include the production Gateway fields, `AI_PYTHON_INTERNAL_SECRET`,
`AI_TOOLS_INTERNAL_SECRET`, and an HTTPS production `MIJIA_CONSOLE_BASE_URL`. It may carry
an `AI_GATEWAY_ALLOWED_MODELS` value that includes the configured model. The CLI validates
that allowlist without weakening or modifying the deployment policy.

## 1. Check the target without making network calls

```bash
mijia-agent-local-prod check
```

`check` parses the env file without shell evaluation, validates it with the same `Settings`
used by the app, and displays only the environment, hostnames, model, and loopback address.
It performs no Gateway, console, or agent request.

A common stale pull contains:

```text
MIJIA_CONSOLE_BASE_URL=http://localhost:5173
```

The CLI deliberately refuses that value. Correct the deployment environment/pull so it
contains the real HTTPS production console origin; do not work around the check by pointing
a production run at a local console.

## 2. Run a local interactive session

```bash
mijia-agent-local-prod run
```

The CLI will:

1. print the redacted production target;
2. warn about real data, cost, and possible effects;
3. require the exact acknowledgement `USE PRODUCTION SERVICES`;
4. generate the automation token from your pasted `xiaomi_session` cookie by default —
   press Enter, paste the cookie at the hidden prompt, and the CLI delegates to the
   console repo's offline generator (type `token` instead to paste a ready-made token);
5. start the real Uvicorn app on `127.0.0.1:8000`;
6. send each prompt through the existing `POST /ai/command` HTTP boundary; and
7. stop the child process and delete its private LLM log on exit.

Use `/exit`, `/quit`, Ctrl-D, or Ctrl-C to stop. Every prompt gets a new random
`Idempotency-Key`, which is printed before dispatch. The CLI never automatically retries a
timeout, disconnect, `202 processing`, or other unknown result. Preserve the printed key
and inspect the executor state before deciding whether a new action is safe. The current
store is process-local, so **a new CLI process cannot claim exactly-once replay**; do not
repeat a prompt after an uncertain outcome until the executor confirms it did not run.
After confirmation, any new prompt is a deliberate fresh action with a fresh key.

For one prompt and exit:

```bash
mijia-agent-local-prod run --message '我还没回家'
```

An action-like prompt may select `activate_scene`; under the current production policy the
expected response is `AI_SCENE_EXECUTION_DISABLED`. This is the M2 gate working. Do not
change the agent, tool secret, or console route to bypass it.

## Secure token files

`run` generates a fresh token in memory by default (paste the cookie at the hidden
prompt); it is never printed or written to disk. To pre-generate one for repeated use:

```bash
mijia-agent-local-prod generate-token --cookie-file /path/to/cookie --token-out /path/to/automation-token
```

Both files must be owner-only (`chmod 600`); the CLI refuses group/world-readable inputs
and always writes the token file `0600`. The cookie file and the generated token must never
be committed, put on a command line, or left in shell history. During `run`, the token is
held only by the parent CLI and sent to the loopback route; it is not added to the child
environment or Uvicorn argv. Proxy environment variables are disabled for both loopback and
production requests so credentials cannot be captured by an inherited HTTP(S) proxy.

Token generation requires the console checkout (default `../mijia-web-console`, override
with `--console-repo`) because sealing uses the console's own libraries and the
`AI_AUTOMATION_TOKEN_SECRET` / `XIAOMI_SESSION_SECRET` values from the pulled env file.
Generated tokens bind to `NODE_ENV=production`, matching the deployed console; a token from
a differently configured console is rejected at verification time.

Automation can skip the typed phrase with the intentionally explicit
`--i-understand-this-uses-production` flag. This acknowledges production use; it does not
change permissions or execution behavior.

## Logs and data handling

By default, model-call JSONL is written into a private temporary directory (`0700`, file
`0600`) and deleted when the session exits. To retain it for debugging:

```bash
mijia-agent-local-prod run --keep-log .local-prod/llm-calls.jsonl
```

The retained file is set to `0600`, and `.local-prod/` is ignored by Git. The log excludes
credentials and automation tokens by construction, but it **does contain production user
text, conversation context, scene names/descriptions, model output, and usage**. Delete it
when the investigation is complete.

## Scope and expected results

- Model calls use the real configured Makers Gateway and cost real quota/billing.
- Scene discovery uses the production console and real Xiaomi account context contained in
  the opaque automation token.
- `AI_QUOTA_ENABLED=false` offers **no cost ceiling**. The CLI does not claim quota or rate
  protection; keep smoke sessions short.
- `/ai/command` exposes scene command behavior, not the web-chat-only `get_home_status`
  response. Test that through the deployed web chat path if needed.
- The production console remains authoritative for token expiry, home access, scene
  approval, and physical execution.

Examples:

| Prompt / condition | Expected current behavior |
|---|---|
| `我还没回家` | `200`, `status: not_understood`, `intent: none` |
| `今天天气怎么样` | `200`, `status: not_understood` |
| expired token | `401 AUTOMATION_TOKEN_EXPIRED` or `AUTOMATION_TOKEN_INVALID` |
| unknown home via `--home` | `404 AI_HOME_NOT_FOUND` |
| an accepted scene activation intent | `403 AI_SCENE_EXECUTION_DISABLED` until M2 is complete |

This CLI verifies the locally edited Python process against production dependencies. It
does not exercise EdgeOne Agent memory/KV, the adapter authorization sequence, quota
settlement, or cloud-function routing; use the deployment runbook for those surfaces.
