import json

from pydantic import ValidationError

from mijia_agent.gateway import Gateway
from mijia_assistant.models import (
    AssistantContext,
    AssistantError,
    ModelMessage,
    ModelTurn,
    ToolCall,
    Usage,
)


def normalize_message(raw: dict, usage: Usage, *, truncated: bool = False) -> ModelTurn:
    try:
        calls = []
        for index, item in enumerate(raw.get("tool_calls") or []):
            if item.get("type") != "function":
                raise ValueError("unsupported tool call")
            function = item["function"]
            raw_arguments = function.get("arguments") or {}
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments)
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise TypeError("arguments must be a JSON string or object")
            if not isinstance(arguments, dict):
                raise TypeError("arguments must be an object")
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=function["name"],
                    arguments=arguments,
                )
            )
        return ModelTurn(
            content=str(raw.get("content") or ""),
            tool_calls=calls,
            usage=usage,
            truncated=truncated,
        )
    except (KeyError, TypeError, ValidationError, ValueError):
        raise AssistantError("MODEL_RESPONSE_INVALID", 502) from None


class OpenAIStreamNormalizer:
    """Accumulate ordered chat-completion deltas into exactly one normalized turn."""

    def __init__(self):
        self._content: list[str] = []
        self._calls: dict[int, dict] = {}
        self._terminal = False
        self._truncated = False

    def push(self, chunk: dict) -> None:
        if self._terminal:
            raise AssistantError("MODEL_STREAM_INVALID", 502)
        try:
            choices = chunk.get("choices") or []
            if len(choices) > 1:
                raise ValueError("multiple choices")
            if not choices:
                return
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise TypeError("invalid content delta")
                self._content.append(content)
            for item in delta.get("tool_calls") or []:
                index = item["index"]
                if type(index) is not int or index < 0 or index > 7:
                    raise ValueError("invalid tool index")
                target = self._calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if item.get("id"):
                    target["id"] += item["id"]
                function = item.get("function") or {}
                target["function"]["name"] += function.get("name") or ""
                target["function"]["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason") is not None:
                self._terminal = True
                self._truncated = choice["finish_reason"] in {"length", "max_tokens"}
        except (KeyError, TypeError, ValueError):
            raise AssistantError("MODEL_STREAM_INVALID", 502) from None

    def finish(self, usage: Usage | None = None) -> ModelTurn:
        if not self._terminal:
            raise AssistantError("MODEL_STREAM_INCOMPLETE", 502)
        raw = {
            "content": "".join(self._content),
            "tool_calls": [self._calls[index] for index in sorted(self._calls)],
        }
        return normalize_message(raw, usage or Usage(estimated=True), truncated=self._truncated)


class OpenAICompatibleProvider:
    """Normalize the configured Makers OpenAI-compatible response into core events."""

    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    async def complete(
        self, messages: list[ModelMessage], tools: list[dict], ctx: AssistantContext
    ) -> ModelTurn:
        request = {
            "model": self.gateway.settings.model,
            "temperature": 0,
            "enable_thinking": False,
            "max_tokens": getattr(
                self.gateway.settings,
                "assistant_max_output_tokens",
                self.gateway.settings.max_output_tokens,
            ),
            "tool_choice": "auto",
            "tools": tools,
            "messages": [self._message(message) for message in messages],
        }
        body, legacy_usage = await self.gateway.chat(
            request,
            {
                "requestId": ctx.request_id,
                "conversationId": ctx.conversation_id,
                "source": "assistant_turn",
            },
            log_content=False,
        )
        try:
            choice = body["choices"][0]
            raw = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise AssistantError("MODEL_RESPONSE_INVALID", 502) from None
        return normalize_message(
            raw,
            Usage(
                prompt_tokens=legacy_usage.promptTokens,
                completion_tokens=legacy_usage.completionTokens,
                total_tokens=legacy_usage.totalTokens,
                estimated=legacy_usage.estimated,
            ),
            truncated=choice.get("finish_reason") in {"length", "max_tokens"},
        )

    @staticmethod
    def _message(message: ModelMessage) -> dict:
        result: dict = {"role": message.role, "content": message.content}
        if message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            result["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            result["name"] = message.name
        return result
