"""Bounded, redacted process-local model history for the Phase 0 harness.

Makers ``context.store`` replaces this implementation in Phase 1. This store deliberately keeps
only conversational summaries and never stores tokens, bindings, private tool payloads, or action
receipts.
"""

import asyncio
import hashlib

from mijia_assistant.models import AssistantContext, AssistantResponse, ModelMessage

MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT = 2000


class ConversationRepository:
    def __init__(self):
        self._messages: dict[str, list[ModelMessage]] = {}
        self._lock = asyncio.Lock()

    async def get(self, ctx: AssistantContext) -> list[ModelMessage]:
        async with self._lock:
            return list(self._messages.get(self._key(ctx), ()))

    async def append(
        self, ctx: AssistantContext, user_message: str, response: AssistantResponse
    ) -> None:
        messages = [
            ModelMessage(role="user", content=user_message.strip()[:MAX_HISTORY_CONTENT]),
            ModelMessage(role="assistant", content=self._history_response(response)),
        ]
        async with self._lock:
            key = self._key(ctx)
            self._messages[key] = (self._messages.get(key, []) + messages)[-MAX_HISTORY_MESSAGES:]

    @staticmethod
    def _key(ctx: AssistantContext) -> str:
        token = ctx.automation_token.get_secret_value() if ctx.automation_token else ""
        material = "\x00".join((token, ctx.home_selector or "", ctx.conversation_id))
        return hashlib.sha256(material.encode()).hexdigest()

    @staticmethod
    def _history_response(response: AssistantResponse) -> str:
        if response.outcome == "clarification":
            return response.answer.text[:MAX_HISTORY_CONTENT]
        if response.tool_events:
            names = ", ".join(event.name for event in response.tool_events)
            return f"Answered the user's request using {names}."
        return response.answer.text[:MAX_HISTORY_CONTENT]
