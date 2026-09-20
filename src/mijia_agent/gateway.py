import json
import time

import httpx

from .command_rules import (
    CHAT_TOOLS_ADDENDUM,
    SYSTEM_PROMPT,
    chat_tools,
    user_content,
)
from .config import Settings
from .llm_log import LlmCallLogger
from .models import AgentError, Decision, Scene, Turn, Usage

LOG_EXCERPT_CONTENT = 2000
LOG_EXCERPT_ARGUMENTS = 500


def payload(turn: Turn, scenes: list[Scene], settings: Settings) -> dict:
    """Chat-pipeline model request: prompt and tool schema from command_rules."""
    return {
        "model": settings.model,
        "temperature": 0,
        "enable_thinking": False,
        "max_tokens": settings.max_output_tokens,
        "tools": chat_tools(scenes, "scene:activate" in turn.scopes),
        "tool_choice": "auto",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT + CHAT_TOOLS_ADDENDUM}]
        + [m.model_dump() for m in turn.history]
        + [
            {
                "role": "user",
                "content": user_content(turn.message, turn.locale, turn.timezone, scenes),
            }
        ],
    }


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

    async def chat(self, request: dict, context: dict) -> tuple[dict, Usage]:
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
        self.logger.log(
            {
                "event": "llm_call",
                "context": context,
                "request": request,
                "response": response_excerpt(body),
                "usage": usage.model_dump(),
                "latencyMs": round((time.monotonic() - started) * 1000),
            }
        )
        return body, usage

    async def decide(self, turn: Turn, scenes: list[Scene]) -> Decision:
        request = payload(turn, scenes, self.settings)
        context = {
            "requestId": turn.requestId,
            "conversationId": turn.conversationId,
            "source": "internal_turn",
        }
        body, usage = await self.chat(request, context)
        try:
            message = body["choices"][0]["message"]
            calls = message.get("tool_calls", [])
            if not calls:
                return Decision(
                    tool="none", message=str(message.get("content") or "")[:2000], usage=usage
                )
            if len(calls) != 1 or calls[0].get("type") != "function":
                raise ValueError("unsupported calls")
            call = calls[0]["function"]
            args = json.loads(call["arguments"])
            if not isinstance(args, dict):
                raise TypeError("invalid arguments")
            if call["name"] == "list_scenes" and not args:
                return Decision(tool="list_scenes", usage=usage)
            if call["name"] == "get_home_status" and not args:
                return Decision(tool="get_home_status", usage=usage)
            if call["name"] == "activate_scene" and {"sceneId"} <= set(args) <= {
                "sceneId",
                "replyMessage",
            }:
                scene_id = args["sceneId"]
                reply = args.get("replyMessage")
                if scene_id not in {s.alias for s in scenes} or not isinstance(
                    reply if reply is not None else "", str
                ):
                    raise ValueError("unknown scene or reply")
                return Decision(
                    tool="activate_scene",
                    sceneId=scene_id,
                    replyMessage=str(reply or "").strip(),
                    usage=usage,
                )
            raise ValueError("unsupported tool")
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise AgentError("AI_GATEWAY_RESPONSE_INVALID", usage=usage) from None

    def _log_failure(self, context: dict, code: str, started: float) -> None:
        self.logger.log(
            {
                "event": "llm_call_failed",
                "context": context,
                "code": code,
                "latencyMs": round((time.monotonic() - started) * 1000),
            }
        )
