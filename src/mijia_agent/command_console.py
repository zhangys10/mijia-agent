"""Console tool client for ``POST /ai/command``.

Calls the console's existing ``/api/ai/tools`` with the shared service secret
plus the caller's opaque automation token in ``X-Ai-User-Token``. The console
decrypts the token, derives the principal, resolves the home (name or ID), and
queries Xiaomi live — scene catalogs are never sourced from configuration. The
token is forwarded as an opaque header value: Python never opens it, never
logs it, and never uses the token's BYOK provider fields.
"""

import httpx
from pydantic import ValidationError

from .config import Settings
from .models import AgentError, Scene

_ALLOWED_ERRORS = {
    "AI_UNAUTHENTICATED": 401,
    "AUTOMATION_TOKEN_EXPIRED": 401,
    "AUTOMATION_TOKEN_INVALID": 401,
    "AI_HOME_NOT_FOUND": 404,
    "AI_HOME_FORBIDDEN": 403,
    "AI_SCENE_NOT_FOUND": 400,
    "XIAOMI_SCENE_DISABLED": 400,
    "AI_SCENE_EXECUTION_DISABLED": 403,
    "AI_PREVIEW_READ_ONLY": 403,
    "AI_IDEMPOTENCY_CONFLICT": 409,
    "AI_REQUEST_IN_PROGRESS": 409,
    "MI_CLOUD_ERROR": 502,
    "DEVICE_TIMEOUT": 504,
    "AI_AGENT_UNAVAILABLE": 502,
}


class ConsoleAgentTools:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def call(
        self,
        user_token: str,
        request_id: str,
        tool: str,
        home: str | None,
        arguments: dict,
        idempotency_key: str | None = None,
    ) -> dict:
        # The console rejects an explicit null home (it validates any present
        # value as a string), so the key is omitted entirely when unset.
        body: dict = {"requestId": request_id, "tool": tool, "arguments": arguments}
        if home is not None:
            body["home"] = home
        if idempotency_key is not None:
            body["idempotencyKey"] = idempotency_key
        try:
            response = await self.client.post(
                self.settings.console_url.rstrip("/") + "/api/ai/tools",
                headers={
                    "Authorization": "Bearer " + self.settings.tools_secret,
                    "X-Ai-User-Token": user_token,
                },
                json=body,
                timeout=15,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise AgentError(
                "DEVICE_TIMEOUT" if tool == "activate_scene" else "MI_CLOUD_ERROR", 504
            ) from None
        except httpx.HTTPError:
            raise AgentError("MI_CLOUD_ERROR") from None
        # Never retry writes: an interrupted response does not mean the action failed.
        if response.status_code != 200 or len(response.content) > 65536:
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                code = None
            if isinstance(code, str) and code in _ALLOWED_ERRORS:
                raise AgentError(code, _ALLOWED_ERRORS[code])
            raise AgentError("MI_CLOUD_ERROR")
        try:
            return response.json()
        except ValueError:
            raise AgentError("MI_CLOUD_ERROR") from None

    async def list_scenes(self, user_token: str, request_id: str, home: str | None) -> list[Scene]:
        body = await self.call(user_token, request_id, "list_scenes", home, {})
        try:
            raw = body["scenes"]
            if not isinstance(raw, list) or len(raw) > 200:
                raise ValueError("invalid scene list")
            scenes = [Scene.model_validate(scene) for scene in raw]
            if len({scene.alias for scene in scenes}) != len(scenes):
                raise ValueError("duplicate aliases")
            return scenes
        except (KeyError, TypeError, ValueError, ValidationError):
            raise AgentError("AI_AGENT_UNAVAILABLE") from None

    async def activate_scene(
        self,
        user_token: str,
        request_id: str,
        home: str | None,
        alias: str,
        idempotency_key: str,
    ) -> dict:
        body = await self.call(
            user_token,
            request_id,
            "activate_scene",
            home,
            {"sceneId": alias},
            idempotency_key,
        )
        try:
            status = body["status"]
            if status not in ("success", "partial_success"):
                raise ValueError("invalid status")
            succeeded, failed = body.get("succeeded", 0), body.get("failed", 0)
            if type(succeeded) is not int or type(failed) is not int:
                raise ValueError("invalid counts")
            if succeeded < 0 or failed < 0:
                raise ValueError("negative counts")
            message = body.get("message", "")
            if not isinstance(message, str) or len(message) > 2000:
                raise ValueError("invalid message")
            return {"status": status, "succeeded": succeeded, "failed": failed}
        except (KeyError, TypeError, ValueError, ValidationError):
            raise AgentError("AI_AGENT_UNAVAILABLE") from None
