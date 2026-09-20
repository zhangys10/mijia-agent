"""Request/response models for ``POST /ai/command``.

Mirrors the console's ``/api/ai/command`` public contract so Postman or a Siri
shortcut can call the Python agent without changing payloads. The console's
sealed ``sessionContext`` is intentionally not reproduced: sealing stays
console-owned and this ingress accepts an explicit bounded ``history`` array
instead, echoing back only ``conversationId``/``turnIndex``/``conversationReset``.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Scene

MAX_AUTOMATION_TOKEN = 8192


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HistoryMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("content")
    @classmethod
    def trim_content(cls, value: str) -> str:
        trimmed = value.strip()[:300]
        if not trimmed:
            raise ValueError("empty history content")
        return trimmed


class ClientTag(StrictModel):
    type: Literal["siri_shortcut"]


class CommandRequest(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=200)]
    home: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    locale: Literal["zh-CN"] = "zh-CN"
    timezone: Literal["Asia/Shanghai", "UTC"] = "Asia/Shanghai"
    client: ClientTag | None = None
    conversationId: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    history: Annotated[list[HistoryMessage], Field(max_length=32)] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def trim_text(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("empty text")
        return trimmed

    @field_validator("home")
    @classmethod
    def trim_home(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("conversationId")
    @classmethod
    def trim_conversation(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class CommandResponse(StrictModel):
    """AiCommandResponse parity: executor status wins, model text is the reply."""

    requestId: str
    conversationId: str
    conversationReset: bool = False
    turnIndex: int = 1
    status: Literal["completed", "partial_success", "not_understood"]
    intent: Literal["activate_scene", "none"] = "none"
    sceneId: str | None = None
    sceneName: str | None = None
    message: str
    execution: dict | None = None
    decisionSource: Literal["llm", "deterministic_fallback"] = "llm"
    llmOutput: str | None = None


class ProcessingResponse(StrictModel):
    """202 body while a prior request with the same Idempotency-Key is running."""

    requestId: str
    status: Literal["processing"] = "processing"
    message: str = "请求正在处理"


class CommandDecision(StrictModel):
    """Internal decision: console IntentDecision parity."""

    type: Literal["tool_call", "no_action"]
    scene: Scene | None = None
    reply: str = ""
    llm_output: str = ""
    decisionSource: Literal["llm", "deterministic_fallback"] = "llm"
