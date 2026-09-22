# AI request latency plan

Status: **proposed, not implemented.** This document records the measured problem, the
intended optimization sequence, and the gates that must hold before each change ships.
It is a plan, not a description of current behavior; the current implementation remains
governed by [architecture.md](architecture.md) and [contracts.md](contracts.md).

## Context

The observed latency is cumulative, not only an LLM problem. A normal browser turn
currently runs `/api/ai/chat` → Makers `/ai-home` → Python `/internal/v1/turn`, while
synchronously validating Xiaomi home membership three times, loading the scene catalog,
calling `@makers/deepseek-v4-flash` non-streaming, and persisting receipts/history. The
model call alone is logged at roughly 4–5 seconds; the duplicated serial work explains the
roughly 10-second public response.

The goal is to make common voice-style commands model-free where they can be handled
safely, reduce the remaining LLM path to one ownership check and one compact prompt, and
establish phase-level latency evidence before changing provider/model settings. Security
and execution invariants remain stricter than latency goals: no cross-home access, no
model-provided identity, no false activation, no blind retry after a possibly dispatched
write, and unchanged idempotency/replay behavior.

### Current critical path to verify

- `mijia-web-console/lib/ai/web-chat/web-chat-service.ts`: `AiWebService.identity()` calls
  Xiaomi `listHomes`, validates the requested home, then creates the sealed binding.
- `adapters/edgeone/agents/ai-home/shared.ts`: `authorize()` calls the console `authorize`
  tool, which repeats `listHomes` before memory is read.
- `src/mijia_agent/service.py`: every non-preview turn calls `list_scenes` before routing.
- `mijia-web-console/lib/ai/tools/remote-tool-service.ts`: every tool verifies the binding,
  re-derives the principal, repeats `listHomes`, then `list_scenes` performs a Xiaomi
  scene-list request.
- `src/mijia_agent/gateway.py`: the remaining model call is one non-streaming request with a
  5-second default timeout, 256-token cap, and duplicated scene data in user content/tool
  schema.

## 1. Instrument the existing path before optimizing it

Add lightweight, sanitized phase timing keyed by the existing `requestId`; do not add a
tracing dependency yet.

- In the console, time cookie/session authentication, principal derivation, initial
  `listHomes`, binding/claim creation, remote Agent call, and total request duration. Add a
  development/diagnostic-only `Server-Timing` header with generic phase names; production
  JSON logs remain the source of truth.
- In the Makers adapter, time authorization, receipt read, history read, processing receipt
  write, Python call, completed receipt write, and history append. Log a module-start
  age/cold-start marker and whether the turn was replayed, deterministic, or LLM-routed.
- In Python, emit one `turn_timing` record around scene loading, deterministic routing,
  Gateway HTTP, post-model tool execution, logging, and total turn time. Extend the existing
  `llm_call` metadata with `sceneCount`, `historyMessageCount`, `historyChars`,
  `messagesBytes`, `toolSchemaBytes`, `requestBytes`, provider token usage, and an explicit
  usage source. Keep byte estimates separate from token counts.
- In the console tool facade, count/timestamp binding verification, `listHomes`, scene
  loading, device discovery, MIoT specification loading, property batches, and cancellation.
  Never log user text, principal/home values, bindings, credentials, real scene IDs, or DIDs.
- Add deterministic tests for timing field names/sanitization, but keep exact durations
  unconstrained. Capture cold-first, immediate-second, and warm-sequence baselines for list
  scenes, exact activation intent, ordinary chat, and home status before enabling later
  stages.

Representative files:

- `mijia-web-console/edge-functions/api/ai/chat.ts`
- `mijia-web-console/lib/ai/web-chat/web-chat-service.ts`
- `adapters/edgeone/agents/ai-home/index.ts`
- `src/mijia_agent/service.py`
- `src/mijia_agent/gateway.py`

## 2. Replace repeated Xiaomi ownership lookups with a short-lived request authorization proof

