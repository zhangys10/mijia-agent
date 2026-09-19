import { authorize, failure, json, type Context } from "./shared.ts";

export async function onRequest(context: Context) {
  try {
    const input = await authorize(context);
    // Deliberately retain state receipts: clearing memory must not enable replay.
    try {
      await input.store.deleteConversation({ conversationId: input.scopedId });
      return json({ ok: true, requestId: input.requestId, deleted: true });
    } catch (error) {
      // Makers raises MemoryNotFoundError only when the conversation is absent: already deleted.
      if (error instanceof Error && (error as Error & { code?: string }).code === "MemoryNotFoundError") {
        return json({ ok: true, requestId: input.requestId, deleted: false });
      }
      throw error;
    }
  } catch (error) { return failure(error); }
}
