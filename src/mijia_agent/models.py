from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Message(StrictModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=2000)]


class Turn(StrictModel):
    requestId: Annotated[str, Field(min_length=16, max_length=128)]
    conversationId: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{6,36}$")]
    principalId: Annotated[str, Field(pattern=r"^usr_[A-Za-z0-9_-]{1,128}$")]
    homeId: Annotated[str, Field(min_length=1, max_length=100)]
    message: Annotated[str, Field(min_length=1, max_length=500)]
    idempotencyKey: Annotated[str, Field(min_length=16, max_length=128)]
    scopes: list[Literal["ai:chat", "scene:activate"]]
    sessionBinding: SecretStr
    locale: Literal["zh-CN", "en-US"] = "zh-CN"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    history: Annotated[list[Message], Field(max_length=12)] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def trim_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty message")
        return value.strip()

    @field_validator("sessionBinding")
    @classmethod
    def binding_size(cls, value: SecretStr) -> SecretStr:
        if not 1 <= len(value.get_secret_value()) <= 16384:
            raise ValueError("invalid binding")
        return value


class Scene(StrictModel):
    alias: Annotated[str, Field(pattern=r"^scene_[a-f0-9]{16}$")]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(max_length=500)]
    actionCount: Annotated[int, Field(ge=0)]


HomeMetric = Literal[
    "temperature",
    "humidity",
    "co2",
    "formaldehyde",
    "pm25",
    "pm10",
    "tvoc",
    "pressure",
    "battery",
]


class HomeStatusReading(StrictModel):
    value: Annotated[float, Field(allow_inf_nan=False, ge=-1000000, le=1000000)]
    unit: Annotated[str, Field(min_length=1, max_length=24)]
    sourceLabel: Annotated[str, Field(min_length=1, max_length=200)]
    roomName: Annotated[str | None, Field(default=None, max_length=200)] = None
    capturedAt: Annotated[str, Field(min_length=1, max_length=40)]
    freshness: Literal["fresh", "stale"] = "fresh"

    @field_validator("value", mode="before")
    @classmethod
    def widen_integer(cls, value: Any) -> Any:
        if type(value) is int:
            return float(value)
        return value


class HomeStatusGroup(StrictModel):
    metric: HomeMetric
    label: Annotated[str, Field(min_length=1, max_length=40)]
    unit: Annotated[str, Field(min_length=1, max_length=24)]
    latest: HomeStatusReading | None = None
    readings: Annotated[list[HomeStatusReading], Field(max_length=20)] = Field(default_factory=list)


class HomeStatus(StrictModel):
    capturedAt: Annotated[str, Field(min_length=1, max_length=40)]
    completeness: Literal["complete", "partial", "empty"]
    groups: Annotated[list[HomeStatusGroup], Field(max_length=16)] = Field(default_factory=list)
    warnings: Annotated[list[str], Field(max_length=8)] = Field(default_factory=list)


class Usage(StrictModel):
    promptTokens: Annotated[int, Field(ge=0)] = 0
    completionTokens: Annotated[int, Field(ge=0)] = 0
    totalTokens: Annotated[int, Field(ge=0)] = 0
    estimated: bool = False


class Decision(StrictModel):
    tool: Literal["none", "list_scenes", "get_home_status", "activate_scene"]
    sceneId: str | None = None
    message: str = ""
    replyMessage: str = ""
    usage: Usage = Field(default_factory=Usage)


class Execution(StrictModel):
    status: Literal["success", "partial_success"]
    message: Annotated[str, Field(min_length=1, max_length=2000)]


class ToolResult(StrictModel):
    name: Literal["list_scenes", "get_home_status", "activate_scene"]
    status: Literal["success", "partial_success"]
    sceneName: str | None = None


class Result(StrictModel):
    requestId: str
    conversationId: str
    message: str
    intent: Literal["none", "list_scenes", "get_home_status", "activate_scene"]
    tool: ToolResult | None = None
    scenes: list[Scene] | None = None
    homeStatus: HomeStatus | None = None
    usage: Usage = Field(default_factory=Usage)


class AgentError(Exception):
    def __init__(self, code: str, status: int = 502, usage: Usage | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.usage = usage
