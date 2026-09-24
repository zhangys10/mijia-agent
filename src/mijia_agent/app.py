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
    CaiyunWeatherCapability,
    CapabilityRegistry,
    CurrentDateTimeCapability,
    DeviceStatusCapability,
    HomeEnvironmentCapability,
)
from mijia_assistant.conversation import ConversationEngine, ConversationRepository
from mijia_assistant.models import AssistantContext, AssistantError, ModelMessage
from mijia_assistant.providers import OpenAICompatibleProvider

from .command_console import ConsoleAgentTools
from .command_idempotency import request_hash, valid_idempotency_key
from .command_models import CommandRequest, ProcessingResponse
from .command_service import CommandService
from .config import Settings
from .console import ConsoleTools
from .gateway import Gateway
from .models import AgentError, AssistantTurn, Turn
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
    "AI_EXECUTION_STATUS_UNKNOWN": (
        "AI_EXECUTION_STATUS_UNKNOWN",
        409,
        "执行结果待确认，请刷新状态核实；请勿重发。",
    ),
    "AI_ACTION_LEDGER_UNAVAILABLE": (
        "AI_ACTION_LEDGER_UNAVAILABLE",
        503,
        "安全执行记录暂时不可用，未能确认操作结果。",
    ),
    "AI_SCENE_REVISION_CHANGED": (
        "AI_SCENE_REVISION_CHANGED",
        409,
        "场景内容已变化，请重新确认后再试。",
    ),
    "AI_SCENE_RISK_BLOCKED": ("AI_SCENE_RISK_BLOCKED", 403, "该场景包含当前不支持的操作。"),
    "AI_SCENE_NOT_EXPOSED": ("AI_SCENE_NOT_EXPOSED", 403, "该场景未获家庭成员授权。"),
    "AI_EXPOSURE_STORE_UNAVAILABLE": (
        "AI_EXPOSURE_STORE_UNAVAILABLE",
        503,
        "家庭授权记录暂时不可用，请稍后再试。",
    ),
    "AUTOMATION_TOKEN_EXPIRED": ("AUTOMATION_TOKEN_EXPIRED", 401, "自动化凭据已过期，请重新生成"),
    "AUTOMATION_TOKEN_INVALID": ("AUTOMATION_TOKEN_INVALID", 401, "自动化凭据无效，请重新生成"),
    "AI_HOME_NOT_FOUND": ("AI_HOME_NOT_FOUND", 404, "未找到可用的家庭"),
    "IDEMPOTENCY_CONFLICT": ("IDEMPOTENCY_CONFLICT", 409, "请求重复且内容不一致"),
    "INVALID_REQUEST": ("INVALID_REQUEST", 400, "请求格式不正确"),
    "AI_SCENE_EXECUTION_DISABLED": ("AI_SCENE_EXECUTION_DISABLED", 403, "场景执行尚未开放"),
    "AI_CAPABILITY_UNAVAILABLE": (
        "AI_CAPABILITY_UNAVAILABLE",
        403,
        "该家庭尚未向 AI 助手开放这类只读信息",
    ),
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
            capabilities = [
                CurrentDateTimeCapability(),
                HomeEnvironmentCapability(console_tools),
                DeviceStatusCapability(console_tools),
            ]
            if config.caiyun_base_url and config.amap_base_url:
                capabilities.append(
                    CaiyunWeatherCapability(
                        client,
                        base_url=config.caiyun_base_url,
                        app_key=config.caiyun_app_key,
                        app_secret=config.caiyun_app_secret,
                        geocoding_url=config.amap_base_url,
                        geocoding_key=config.amap_api_key,
                        geocoding_private_key=config.amap_private_key,
                        timeout_seconds=config.weather_timeout_ms / 1000,
                        cache_ttl_seconds=config.weather_cache_ttl_seconds,
                    )
                )
            app.state.assistant_engine = assistant_engine or ConversationEngine(
                OpenAICompatibleProvider(gateway),
                CapabilityRegistry(capabilities),
                tool_result_logger=gateway.logger.log_tool_result,
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
            response_body = public_response(result)
            if body.channel in {"siri", "voice"}:
                response_body["answer"]["speechText"] = result.answer.text[:280]
            return JSONResponse(response_body, headers=headers)
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

    @app.post("/internal/v1/assistant")
    async def assistant_internal(request: Request):
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
            turn = AssistantTurn.model_validate(json.loads(raw))
            request_id = turn.requestId
            context = AssistantContext(
                request_id=turn.requestId,
                conversation_id=turn.conversationId,
                channel=turn.channel,
                locale=turn.locale,
                timezone=turn.timezone,
                scopes=frozenset(turn.scopes),
                deadline=datetime.now(timezone.utc) + timedelta(seconds=20),
                principal_ref=turn.principalId,
                home_ref=turn.homeId,
                home_selector=turn.homeId,
                automation_token=turn.automationToken,
            )
            history = [ModelMessage(role=item.role, content=item.content) for item in turn.history]
            result = await app.state.assistant_engine.run(context, turn.message, history)
            await app.state.conversation_repository.append(context, turn.message, result)
            event = next(
                (
                    item
                    for item in reversed(result.tool_events)
                    if item.status in {"success", "partial"}
                ),
                None,
            )
            intent = (
                {
                    "get_home_environment": "get_home_status",
                    "get_device_status": "get_device_status",
                }.get(event.name, "none")
                if event
                else "none"
            )
            body = {
                "requestId": result.request_id,
                "conversationId": result.conversation_id,
                "status": result.status,
                "outcome": result.outcome,
                "answer": {
                    "text": result.answer.text,
                    "speak": result.answer.speak,
                    "continueConversation": result.answer.continue_conversation,
                },
                "message": result.answer.text,
                "speak": result.answer.text[:280]
                if turn.channel in {"siri", "voice"}
                else result.answer.text,
                "intent": intent,
                "toolEvents": [
                    {"name": item.name, "status": item.status} for item in result.tool_events
                ],
                "usage": {
                    "promptTokens": result.usage.prompt_tokens,
                    "completionTokens": result.usage.completion_tokens,
                    "totalTokens": result.usage.total_tokens,
                    "estimated": result.usage.estimated,
                },
            }
            if result.data is not None:
                body["data"] = result.data
                if result.data.get("type") == "home_environment":
                    body["homeStatus"] = {
                        key: value for key, value in result.data.items() if key != "type"
                    }
                elif result.data.get("type") == "device_status":
                    body["deviceStatus"] = {
                        key: value for key, value in result.data.items() if key != "type"
                    }
            if event is not None and intent in {"get_home_status", "get_device_status"}:
                body["tool"] = {
                    "name": intent,
                    "status": "partial_success" if event.status == "partial" else "success",
                }
            return JSONResponse(body, headers=headers)
        except (ValueError, ValidationError):
            return JSONResponse(
                {"code": "AI_INVALID_REQUEST", "requestId": request_id},
                400,
                headers=headers,
            )
        except AssistantError as error:
            return JSONResponse(
                {
                    "code": error.code,
                    "message": "AI 助手暂时无法完成请求，请稍后再试。",
                    "requestId": request_id,
                },
                error.status,
                headers=headers,
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
