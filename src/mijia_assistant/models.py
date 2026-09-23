from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Usage(StrictModel):
    prompt_tokens: Annotated[int, Field(ge=0)] = 0
    completion_tokens: Annotated[int, Field(ge=0)] = 0
    total_tokens: Annotated[int, Field(ge=0)] = 0
    estimated: bool = False

    def plus(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            estimated=self.estimated or other.estimated,
        )


class ToolCall(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    arguments: dict[str, Any]


class ModelMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Annotated[str, Field(max_length=12000)] = ""
    tool_calls: Annotated[list[ToolCall], Field(max_length=8)] = Field(default_factory=list)
    tool_call_id: Annotated[str | None, Field(default=None, max_length=128)] = None
    name: Annotated[str | None, Field(default=None, max_length=64)] = None


class ModelTurn(StrictModel):
    content: Annotated[str, Field(max_length=4000)] = ""
    tool_calls: Annotated[list[ToolCall], Field(max_length=8)] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    response_kind: Literal["answer", "clarification"] = "answer"
    truncated: bool = False


RiskClass = Literal[
    "general_read", "home_read", "home_write_low", "home_write_high", "external_write"
]


@dataclass(frozen=True)
class AssistantContext:
    """Trusted server context. Server-only values are never serialized to the model."""

    request_id: str
    conversation_id: str
    channel: Literal["web", "siri", "voice", "automation"] = "web"
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"ai:chat"}))
    deadline: datetime = field(default_factory=lambda: datetime.max.replace(tzinfo=timezone.utc))
    principal_ref: str | None = None
    home_ref: str | None = None
    home_selector: str | None = None
    automation_token: SecretStr | None = field(default=None, repr=False)


class CapabilityResult(StrictModel):
    status: Literal["success", "partial", "error", "outcome_unknown"]
    model_content: dict[str, Any] | str | None = None
    client_data: dict[str, Any] | None = None
    display_text: Annotated[str | None, Field(default=None, max_length=4000)] = None
    persistence: Literal["full", "redacted", "none"] = "none"
    is_terminal: bool = False


class ToolEvent(StrictModel):
    name: str
    status: Literal["success", "partial", "error", "outcome_unknown", "rejected"]


class Answer(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    speak: bool = True
    continue_conversation: bool = False


class AssistantResponse(StrictModel):
    request_id: str
    conversation_id: str
    status: Literal["completed", "failed"]
    outcome: Literal[
        "direct_answer",
        "tool_answer",
        "clarification",
        "action_result",
        "refused",
        "failed",
        "outcome_unknown",
    ]
    answer: Answer
    data: dict[str, Any] | None = None
    tool_events: list[ToolEvent] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)


class AssistantError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status
