import { digest, failure, json, serviceUrl, type Context } from "../ai-home/shared.ts";

function requiredHeader(context: Context, name: string) {
  return Object.entries(context.request.headers ?? {}).find(([key]) => key.toLowerCase() === name)?.[1] ?? "";
}

export async function onRequest(context: Context) {
  try {
    if ((context.request.method ?? "POST") !== "POST") throw new Error("AI_INVALID_REQUEST");
    const secret = context.env.AI_AGENT_INTERNAL_SECRET;
    if (!secret || secret.length < 32
      || await digest(requiredHeader(context, "authorization")) !== await digest(`Bearer ${secret}`)) {
      throw new Error("AI_UNAUTHENTICATED");
    }
    const body = context.request.body;
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("AI_INVALID_REQUEST");
    const raw = JSON.stringify(body);
    if (raw.length > 65536 || /"(?:homeId|principalId|did|deviceId|automationToken)"\s*:/.test(raw)) {
      throw new Error("AI_INVALID_REQUEST");
    }
    const pythonSecret = context.env.AI_PYTHON_INTERNAL_SECRET;
    if (!pythonSecret || pythonSecret.length < 32) throw new Error("AI_AGENT_UNAVAILABLE");
    const response = await fetch(serviceUrl(
      context.env.AI_PYTHON_BASE_URL,
      "/internal/v1/climate-profile/derive",
      context.env.AI_ENVIRONMENT === "development",
    ), {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(15000),
      headers: { Authorization: `Bearer ${pythonSecret}`, "Content-Type": "application/json" },
      body: raw,
    });
    const result = await response.text();
    if (!result || result.length > 65536) throw new Error("AI_AGENT_UNAVAILABLE");
    let parsed: unknown;
    try { parsed = JSON.parse(result); } catch { throw new Error("AI_AGENT_UNAVAILABLE"); }
    return json(parsed, response.status);
  } catch (error) {
    return failure(error);
  }
}
