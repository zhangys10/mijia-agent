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


def model_history_answer(response: AssistantResponse) -> str:
    """Return conversational context without persisting a tool result or live reading."""
    if response.outcome == "clarification":
        return response.answer.text[:MAX_HISTORY_CONTENT]
    if response.outcome == "action_result":
        # Action replies contain only the sanitized display name and operation
        # acknowledged by the trusted executor. Retaining that summary lets the
        # model resolve conversational references without storing tool arguments,
        # opaque aliases, revisions, or device identifiers.
        return response.answer.text[:MAX_HISTORY_CONTENT]
    if response.tool_events:
        return "Answered the user's previous request using a fresh lookup."
    return response.answer.text[:MAX_HISTORY_CONTENT]


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
            ModelMessage(role="assistant", content=model_history_answer(response)),
        ]
        async with self._lock:
            key = self._key(ctx)
            self._messages[key] = (self._messages.get(key, []) + messages)[-MAX_HISTORY_MESSAGES:]

    @staticmethod
    def _key(ctx: AssistantContext) -> str:
        # Automation tokens are intentionally short-lived and may rotate on
        # every CLI/web request. They authenticate the turn, but must not split
        # one conversation's bounded, sanitized history into separate stores.
        material = "\x00".join((ctx.home_selector or "", ctx.conversation_id))
        return hashlib.sha256(material.encode()).hexdigest()
