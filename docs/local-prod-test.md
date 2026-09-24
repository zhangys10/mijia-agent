# Local agent with production services

This guide runs the Python agent locally while using the real production Makers Gateway
and production console tool facade. It is a **live production integration check**, not a
unit test: it incurs real model cost, reads real account/home data, and follows whatever
read-only home policy production currently enforces. The Phase 0 harness does not register a
physical-write capability.

The workflow does not add an execution bypass. Action-like model output is rejected because
`activate_scene` is unavailable; it is never forwarded to the console.

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

## 2. Run the local fake acceptance matrix

```bash
mijia-agent-local-prod smoke --profile fake
```

This requires no env file, token, cookie, console checkout, network, or production model. It
checks a direct answer, clarification, a two-step fake-weather loop, malformed arguments,
deadline handling, and physical-write rejection.

## 3. Run a local live-read session

```bash
mijia-agent-local-prod run --profile live-read --console-repo /path/to/mijia-web-console
```

The CLI will:

1. print the redacted production target;
2. warn about real data, cost, and possible effects;
3. require the exact acknowledgement `USE PRODUCTION SERVICES`;
4. generate the automation token from your pasted `xiaomi_session` cookie by default —
   paste the cookie at the hidden prompt, and the CLI delegates to the
   console repo's offline generator (use `--token-file` for a ready-made token);
5. start the real Uvicorn app on `127.0.0.1:8000`;
6. send each prompt through the canonical `POST /ai/assistant` HTTP boundary; and
7. stop the child process and delete its private LLM log on exit.

Use `/exit`, `/quit`, Ctrl-D, or Ctrl-C to stop. Every prompt gets a new random visible
`Request-Key`. The CLI never automatically retries a timeout or disconnect. Physical writes are
unavailable, so this profile cannot use a request key to dispatch an action.

For one prompt and exit:

```bash
mijia-agent-local-prod run --profile live-read --message '客厅温度是多少？'
```

To make the live-read check assert that the exposure-filtered home tool actually ran,
require a successful event for that capability:

```bash
mijia-agent-local-prod run --profile live-read --message '客厅温度是多少？' --expect-tool get_home_environment
mijia-agent-local-prod run --profile live-read --message '客厅灯开着吗？' --expect-tool get_device_status
```

The command exits with an error if the requested tool is absent or did not complete as
`success` or `partial`; it never retries. To exercise the short Siri rendering contract,
select `--channel siri` (or `voice`); the CLI requires and displays a `speechText` of at
most 280 characters:

```bash
mijia-agent-local-prod run --profile live-read --channel siri --message '客厅温度是多少？' --expect-tool get_home_environment
```

These commands use the real configured model and production home data, so each incurs the
same cost and privacy impact as any other live-read run.

## Secure token files

`run` generates a fresh token in memory by default (paste the cookie at the hidden
prompt); it is never printed or written to disk. To pre-generate one for repeated use:

```bash
mijia-agent-local-prod generate-token --console-repo /path/to/mijia-web-console --cookie-file /path/to/cookie --token-out /path/to/automation-token
```

Both files must be owner-only (`chmod 600`); the CLI refuses group/world-readable inputs
and always writes the token file `0600`. The cookie file and the generated token must never
be committed, put on a command line, or left in shell history. During `run`, the token is
held only by the parent CLI and sent to the loopback route; it is not added to the child
environment or Uvicorn argv. The canonical assistant forwards that same opaque automation
token to the production console only after selecting a home-read capability, matching the
web adapter's production tool envelope. Proxy environment variables are disabled for both loopback and
production requests so credentials cannot be captured by an inherited HTTP(S) proxy.

Token generation requires the console checkout selected with `--console-repo` because
sealing uses the console's own libraries. The pulled agent env file supplies
`AI_AUTOMATION_TOKEN_SECRET`; the console checkout's `.env` supplies
`XIAOMI_SESSION_SECRET` when it is absent from the pulled file. Both secrets must match
the deployed console. Generated tokens bind to `APP_ENV=production`, matching production;
the checkout's local `.env` cannot override the selected token secret.

Automation can skip the typed phrase with the intentionally explicit
`--i-understand-this-uses-production` flag. This acknowledges production use; it does not
change permissions or execution behavior.

## Logs and data handling

By default, model-call JSONL is written into a private temporary directory (`0700`, file
`0600`) and deleted when the session exits. To retain it for debugging:

```bash
mijia-agent-local-prod run --keep-log .local-prod/llm-calls.jsonl
```

The retained file is set to `0600`, and `.local-prod/` is ignored by Git. Canonical-assistant
records contain request metadata, tool names, bounded response lengths, usage, and latency; tool
outcomes add only the tool name, status (`success`, `partial`, `error`, or `outcome_unknown`), and a safe error code on failures. They exclude credentials, tokens,
prompts, model response text, and private tool results. Delete a retained operational log when
the investigation is complete.

## Scope and expected results

- Model calls use the real configured Makers Gateway and cost real quota/billing.
- Home reads use the production console and real Xiaomi account context contained in the opaque
  automation token, but only after model tool selection.
- `AI_QUOTA_ENABLED=false` offers **no cost ceiling**. The CLI does not claim quota or rate
  protection; keep smoke sessions short.
- Generic questions do not invoke the console. Home reads are returned through
  `get_home_environment` or `get_device_status` tool events.
- The production console remains authoritative for token expiry, home access, scene
  approval, and physical execution.

Examples:

| Prompt / condition | Expected current behavior |
|---|---|
| a stable general question | `200`, `outcome: direct_answer`, no console tool call |
| `今天天气怎么样` | clarification because no location is available in Phase 0 |
| `客厅温度是多少？` | a `get_home_environment` tool loop and grounded answer |
| expired token | `401 AUTOMATION_TOKEN_EXPIRED` or `AUTOMATION_TOKEN_INVALID` |
| unknown home via `--home` | `404 AI_HOME_NOT_FOUND` |
| a scene activation request | rejected before dispatch because no write tool is registered |

This CLI verifies the locally edited Python process against production dependencies. It
does not exercise EdgeOne Agent memory/KV, the adapter authorization sequence, quota
settlement, or cloud-function routing; use the deployment runbook for those surfaces.
