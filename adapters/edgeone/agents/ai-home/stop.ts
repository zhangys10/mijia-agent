import { authorize, failure, json, type Context } from "./shared.ts";

export async function onRequest(context: Context) {
  try {
    const input = await authorize(context);
    if (input.body.conversation_id !== input.conversationId) throw new Error("AI_INVALID_REQUEST");
    if (!context.utils?.abortActiveRun) return json({ code: "AI_AGENT_STOP_UNAVAILABLE" }, 503);
    return json({ ok: true, stopped: await context.utils.abortActiveRun(input.conversationId) });
  } catch (error) { return failure(error); }
}
