# Implementation backlog

The [steward report alignment](steward-report-alignment.md) doc maps the 云栖管家
architecture report to this repo and the web console, and slices the remaining work
(P0–P3) against the milestones below. Resolve its drift ledger (deployment evidence,
`get_home_status` command parity) before starting new capability.

## Completed extraction

- [x] Read every repository document and review PRs #29–31 instead of copying main alone.
- [x] Port Gateway configuration, request building, intent parsing and usage estimation into Python.
- [x] Port agent turn orchestration and controlled tools into Python with injected protocols.
- [x] Remove direct Xiaomi credential/protocol dependencies from the extracted agent.
- [x] Add authenticated internal ASGI endpoint and strict redacted errors.
- [x] Add Makers lifecycle/history/stop/delete adapter and conservative replay handling.
- [x] Add console authorization/discovery API companion patch with current-home revalidation.
- [x] Preserve AI design documents and write current repo boundary, deployment plan and contracts.
- [x] Add locked dependencies, Dockerfile, CI, Python tests and adapter tests.
- [x] Add the EdgeOne Cloud Functions ASGI entry and deterministic Python package sync.

## Phase 1 — Postman-callable agent (completed locally)

- [x] Add `/ai/command` ingress with the shared decision core (`command_rules.py`),
  automation-token bearer auth, `Idempotency-Key` handling, and `AiCommandResponse`
  parity (`message`/`status`/`intent`/`sceneName`/`execution`/`decisionSource`).
- [x] Add the JSONL LLM call logger (`llm_log.py`, `AI_LLM_LOG_PATH`, stdout fallback)
  and wire it into both pipelines' Gateway calls; no tokens, bindings or gateway
  credentials are ever logged.
- [x] Console companion: `/api/ai/tools` accepts `X-Ai-User-Token` automation tokens
  (principal re-derived console-side, BYOK fields ignored, home resolution mirrors
  `/api/ai/command`); the `sessionBinding` envelope path is unchanged.
- [x] Add `get_home_status` read-only tool to the Python tool protocol and the console
  facade (sanitized snapshot; rejected in preview).

## Phase 2 — console cleanup & drift fixes (completed locally)

- [x] Console `AI_COMMAND_ENABLED` default flipped to `false` (`lib/ai/config.ts`); the
  legacy `/api/ai/command` route stays closed unless explicitly enabled, and this repo's
  `POST /ai/command` is the command ingress.
- [x] Docs updated: console `python-agent-extraction.md` documents the token path; this
  repo's contracts/architecture record the tool list, `/ai/command` contract, response
  fields, and the LLM call log. The `list_devices` read-only tool remains deferred by
  plan.

## Phase 3 — retire legacy command implementation (prepared locally, gated on dev cutover)

- [x] Console branch `feat/phase3-retire-legacy-command` (stacked on phase 2):
  embedded orchestration deleted (orchestrator/fallback/qwen provider/catalog/
  scene service/executor/tool validator/binding/conversation/idempotency/auth
  modules and the orphan `/api/ai/token` route); `/api/ai/command` is a retired
  stub returning `410 AI_COMMAND_RETIRED`; BYOK is gone from token issuance
  (payload fields optional, route rejects them; settings UI and offline
  generator simplified; token generation itself stays as the agent ingress
  credential). Merge only after M1/M2 dev cutover verifies the remote path.
- [x] The Siri/console pass-through question stays open by design: pick it at
  cutover — direct agent ingress or a thin console proxy.
- [x] Python: pruned the dead `MAX_HISTORY_MESSAGES`/`MAX_CONVERSATION_CONTENT`
  duplicates (limits live in `command_models.py` Field constraints); preview path
  stays for deployment policy.
- [x] Docs refreshed: contracts/architecture note the retirement and no-BYOK tokens;
  this TODO's M3 cleanup item is marked done below.

## M1 — Repository and read-only integration

