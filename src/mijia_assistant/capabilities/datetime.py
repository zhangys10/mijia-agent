from datetime import datetime
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mijia_assistant.models import AssistantContext, AssistantError, CapabilityResult


class CurrentDateTimeCapability:
    """Return the trusted server clock in the channel's requested IANA timezone."""

    name = "get_current_datetime"
    description = "Get the current date and time in the user's requested timezone."
    risk: Literal["general_read"] = "general_read"
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }

    async def is_available(self, ctx: AssistantContext) -> bool:
        return "ai:chat" in ctx.scopes

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        if args:
            raise AssistantError("INVALID_TOOL_ARGUMENTS")
        try:
            now = datetime.now(ZoneInfo(ctx.timezone))
        except ZoneInfoNotFoundError:
            raise AssistantError("INVALID_TIMEZONE") from None
        snapshot = {
            "datetime": now.isoformat(),
            "timezone": ctx.timezone,
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M:%S"),
        }
        return CapabilityResult(
            status="success", model_content=snapshot, client_data={"type": "datetime", **snapshot}
        )
