import json
import time

import httpx

from .config import Settings
from .llm_log import LlmCallLogger
from .models import AgentError, Usage

LOG_EXCERPT_CONTENT = 2000
LOG_EXCERPT_ARGUMENTS = 500


def parse_usage(body: dict, request: dict, response: str) -> Usage:
    raw = body.get("usage")
    if isinstance(raw, dict):
        values = [raw.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")]
        if all(type(v) is int and v >= 0 for v in values) and values[2] == sum(values[:2]):
            return Usage(promptTokens=values[0], completionTokens=values[1], totalTokens=values[2])
    prompt = len(json.dumps(request, ensure_ascii=False).encode())
    completion = len(response.encode())
    return Usage(
        promptTokens=prompt,
        completionTokens=completion,
        totalTokens=prompt + completion,
        estimated=True,
    )


def response_excerpt(body: object) -> dict:
    """Bounded content/toolCalls excerpt for logging; never raises."""
    try:
        message = body["choices"][0]["message"]  # type: ignore[index]
        calls = message.get("tool_calls") or []
        return {
            "content": str(message.get("content") or "")[:LOG_EXCERPT_CONTENT],
            "toolCalls": [
                {
                    "name": str((call.get("function") or {}).get("name") or ""),
                    "arguments": str((call.get("function") or {}).get("arguments") or "")[
                        :LOG_EXCERPT_ARGUMENTS
                    ],
                }
                for call in calls
                if isinstance(call, dict)
            ],
        }
    except (KeyError, IndexError, TypeError, AttributeError):
        return {}


class Gateway:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        logger: LlmCallLogger | None = None,
    ):
        self.settings, self.client = settings, client
        self.logger = logger or LlmCallLogger(settings.llm_log_path)

    async def chat(
        self, request: dict, context: dict, *, log_content: bool = True
    ) -> tuple[dict, Usage]:
        """One model call through the Makers Gateway; every call is logged.

        ``context`` carries non-secret identifiers (requestId, source, optional
        home name) — never principal IDs or secrets, which never reach payloads.
        """
        started = time.monotonic()
        try:
            response = await self.client.post(
                self.settings.gateway_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + self.settings.gateway_key},
                json=request,
                timeout=self.settings.timeout_ms / 1000,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            self._log_failure(context, "AI_GATEWAY_TIMEOUT", started)
            raise AgentError("AI_GATEWAY_TIMEOUT", 504) from None
        except httpx.HTTPError:
            self._log_failure(context, "AI_GATEWAY_UNAVAILABLE", started)
            raise AgentError("AI_GATEWAY_UNAVAILABLE") from None
        if response.status_code == 429:
            self._log_failure(context, "AI_GATEWAY_RATE_LIMITED", started)
            raise AgentError("AI_GATEWAY_RATE_LIMITED", 429)
        if response.status_code != 200 or len(response.content) > 65536:
            self._log_failure(context, "AI_GATEWAY_UNAVAILABLE", started)
            raise AgentError("AI_GATEWAY_UNAVAILABLE")
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise TypeError("not an object")
        except (TypeError, ValueError):
            self._log_failure(context, "AI_GATEWAY_RESPONSE_INVALID", started)
            raise AgentError("AI_GATEWAY_RESPONSE_INVALID") from None
        usage = parse_usage(body, request, response.text)
        excerpt = response_excerpt(body)
        logged_request = request
        logged_response = excerpt
        if not log_content:
            logged_request = {
                "model": request.get("model"),
                "messageCount": len(request.get("messages") or []),
                "toolNames": [
                    str((tool.get("function") or {}).get("name") or "")
                    for tool in request.get("tools") or []
                    if isinstance(tool, dict)
                ],
            }
            logged_response = {
                "contentLength": len(str(excerpt.get("content") or "")),
                "toolNames": [
                    str(call.get("name") or "")
                    for call in excerpt.get("toolCalls") or []
                    if isinstance(call, dict)
                ],
            }
        self.logger.log(
            {
                "event": "llm_call",
                "context": context,
                "request": logged_request,
                "response": logged_response,
                "usage": usage.model_dump(),
                "latencyMs": round((time.monotonic() - started) * 1000),
            }
        )
        return body, usage

    def _log_failure(self, context: dict, code: str, started: float) -> None:
        self.logger.log(
            {
                "event": "llm_call_failed",
                "context": context,
                "code": code,
                "latencyMs": round((time.monotonic() - started) * 1000),
            }
        )
