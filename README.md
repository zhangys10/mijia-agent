# Mijia Agent

Python agent development extracted from `zhangys10/mijia-web-console`.
This repository owns model integration, intent handling, controlled tool calls,
and future reminder/preference features. Xiaomi protocol code stays in the web console.

**Status: Phase 3 safety foundations present; canonical scene-action registration is pending.** The Python core,
Makers adapter, and companion web-console implementation include per-home scene approval,
revision-bound checks, and a durable console execution ledger. Scene writes remain disabled
until the deployed checks in [docs/TODO.md](docs/TODO.md) pass.
No live deployment, model invocation, or device control was performed during extraction.

## Read first

1. [Architecture and decisions](docs/architecture.md)
2. [Service contracts](docs/contracts.md)
3. [Deployment and rollback](docs/deployment.md)

## Local development

Python 3.11+; Python 3.12 is the tested/container baseline.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.lock
pip install --no-deps -e .
pytest -q
ruff check src tests
ruff format --check src tests
```

Inject configuration through your shell or deployment secret manager, following
[deployment.md](docs/deployment.md). No `.env` file is committed. Then:

```bash
uvicorn mijia_agent.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

The canonical Python endpoint is `POST /internal/v1/assistant`, authenticated with a
dedicated server secret. It is not a browser or Siri endpoint. It must only be called by
the Makers adapter after the web console has authenticated the user and re-derived the
token-bound principal/home context.
`GET /healthz` is a liveness check, not proof that Gateway or console access works.

### Local agent with production services

> **Live production integration — not a unit test.** These commands use real production
> account/home data and incur real Gateway cost. They preserve the production execution
> policy; they do not enable or bypass physical scene execution.

The fake smoke check is local-only:

```bash
mijia-agent-local-prod smoke --profile fake
```

Run [`scripts/local-prod-setup.sh`](scripts/local-prod-setup.sh) manually before the first
`mijia-agent-local-prod run --profile live-read`, and whenever its prerequisites become stale.
It prepares `.venv`, builds the adapter, links the `mijia-agent` Makers project, and pulls the
selected production environment into the ignored `adapters/edgeone/.env`. The CLI neither
checks setup readiness nor runs the setup script; invoke it from the Python environment you
prepared. Running the setup script again pulls the environment and replaces `.env`. `check`
validates the config and prints a redacted target. `check` and fake smoke do not run setup.

Then run an interactive session — the CLI generates the automation token from your pasted
`xiaomi_session` cookie (hidden prompt) via the console repo's offline generator, or type
`token` to paste a ready-made one:

```bash
mijia-agent-local-prod run --profile live-read
mijia-agent-local-prod run --profile live-read --message '客厅温度是多少？'
```

The fake profile is local-only and needs no credentials or network. The live-read profile calls
the canonical `POST /ai/assistant` endpoint and never registers physical-write capabilities.

The CLI starts the real ASGI app on loopback and shuts it down on exit. See
[the live test guide](docs/local-prod-test.md) for cookie/token files, retained logs,
expected execution-gate behavior, and operational warnings.

For a local Console → Agent → Python → Console round trip with a deterministic fake Gateway
and no real model calls, see the companion Console repository's
[local integration guide](../mijia-web-console/docs/local-integration-test.md).

## EdgeOne adapter

`adapters/edgeone/` contains the thin TypeScript shell needed by the existing
Makers deployment contract and the Python ASGI Cloud Function. It stores bounded
conversation history and receipts, forwards turns to Python, and implements
delete/stop routes. It does not call the model or decrypt Xiaomi credentials.
Run its tests with Node 22.13+:

```bash
npm test --prefix adapters/edgeone
```

The adapter has no npm dependencies. `npm run build --prefix adapters/edgeone`
syncs `src/mijia_agent` and `src/mijia_assistant` into the Cloud Functions build tree; deploy with the EdgeOne
Makers project rooted at `adapters/edgeone`. That root contains both platform markers:
`edgeone.json` plus `agents/` for Agent routes, and `cloud-functions/` for Python.
The Cloud Function is exposed as `/api`, and EdgeOne strips that prefix before invoking
the existing FastAPI routes. Its runtime APIs and Cloud Functions build behavior still
need live verification.
Python remains unable to access EdgeOne KV.

## Layout

| Path | Responsibility |
|---|---|
| `src/mijia_agent/` | Python HTTP boundary, Gateway, tools client, agent service |
| `src/mijia_assistant/` | Capability-neutral conversation engine and provider/tool contracts |
| `adapters/edgeone/` | Makers runtime, memory/lifecycle adapter, and Python Cloud Function entry |
| `tests/` | Credential isolation, unsafe intent, tool and HTTP contract tests |
| `docs/` | Current architecture, contracts, operational guides, and actionable backlog |

No license is added: the source repository declares no open-source license.
Extraction is performed at the repository owner's request; public availability
does not itself grant redistribution rights.