- [x] Create public `zhangys10/mijia-agent` and publish the extracted project.
- [x] Verify the first [GitHub CI run](https://github.com/zhangys10/mijia-agent/actions/runs/35288812809): Python lint/format, 33 Python tests, and five adapter tests passed.
- [x] Open companion draft PR [mijia-web-console#32](https://github.com/zhangys10/mijia-web-console/pull/32).
- [x] Reconcile the existing PR stack deliberately. The current companion baseline is
  `mijia-web-console` PR #33 (`02597111116c6ebd6b2aa61f891e31bbabcf15e8`), based on the
  PR #31 stack; it passes the web console's targeted AI tests, typecheck, lint, and full
  test suite. The checked-in extraction patches remain historical provenance.
- [ ] Deploy the console tool facade in a development environment.
- [ ] Follow `docs/m1-deployment-runbook.md` for the deployment sequence, verification
  record, and rollback sign-off.
- [x] Validate the local Makers runtime with `edgeone makers dev` after running
  `npm run build --prefix adapters/edgeone`; `/api/healthz` returned `200 {"status":"ok"}`.
  This check did not invoke the Gateway, send Xiaomi bindings, or execute a scene.
- [ ] Deploy the new repo's Makers adapter subproject with the Python ASGI Cloud Function;
  configure Gateway credentials explicitly and verify the `/api` route stripping contract.
- [ ] Configure console `AI_AGENT_BASE_URL` to the new adapter and exercise create/chat/list/delete.
  For M1 development, set console `AI_QUOTA_ENABLED=false`: the console returns a
  principal-bound disabled summary and neither side reads or writes a quota ledger. This is
  not cost protection; adapter settlement and quota summaries are deferred to M3.
  Note (2026-09-20): the console now ships a page AI assistant UI
  (`app/components/ai-assistant/`) that drives create/chat/delete through the same
  public Web Chat API, so this remote-mode exercise has a real browser client. No new
  M1 gates result from the UI; the browser stop button only aborts the local request
  (there is no public stop route in the frozen contract).
- [ ] Validate stop propagation on the real Makers runtime, including an in-flight Gateway call.
- [x] Ensure preview is blocked/mock at the outer console boundary as well as Python.
  Console Web Chat now authenticates and validates the home/conversation before returning the
  fixed mock without Agent, quota, or device calls.
- [ ] Verify Gateway model availability from the new Python Cloud Function; previous project verification is insufficient.

Acceptance: logged-in A/B users cannot read each other's history or catalog; raw Xiaomi
credentials never enter the new service; no device actions occur. M1 development runs with
quota explicitly disabled (`mode: disabled`, no enforcement or cost protection); fail-closed
quota is an M3 production gate.

## M2 — Safe execution parity (blocks physical activation)

Owner: console executor contract; Python consumes the same narrow Tool API.

- [ ] Choose a durable store with atomic create/compare-and-set for execution receipts; do not use
  eventually consistent quota KV or process-local maps as the authoritative claim.
- [ ] Scope receipt key to environment + principal + home + idempotency key, independent of conversation.
- [ ] Store canonical request hash, scene revision, processing/result/uncertain state and timestamps.
- [ ] Different payload/scope with same key → conflict; concurrent identical calls → one claim;
  completed call → replay; timeout/crash after dispatch → uncertain, never blind retry.
- [ ] Reload scene and validate current home, enabled state, alias, reviewed action revision and low-risk policy.
- [ ] Wire existing `runManualScene` through this executor, replacing the explicit disabled response.
- [x] Extend stable console error mapping for `AI_EXECUTION_STATUS_UNKNOWN` and disabled execution.
- [x] Settle known model usage on errors; conservatively account for unknown Gateway/transport outcomes.
  Rebased onto console `main` (branch `feat/ai-settlement-preview-runbook`) together with the
  outer chat Preview mock. Known error usage is committed; unknown transport outcomes use the
  conservative request estimate; clearly pre-flight errors release their reservation. The
  checked-in patch remains historical provenance.
- [ ] Ensure quota settlement failure after execution cannot cause another physical action.
- [ ] Test duplicate keys across conversations, workers and restarts; scene edits after approval;
  partial result; client disconnect; cancellation and timeout after dispatch.
- [ ] Verify real low-risk scene E2E only after deployment settings and approval are ready.

Acceptance: one durable claim per logical command; no false success; uncertain state remains
visible and reconciliation is explicit. No claim of exactly-once physical execution without
upstream idempotency/observable reconciliation.

## M3 — State, quota and cutover

- [ ] Implement adapter-owned remote quota reserve/commit/release with known-usage,
  unknown-outcome and clearly pre-flight settlement categories.
- [ ] Attach a principal-bound quota summary to every successful chat and implement authenticated
  `POST /api/internal/quota` summary reads; validate 429 recovery time and A/B isolation.
- [ ] Bind real `ai_quota_kv`, measure propagation/overrun, retain `softLimit: true` and production fail-closed.
- [ ] Restore console `AI_QUOTA_ENABLED=true` only after the adapter quota contract and deployed
  settlement checks pass. Never run a second console ledger in remote mode.
- [ ] Configure WAF/minute burst limits and conservative quota headroom.
- [ ] Verify Makers state atomicity/serialization and delete semantics; add durable ledger independent of memory.
- [ ] Implement bounded receipt retention and conversation TTL; never expire unresolved physical outcomes silently.
- [ ] Migrate or reset old histories explicitly; do not assume identical IDs imply shared stores across projects.
- [ ] Keep one active agent backend at a time; add rollback smoke check.
- [x] After parity and rollout, remove old TS Gateway/orchestration/runtime code from the console in a follow-up PR.
  Keep catalog/execution/auth/quota modules and their tests.
  Done on the phase-3 branch: the legacy command implementation is deleted and the
  console keeps the alias catalog, `runManualScene` execution path, auth/token
  generation, and quota modules with their tests.

## M4 — Continue original roadmap here

- [x] Add read-only `get_home_status` agent tool: Python advertises the empty-argument
      tool, fetches the sanitized environment snapshot from the console tools API only
      after the model selects it, and returns it as `Result.homeStatus` (values never
      enter model messages or conversation history). Companion console collector,
      dashboard, and tool facade are tracked in the web console repo.
- [ ] Web assistant UI stays in console; new repo owns behavior and API evolution.
- [ ] Siri/Automation Token migration: no model key; same agent, quota and executor; command remains disabled until ready.
- [ ] Verify Makers scheduler APIs rather than infer them from “scheduled tasks” use cases.
- [ ] Add ReminderStore + scheduler + notification adapter with durable delivery IDs and cancel/retry semantics.
- [ ] Implement create/list/cancel reminder tools; reminders suggest/notify by default.
- [ ] Add explicit-consent preference memory with inspect/pause/delete; separate household scopes.
- [ ] Habit evidence yields suggestions, never implicit authority for device operations.
- [ ] Add Home Assistant executor adapter only when needed; do not duplicate the orchestration core.

## Definition of done for the complete migration

Both repos pass CI; live auth/quota/memory/tools are verified; safe execution parity is
demonstrated; source legacy agent implementation is retired; deployment and rollback
are tested. M1's explicitly disabled quota does not satisfy this production definition.
