from typing import ClassVar, Literal

from mijia_agent.command_console import ConsoleAgentTools
from mijia_agent.models import AgentError
from mijia_assistant.models import AssistantContext, AssistantError, CapabilityResult


class _HomeReadCapability:
    risk: Literal["home_read"] = "home_read"
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }

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
    def _validate_empty(args: dict) -> None:
        if args:
            raise AssistantError("INVALID_TOOL_ARGUMENTS")


class HomeEnvironmentCapability(_HomeReadCapability):
    name = "get_home_environment"
    description = "Read the exposed home's current environmental measurements."

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        self._validate_empty(args)
        try:
            status = await self.tools.get_home_status(
                self._token(ctx), ctx.request_id, ctx.home_selector
            )
        except AgentError as error:
            raise AssistantError(error.code, error.status) from None
        content = status.model_dump(exclude_none=True)
        return CapabilityResult(
            status="partial" if status.completeness == "partial" else "success",
            model_content=content,
            client_data={"type": "home_environment", **content},
            display_text=(
                "当前家庭暂无可用的环境读数。"
                if status.completeness == "empty"
                else "已读取当前家庭环境状态。"
            ),
        )


class DeviceStatusCapability(_HomeReadCapability):
    name = "get_device_status"
    description = "Read the exposed home's current per-room device power status."

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        self._validate_empty(args)
        try:
            status = await self.tools.get_device_status(
                self._token(ctx), ctx.request_id, ctx.home_selector
            )
        except AgentError as error:
            raise AssistantError(error.code, error.status) from None
        content = status.model_dump(exclude_none=True)
        return CapabilityResult(
            status="partial" if status.completeness == "partial" else "success",
            model_content=content,
            client_data={"type": "device_status", **content},
            display_text=(
                "当前家庭暂无可用的设备状态。"
                if status.completeness == "empty"
                else "已读取当前家庭设备状态。"
            ),
        )
