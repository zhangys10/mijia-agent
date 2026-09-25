import test from "node:test";
import assert from "node:assert/strict";
import { onRequest } from "../agents/ai-home/index.ts";
import { onRequest as deleteConversation } from "../agents/ai-home/delete.ts";
import { digest } from "../agents/ai-home/shared.ts";

function fixture() {
  const state = new Map();
  const history = new Map();
  const env = { AI_AGENT_INTERNAL_SECRET: "fake-agent-secret-".repeat(3), AI_PYTHON_INTERNAL_SECRET: "fake-python-secret-".repeat(3), AI_TOOLS_INTERNAL_SECRET: "fake-tools-secret-".repeat(3), MIJIA_CONSOLE_BASE_URL: "https://console.example", AI_PYTHON_BASE_URL: "https://python.example" };
  const context = { env, conversation_id: "conv_test_123", request: { method: "POST", headers: { Authorization: `Bearer ${env.AI_AGENT_INTERNAL_SECRET}` }, body: { requestId: "req_example_000001", principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"], automationToken: "opaque-test-token", message: "查看可用场景", idempotencyKey: "idem_test_example_1" } }, store: {
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
    if (url.includes("console.example")) {
      assert.equal(options.headers["X-Ai-User-Token"], "opaque-test-token");
      assert.deepEqual(JSON.parse(options.body), {
        requestId: context.request.body.requestId,
        tool: "authorize",
        arguments: {},
      });
      return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    }
    calls++;
    const body = JSON.parse(options.body);
    assert.equal(body.automationToken, "opaque-test-token");
    assert.equal(body.history.length, 0);
    return Response.json({ requestId: body.requestId, conversationId: body.conversationId, message: "场景列表", historyAnswer: "Answered the user's previous request using a fresh lookup.", intent: "list_scenes", usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15, estimated: false } });
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
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    calls++;
    throw new Error("timeout with upstream secret");
  });
  assert.equal((await onRequest(context)).status, 409);
  assert.equal((await onRequest(context)).status, 409);
  assert.equal(calls, 1);
});

test("memory append failure after a successful turn neither fails the reply nor poisons the receipt", async t => {
  const { context, history } = fixture();
  let appendCalls = 0;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    return Response.json({ requestId: JSON.parse(options.body).requestId, conversationId: "conv_test_123", message: "回复", historyAnswer: "回复", intent: "none" });
  });
  const original = context.store.appendMessage;
  // First append throws (simulating a store blip), the retry path recovers.
  context.store.appendMessage = async params => {
    appendCalls++;
    if (appendCalls === 1) throw new Error("memory store unavailable");
    return original(params);
  };
  const ok = await onRequest(context);
  assert.equal(ok.status, 200);
  // The receipt is completed with the real result, so a same-key replay returns 200, not 409.
  context.request.body.requestId = "req_example_000002";
  const replay = await (await onRequest(context)).json();
  assert.equal(replay.requestId, "req_example_000002");
  assert.equal((replay.usage?.totalTokens) ?? 0, 0);
});

test("model history appends each completed turn in user then assistant order", async t => {
  const { context } = fixture();
  const roles = [];
  context.store.appendMessage = async ({ role }) => {
    if (role === "user") await new Promise(resolve => setTimeout(resolve, 10));
    roles.push(role);
  };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    const body = JSON.parse(options.body);
    return Response.json({ requestId: body.requestId, conversationId: body.conversationId, message: "Done.", historyAnswer: "Done.", intent: "none" });
  });
  assert.equal((await onRequest(context)).status, 200);
  assert.deepEqual(roles, ["user", "assistant"]);
});

test("unparseable upstream body is a finalized 502 that replays as 502, not 409", async t => {
  const { context, state, history } = fixture();
  let calls = 0;
  t.mock.method(globalThis, "fetch", async url => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    calls++;
    // Runtime error pages (e.g. module load failure) arrive as HTML with a 404/502.
    return new Response("<html>Error loading module</html>", { status: 404 });
  });
  const response = await onRequest(context);
  assert.equal(response.status, 502);
  assert.equal((await response.json()).code, "AI_AGENT_UNAVAILABLE");
  const receipt = state.get("idem_" + await digest(JSON.stringify(["usr_test", "home-test", "idem_test_example_1"])));
  assert.equal(receipt.status, "completed");
  assert.equal(receipt.httpStatus, 502);
  assert.equal(receipt.result.code, "AI_AGENT_UNAVAILABLE");
  assert.equal(history.size, 0);
  // Same key replays the finalized failure instead of re-running the turn.
  const replay = await (await onRequest(context)).json();
  assert.equal(replay.code, "AI_AGENT_UNAVAILABLE");
  assert.equal(calls, 1);
});

