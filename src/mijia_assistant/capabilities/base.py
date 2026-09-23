from typing import Protocol

from mijia_assistant.models import AssistantContext, CapabilityResult, RiskClass


class Capability(Protocol):
    name: str
    description: str
    risk: RiskClass
    input_schema: dict

    async def is_available(self, ctx: AssistantContext) -> bool: ...

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult: ...


def tool_schema(capability: Capability) -> dict:
    return {
        "type": "function",
        "function": {
            "name": capability.name,
            "description": capability.description,
            "parameters": capability.input_schema,
        },
    }
