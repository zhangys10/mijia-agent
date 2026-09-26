import hmac
import json
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
from mijia_assistant.conversation.history import model_history_answer
from mijia_assistant.models import AssistantContext, AssistantError, ModelMessage
from mijia_assistant.providers import OpenAICompatibleProvider

from .config import Settings
from .console_tools import ConsoleAgentTools
from .gateway import Gateway
from .models import AgentError, AssistantTurn

MAX_ASSISTANT_BODY = 65536
MAX_AUTOMATION_TOKEN = 8192


def create_lifespan(
    config: Settings,
    assistant_engine: ConversationEngine | None = None,
    assistant_tools: ConsoleAgentTools | None = None,
    conversation_repository: ConversationRepository | None = None,
):
    @asynccontextmanager
    async def lifespan(app):
        if assistant_engine is not None and assistant_tools is not None:
            app.state.assistant_engine = assistant_engine
            app.state.assistant_tools = assistant_tools
            app.state.conversation_repository = conversation_repository or ConversationRepository()
            yield
            return
        async with httpx.AsyncClient(follow_redirects=False) as client:
            gateway = Gateway(config, client)
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

    @app.post("/ai/assistant")
    async def assistant(request: Request):
        headers = {"Cache-Control": "no-store"}
        token = bearer_token(request.headers.get("authorization"))
        if not token:
            return JSONResponse({"code": "UNAUTHORIZED"}, 401, headers=headers)
        if len(token) > MAX_AUTOMATION_TOKEN:
            return JSONResponse({"code": "AUTOMATION_TOKEN_INVALID"}, 401, headers=headers)
        if config.environment == "preview":
            return JSONResponse({"code": "AI_PREVIEW_READ_ONLY"}, 403, headers=headers)
        request_id = "req_" + uuid.uuid4().hex
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_ASSISTANT_BODY:
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
        if config.environment == "preview":
            return JSONResponse({"code": "AI_PREVIEW_READ_ONLY"}, 403, headers=headers)
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
                "historyAnswer": model_history_answer(result),
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
    assistant_engine: ConversationEngine | None = None,
    assistant_tools: ConsoleAgentTools | None = None,
    conversation_repository: ConversationRepository | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(
        lifespan=create_lifespan(
            config,
            assistant_engine,
            assistant_tools,
            conversation_repository,
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    return register_routes(app, config)
