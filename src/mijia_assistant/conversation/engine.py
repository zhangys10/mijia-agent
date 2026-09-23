import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone

from mijia_assistant.capabilities.base import tool_schema
from mijia_assistant.capabilities.registry import CapabilityRegistry
from mijia_assistant.models import (
    Answer,
    AssistantContext,
    AssistantError,
    AssistantResponse,
    ModelMessage,
    ToolEvent,
    Usage,
)
from mijia_assistant.providers.base import ModelProvider

from .policy import needs_weather_location_clarification
from .prompt import SYSTEM_PROMPT


class ConversationEngine:
    def __init__(
        self,
        provider: ModelProvider,
        registry: CapabilityRegistry,
        *,
        max_iterations: int = 4,
        max_reads: int = 8,
        max_reads_per_tool: int = 4,
        tool_result_logger: Callable[..., None] | None = None,
    ):
        self.provider = provider
        self.registry = registry
        self.max_iterations = max_iterations
        self.max_reads = max_reads
        self.max_reads_per_tool = max_reads_per_tool
        self.tool_result_logger = tool_result_logger

    async def run(
        self, ctx: AssistantContext, message: str, history: list[ModelMessage] | None = None
    ) -> AssistantResponse:
        if "ai:chat" not in ctx.scopes:
            raise AssistantError("SCOPE_FORBIDDEN", 403)
        if datetime.now(timezone.utc) >= ctx.deadline:
            raise AssistantError("DEADLINE_EXCEEDED", 504)
        capabilities = await self.registry.for_context(ctx)
        if "get_weather" not in capabilities and needs_weather_location_clarification(message):
            return AssistantResponse(
                request_id=ctx.request_id,
                conversation_id=ctx.conversation_id,
                status="completed",
                outcome="clarification",
                answer=Answer(
                    text=(
                        "你想查询哪个城市的天气？"
                        if ctx.locale.startswith("zh")
                        else "Which city would you like me to check?"
                    )
                ),
            )
        schemas = [tool_schema(item) for item in capabilities.values()]
        transcript = [
            ModelMessage(role="system", content=SYSTEM_PROMPT),
            *(history or []),
            ModelMessage(
                role="user",
                content=(
                    f"locale={ctx.locale}; timezone={ctx.timezone}; channel={ctx.channel}\n"
                    f"{message.strip()}"
                ),
            ),
        ]
        usage = Usage()
        events: list[ToolEvent] = []
        read_count = 0
        per_tool: dict[str, int] = {}
        client_data = None
        fallback_text = None

        for _iteration in range(self.max_iterations):
            if datetime.now(timezone.utc) >= ctx.deadline:
                raise AssistantError("DEADLINE_EXCEEDED", 504)
            try:
                step = await asyncio.wait_for(
                    self.provider.complete(transcript, schemas, ctx),
                    timeout=self._remaining_seconds(ctx),
                )
            except asyncio.TimeoutError:
                raise AssistantError("DEADLINE_EXCEEDED", 504) from None
            usage = usage.plus(step.usage)
            if not step.tool_calls:
                if step.truncated:
                    if events and fallback_text:
                        return AssistantResponse(
                            request_id=ctx.request_id,
                            conversation_id=ctx.conversation_id,
                            status="completed",
                            outcome="tool_answer",
                            answer=Answer(text=fallback_text),
                            data=client_data,
                            tool_events=events,
                            usage=usage,
                        )
                    raise AssistantError("MODEL_RESPONSE_TRUNCATED", 502)
                text = step.content.strip()
                if not text:
                    if events and fallback_text:
                        return AssistantResponse(
                            request_id=ctx.request_id,
                            conversation_id=ctx.conversation_id,
                            status="completed",
                            outcome="tool_answer",
                            answer=Answer(text=fallback_text),
                            data=client_data,
                            tool_events=events,
                            usage=usage,
                        )
                    raise AssistantError("MODEL_RESPONSE_INVALID", 502)
                outcome = (
                    "clarification"
                    if step.response_kind == "clarification"
                    else ("tool_answer" if events else "direct_answer")
                )
                return AssistantResponse(
                    request_id=ctx.request_id,
                    conversation_id=ctx.conversation_id,
                    status="completed",
                    outcome=outcome,
                    answer=Answer(text=text),
                    data=client_data,
                    tool_events=events,
                    usage=usage,
                )

            selected = []
            for call in step.tool_calls:
                capability = capabilities.get(call.name)
                if capability is None:
                    raise AssistantError("TOOL_NOT_AVAILABLE", 400)
                selected.append((call, capability))
            writes = [item for item in selected if self._is_write(item[1].risk)]
            if writes and (len(writes) != 1 or len(selected) != 1):
                raise AssistantError("WRITE_MUST_BE_EXCLUSIVE", 400)

            transcript.append(
                ModelMessage(role="assistant", content=step.content, tool_calls=step.tool_calls)
            )
            for call, capability in selected:
                if capability.risk in {"general_read", "home_read"}:
                    read_count += 1
                    per_tool[call.name] = per_tool.get(call.name, 0) + 1
                    if read_count > self.max_reads or per_tool[call.name] > self.max_reads_per_tool:
                        raise AssistantError("TOOL_BUDGET_EXCEEDED", 400)
                try:
                    result = await asyncio.wait_for(
                        capability.invoke(ctx, call.arguments),
                        timeout=self._remaining_seconds(ctx),
                    )
                except AssistantError as error:
                    events.append(ToolEvent(name=call.name, status="error"))
                    self._log_tool_result(call.name, "error", error.code)
                    return self._tool_error_response(
                        ctx, events, usage, client_data, error.code, call.name
                    )
                except asyncio.TimeoutError:
                    if self._is_write(capability.risk):
                        events.append(ToolEvent(name=call.name, status="outcome_unknown"))
                        self._log_tool_result(call.name, "outcome_unknown", "DEADLINE_EXCEEDED")
                        return AssistantResponse(
                            request_id=ctx.request_id,
                            conversation_id=ctx.conversation_id,
                            status="completed",
                            outcome="outcome_unknown",
                            answer=Answer(
                                text="The action outcome is unknown. Do not retry automatically."
                            ),
                            tool_events=events,
                            usage=usage,
                        )
                    events.append(ToolEvent(name=call.name, status="error"))
                    self._log_tool_result(call.name, "error", "DEADLINE_EXCEEDED")
                    return self._tool_error_response(
                        ctx, events, usage, client_data, "DEADLINE_EXCEEDED", call.name
                    )
                except Exception:  # noqa: BLE001 -- tool failures become readable assistant replies.
                    events.append(ToolEvent(name=call.name, status="error"))
                    self._log_tool_result(call.name, "error", "TOOL_FAILED")
                    return self._tool_error_response(
                        ctx, events, usage, client_data, "TOOL_FAILED", call.name
                    )
                events.append(ToolEvent(name=call.name, status=result.status))
                if result.client_data is not None:
                    client_data = result.client_data
                if result.display_text:
                    fallback_text = result.display_text
                if result.status == "error":
                    detail = result.model_content if isinstance(result.model_content, dict) else {}
                    error_code = str(detail.get("status", "TOOL_FAILED"))
                    self._log_tool_result(call.name, "error", error_code)
                    return self._tool_error_response(
                        ctx,
                        events,
                        usage,
                        client_data,
                        str(detail.get("status", "TOOL_FAILED")),
                        call.name,
                    )
                self._log_tool_result(call.name, result.status)
                if self._is_write(capability.risk) or result.is_terminal:
                    outcome = (
                        "outcome_unknown" if result.status == "outcome_unknown" else "action_result"
                    )
                    text = result.display_text or "Action request completed."
                    return AssistantResponse(
                        request_id=ctx.request_id,
                        conversation_id=ctx.conversation_id,
                        status="completed",
                        outcome=outcome,
                        answer=Answer(text=text),
                        data=result.client_data,
                        tool_events=events,
                        usage=usage,
                    )
                serialized = json.dumps(
                    result.model_content, ensure_ascii=False, separators=(",", ":")
                )
                if len(serialized.encode()) > 12000:
                    raise AssistantError("TOOL_RESULT_TOO_LARGE", 502)
                transcript.append(
                    ModelMessage(
                        role="tool", content=serialized, tool_call_id=call.id, name=call.name
                    )
                )
            await asyncio.sleep(0)

        raise AssistantError("MODEL_ITERATION_LIMIT", 502)

    def _log_tool_result(self, tool_name: str, status: str, error_code: str | None = None) -> None:
        if self.tool_result_logger is None:
            return
        try:
            self.tool_result_logger(tool_name=tool_name, status=status, error_code=error_code)
        except Exception:  # noqa: BLE001 -- logging must never fail a turn.
            return

    @staticmethod
    def _tool_error_response(
        ctx: AssistantContext,
        events: list[ToolEvent],
        usage: Usage,
        client_data: dict | None,
        code: str,
        tool_name: str,
    ) -> AssistantResponse:
        chinese = ctx.locale.startswith("zh")
        weather = tool_name == "get_weather"
        messages = {
            "location_unavailable": (
                "抱歉，这个地点的天气暂时查询不到，请稍后再试。"
                if chinese and weather
                else "Sorry, weather for that location is temporarily unavailable. Please try again later."
            ),
            "DEADLINE_EXCEEDED": (
                "天气服务响应超时，请稍后再试。"
                if chinese and weather
                else "The weather service timed out. Please try again later."
            ),
        }
        fallback = messages.get(code) if weather else None
        fallback = fallback or (
            ("天气服务暂时不可用，请稍后再试。" if weather else "查询暂时无法完成，请稍后再试。")
            if chinese
            else (
                "The weather service is temporarily unavailable. Please try again later."
                if weather
                else "The request could not be completed. Please try again later."
            )
        )
        return AssistantResponse(
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            status="completed",
            outcome="tool_answer",
            answer=Answer(text=fallback),
            data=client_data,
            tool_events=events,
            usage=usage,
        )

    @staticmethod
    def _remaining_seconds(ctx: AssistantContext) -> float:
        remaining = (ctx.deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise AssistantError("DEADLINE_EXCEEDED", 504)
        return remaining

    @staticmethod
    def _is_write(risk: str) -> bool:
        return risk not in {"general_read", "home_read"}
