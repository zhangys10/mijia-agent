import { createServer } from "node:http";
import { onRequest } from "../agents/ai-home/index.ts";
import { onRequest as onDelete } from "../agents/ai-home/delete.ts";

const host = "127.0.0.1";
const port = Number(process.env.LOCAL_AGENT_PORT ?? "8789");
if (process.env.AI_ENVIRONMENT !== "development") {
  throw new Error("LOCAL_AGENT_SERVER requires AI_ENVIRONMENT=development");
}
for (const name of [
  "AI_AGENT_INTERNAL_SECRET",
  "AI_PYTHON_INTERNAL_SECRET",
  "AI_TOOLS_INTERNAL_SECRET",
  "AI_PYTHON_BASE_URL",
  "MIJIA_CONSOLE_BASE_URL",
]) {
  if (!process.env[name]) throw new Error(`Missing ${name}`);
}

const state = new Map();
const history = new Map();

const store = {
  state: {
    async get(key) { return state.get(key) ?? null; },
    async set(key, value) { state.set(key, value); },
  },
  async getMessages({ conversationId, limit, order }) {
    if (order !== "asc") throw new Error("Unsupported local history order");
    return (history.get(conversationId) ?? []).slice(-limit);
  },
  async appendMessage({ conversationId, role, content }) {
    history.set(conversationId, [...(history.get(conversationId) ?? []), { role, content }]);
  },
  async deleteConversation({ conversationId }) {
    if (!history.has(conversationId)) {
      const error = new Error("conversation not found");
      error.code = "MemoryNotFoundError";
      throw error;
    }
    history.delete(conversationId);
  },
};

async function readBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 32768) throw new Error("AI_INVALID_REQUEST");
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw new Error("AI_INVALID_REQUEST"); }
}

const server = createServer(async (request, response) => {
  const route = request.url === "/ai-home" ? onRequest
    : request.url === "/ai-home/delete" ? onDelete
      : null;
  if (request.method !== "POST" || !route) {
    response.writeHead(404, { "Cache-Control": "no-store" }).end();
    return;
  }
  try {
    const body = await readBody(request);
    const headers = Object.fromEntries(
      Object.entries(request.headers).flatMap(([key, value]) =>
        typeof value === "string" ? [[key.toLowerCase(), value]] : []),
    );
    const result = await route({
      env: process.env,
      conversation_id: headers["makers-conversation-id"],
      request: { method: request.method, headers, body },
      store,
    });
    response.writeHead(result.status, Object.fromEntries(result.headers.entries()));
    response.end(await result.text());
  } catch (error) {
    const code = error instanceof Error ? error.message : "AI_AGENT_UNAVAILABLE";
    response.writeHead(code === "AI_INVALID_REQUEST" ? 400 : 500, {
      "Cache-Control": "no-store",
      "Content-Type": "application/json",
    });
    response.end(JSON.stringify({ code: code === "AI_INVALID_REQUEST" ? code : "AI_AGENT_UNAVAILABLE" }));
  }
});

server.listen(port, host, () => {
  console.log(`local Makers adapter shim listening on http://${host}:${port}`);
  console.log("Agent state/history use in-memory maps; no Makers service or model provider is contacted by this shim.");
});
