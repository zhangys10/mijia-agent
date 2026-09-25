from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


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


class AssistantTurn(StrictModel):
    """Canonical assistant turn forwarded by the Makers adapter.

    ``automationToken`` is opaque to Python.  It is only forwarded to the
    console's read-only tools endpoint after the model selects a home
    capability.
    """

    requestId: Annotated[str, Field(min_length=16, max_length=128)]
    conversationId: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{6,36}$")]
    principalId: Annotated[str, Field(pattern=r"^usr_[A-Za-z0-9_-]{1,128}$")]
    homeId: Annotated[str, Field(min_length=1, max_length=100)]
    message: Annotated[str, Field(min_length=1, max_length=500)]
    idempotencyKey: Annotated[str, Field(min_length=16, max_length=128)]
    scopes: list[Literal["ai:chat"]]
    automationToken: SecretStr
    locale: Literal["zh-CN", "en-US"] = "zh-CN"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    channel: Literal["web", "siri", "voice", "automation"] = "web"
    history: Annotated[list[Message], Field(max_length=12)] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def trim_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty message")
        return value.strip()

    @field_validator("automationToken")
    @classmethod
    def token_size(cls, value: SecretStr) -> SecretStr:
        if not 1 <= len(value.get_secret_value()) <= 8192:
            raise ValueError("invalid automation token")
        return value


class SceneActionDetail(StrictModel):
    label: Annotated[str, Field(max_length=80)]
    value: Annotated[str, Field(max_length=80)]


class SceneActionSummary(StrictModel):
    room: Annotated[str | None, Field(default=None, max_length=200)] = None
    device: Annotated[str | None, Field(default=None, max_length=200)] = None
    actions: Annotated[list[SceneActionDetail], Field(max_length=12)] = Field(default_factory=list)


class Scene(StrictModel):
    alias: Annotated[str, Field(pattern=r"^scene_[a-f0-9]{16}$")]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(max_length=500)]
    actionCount: Annotated[int, Field(ge=0)]
    revision: Annotated[str, Field(pattern=r"^rev_[a-f0-9]{24}$")] = "rev_000000000000000000000000"
    risk: Literal["low", "blocked"] = "blocked"
    actionSummaries: Annotated[list[SceneActionSummary], Field(max_length=32)] = Field(
        default_factory=list
    )


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


DeviceState = Literal["on", "off", "unknown"]


class DeviceStatusItem(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    kind: Annotated[str, Field(min_length=1, max_length=40)]
    state: DeviceState
    online: bool


class DeviceStatusRoom(StrictModel):
    room: Annotated[str, Field(min_length=1, max_length=200)]
    items: Annotated[list[DeviceStatusItem], Field(max_length=40)] = Field(default_factory=list)


class DeviceStatus(StrictModel):
    capturedAt: Annotated[str, Field(min_length=1, max_length=40)]
    completeness: Literal["complete", "partial", "empty"]
    poweredOn: Annotated[int, Field(ge=0)]
    rooms: Annotated[list[DeviceStatusRoom], Field(max_length=20)] = Field(default_factory=list)
    warnings: Annotated[list[str], Field(max_length=8)] = Field(default_factory=list)


class HomeCapability(StrictModel):
    name: Literal["get_home_environment", "get_device_status"]
    available: bool
    risk: Literal["home_read"]


class HomeCapabilityProjection(StrictModel):
    rooms: Annotated[list[str], Field(max_length=20)] = Field(default_factory=list)
    measurementTypes: Annotated[list[HomeMetric], Field(max_length=9)] = Field(default_factory=list)
    deviceKinds: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=40)
    ] = Field(default_factory=list)
    roomMetrics: Annotated[
        dict[
            Annotated[str, Field(min_length=1, max_length=200)],
            Annotated[list[HomeMetric], Field(max_length=9)],
        ],
        Field(max_length=20),
    ]
    roomDeviceKinds: Annotated[
        dict[
            Annotated[str, Field(min_length=1, max_length=200)],
            Annotated[
                list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=40)
            ],
        ],
        Field(max_length=20),
    ]
    sceneSearchAvailable: bool

    @model_validator(mode="after")
    def validate_exposure_lists(self):
        rooms = set(self.rooms)
        metrics = set(self.measurementTypes)
        kinds = set(self.deviceKinds)
        if any(
            room not in rooms or any(metric not in metrics for metric in values)
            for room, values in self.roomMetrics.items()
        ):
            raise ValueError("invalid room metrics")
        if any(
            room not in rooms or any(kind not in kinds for kind in values)
            for room, values in self.roomDeviceKinds.items()
        ):
            raise ValueError("invalid room device kinds")
        return self


class HomeCapabilities(StrictModel):
    contextVersion: Literal["1"]
    exposureRevision: Annotated[str, Field(min_length=1, max_length=64)]
    capabilities: Annotated[list[HomeCapability], Field(max_length=4)] = Field(default_factory=list)
    projection: HomeCapabilityProjection


class Usage(StrictModel):
    promptTokens: Annotated[int, Field(ge=0)] = 0
    completionTokens: Annotated[int, Field(ge=0)] = 0
    totalTokens: Annotated[int, Field(ge=0)] = 0
    estimated: bool = False


class Decision(StrictModel):
    tool: Literal["none", "list_scenes", "get_home_status", "get_device_status", "activate_scene"]
    sceneId: str | None = None
    message: str = ""
    replyMessage: str = ""
    usage: Usage = Field(default_factory=Usage)


class Execution(StrictModel):
    status: Literal["success", "partial_success"]
    message: Annotated[str, Field(min_length=1, max_length=2000)]


class ToolResult(StrictModel):
    name: Literal["list_scenes", "get_home_status", "get_device_status", "activate_scene"]
    status: Literal["success", "partial_success"]
    sceneName: str | None = None


class Result(StrictModel):
    requestId: str
    conversationId: str
    message: str
    intent: Literal["none", "list_scenes", "get_home_status", "get_device_status", "activate_scene"]
    tool: ToolResult | None = None
    scenes: list[Scene] | None = None
    homeStatus: HomeStatus | None = None
    deviceStatus: DeviceStatus | None = None
    usage: Usage = Field(default_factory=Usage)


class AgentError(Exception):
    def __init__(
        self,
        code: str,
        status: int = 502,
        usage: Usage | None = None,
        diagnostic_code: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.status = status
        self.usage = usage
        self.diagnostic_code = diagnostic_code
