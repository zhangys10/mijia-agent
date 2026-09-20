import httpx
from pydantic import ValidationError

from .config import Settings
from .models import AgentError, Execution, HomeStatus, Scene, Turn


class ConsoleTools:
    """Only the web console can decrypt the opaque binding and resolve scene aliases."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def call(self, turn: Turn, tool: str, arguments: dict):
        try:
            response = await self.client.post(
                self.settings.console_url.rstrip("/") + "/api/ai/tools",
                headers={"Authorization": "Bearer " + self.settings.tools_secret},
                json={
                    "requestId": turn.requestId,
                    "principalId": turn.principalId,
                    "homeId": turn.homeId,
                    "scopes": turn.scopes,
                    "sessionBinding": turn.sessionBinding.get_secret_value(),
                    "idempotencyKey": turn.idempotencyKey,
                    "tool": tool,
                    "arguments": arguments,
                },
                timeout=15,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise AgentError(
                "AI_SCENE_TIMEOUT" if tool == "activate_scene" else "AI_AGENT_UNAVAILABLE", 504
            ) from None
        except httpx.HTTPError:
            raise AgentError("AI_AGENT_UNAVAILABLE") from None
        # Never retry writes: an interrupted response does not mean the action failed.
        if response.status_code != 200 or len(response.content) > 65536:
            allowed = {
                "AI_SCOPE_FORBIDDEN": 403,
                "AI_HOME_FORBIDDEN": 403,
                "AI_UNAUTHENTICATED": 401,
                "AI_PREVIEW_READ_ONLY": 403,
                "AI_SCENE_EXECUTION_DISABLED": 403,
                "AI_SCENE_NOT_FOUND": 400,
                "AI_IDEMPOTENCY_CONFLICT": 409,
                "AI_REQUEST_IN_PROGRESS": 409,
                "AI_SCENE_FAILED": 502,
                "AI_SCENE_TIMEOUT": 504,
            }
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                code = None
            if code in allowed:
                raise AgentError(code, allowed[code])
            raise AgentError("AI_AGENT_UNAVAILABLE")
        try:
            return response.json()
        except ValueError:
            raise AgentError("AI_AGENT_UNAVAILABLE") from None

    async def list_scenes(self, turn: Turn) -> list[Scene]:
        body = await self.call(turn, "list_scenes", {})
        try:
            raw = body["scenes"]
            if not isinstance(raw, list) or len(raw) > 100:
                raise ValueError("invalid scene list")
            result = [Scene.model_validate(scene) for scene in raw]
            if len({scene.alias for scene in result}) != len(result):
                raise ValueError("duplicate aliases")
            return result
        except (KeyError, TypeError, ValueError, ValidationError):
            raise AgentError("AI_AGENT_UNAVAILABLE") from None

    async def get_home_status(self, turn: Turn) -> HomeStatus:
        body = await self.call(turn, "get_home_status", {})
        try:
            return HomeStatus.model_validate(body)
        except ValidationError:
            raise AgentError("AI_AGENT_UNAVAILABLE") from None

    async def activate_scene(self, turn: Turn, alias: str) -> Execution:
        body = await self.call(turn, "activate_scene", {"sceneId": alias})
        try:
            return Execution.model_validate(body)
        except ValidationError:
            raise AgentError("AI_AGENT_UNAVAILABLE") from None
