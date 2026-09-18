import hmac
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import Settings
from .console import ConsoleTools
from .gateway import Gateway
from .models import AgentError, Turn
from .service import AgentService


def create_lifespan(config: Settings, service: AgentService | None = None):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            app.state.service = service
            yield
            return
        async with httpx.AsyncClient(follow_redirects=False) as client:
            app.state.service = service or AgentService(
                Gateway(config, client),
                ConsoleTools(config, client),
                preview=config.environment == "preview",
            )
            yield

    return lifespan


def register_routes(app: FastAPI, config: Settings) -> FastAPI:

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

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

    return app


def create_app(
    settings: Settings | None = None,
    service: AgentService | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(
        lifespan=create_lifespan(config, service),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    return register_routes(app, config)
