import { authorize, failure, json, type Context } from "./shared.ts";

export async function onRequest(context: Context) {
  try {
    const input = await authorize(context);
    // Deliberately retain state receipts: clearing memory must not enable replay.
    return json({ ok: true, requestId: input.requestId, deleted: await input.store.deleteConversation(input.scopedId) });
  } catch (error) { return failure(error); }
}