Retain the current sealed `sessionBinding`, but add a separate versioned authorization proof
signed with a new purpose-specific secret shared only by the console and Makers adapter. Do
not share `XIAOMI_SESSION_SECRET` or the new signing secret with Python; Python only carries
the proof as an opaque `SecretStr` to console tools.

Proof fields must include version/kind, environment/audience, `requestId`, `principalId`,
`homeId`, sorted scopes, digest of the sealed binding, issued/expiry timestamps (30–60
seconds), and signature. The proof is valid only for one turn and cannot authorize another
home, principal, request, scope set, or binding.

Deploy compatibly in this order:

1. Console tool facade accepts an optional proof, verifies it plus the existing
   binding/principal derivation, and for **read-only** tools skips only the repeated remote
   `listHomes` call when all bindings match. Missing/invalid proofs continue through the
   existing full home-membership check during rollout.
2. Makers adapter accepts and verifies the optional proof locally before reading memory; a
   valid proof replaces the `authorize` callback. It forwards the opaque proof to Python.
   Absent proofs retain the legacy callback.
3. Python `Turn`/`ConsoleTools` accept and forward the opaque proof without exposing it to
   model payloads or logs.
4. Console issues the proof only after `AiWebService.identity()` has performed the one live
   `listHomes` membership validation for that request.
5. After shadow metrics show parity, enable proof use and assert one Xiaomi home lookup per
   browser turn. Keep the legacy path as a rollback switch until all runtimes are upgraded.

A proof never replaces the future executor's write-time checks: physical activation must
still refresh the current scene/home, validate enabled/reviewed state, acquire the durable
execution claim, and preserve uncertain outcomes.

Representative files:

- `mijia-web-console/lib/ai/security/agent-binding.ts` (or a sibling `request-authorization.ts`)
- `mijia-web-console/lib/ai/web-chat/web-chat-service.ts`
- `mijia-web-console/lib/ai/tools/remote-tool-service.ts`
- `adapters/edgeone/agents/ai-home/shared.ts`
- `src/mijia_agent/models.py`, `src/mijia_agent/console.py`

## 3. Add a conservative read-only fast path before Python and the LLM

Place the first fast path in the Makers adapter after authorization/receipt lookup but before
loading history or calling Python. The adapter already owns replay and receipts, so this
preserves one lifecycle/idempotency domain while removing both the Python cold-start hop and
the model call.

- Match only a closed, whole-utterance phrase set for `list_scenes`; call the existing console
  `list_scenes` tool directly and return the same structured `Result` shape.
- Match only a closed, whole-utterance phrase set for the existing no-argument
  `get_home_status`; call that console tool directly without first loading scenes.
- Persist/replay deterministic results through the same receipt semantics. Keep read-only
  keys/namespaces distinct from future write claims so a read can never complete a write
  receipt.
- Any negation, question, conditional, quotation/third-party report, conjunction, unsupported
  room/device/detail request, pronoun/reference, or ambiguous phrase falls through to the
  current Python/LLM path. Multi-turn references always remain LLM-routed initially.
- Log the deterministic candidate in shadow mode while serving the legacy result; compare
  intent/result shape before turning it on. Use a dedicated `off | shadow | on` kill switch.
- Do **not** mirror activation consent in TypeScript. Instead, add a separate Python fast path
  inside `AgentService.run()`: after loading current scenes, test
  `explicit_activation(message, scene)` against every scene; only when exactly one scene passes
  may the service call the existing `activate_scene` tool without an LLM decision. This
  preserves the one server-side consent grammar and immediately removes 4–5 seconds from
  exact voice commands such as "执行明亮模式" and "我回家了". In the current deployment it still
  returns `AI_SCENE_EXECUTION_DISABLED`; after M2, the same path must use the durable
  executor's atomic claim and live scene revalidation.
- Every other activation-shaped message—including "打开…", which the current consent gate
  intentionally does not accept—remains on the LLM path and still must pass
  `explicit_activation` before any tool call.

Create a routing fixture set that includes exact reads, exact accepted activations, negations,
conditions, questions, quoted/attributed commands, compound requests, partial/duplicate scene
names, unsupported detail requests, and multi-turn references. Blocking invariant: zero
deterministic false activations and no physical write before M2.

