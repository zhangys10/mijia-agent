import hmac
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .command_console import ConsoleAgentTools
from .command_idempotency import request_hash, valid_idempotency_key
from .command_models import CommandRequest, ProcessingResponse
from .command_service import CommandService
from .config import Settings
from .console import ConsoleTools
from .gateway import Gateway
from .models import AgentError, Turn
from .service import AgentService

MAX_COMMAND_BODY = 65536
MAX_AUTOMATION_TOKEN = 8192

# Console /api/ai/command public error codes, mapped from internal codes.
COMMAND_ERROR_MAP = {
    "LLM_TIMEOUT": ("LLM_TIMEOUT", 504, "AI 响应超时"),
    "LLM_PROVIDER_ERROR": ("LLM_PROVIDER_ERROR", 502, "AI 服务暂时不可用"),
    "MI_CLOUD_ERROR": ("MI_CLOUD_ERROR", 502, "米家服务暂时不可用"),
    "DEVICE_TIMEOUT": ("DEVICE_TIMEOUT", 504, "设备响应超时"),
    "AUTOMATION_TOKEN_EXPIRED": ("AUTOMATION_TOKEN_EXPIRED", 401, "自动化凭据已过期，请重新生成"),
    "AUTOMATION_TOKEN_INVALID": ("AUTOMATION_TOKEN_INVALID", 401, "自动化凭据无效，请重新生成"),
    "AI_HOME_NOT_FOUND": ("AI_HOME_NOT_FOUND", 404, "未找到可用的家庭"),
    "IDEMPOTENCY_CONFLICT": ("IDEMPOTENCY_CONFLICT", 409, "请求重复且内容不一致"),
    "INVALID_REQUEST": ("INVALID_REQUEST", 400, "请求格式不正确"),
    "AI_SCENE_EXECUTION_DISABLED": ("AI_SCENE_EXECUTION_DISABLED", 403, "场景执行尚未开放"),
    "AI_SCENE_NOT_FOUND": ("AI_SCENE_NOT_FOUND", 400, "未找到匹配的场景"),
    "AI_PREVIEW_READ_ONLY": ("AI_PREVIEW_READ_ONLY", 403, "预览环境只读"),
    "AI_AGENT_UNAVAILABLE": ("MI_CLOUD_ERROR", 502, "米家服务暂时不可用"),
    "UNAUTHORIZED": ("UNAUTHORIZED", 401, "请在 Authorization 标头提供自动化凭据"),
}
COMMAND_FALLBACK_ERROR = ("MI_CLOUD_ERROR", 502, "米家服务暂时不可用")


def create_lifespan(
    config: Settings,
    service: AgentService | None = None,
    command_service: CommandService | None = None,
):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None and command_service is not None:
            app.state.service = service
            app.state.command_service = command_service
            yield
            return
        async with httpx.AsyncClient(follow_redirects=False) as client:
            gateway = Gateway(config, client)
            app.state.service = service or AgentService(
                gateway,
                ConsoleTools(config, client),
                preview=config.environment == "preview",
            )
            app.state.command_service = command_service or CommandService(
                gateway,
                ConsoleAgentTools(config, client),
                preview=config.environment == "preview",
            )
            yield

    return lifespan


def register_routes(app: FastAPI, config: Settings) -> FastAPI:

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/ai/command")
    async def command_info():
        return JSONResponse(
            {
                "status": "ok",
                "service": "mijia-agent",
                "endpoint": "POST /ai/command",
                "message": "请使用 POST 方法发送请求，并附带 Authorization 与 Idempotency-Key 标头。",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/internal/v1/turn")
    async def run(request: Request):
        headers = {"Cache-Control": "no-store"}
        expected = "Bearer " + config.internal_secret
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            return JSONResponse({"code": "AI_UNAUTHENTICATED"}, 401, headers=headers)
        request_id = None
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise AgentError("AI_INVALID_REQUEST", 400)
            turn = Turn.model_validate(json.loads(raw))
            request_id = turn.requestId
            result = await app.state.service.run(turn)
            return JSONResponse(result.model_dump(exclude_none=True), headers=headers)
        except (ValueError, ValidationError):
            # Pydantic errors contain input values. Never serialize or log them.
            return JSONResponse(
                {"code": "AI_INVALID_REQUEST", "requestId": request_id}, 400, headers=headers
            )
        except AgentError as error:
            body = {
                "code": error.code,
                "message": "AI 请求未完成，请检查状态后重试。",
                "requestId": request_id,
            }
            if error.usage is not None:
                body["usage"] = error.usage.model_dump()
            return JSONResponse(body, error.status, headers=headers)
        except Exception:  # noqa: BLE001 -- HTTP boundary must redact upstream secrets.
            return JSONResponse(
                {"code": "AI_AGENT_UNAVAILABLE", "requestId": request_id}, 502, headers=headers
            )

    @app.post("/ai/command")
    async def command(request: Request):
        headers = {"Cache-Control": "no-store"}
        request_id = None

        def error(code: str, status: int, message: str) -> JSONResponse:
            return JSONResponse(
                {"code": code, "message": message, "requestId": request_id},
                status,
                headers=headers,
            )

        # The automation token is opaque here: forwarded to the console, which
        # alone decrypts it. Never logged or echoed back.
        token = bearer_token(request.headers.get("authorization"))
        if not token:
            return error("UNAUTHORIZED", 401, "请在 Authorization 标头提供自动化凭据。")
        if len(token) > MAX_AUTOMATION_TOKEN:
            return error("AUTOMATION_TOKEN_INVALID", 401, "自动化凭据无效，请重新生成")

        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_COMMAND_BODY:
                    raise AgentError("INVALID_REQUEST", 400)
            body = json.loads(raw)
            command_request = CommandRequest.model_validate(body)
        except AgentError as caught:
            return error(caught.code, caught.status, "请求格式不正确")
        except (ValueError, ValidationError):
            # Pydantic errors contain input values. Never serialize or log them.
            return error("INVALID_REQUEST", 400, "请求格式不正确")

        # Malformed keys are ignored (console semantics); a valid one becomes
        # mandatory once the decision selects an action.
        raw_key = request.headers.get("idempotency-key")
        idempotency_key = raw_key if valid_idempotency_key(raw_key) else None
        body_hash = request_hash(body)
        try:
            result = await app.state.command_service.run(
                token, command_request, idempotency_key, body_hash
            )
            status = 202 if isinstance(result, ProcessingResponse) else 200
            return JSONResponse(result.model_dump(exclude_none=True), status, headers=headers)
        except AgentError as caught:
            code, status_code, message = COMMAND_ERROR_MAP.get(caught.code, COMMAND_FALLBACK_ERROR)
            return error(code, status_code, message)
        except Exception:  # noqa: BLE001 -- HTTP boundary must redact upstream secrets.
            return error("MI_CLOUD_ERROR", 502, "米家服务暂时不可用")

    return app


def bearer_token(header: str | None) -> str | None:
    if not header or not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    return token or None


def create_app(
    settings: Settings | None = None,
    service: AgentService | None = None,
    command_service: CommandService | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(
        lifespan=create_lifespan(config, service, command_service),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    return register_routes(app, config)
