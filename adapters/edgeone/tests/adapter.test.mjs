import test from "node:test";
import assert from "node:assert/strict";
import { onRequest } from "../agents/ai-home/index.ts";
import { onRequest as deleteConversation } from "../agents/ai-home/delete.ts";
import { digest } from "../agents/ai-home/shared.ts";

function fixture() {
  const state = new Map();
  const history = new Map();
  const env = { AI_AGENT_INTERNAL_SECRET: "fake-agent-secret-".repeat(3), AI_PYTHON_INTERNAL_SECRET: "fake-python-secret-".repeat(3), AI_TOOLS_INTERNAL_SECRET: "fake-tools-secret-".repeat(3), MIJIA_CONSOLE_BASE_URL: "https://console.example", AI_PYTHON_BASE_URL: "https://python.example" };
  const context = { env, conversation_id: "conv_test_123", request: { method: "POST", headers: { Authorization: `Bearer ${env.AI_AGENT_INTERNAL_SECRET}` }, body: { requestId: "req_example_000001", principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"], sessionBinding: "opaque-test-binding", message: "查看可用场景", idempotencyKey: "idem_test_example_1" } }, store: {
    state: { get: async key => state.get(key) ?? null, set: async (key, value) => state.set(key, value) },
    getMessages: async ({ conversationId }) => history.get(conversationId) ?? [],
    appendMessage: async ({ conversationId, role, content }) => history.set(conversationId, [...(history.get(conversationId) ?? []), { role, content }]),
    // Runtime-shaped Makers contract: object input, void on success, MemoryNotFoundError when absent.
    deleteConversation: async ({ conversationId }) => {
      if (!history.has(conversationId)) {
        const error = new Error(`conversation ${String(conversationId)} not found`);
        error.code = "MemoryNotFoundError";
        throw error;
      }
      history.delete(conversationId);
    },
  } };
  return { context, state, history };
}

test("Makers adapter forwards a bounded turn, stores history, and replays without a new model call", async t => {
  const { context, history } = fixture();
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true });
    calls++;
    const body = JSON.parse(options.body);
    assert.equal(body.sessionBinding, "opaque-test-binding");
    assert.equal(body.history.length, 0);
    return Response.json({ requestId: body.requestId, conversationId: body.conversationId, message: "场景列表", intent: "list_scenes", usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15, estimated: false } });
  });
  assert.equal((await onRequest(context)).status, 200);
  context.request.body.requestId = "req_example_000002";
  const replay = await (await onRequest(context)).json();
  assert.equal(replay.requestId, "req_example_000002");
  assert.equal(replay.usage.totalTokens, 0);
  assert.equal(calls, 1);
  assert.equal([...history.values()][0].length, 2);
  context.request.body.message = "different command";
  assert.equal((await onRequest(context)).status, 409);
});

test("uncertain upstream outcome never automatically reruns", async t => {
  const { context } = fixture();
  let calls = 0;
  t.mock.method(globalThis, "fetch", async url => {
    if (url.includes("console.example")) return Response.json({ ok: true });
    calls++;
    throw new Error("timeout with upstream secret");
  });
  assert.equal((await onRequest(context)).status, 409);
  assert.equal((await onRequest(context)).status, 409);
  assert.equal(calls, 1);
});

test("structured homeStatus survives forwarding, receipt storage, and replay without entering history", async t => {
  const { context, history } = fixture();
  const homeStatus = {
    capturedAt: "2026-09-20T08:00:00Z",
    completeness: "partial",
    groups: [{ metric: "temperature", label: "温度", unit: "°C", latest: { value: 25.5, unit: "°C", sourceLabel: "客厅温湿度计", roomName: "客厅", capturedAt: "2026-09-20T08:00:00Z", freshness: "fresh" }, readings: [] }],
    warnings: ["部分设备读取失败"],
  };
  const pythonResult = { requestId: "req_example_000001", conversationId: "conv_test_123", message: "已读取当前家庭环境状态。", intent: "get_home_status", homeStatus, usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15, estimated: false } };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true });
    return Response.json({ ...pythonResult, requestId: JSON.parse(options.body).requestId });
  });
  const first = await (await onRequest(context)).json();
  assert.deepEqual(first.homeStatus, homeStatus);
  // Only the generic message enters conversation history, never the structured readings.
  assert.equal([...history.values()][0][1].content, "已读取当前家庭环境状态。");
  context.request.body.requestId = "req_example_000002";
  const replay = await (await onRequest(context)).json();
  assert.deepEqual(replay.homeStatus, homeStatus);
  assert.equal(replay.requestId, "req_example_000002");
  assert.equal(replay.usage.totalTokens, 0);
});

test("finalized upstream failure replay retains known model usage", async t => {
  const { context, history } = fixture();
  let calls = 0;
  t.mock.method(globalThis, "fetch", async url => {
    if (url.includes("console.example")) return Response.json({ ok: true });
    calls++;
    return Response.json({
      code: "AI_GATEWAY_RATE_LIMITED",
      message: "AI 请求未完成，请检查状态后重试。",
      requestId: context.request.body.requestId,
      usage: { promptTokens: 11, completionTokens: 3, totalTokens: 14, estimated: false },
    }, { status: 429 });
  });

  assert.equal((await onRequest(context)).status, 429);
  context.request.body.requestId = "req_example_000002";
  const replay = await (await onRequest(context)).json();
  assert.equal(replay.requestId, "req_example_000002");
  assert.deepEqual(replay.usage, { promptTokens: 11, completionTokens: 3, totalTokens: 14, estimated: false });
  assert.equal(calls, 1);
  assert.equal(history.size, 0);
});

test("failed binding authorization prevents memory reads", async t => {
  const { context } = fixture();
  t.mock.method(globalThis, "fetch", async () => Response.json({ code: "AI_UNAUTHENTICATED" }, { status: 401 }));
  const spy = t.mock.method(context.store, "getMessages");
  assert.equal((await onRequest(context)).status, 401);
  assert.equal(spy.mock.callCount(), 0);
});

test("delete of absent history is idempotent and keeps receipts", async t => {
  const { context, state } = fixture();
  state.set("receipt", { status: "uncertain" });
  t.mock.method(globalThis, "fetch", async () => Response.json({ ok: true }));
  const response = await (await deleteConversation(context)).json();
  assert.equal(response.ok, true);
  assert.equal(response.deleted, false);
  assert.ok(state.has("receipt"));
});

test("delete removes only this principal's scoped history and repeats idempotently", async t => {
  const { context, history } = fixture();
  const scoped = `agent_${(await digest("conv_test_123:usr_test:home-test")).slice(0, 24)}`;
  const other = `agent_${(await digest("conv_test_123:usr_intruder:home-test")).slice(0, 24)}`;
  history.set(scoped, [{ role: "user", content: "查看可用场景" }]);
  history.set(other, [{ role: "user", content: "别的用户" }]);
  t.mock.method(globalThis, "fetch", async () => Response.json({ ok: true }));
  const first = await (await deleteConversation(context)).json();
  assert.equal(first.ok, true);
  assert.equal(first.deleted, true);
  assert.equal(first.requestId, "req_example_000001");
  assert.equal(history.has(scoped), false);
  assert.equal(history.has(other), true);
  const second = await (await deleteConversation(context)).json();
  assert.deepEqual(second, { ok: true, requestId: "req_example_000001", deleted: false });
});

test("internal requests require correct service secret", async () => {
  const { context } = fixture();
  context.request.headers.Authorization = "Bearer wrong";
  assert.equal((await onRequest(context)).status, 401);
});
