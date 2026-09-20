import re
from typing import Protocol

from .models import AgentError, Decision, Execution, Result, Scene, ToolResult, Turn


class Provider(Protocol):
    async def decide(self, turn: Turn, scenes: list[Scene]) -> Decision: ...


class Tools(Protocol):
    async def list_scenes(self, turn: Turn) -> list[Scene]: ...
    async def get_home_status(self, turn: Turn) -> dict: ...
    async def activate_scene(self, turn: Turn, alias: str) -> Execution: ...


def explicit_activation(message: str, scene: Scene) -> bool:
    """Conservative server-side consent gate, independent of model output.

    Additional natural-language forms require explicit test cases or a future confirmation flow.
    """
    normalized = re.sub(r"[。！!\s]+$", "", message.strip())
    commands = {
        f"{prefix}{scene.name}" for prefix in ("执行", "开启", "启动", "请执行", "请开启", "请启动")
    }
    commands |= {f"{prefix}{scene.name}" for prefix in ("activate ", "run ")}
    if scene.name in {"回家模式", "回家"}:
        commands |= {"我回家了", "我到家了"}
    return normalized in commands


class AgentService:
    def __init__(self, provider: Provider, tools: Tools, preview: bool = False):
        self.provider, self.tools, self.preview = provider, tools, preview

    async def run(self, turn: Turn) -> Result:
        if "ai:chat" not in turn.scopes:
            raise AgentError("AI_SCOPE_FORBIDDEN", 403)
        base = {"requestId": turn.requestId, "conversationId": turn.conversationId}
        if self.preview:
            return Result(**base, message="预览模式：不会调用模型或控制真实设备。", intent="none")
        scenes = await self.tools.list_scenes(turn)
        decision = await self.provider.decide(turn, scenes)
        base["usage"] = decision.usage
        if decision.tool == "none":
            reply = decision.message or "请说明需要执行的场景，或查看可用场景。"
            for scene in scenes:
                reply = reply.replace(scene.alias, scene.name)
            return Result(**base, message=reply, intent="none")
        if decision.tool == "list_scenes":
            return Result(
                **base,
                message="当前家庭可用场景如下。" if scenes else "暂无审核场景。",
                intent="list_scenes",
                scenes=scenes,
                tool=ToolResult(name="list_scenes", status="success"),
            )
        if decision.tool == "get_home_status":
            home_status = await self.tools.get_home_status(turn)
            return Result(
                **base,
                message="当前家中环境状态如下。",
                intent="get_home_status",
                homeStatus=home_status,
                tool=ToolResult(name="get_home_status", status="success"),
            )
        if "scene:activate" not in turn.scopes:
            raise AgentError("AI_SCOPE_FORBIDDEN", 403, decision.usage)
        scene = next((s for s in scenes if s.alias == decision.sceneId), None)
        if scene is None:
            raise AgentError("AI_SCENE_NOT_FOUND", 400, decision.usage)
        if not explicit_activation(turn.message, scene):
            return Result(**base, message=f"如需执行，请明确说“执行{scene.name}”。", intent="none")
        try:
            execution = await self.tools.activate_scene(turn, scene.alias)
        except AgentError as error:
            error.usage = decision.usage
            raise
        # Use the executor's actual result, never the model's predicted success text.
        return Result(
            **base,
            message=execution.message,
            intent="activate_scene",
            tool=ToolResult(name="activate_scene", status=execution.status, sceneName=scene.name),
        )
