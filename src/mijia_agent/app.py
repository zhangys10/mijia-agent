import hmac
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError

from mijia_assistant.api import AssistantRequest, public_response
from mijia_assistant.capabilities import (
    CapabilityRegistry,
    DeviceStatusCapability,
    HomeEnvironmentCapability,
)
from mijia_assistant.conversation import ConversationEngine, ConversationRepository
from mijia_assistant.models import AssistantContext, AssistantError
from mijia_assistant.providers import OpenAICompatibleProvider

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
LOGGER = logging.getLogger("mijia_agent.legacy")

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
    assistant_engine: ConversationEngine | None = None,
    assistant_tools: ConsoleAgentTools | None = None,
    conversation_repository: ConversationRepository | None = None,
):
    @asynccontextmanager
    async def lifespan(app):
        if (
            service is not None
            and command_service is not None
            and assistant_engine is not None
            and assistant_tools is not None
        ):
            app.state.service = service
            app.state.command_service = command_service
            app.state.assistant_engine = assistant_engine
            app.state.assistant_tools = assistant_tools
            app.state.conversation_repository = conversation_repository or ConversationRepository()
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
            console_tools = ConsoleAgentTools(config, client)
            app.state.assistant_tools = assistant_tools or console_tools
            app.state.conversation_repository = conversation_repository or ConversationRepository()
            app.state.assistant_engine = assistant_engine or ConversationEngine(
                OpenAICompatibleProvider(gateway),
                CapabilityRegistry(
                    [
                        HomeEnvironmentCapability(console_tools),
                        DeviceStatusCapability(console_tools),
                    ]
                ),
            )
            yield

    return lifespan


def register_routes(app: FastAPI, config: Settings) -> FastAPI:

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/ai/command")
    async def command_info():
        if not config.legacy_router_enabled:
            return JSONResponse(
                {"code": "AI_COMMAND_RETIRED", "message": "Legacy router is disabled."},
                410,
                headers={"Cache-Control": "no-store"},
            )
        LOGGER.info('{"event":"legacy_router_traffic","method":"GET"}')
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

        if not config.legacy_router_enabled:
            return JSONResponse(
                {"code": "AI_COMMAND_RETIRED", "message": "Legacy router is disabled."},
                410,
                headers=headers,
            )
        LOGGER.info('{"event":"legacy_router_traffic","method":"POST"}')

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

    @app.post("/ai/assistant")
    async def assistant(request: Request):
        headers = {"Cache-Control": "no-store"}
        token = bearer_token(request.headers.get("authorization"))
        if not token:
            return JSONResponse({"code": "UNAUTHORIZED"}, 401, headers=headers)
        if len(token) > MAX_AUTOMATION_TOKEN:
            return JSONResponse({"code": "AUTOMATION_TOKEN_INVALID"}, 401, headers=headers)
        request_id = "req_" + uuid.uuid4().hex
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_COMMAND_BODY:
                    raise AssistantError("INVALID_REQUEST")
            body = AssistantRequest.model_validate(json.loads(raw))
            conversation_id = body.conversationId or "conv_" + uuid.uuid4().hex
            authorization = await app.state.assistant_tools.call(
                token, request_id, "authorize", body.home, {}
            )
            if authorization.get("ok") is not True:
                raise AssistantError("UNAUTHORIZED", 401)
            timeout = 12 if body.channel == "siri" else 20
            context = AssistantContext(
                request_id=request_id,
                conversation_id=conversation_id,
                channel=body.channel,
                locale=body.locale,
                timezone=body.timezone,
                scopes=frozenset({"ai:chat"}),
                deadline=datetime.now(timezone.utc) + timedelta(seconds=timeout),
                home_selector=body.home,
                automation_token=SecretStr(token),
            )
            history = await app.state.conversation_repository.get(context)
            result = await app.state.assistant_engine.run(context, body.text, history)
            await app.state.conversation_repository.append(context, body.text, result)
            return JSONResponse(public_response(result), headers=headers)
        except (ValueError, ValidationError):
            return JSONResponse(
                {"code": "INVALID_REQUEST", "requestId": request_id}, 400, headers=headers
            )
        except AssistantError as error:
            return JSONResponse(
                {"code": error.code, "requestId": request_id}, error.status, headers=headers
            )
        except AgentError as error:
            return JSONResponse(
                {"code": error.code, "requestId": request_id}, error.status, headers=headers
            )
        except Exception:  # noqa: BLE001 -- redact provider and credential failures.
            return JSONResponse(
                {"code": "AI_AGENT_UNAVAILABLE", "requestId": request_id},
                502,
                headers=headers,
            )

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
    assistant_engine: ConversationEngine | None = None,
    assistant_tools: ConsoleAgentTools | None = None,
    conversation_repository: ConversationRepository | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(
        lifespan=create_lifespan(
            config,
            service,
            command_service,
            assistant_engine,
            assistant_tools,
            conversation_repository,
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    return register_routes(app, config)
