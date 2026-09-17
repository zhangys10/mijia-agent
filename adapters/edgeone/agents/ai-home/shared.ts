export type Context = {
  request: { body?: unknown; headers?: Record<string, string>; method?: string; signal?: AbortSignal };
  env: Record<string, string | undefined>;
  conversation_id?: string;
  store?: {
    getMessages(input: { conversationId: string; limit: number; order: "asc" }): Promise<Array<{ role: string; content: string }>>;
    appendMessage(input: { conversationId: string; role: string; content: string }): Promise<void>;
    deleteConversation(id: string): Promise<boolean>;
    state: { get<T>(key: string): Promise<T | null>; set(key: string, value: unknown): Promise<void> };
  };
  utils?: { abortActiveRun?: (id: string) => Promise<boolean> | boolean };
};

export function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}

export async function digest(value: string) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))), x => x.toString(16).padStart(2, "0")).join("");
}

function required(value: unknown, max = 128): string {
  if (typeof value !== "string" || !value.trim() || value.length > max) throw new Error("AI_INVALID_REQUEST");
  return value;
}

export function serviceUrl(value: string | undefined, path: string, development = false) {
  const url = new URL(required(value, 2048));
  const local = development && url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname);
  if ((!local && url.protocol !== "https:") || url.username || url.password || url.search || url.hash) throw new Error("AI_AGENT_UNAVAILABLE");
  return url.toString().replace(/\/$/, "") + path;
}

export async function authorize(context: Context) {
  if ((context.request.method ?? "POST") !== "POST") throw new Error("AI_INVALID_REQUEST");
  const secret = context.env.AI_AGENT_INTERNAL_SECRET;
  const entry = Object.entries(context.request.headers ?? {}).find(([key]) => key.toLowerCase() === "authorization");
  if (!secret || secret.length < 32) throw new Error("AI_UNAUTHENTICATED");
  const a = await digest(entry?.[1] ?? "");
  const b = await digest(`Bearer ${secret}`);
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  if (mismatch) throw new Error("AI_UNAUTHENTICATED");
  const conversationId = required(context.conversation_id, 36);
  if (!/^[A-Za-z0-9_.-]{6,36}$/.test(conversationId)) throw new Error("AI_INVALID_REQUEST");
  if (!context.store?.state) throw new Error("AI_AGENT_STORE_UNAVAILABLE");
  const raw = context.request.body;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("AI_INVALID_REQUEST");
  const body = raw as Record<string, unknown>;
  if (JSON.stringify(body).length > 32768) throw new Error("AI_INVALID_REQUEST");
  const principalId = required(body.principalId);
  const homeId = required(body.homeId, 100);
  const requestId = required(body.requestId);
  const sessionBinding = required(body.sessionBinding, 16384);
  const scopes = body.scopes;
  if (!Array.isArray(scopes) || !scopes.includes("ai:chat") || scopes.some(x => !["ai:chat", "scene:activate"].includes(x))) throw new Error("AI_INVALID_REQUEST");
  const toolSecret = context.env.AI_TOOLS_INTERNAL_SECRET;
  if (!toolSecret || toolSecret.length < 32) throw new Error("AI_AGENT_UNAVAILABLE");
  // Only the console has Xiaomi encryption keys. Validate before reading or deleting memory.
  const verified = await fetch(serviceUrl(context.env.MIJIA_CONSOLE_BASE_URL, "/api/ai/tools", context.env.AI_ENVIRONMENT === "development"), {
    method: "POST", redirect: "error", signal: AbortSignal.timeout(15000),
    headers: { Authorization: `Bearer ${toolSecret}`, "Content-Type": "application/json" },
    body: JSON.stringify({ principalId, homeId, requestId, sessionBinding, scopes, tool: "authorize", arguments: {} }),
  });
  if (!verified.ok) throw new Error("AI_UNAUTHENTICATED");
  const scopedId = `agent_${(await digest(`${conversationId}:${principalId}:${homeId}`)).slice(0, 24)}`;
  return { body, conversationId, scopedId, principalId, homeId, requestId, sessionBinding, scopes, store: context.store };
}

export function failure(error: unknown) {
  const code = error instanceof Error ? error.message : "";
  const statuses: Record<string, number> = {
    AI_UNAUTHENTICATED: 401, AI_INVALID_REQUEST: 400, AI_AGENT_STORE_UNAVAILABLE: 503,
    AI_IDEMPOTENCY_CONFLICT: 409, AI_REQUEST_IN_PROGRESS: 409, AI_EXECUTION_STATUS_UNKNOWN: 409,
  };
  return json({ code: Object.hasOwn(statuses, code) ? code : "AI_AGENT_UNAVAILABLE" }, statuses[code] ?? 502);
}
