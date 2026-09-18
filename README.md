# Mijia Agent

Python agent development extracted from `zhangys10/mijia-web-console`.
This repository owns model integration, intent handling, controlled tool calls,
and future reminder/preference features. Xiaomi protocol code stays in the web console.

**Status: tested initial extraction, not a production cutover.** The Python core,
Makers adapter, CI, and companion web-console patch are present. The new console
tool boundary supports authorization and scene discovery. Remote physical execution
is deliberately disabled until durable executor idempotency is implemented.
No live deployment, model invocation, or device control was performed during extraction.

## Read first

1. [Architecture and decisions](docs/architecture.md)
2. [Migration audit and provenance](docs/migration.md)
3. [Service contracts](docs/contracts.md)
4. [Implementation backlog](docs/TODO.md)
5. [Deployment and rollback](docs/deployment.md)

The three original AI design documents are preserved in `docs/source-snapshot/`.
They describe the TypeScript baseline; the documents above supersede their repo
ownership and deployment assumptions. Their unchecked tasks remain unchecked here.

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

The Python endpoint is `POST /internal/v1/turn`, authenticated with a dedicated
server secret. It is not a browser or Siri endpoint. It must only be called by
the Makers adapter after the web console has authenticated the user and reserved quota.
`GET /healthz` is a liveness check, not proof that Gateway or console access works.

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
syncs `src/mijia_agent` into the Cloud Functions build tree; deploy with the EdgeOne
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
| `adapters/edgeone/` | Makers runtime, memory/lifecycle adapter, and Python Cloud Function entry |
| `tests/` | Credential isolation, unsafe intent, tool and HTTP contract tests |
| `integration/*.patch` | Companion patches against PR #31's pinned head |
| `docs/` | Current architecture, source audit, contracts, actionable backlog |

No license is added: the source repository declares no open-source license.
Extraction is performed at the repository owner's request; public availability
does not itself grant redistribution rights.