test("confirmed 200 with a broken response contract stays uncertain and never reruns", async t => {
  const { context } = fixture();
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    calls++;
    // 200 whose conversationId does not match ours: the turn ran upstream, its outcome is unreadable.
    return Response.json({ requestId: JSON.parse(options.body).requestId, conversationId: "cv_someone_else", message: "回复" });
  });
  assert.equal((await onRequest(context)).status, 409);
  context.request.body.requestId = "req_example_000002";
  assert.equal((await onRequest(context)).status, 409);
  assert.equal(calls, 1);
});

test("failed upstream status with a broken body finalizes as 502 and replays", async t => {
  const { context } = fixture();
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    calls++;
    return new Response("upstream exploded", { status: 500 });
  });
  assert.equal((await onRequest(context)).status, 502);
  context.request.body.requestId = "req_example_000002";
  assert.equal((await onRequest(context)).status, 502);
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
  const pythonResult = { requestId: "req_example_000001", conversationId: "conv_test_123", message: "客厅温度当前为 25.5°C。", historyAnswer: "Answered the user's previous request using a fresh lookup.", intent: "get_home_status", homeStatus, usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15, estimated: false } };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    return Response.json({ ...pythonResult, requestId: JSON.parse(options.body).requestId });
  });
  const first = await (await onRequest(context)).json();
  assert.deepEqual(first.homeStatus, homeStatus);
  assert.equal(first.message, "客厅温度当前为 25.5°C。");
  assert.equal([...history.values()][0][1].content, pythonResult.historyAnswer);
  context.request.body.requestId = "req_example_000002";
  const replay = await (await onRequest(context)).json();
  assert.deepEqual(replay.homeStatus, homeStatus);
  assert.equal(replay.requestId, "req_example_000002");
  assert.equal(replay.usage.totalTokens, 0);
});

test("structured deviceStatus survives forwarding, receipt storage, and replay without entering history", async t => {
  const { context, history } = fixture();
  const deviceStatus = {
    capturedAt: "2026-09-21T08:00:00Z",
    completeness: "complete",
    poweredOn: 1,
    rooms: [{ room: "客厅", items: [{ name: "客厅吸顶灯", kind: "light", state: "on", online: true }] }],
    warnings: [],
  };
  const pythonResult = { requestId: "req_example_000001", conversationId: "conv_test_123", message: "客厅吸顶灯当前已开启。", historyAnswer: "Answered the user's previous request using a fresh lookup.", intent: "get_device_status", deviceStatus, usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15, estimated: false } };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    return Response.json({ ...pythonResult, requestId: JSON.parse(options.body).requestId });
  });
  const first = await (await onRequest(context)).json();
  assert.deepEqual(first.deviceStatus, deviceStatus);
  assert.equal(first.message, "客厅吸顶灯当前已开启。");
  assert.equal([...history.values()][0][1].content, pythonResult.historyAnswer);
  context.request.body.requestId = "req_example_000002";
  const replay = await (await onRequest(context)).json();
  assert.deepEqual(replay.deviceStatus, deviceStatus);
  assert.equal(replay.requestId, "req_example_000002");
  assert.equal(replay.usage.totalTokens, 0);
});

test("home read history stays redacted while later turns retain conversation context", async t => {
  const { context, history } = fixture();
  const forwardedHistories = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
    const body = JSON.parse(options.body);
    forwardedHistories.push(body.history);
    return Response.json({ requestId: body.requestId, conversationId: body.conversationId,
      message: body.message === "查看客厅温度" ? "客厅温度为 25°C。" : "上海今天晴。",
      historyAnswer: body.message === "查看客厅温度" ? "Answered the user's previous request using a fresh lookup." : "上海今天晴。",
      intent: body.message === "查看客厅温度" ? "get_home_status" : "none",
      ...(body.message === "查看客厅温度" ? { homeStatus: { completeness: "complete" } } : {}) });
  });
  context.request.body.message = "查看客厅温度";
  assert.equal((await onRequest(context)).status, 200);
  context.request.body.requestId = "req_example_000002";
  context.request.body.message = "上海天气怎么样？";
  context.request.body.idempotencyKey = "idem_test_example_2";
  assert.equal((await onRequest(context)).status, 200);
  assert.deepEqual(forwardedHistories, [[], [
    { role: "user", content: "查看客厅温度" },
    { role: "assistant", content: "Answered the user's previous request using a fresh lookup." },
  ]]);
  assert.equal(history.size, 1);
});

test("finalized upstream failure replay retains known model usage", async t => {
  const { context, history } = fixture();
  let calls = 0;
  t.mock.method(globalThis, "fetch", async url => {
    if (url.includes("console.example")) return Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] });
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
  t.mock.method(globalThis, "fetch", async () => Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] }));
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
  t.mock.method(globalThis, "fetch", async () => Response.json({ ok: true, principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"] }));
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