Representative files:

- `adapters/edgeone/agents/ai-home/index.ts`
- `adapters/edgeone/agents/ai-home/shared.ts`
- `adapters/edgeone/tests/adapter.test.mjs`
- `src/mijia_agent/service.py`
- `tests/test_agent.py`

## 4. Shrink and benchmark the LLM fallback separately

Apply prompt changes one at a time against the routing fixture set so latency gains are
attributable and correctness regressions are visible.

- Remove `replyMessage` from `activate_scene`: it is currently generated and parsed but
  ignored; the executor's actual message already wins. Require only `sceneId`.
- Send the scene catalog once in compact `alias + name` form. Drop the mechanically duplicated
  description (`当前家庭的手动场景：<name>`) from model input while retaining public response
  descriptions if required by the browser contract.
- Keep the `sceneId` enum initially because it is a useful schema constraint; only consider
  replacing it with server-side resolution after a dedicated eval.
- Bound history more tightly for the fallback path (shorter per-message content and/or fewer
  turns) only after testing multi-turn references. Exact deterministic commands need no model
  history.
- Record completion-token p99 before reducing `AI_GATEWAY_MAX_OUTPUT_TOKENS`; test
  128/160/192 versus the current 256 rather than choosing blindly.
- Replay the same sanitized fixtures through approved Gateway models/configurations and
  compare decision accuracy, tool validity, p50/p95 Gateway latency, timeout rate, and token
  counts. Keep `temperature=0` and thinking disabled. A model change is configuration-only and
  ships separately from prompt changes.
- Do not add streaming in this optimization. Tiny tool-routing responses must be fully parsed
  before execution, so streaming does not improve action latency; the public API also
  currently promises non-streaming JSON. Revisit streaming/TTS as a separate voice UX contract
  after server latency is reduced.

Any live model benchmark incurs cost and must be a small, explicitly approved run; sustained
tests use a fake Gateway.

Representative files:

- `src/mijia_agent/command_rules.py`
- `src/mijia_agent/gateway.py`
- `src/mijia_agent/config.py`
- `tests/test_agent.py`

## 5. Optimize `get_home_status` as its own read pipeline

The model-free route exposes the collector's current cost, so give it explicit home-scoped
work and cancellation budgets.

- Add a home-scoped device-discovery function that pages only the already-authorized target
  home instead of calling all-account `listDevices` and processing homes serially.
- Thread the incoming abort/deadline through `/api/ai/tools` → `runRemoteTool` →
  `collectHomeEnvironment` → Xiaomi/MIoT requests. Caller cancellation stops work; internal
  phase deadlines produce an honest `partial` snapshot and warning rather than fabricated
  values.
- Bound discovery, specification loading, and property reading independently; retain bounded
  parallel specification loads and 40-property batches.
- Preserve and instrument the existing MIoT specification cache. If measurement justifies
  another cache, add only a bounded, process-local, short-TTL device-discovery cache keyed by
  principal + home. It is a warm-instance optimization, contains no secrets, is never an
  authorization source, and must never authorize writes or cross homes.
- Keep response sanitization and structured readings unchanged; values still do not enter
  model history.

Representative files:

- `mijia-web-console/lib/home-environment.ts`
- `mijia-web-console/lib/xiaomi-cloud.ts`
- `mijia-web-console/lib/miot-spec.ts`
- `mijia-web-console/lib/ai/tools/remote-tool-service.ts`
- `mijia-web-console/tests/ai-remote-tools.test.mjs`

## 6. Remove measured tail work and align deadlines

Only optimize these after phase timings show material contribution.

- Keep the completed/uncertain receipt write synchronous and before the response; it protects
  idempotency and must not be weakened.
- Keep user/assistant history appends concurrent. Move them off the response path only if the
  Makers runtime exposes a verified lifecycle primitive such as `waitUntil`; otherwise
  preserve the current awaited behavior.
- Change production LLM logs to compact metadata/timing by default. Make full prompt/response
  excerpts an explicit development diagnostic. Avoid per-call full-payload
  serialization/open/flush on the event-loop path; use a bounded logger/worker strategy that
  cannot fail the turn.
