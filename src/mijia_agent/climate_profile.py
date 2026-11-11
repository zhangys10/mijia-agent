import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PreviousClimateConfiguration(StrictModel):
    targetTemperature: Annotated[float, Field(ge=5, le=40, allow_inf_nan=False)]
    mode: Annotated[str, Field(min_length=1, max_length=40)] | None


class ClimateObservation(StrictModel):
    observationRef: Annotated[str, Field(pattern=r"^obs_[a-f0-9]{24}$")]
    roomRef: Annotated[str, Field(pattern=r"^room_[a-f0-9]{24}$")]
    actuatorRef: Annotated[str, Field(pattern=r"^actuator_[a-f0-9]{24}$")]
    ambientTemperature: Annotated[float, Field(ge=-40, le=80, allow_inf_nan=False)]
    targetTemperature: Annotated[float, Field(ge=5, le=40, allow_inf_nan=False)]
    mode: Annotated[str, Field(min_length=1, max_length=40)] | None
    powerOn: bool | None = None
    capturedAt: datetime
    localHour: Annotated[int, Field(ge=0, le=23)]
    dayOfWeek: Annotated[int, Field(ge=1, le=7)]
    source: Literal["console_override", "external_override"]
    excluded: bool = False
    previous: PreviousClimateConfiguration

    @field_validator("capturedAt", mode="before")
    @classmethod
    def require_timezone(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("capturedAt must be timezone-aware")
        return value


class ClimateProfileRequest(StrictModel):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    observations: Annotated[list[ClimateObservation], Field(max_length=4096)]


class ClimateProfileCell(StrictModel):
    timeWindow: Literal["00-06", "06-12", "12-18", "18-24"]
    ambientBand: Literal["below-16", "16-20", "20-24", "24-28", "28-plus"]
    ambientRange: tuple[float | None, float | None]
    status: Literal["learned", "insufficient_evidence"]
    derivedTargetTemperature: float | None
    derivedMode: str | None
    confidence: Annotated[float, Field(ge=0, le=1)]
    supportCount: Annotated[int, Field(ge=0)]
    distinctDates: Annotated[int, Field(ge=0)]
    contributingFrom: datetime | None
    contributingTo: datetime | None
    explanation: Annotated[str, Field(min_length=1, max_length=240)]


class ClimateRoomProfile(StrictModel):
    roomRef: Annotated[str, Field(pattern=r"^room_[a-f0-9]{24}$")]
    actuatorRef: Annotated[str, Field(pattern=r"^actuator_[a-f0-9]{24}$")]
    cells: list[ClimateProfileCell]


class ClimateProfileResponse(StrictModel):
    version: Literal["1"] = "1"
    revision: Annotated[str, Field(pattern=r"^climate_[a-f0-9]{24}$")]
    generatedAt: datetime
    rooms: list[ClimateRoomProfile]


TIME_WINDOWS = ("00-06", "06-12", "12-18", "18-24")
AMBIENT_BANDS = (
    ("below-16", None, 16.0),
    ("16-20", 16.0, 20.0),
    ("20-24", 20.0, 24.0),
    ("24-28", 24.0, 28.0),
    ("28-plus", 28.0, None),
)


def _window(hour: int) -> str:
    return TIME_WINDOWS[min(3, hour // 6)]


def _band(value: float) -> tuple[str, float | None, float | None]:
    for name, low, high in AMBIENT_BANDS:
        if (low is None or value >= low) and (high is None or value < high):
            return name, low, high
    raise AssertionError("unreachable ambient band")


def _weighted_median(values: list[tuple[float, float]]) -> float:
    ordered = sorted(values)
    midpoint = sum(weight for _, weight in ordered) / 2
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= midpoint:
            return value
    return ordered[-1][0]


def derive_climate_profile(
    request: ClimateProfileRequest, *, now: datetime | None = None
) -> ClimateProfileResponse:
    generated_at = now or datetime.now(timezone.utc)
    usable = [item for item in request.observations if not item.excluded]
    room_actuators = sorted({(item.roomRef, item.actuatorRef) for item in usable})
    rooms: list[ClimateRoomProfile] = []
    for room_ref, actuator_ref in room_actuators:
        cells: list[ClimateProfileCell] = []
        room_items = [
            item
            for item in usable
            if item.roomRef == room_ref and item.actuatorRef == actuator_ref
        ]
        for time_window in TIME_WINDOWS:
            for band_name, low, high in AMBIENT_BANDS:
                items = [
                    item
                    for item in room_items
                    if _window(item.localHour) == time_window
                    and _band(item.ambientTemperature)[0] == band_name
                ]
                dates = {item.capturedAt.date() for item in items}
                learned = len(items) >= 6 and len(dates) >= 3
                if items:
                    newest = max(item.capturedAt for item in items)
                    values = []
                    for item in items:
                        age_days = max(0.0, (newest - item.capturedAt).total_seconds() / 86400)
                        values.append((item.targetTemperature, math.pow(0.5, age_days / 30)))
                    target = round(_weighted_median(values), 2) if learned else None
                    modes = Counter(item.mode for item in items if item.mode is not None)
                    if modes:
                        top_mode, top_count = modes.most_common(1)[0]
                        mode = top_mode if learned and top_count >= 3 and list(modes.values()).count(top_count) == 1 else None
                    else:
                        mode = None
                    confidence = min(1.0, (len(items) / 10) * min(1.0, len(dates) / 3))
                    first, last = min(item.capturedAt for item in items), newest
                else:
                    target = mode = first = last = None
                    confidence = 0.0
                status = "learned" if learned else "insufficient_evidence"
                explanation = (
                    f"基于 {len(items)} 次手动设置、{len(dates)} 个日期计算。"
                    if learned
                    else f"证据不足：已有 {len(items)} 次设置、{len(dates)} 个日期；至少需要 6 次且跨 3 天。"
                )
                cells.append(
                    ClimateProfileCell(
                        timeWindow=time_window,
                        ambientBand=band_name,
                        ambientRange=(low, high),
                        status=status,
                        derivedTargetTemperature=target,
                        derivedMode=mode,
                        confidence=round(confidence, 3),
                        supportCount=len(items),
                        distinctDates=len(dates),
                        contributingFrom=first,
                        contributingTo=last,
                        explanation=explanation,
                    )
                )
        rooms.append(
            ClimateRoomProfile(
                roomRef=room_ref, actuatorRef=actuator_ref, cells=cells
            )
        )
    canonical = json.dumps(
        [room.model_dump(mode="json") for room in rooms],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    revision = "climate_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return ClimateProfileResponse(
        revision=revision, generatedAt=generated_at, rooms=rooms
    )
