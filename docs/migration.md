# Migration audit — 2026-09-17

## Sources actually reviewed

Repository: https://github.com/zhangys10/mijia-web-console

- Default branch: `05f6d2c2aef41a3112691405b63c1d17dbcd7bcf`.
- Latest reviewed agent integration: PR #31, branch `codex/ai-home-phase-5-web-chat`,
  commit `3009be70bf82f90ad5b3b51de9f8f17b1c7f213d`.
- PR #29 is open against the phase-2 branch. PR #30 was merged into phase-3,
  not main. PR #31 remains open against phase-4. Do not equate “merged PR #30”
  with “all agent features are on main”. Do not merge/rebase the stack incidentally.

All seven Markdown documents on main were read, and README/TODO changes through
the PR #31 head were reviewed:

| Document | Architectural findings carried forward |
|---|---|
| `AGENTS.md` | Existing TypeScript repo gates, no credential logging, explicit unknown states |
| `README.md` | Next/Vinext multi-target builds, encrypted Cookie auth, Gateway config |
| `docs/architecture.md` | Xiaomi, MIoT, topology, scenes, safe automation edits remain console-owned |
| `docs/device-management-design.md` | Home-first indexes, physical DID semantics, evidence levels, no inferred execution |
| `docs/ai-home-poc-design.md` | Gateway, principal quotas, Web first, future Siri/reminders/preferences |
| `docs/ai-home-phase-0-contract.md` | Web/internal contracts, Makers routing/header, preview, errors |
| `docs/ai-home-implementation-todo.md` | Existing phase progress and remaining manual validation |

The broader device/topology documents contain concrete source examples and are linked
at their source rather than copied into a Python project that does not own that domain.
The three AI docs are preserved verbatim under `source-snapshot/` at the reviewed
agent head, with their historical status retained.

## Implementation mapping

| Source | Destination / disposition |
|---|---|
| `lib/ai/providers/makers-gateway-provider.ts` | `src/mijia_agent/gateway.py`; strict args and usage validation |
| `lib/ai/agent/ai-agent-service.ts` | `src/mijia_agent/service.py`; no XiaomiSession dependency |
| `lib/ai/tools/{list-scenes,activate-scene}.ts` | Python protocol/client plus console policy boundary |
| `lib/ai/agent/{agent-store,idempotency}.ts` | Makers lifecycle adapter; production claim gap documented |
| `agents/ai-home/{index,stop,delete}.ts` | New repo `adapters/edgeone/agents/ai-home/` |
| `lib/ai/tools/agent-scene-catalog.ts` | Keep alias-to-real-ID mapping in console; expose summaries only |
| `lib/ai/security/{principal,agent-binding}.ts` | Keep console-owned; new services do not decrypt Xiaomi sessions |
| `lib/ai/quota/*`, Edge quota routes | Keep console Edge Function boundary, reuse existing ledger |
| `lib/ai/web-chat/*`, conversation handles | Keep console ingress; optional remote Agent base URL |
| Legacy Qwen/BYOK/Siri token paths | Do not copy; remain disabled pending shared-agent migration |
| Assistant UI, reminders, habit learning | Not implemented by the baseline; tracked explicitly in new TODO |

## Improvements and limitations

Implemented changes include strict Python request schemas, no model-visible trusted
identity, explicit activation grammar, actual executor result messages, no redirect
credential forwarding, no automatic tool retries, and conservative uncertain receipts.

This is a staged migration: production traffic is not switched; source code is not
deleted; the extracted console API is read-only. A tested Python execution protocol
is not equivalent to live physical execution parity. Full cutover requires the gates in
[steward-report-alignment.md](steward-report-alignment.md) and both repositories'
deployment checks.

The source `AI_SCENE_APPROVED_IDS` catalog treats a listed scene as low risk; it does
not bind approval to the scene's current action revision. Implement reviewed revision
and risk checks before enabling the new remote executor, so editing an approved scene
cannot silently add unsafe actions.

Source code has no declared open-source license. Preserve attribution to the original
repo and do not introduce a license without the owner's decision.