- Propagate one request deadline/remaining budget across console, adapter, Python, Gateway, and
  read-only tools instead of relying only on nested 55s/45s/15s/5s caps. Derive hard budgets
  from the baseline and expose route-specific configuration.
- Preserve the write rule: cancellation/timeout before dispatch is safe; after dispatch is
  `uncertain`, never automatically retried. Manual retry must reuse the logical idempotency key
  when replay is intended.
- Confirm warm connection reuse before tuning pools: Python already shares one lifespan
  `httpx.AsyncClient`, and Node uses runtime-global `fetch`. Do not add connection complexity
  without evidence.

Representative files:

- `adapters/edgeone/agents/ai-home/index.ts`
- `src/mijia_agent/llm_log.py`
- `src/mijia_agent/app.py`
- `mijia-web-console/lib/ai/web-chat/agent-client.ts`
- `mijia-web-console/lib/ai/web-chat/web-api-boundary.ts`

## Rollout and change boundaries

Use small, independently reversible PRs/commits:

1. **Observability only** — one PR per repository; no behavior change.
2. **Authorization proof contract** — console verifier first, agent dual support second,
   console issuer last; feature flag defaults off/shadow.
3. **Deterministic routing** — adapter shadow/read-only canary plus a Python exact-activation
   fast path that reuses `explicit_activation`; physical execution remains closed until the
   durable executor gate is complete.
4. **Prompt cleanup** — separate from model selection and separate from routing.
5. **Home-status budgets/discovery** — console-only, with unchanged public schema.
6. **Tail/deadline work** — only items justified by measured contribution.

Provisional warm-path rollout gates (refine after the baseline):

- Exact list/activation-intent routes: p50 ≤ 1.5s, p95 ≤ 3s.
- LLM fallback: p50 ≤ 5.5s, p95 ≤ 7s end to end.
- Warm home status: p95 ≤ 5s; cold status reported separately.
- One `listHomes` call per browser turn after proof rollout.
- At least 30% prompt-token reduction on the supplied 19-scene shape with no routing-eval
  regression.
- Error-rate increase ≤ 0.5 percentage points.
- Correctness/security gates: zero false activations, zero cross-home/principal proof
  acceptance, unchanged idempotency conflict/replay behavior, and zero automatic retries of
  ambiguous writes.

Roll out shadow → 5% → 25% → 100%, with request-path counters and kill switches. Keep cold and
warm percentiles separate.

## Verification

### Automated

- Console: `npm run test:unit -- tests/ai-web-chat.test.mjs tests/ai-remote-tools.test.mjs
  tests/abort-signals.test.mjs` plus focused home-environment tests; then `npm run typecheck`,
  `npm run lint`, `npm test`.
- Agent: focused `pytest` for service/Gateway/tool contracts and `npm test --prefix
  adapters/edgeone`; then `pytest -q`, `ruff check src tests`, and `ruff format --check src
  tests`.
- Add proof tests for expiry, signature, audience, request/principal/home/scope/binding
  mismatch, fallback behavior, and cross-home rejection.
- Add deterministic-routing tests for the adversarial phrase matrix and verify the Gateway is
  not called on accepted fast paths.
- Add cancellation tests proving read work stops, write ambiguity becomes `uncertain`, and no
  retry occurs.
- Add deterministic fake-latency benchmarks for 0/10/19/50/100 scenes, 0/6/12 history messages,
  cold/warm markers, and concurrency 1/5/20. These must not contact Xiaomi or a paid model.

### Staged environment

- Record authoritative regions/model/config and compare first-after-idle, second-immediate, and
  20 warm requests for each intent.
- Verify timing logs join by `requestId`, contain no secrets/identifiers/user text, and
  reconcile with client-observed total duration.
- Confirm proof rollout reduces three pre-model home lookups to one without changing 401/403
  behavior.
- Confirm scene responses, home-status sanitization, receipt replay, history isolation, and
  preview behavior remain unchanged.
