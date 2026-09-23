from typing import Annotated, ClassVar, Literal

from pydantic import Field, ValidationError

from mijia_assistant.models import AssistantContext, AssistantError, CapabilityResult, StrictModel


class WeatherArguments(StrictModel):
    location: Annotated[str, Field(min_length=1, max_length=120)]
    days: Annotated[int, Field(ge=1, le=7)] = 1


class FakeWeatherCapability:
    """Deterministic Phase 0 fixture; never performs network access."""

    name = "get_weather"
    description = "Get current weather and a short forecast for an explicit location."
    risk: Literal["general_read"] = "general_read"
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["location"],
        "properties": {
            "location": {"type": "string", "minLength": 1, "maxLength": 120},
            "days": {"type": "integer", "minimum": 1, "maximum": 7},
        },
    }

    def __init__(self):
        self.calls = 0

    async def is_available(self, ctx: AssistantContext) -> bool:
        return "ai:chat" in ctx.scopes

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        try:
            request = WeatherArguments.model_validate(args)
        except ValidationError:
            raise AssistantError("INVALID_TOOL_ARGUMENTS") from None
        self.calls += 1
        snapshot = {
            "provider": "phase0_fake",
            "location": request.location,
            "condition": "阵雨",
            "temperature": 29.0,
            "unit": "°C",
            "forecastDays": request.days,
            "freshness": "fresh",
            "alertsSupported": False,
        }
        return CapabilityResult(
            status="success", model_content=snapshot, client_data={"type": "weather", **snapshot}
        )
