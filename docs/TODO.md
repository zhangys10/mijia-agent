# Implementation backlog

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

## M1 — Repository and read-only integration

- [x] Create public `zhangys10/mijia-agent` and publish the extracted project.
- [ ] Verify the first GitHub CI run.
- [x] Open companion draft PR [mijia-web-console#32](https://github.com/zhangys10/mijia-web-console/pull/32).
- [ ] Reconcile the existing PR stack deliberately. Companion patch is based on PR #31 head, not main.
- [ ] Deploy the console tool facade in a development environment.
- [ ] Deploy Python to a mainland-reachable Python/ASGI host; configure Gateway credentials explicitly.
- [ ] Link/deploy the new repo's Makers adapter subproject; verify build settings and runtime APIs.
- [ ] Configure console `AI_AGENT_BASE_URL` to the new adapter and exercise create/chat/list/delete.
- [ ] Validate stop propagation on the real Makers runtime, including an in-flight Gateway call.
- [ ] Ensure preview is blocked/mock at the outer console boundary as well as Python.
- [ ] Verify Gateway model availability from the new Python host; previous project verification is insufficient.

Acceptance: logged-in A/B users cannot read each other's history or catalog; raw Xiaomi
credentials never enter the new service; no device actions occur; quota remains fail-closed.

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
- [ ] Extend stable console error mapping for `AI_EXECUTION_STATUS_UNKNOWN` and disabled execution.
- [ ] Settle known model usage on errors; conservatively account for unknown Gateway/transport outcomes.
  The current Phase 5 catch/release path discards usage on failure and must be corrected.
- [ ] Ensure quota settlement failure after execution cannot cause another physical action.
- [ ] Test duplicate keys across conversations, workers and restarts; scene edits after approval;
  partial result; client disconnect; cancellation and timeout after dispatch.
- [ ] Verify real low-risk scene E2E only after deployment settings and approval are ready.

Acceptance: one durable claim per logical command; no false success; uncertain state remains
visible and reconciliation is explicit. No claim of exactly-once physical execution without
upstream idempotency/observable reconciliation.

## M3 — State, quota and cutover

- [ ] Bind real `ai_quota_kv`, measure propagation/overrun, retain `softLimit: true` and production fail-closed.
- [ ] Configure WAF/minute burst limits and conservative quota headroom.
- [ ] Verify Makers state atomicity/serialization and delete semantics; add durable ledger independent of memory.
- [ ] Implement bounded receipt retention and conversation TTL; never expire unresolved physical outcomes silently.
- [ ] Migrate or reset old histories explicitly; do not assume identical IDs imply shared stores across projects.
- [ ] Keep one active agent backend at a time; add rollback smoke check.
- [ ] After parity and rollout, remove old TS Gateway/orchestration/runtime code from the console in a follow-up PR.
  Keep catalog/execution/auth/quota modules and their tests.

## M4 — Continue original roadmap here

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
are tested. The present extraction does not yet satisfy this production definition.
