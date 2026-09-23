from typing import Protocol

from mijia_assistant.models import AssistantContext, ModelMessage, ModelTurn


class ModelProvider(Protocol):
    async def complete(
        self, messages: list[ModelMessage], tools: list[dict], ctx: AssistantContext
    ) -> ModelTurn: ...
