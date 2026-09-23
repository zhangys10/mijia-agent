from collections.abc import Iterable

from mijia_assistant.models import AssistantContext

from .base import Capability


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[Capability] = ()):
        items = list(capabilities)
        if len({item.name for item in items}) != len(items):
            raise ValueError("duplicate capability name")
        self._capabilities = tuple(items)

    async def for_context(self, ctx: AssistantContext) -> dict[str, Capability]:
        available: dict[str, Capability] = {}
        for capability in self._capabilities:
            if await capability.is_available(ctx):
                available[capability.name] = capability
        return available
