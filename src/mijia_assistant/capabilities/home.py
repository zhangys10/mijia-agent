from typing import ClassVar, Literal

from mijia_agent.command_console import ConsoleAgentTools
from mijia_agent.models import AgentError
from mijia_assistant.models import AssistantContext, AssistantError, CapabilityResult

HOME_METRICS = (
    "temperature",
    "humidity",
    "co2",
    "formaldehyde",
    "pm25",
    "pm10",
    "tvoc",
    "pressure",
    "battery",
)


class _HomeReadCapability:
    risk: Literal["home_read"] = "home_read"
    input_schema: ClassVar[dict]

    def __init__(self, tools: ConsoleAgentTools):
        self.tools = tools

    async def is_available(self, ctx: AssistantContext) -> bool:
        return ctx.automation_token is not None and "ai:chat" in ctx.scopes

    @staticmethod
    def _token(ctx: AssistantContext) -> str:
        if ctx.automation_token is None:
            raise AssistantError("HOME_CONTEXT_UNAVAILABLE", 403)
        return ctx.automation_token.get_secret_value()

    @staticmethod
    def _validate_args(args: dict, allowed: set[str]) -> dict:
        if not isinstance(args, dict) or set(args) - allowed:
            raise AssistantError("INVALID_TOOL_ARGUMENTS")
        normalized = {}
        for key in allowed:
            value = args.get(key)
            if value is None:
                continue
            limit = 40 if key == "kinds" else 20 if key == "rooms" else 9 if key == "metrics" else 3
            max_length = 40 if key == "kinds" else 200
            options = (
                set(HOME_METRICS)
                if key == "metrics"
                else {"on", "off", "unknown"}
                if key == "states"
                else None
            )
            if (
                not isinstance(value, list)
                or len(value) > limit
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item) > max_length
                    or (options is not None and item not in options)
                    for item in value
                )
            ):
                raise AssistantError("INVALID_TOOL_ARGUMENTS")
            normalized[key] = list(dict.fromkeys(value))
        return normalized


class HomeEnvironmentCapability(_HomeReadCapability):
    name = "get_home_environment"
    description = "Read the exposed home's current environmental measurements."
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rooms": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 20,
            },
            "metrics": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(HOME_METRICS),
                },
                "maxItems": 9,
            },
        },
    }

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        filters = self._validate_args(args, {"rooms", "metrics"})
        try:
            status = await self.tools.invoke_home_environment(
                self._token(ctx), ctx.request_id, ctx.home_selector, filters
            )
        except AgentError as error:
            raise AssistantError(error.code, error.status) from None
        content = {
            "completeness": status.completeness,
            "availableMetrics": len(status.groups),
            "capturedAt": status.capturedAt,
        }
        display = status.model_dump(exclude_none=True)
        return CapabilityResult(
            status="partial" if status.completeness == "partial" else "success",
            model_content=content,
            client_data={"type": "home_environment", **display},
            display_text=(
                "当前家庭暂无可用的环境读数。"
                if status.completeness == "empty"
                else "已读取当前家庭环境状态。"
            ),
        )


class DeviceStatusCapability(_HomeReadCapability):
    name = "get_device_status"
    description = "Read the exposed home's current per-room device power status."
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rooms": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 20,
            },
            "kinds": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 40},
                "maxItems": 40,
            },
            "states": {
                "type": "array",
                "items": {"type": "string", "enum": ["on", "off", "unknown"]},
                "maxItems": 3,
            },
        },
    }

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        filters = self._validate_args(args, {"rooms", "kinds", "states"})
        try:
            status = await self.tools.invoke_device_status(
                self._token(ctx), ctx.request_id, ctx.home_selector, filters
            )
        except AgentError as error:
            raise AssistantError(error.code, error.status) from None
        content = {
            "completeness": status.completeness,
            "roomCount": len(status.rooms),
            "deviceCount": sum(len(room.items) for room in status.rooms),
            "capturedAt": status.capturedAt,
        }
        display = status.model_dump(exclude_none=True)
        return CapabilityResult(
            status="partial" if status.completeness == "partial" else "success",
            model_content=content,
            client_data={"type": "device_status", **display},
            display_text=(
                "当前家庭暂无可用的设备状态。"
                if status.completeness == "empty"
                else "已读取当前家庭设备状态。"
            ),
        )
