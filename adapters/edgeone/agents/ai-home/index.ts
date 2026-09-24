import { authorize, digest, failure, json, serviceUrl, type Context } from "./shared.ts";

type Receipt = { hash: string; status: "processing" | "completed" | "uncertain"; result?: Record<string, unknown>; httpStatus?: number };
// Optimizes same-process serialization only; durable atomic claim is a production gate.
const active = new Set<string>();

export async function onRequest(context: Context) {
  let lock: string | undefined;
  try {
    const input = await authorize(context);
    const { body, store, scopedId, conversationId, requestId } = input;
    const message = body.message;
    const key = body.idempotencyKey;
    if (typeof message !== "string" || !message.trim() || message.length > 500
      || typeof key !== "string" || key.length < 16 || key.length > 128) throw new Error("AI_INVALID_REQUEST");
    const fingerprint = await digest(JSON.stringify([input.principalId, input.homeId, conversationId, message, [...input.scopes].sort(), body.locale ?? "zh-CN", body.timezone ?? "Asia/Shanghai", body.channel ?? "web"]));
    const receiptKey = `idem_${await digest(JSON.stringify([input.principalId, input.homeId, key]))}`;
    lock = scopedId;
    if (active.has(lock)) { lock = undefined; throw new Error("AI_REQUEST_IN_PROGRESS"); }
    active.add(lock);
    const previous = await store.state.get<Receipt>(receiptKey);
    if (previous) {
      if (previous.hash !== fingerprint) throw new Error("AI_IDEMPOTENCY_CONFLICT");
      if (previous.status === "completed" && previous.result) {
        const failed = (previous.httpStatus ?? 200) < 200 || (previous.httpStatus ?? 200) >= 300;
        const replayUsage = failed
          ? previous.result.usage
          : { promptTokens: 0, completionTokens: 0, totalTokens: 0, estimated: false };
        // Successful replays make no new model call. Finalized failures retain known usage.
        return json({ ...previous.result, requestId, usage: replayUsage }, previous.httpStatus ?? 200);
      }
      throw new Error(previous.status === "processing" ? "AI_REQUEST_IN_PROGRESS" : "AI_EXECUTION_STATUS_UNKNOWN");
    }
    const pythonSecret = context.env.AI_PYTHON_INTERNAL_SECRET;
    if (!pythonSecret || pythonSecret.length < 32) throw new Error("AI_AGENT_UNAVAILABLE");
    const url = serviceUrl(context.env.AI_PYTHON_BASE_URL, "/internal/v1/assistant", context.env.AI_ENVIRONMENT === "development");
    const history = (await store.getMessages({ conversationId: scopedId, limit: 12, order: "asc" }))
      .filter(m => m.role === "user" || m.role === "assistant")
      .slice(-12).map(m => ({ role: m.role, content: m.content.slice(0, 2000) }));
    await store.state.set(receiptKey, { hash: fingerprint, status: "processing" });
    // Only transport ambiguity (fetch failure, timeout, unreadable body) may mark the
    // receipt uncertain. Everything after the response is readable has a known outcome.
    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST", redirect: "error",
        signal: AbortSignal.any([AbortSignal.timeout(45000), ...(context.request.signal ? [context.request.signal] : [])]),
        headers: { Authorization: `Bearer ${pythonSecret}`, "Content-Type": "application/json" },
        body: JSON.stringify({ requestId, conversationId, principalId: input.principalId,
          homeId: input.homeId, scopes: input.scopes, automationToken: input.automationToken,
          message, idempotencyKey: key, locale: body.locale ?? "zh-CN", timezone: body.timezone ?? "Asia/Shanghai",
          channel: body.channel ?? "web", history }),
      });
    } catch {
      // Timeout/cancellation may happen after a physical effect. Never automatically replay.
      await store.state.set(receiptKey, { hash: fingerprint, status: "uncertain" });
      throw new Error("AI_EXECUTION_STATUS_UNKNOWN");
    }
    const raw = await response.text();
    // Every finalized read failure (empty, oversized, unparseable) writes a completed
    // 502 receipt rather than leaving "processing", so retries replay instead of deadlocking.
    if (raw.length > 65536 || raw.length === 0) {
      await store.state.set(receiptKey, { hash: fingerprint, status: "completed", result: { code: "AI_AGENT_UNAVAILABLE" }, httpStatus: 502 });
      throw new Error("AI_AGENT_UNAVAILABLE");
    }
    let result: Record<string, unknown>;
    try { result = JSON.parse(raw) as Record<string, unknown>; }
    catch {
      // An unparseable body is a finalized upstream failure, not an unknown effect.
      await store.state.set(receiptKey, { hash: fingerprint, status: "completed", result: { code: "AI_AGENT_UNAVAILABLE" }, httpStatus: 502 });
      throw new Error("AI_AGENT_UNAVAILABLE");
    }
    const contractBroken = result.requestId !== requestId
      || (response.ok && (result.conversationId !== conversationId || typeof result.message !== "string"));
    if (contractBroken) {
      // A failed upstream status proves no effect, so the retry is safe. A 200 whose
      // body we cannot trust means the turn ran but its outcome is unreadable: uncertain.
      if (response.ok) {
        await store.state.set(receiptKey, { hash: fingerprint, status: "uncertain" });
        throw new Error("AI_EXECUTION_STATUS_UNKNOWN");
      }
      await store.state.set(receiptKey, { hash: fingerprint, status: "completed", result: { code: "AI_AGENT_UNAVAILABLE" }, httpStatus: 502 });
      throw new Error("AI_AGENT_UNAVAILABLE");
    }
    // Persist the outcome before memory writes; append failure must not rerun the tool.
    await store.state.set(receiptKey, { hash: fingerprint, status: "completed", result, httpStatus: response.status });
    if (response.ok) {
      // The turn already succeeded; a history write failing must not fail the reply.
      const historyAnswer = result.homeStatus !== undefined || result.intent === "get_home_status"
        ? "Answered the user's current home environment question."
        : result.deviceStatus !== undefined || result.intent === "get_device_status"
          ? "Answered the user's current device status question."
          : String(result.message);
      const remembered = await Promise.allSettled([
        store.appendMessage({ conversationId: scopedId, role: "user", content: message }),
        store.appendMessage({ conversationId: scopedId, role: "assistant", content: historyAnswer }),
      ]);
      for (const settled of remembered) {
        if (settled.status === "rejected") console.error("[ai-home] history append failed after successful turn", settled.reason instanceof Error ? settled.reason.message : settled.reason);
      }
    }
    return json(result, response.status);
  } catch (error) { return failure(error); }
  finally { if (lock) active.delete(lock); }
}
