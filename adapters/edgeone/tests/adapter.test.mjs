import test from "node:test";
import assert from "node:assert/strict";
import { onRequest } from "../agents/ai-home/index.ts";
import { onRequest as deleteConversation } from "../agents/ai-home/delete.ts";

function fixture() {
  const state = new Map();
  const history = new Map();
  const env = { AI_AGENT_INTERNAL_SECRET: "fake-agent-secret-".repeat(3), AI_PYTHON_INTERNAL_SECRET: "fake-python-secret-".repeat(3), AI_TOOLS_INTERNAL_SECRET: "fake-tools-secret-".repeat(3), MIJIA_CONSOLE_BASE_URL: "https://console.example", AI_PYTHON_BASE_URL: "https://python.example" };
  const context = { env, conversation_id: "conv_test_123", request: { method: "POST", headers: { Authorization: `Bearer ${env.AI_AGENT_INTERNAL_SECRET}` }, body: { requestId: "req_example_000001", principalId: "usr_test", homeId: "home-test", scopes: ["ai:chat"], sessionBinding: "opaque-test-binding", message: "查看可用场景", idempotencyKey: "idem_test_example_1" } }, store: {
    state: { get: async key => state.get(key) ?? null, set: async (key, value) => state.set(key, value) },
    getMessages: async ({ conversationId }) => history.get(conversationId) ?? [],
    appendMessage: async ({ conversationId, role, content }) => history.set(conversationId, [...(history.get(conversationId) ?? []), { role, content }]),
    deleteConversation: async key => history.delete(key),
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

test("failed binding authorization prevents memory reads", async t => {
  const { context } = fixture();
  t.mock.method(globalThis, "fetch", async () => Response.json({ code: "AI_UNAUTHENTICATED" }, { status: 401 }));
  const spy = t.mock.method(context.store, "getMessages");
  assert.equal((await onRequest(context)).status, 401);
  assert.equal(spy.mock.callCount(), 0);
});

test("delete does not erase execution receipts", async t => {
  const { context, state } = fixture();
  state.set("receipt", { status: "uncertain" });
  t.mock.method(globalThis, "fetch", async () => Response.json({ ok: true }));
  assert.equal((await deleteConversation(context)).status, 200);
  assert.ok(state.has("receipt"));
});

test("internal requests require correct service secret", async () => {
  const { context } = fixture();
  context.request.headers.Authorization = "Bearer wrong";
  assert.equal((await onRequest(context)).status, 401);
});
