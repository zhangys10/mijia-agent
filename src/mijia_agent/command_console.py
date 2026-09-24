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
from .models import AgentError, DeviceStatus, HomeStatus, Scene

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
    "AI_EXECUTION_STATUS_UNKNOWN": 409,
    "AI_ACTION_LEDGER_UNAVAILABLE": 503,
    "AI_SCENE_REVISION_CHANGED": 409,
    "AI_SCENE_RISK_BLOCKED": 403,
    "AI_SCENE_NOT_EXPOSED": 403,
    "AI_EXPOSURE_STORE_UNAVAILABLE": 503,
    "MI_CLOUD_ERROR": 502,
    "DEVICE_TIMEOUT": 504,
    "AI_AGENT_UNAVAILABLE": 502,
}

_CONSOLE_DIAGNOSTICS = {
    "ASSISTANT_AUTHORIZATION_EXCEPTION",
    "ASSISTANT_REQUEST_BODY_EXCEPTION",
    "ASSISTANT_CAPABILITIES_EXCEPTION",
    "ASSISTANT_TOOL_INVOKE_EXCEPTION",
}


class ConsoleAgentTools:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def invoke_home_environment(
        self, user_token: str, request_id: str, home: str | None, arguments: dict
    ) -> HomeStatus:
        body: dict = {
            "requestId": request_id,
            "operation": "get_home_environment",
            "arguments": arguments,
        }
        if home is not None:
            body["home"] = home
        result = await self._call_v1("tools:invoke", user_token, body)
        try:
            return HomeStatus.model_validate(result)
        except (TypeError, ValueError, ValidationError):
            raise AgentError(
                "AI_AGENT_UNAVAILABLE", diagnostic_code="CONSOLE_HOME_STATUS_SCHEMA_INVALID"
            ) from None

    async def invoke_device_status(
        self, user_token: str, request_id: str, home: str | None, arguments: dict
    ) -> DeviceStatus:
        body: dict = {
            "requestId": request_id,
            "operation": "get_device_status",
            "arguments": arguments,
        }
        if home is not None:
            body["home"] = home
        result = await self._call_v1("tools:invoke", user_token, body)
        try:
            return DeviceStatus.model_validate(result)
        except (TypeError, ValueError, ValidationError):
            raise AgentError(
                "AI_AGENT_UNAVAILABLE", diagnostic_code="CONSOLE_DEVICE_STATUS_SCHEMA_INVALID"
            ) from None

    async def _call_v1(self, endpoint: str, user_token: str, body: dict) -> dict:
        try:
            response = await self.client.post(
                self.settings.console_url.rstrip("/") + f"/api/internal/assistant/v1/{endpoint}",
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
                "AI_AGENT_UNAVAILABLE", 504, diagnostic_code="CONSOLE_TIMEOUT"
            ) from None
        except httpx.HTTPError:
            raise AgentError(
                "AI_AGENT_UNAVAILABLE", diagnostic_code="CONSOLE_TRANSPORT_ERROR"
            ) from None
        if response.status_code != 200 or len(response.content) > 65536:
            allowed = {
                "AI_UNAUTHENTICATED": 401,
                "AUTOMATION_TOKEN_EXPIRED": 401,
                "AUTOMATION_TOKEN_INVALID": 401,
                "AI_AUTOMATION_TOKEN_ENV_NOT_CONFIGURED": 500,
                "AI_HOME_NOT_FOUND": 404,
                "AI_CAPABILITY_UNAVAILABLE": 403,
                "AI_PREVIEW_READ_ONLY": 403,
                "AI_INVALID_REQUEST": 400,
                "AI_EXPOSURE_STORE_UNAVAILABLE": 503,
                "AI_AGENT_UNAVAILABLE": 502,
            }
            if len(response.content) > 65536:
                raise AgentError(
                    "AI_AGENT_UNAVAILABLE", diagnostic_code="CONSOLE_RESPONSE_TOO_LARGE"
                )
            try:
                payload = response.json()
            except (ValueError, AttributeError):
                payload = None
            code = payload.get("code") if isinstance(payload, dict) else None
            reported_diagnostic = (
                payload.get("diagnosticCode") if isinstance(payload, dict) else None
            )
            if isinstance(code, str) and code in allowed:
                diagnostic = (
                    f"CONSOLE_{reported_diagnostic}"
                    if reported_diagnostic in _CONSOLE_DIAGNOSTICS
                    else f"CONSOLE_HTTP_{response.status_code}"
                )
                if code == "AI_AGENT_UNAVAILABLE":
                    raise AgentError(code, allowed[code], diagnostic_code=diagnostic)
                raise AgentError(code, allowed[code])
            raise AgentError(
                "AI_AGENT_UNAVAILABLE",
                diagnostic_code=f"CONSOLE_HTTP_{response.status_code}",
            )
        try:
            value = response.json()
        except ValueError:
            raise AgentError(
                "AI_AGENT_UNAVAILABLE", diagnostic_code="CONSOLE_RESPONSE_INVALID_JSON"
            ) from None
        if not isinstance(value, dict):
            raise AgentError("AI_AGENT_UNAVAILABLE", diagnostic_code="CONSOLE_RESPONSE_NOT_OBJECT")
        return value

    async def call(
        self,
        user_token: str,
        request_id: str,
        tool: str,
        home: str | None,
        arguments: dict,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> dict:
        # The console rejects an explicit null home (it validates any present
        # value as a string), so the key is omitted entirely when unset.
        body: dict = {"requestId": request_id, "tool": tool, "arguments": arguments}
        if home is not None:
            body["home"] = home
        if idempotency_key is not None:
            body["idempotencyKey"] = idempotency_key
        if request_hash is not None:
            body["requestHash"] = request_hash
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
                "AI_EXECUTION_STATUS_UNKNOWN" if tool == "activate_scene" else "MI_CLOUD_ERROR",
                409 if tool == "activate_scene" else 504,
            ) from None
        except httpx.HTTPError:
            raise AgentError(
                "AI_EXECUTION_STATUS_UNKNOWN" if tool == "activate_scene" else "MI_CLOUD_ERROR",
                409 if tool == "activate_scene" else 502,
            ) from None
        # Never retry writes: an interrupted response does not mean the action failed.
        if response.status_code != 200 or len(response.content) > 65536:
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                code = None
            if isinstance(code, str) and code in _ALLOWED_ERRORS:
                raise AgentError(code, _ALLOWED_ERRORS[code])
            raise AgentError(
                "AI_EXECUTION_STATUS_UNKNOWN" if tool == "activate_scene" else "MI_CLOUD_ERROR",
                409 if tool == "activate_scene" else 502,
            )
        try:
            return response.json()
        except ValueError:
            raise AgentError(
                "AI_EXECUTION_STATUS_UNKNOWN" if tool == "activate_scene" else "MI_CLOUD_ERROR",
                409 if tool == "activate_scene" else 502,
            ) from None

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
        revision: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict:
        body = await self.call(
            user_token,
            request_id,
            "activate_scene",
            home,
            {"sceneId": alias, "revision": revision},
            idempotency_key,
            request_hash,
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
            return {"status": status, "succeeded": succeeded, "failed": failed, "message": message}
        except (KeyError, TypeError, ValueError, ValidationError):
            raise AgentError("AI_AGENT_UNAVAILABLE") from None
