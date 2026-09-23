import asyncio
import json
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
    ):
        self.provider = provider
        self.registry = registry
        self.max_iterations = max_iterations
        self.max_reads = max_reads
        self.max_reads_per_tool = max_reads_per_tool

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
            except TimeoutError:
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
                except TimeoutError:
                    if self._is_write(capability.risk):
                        events.append(ToolEvent(name=call.name, status="outcome_unknown"))
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
                    raise AssistantError("DEADLINE_EXCEEDED", 504) from None
                events.append(ToolEvent(name=call.name, status=result.status))
                if result.client_data is not None:
                    client_data = result.client_data
                if result.display_text:
                    fallback_text = result.display_text
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

    @staticmethod
    def _remaining_seconds(ctx: AssistantContext) -> float:
        remaining = (ctx.deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise AssistantError("DEADLINE_EXCEEDED", 504)
        return remaining

    @staticmethod
    def _is_write(risk: str) -> bool:
        return risk not in {"general_read", "home_read"}
