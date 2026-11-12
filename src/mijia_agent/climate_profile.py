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
    targetTemperature: Annotated[float, Field(ge=5, le=80, allow_inf_nan=False)]
    mode: Annotated[str, Field(min_length=1, max_length=40)] | None


class ClimateObservation(StrictModel):
    observationRef: Annotated[str, Field(pattern=r"^obs_[a-f0-9]{24}$")]
    roomRef: Annotated[str, Field(pattern=r"^room_[a-f0-9]{24}$")]
    actuatorRef: Annotated[str, Field(pattern=r"^actuator_[a-f0-9]{24}$")]
    ambientTemperature: Annotated[float, Field(ge=-40, le=80, allow_inf_nan=False)]
    targetTemperature: Annotated[float, Field(ge=5, le=80, allow_inf_nan=False)]
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


class ClimateHistoryPoint(StrictModel):
    sampleRef: Annotated[str, Field(pattern=r"^sample_[a-f0-9]{24}$")]
    roomRef: Annotated[str, Field(pattern=r"^room_[a-f0-9]{24}$")]
    actuatorRef: Annotated[str, Field(pattern=r"^actuator_[a-f0-9]{24}$")]
    ambientTemperature: Annotated[float, Field(ge=-40, le=80, allow_inf_nan=False)]
    targetTemperature: Annotated[float, Field(ge=5, le=80, allow_inf_nan=False)]
    mode: Annotated[str, Field(min_length=1, max_length=40)] | None
    powerOn: bool | None = None
    capturedAt: datetime
    localHour: Annotated[int, Field(ge=0, le=23)]
    dayOfWeek: Annotated[int, Field(ge=1, le=7)]

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
    historyPoints: Annotated[list[ClimateHistoryPoint], Field(max_length=4096)] = Field(
        default_factory=list
    )


class ClimateProfileCell(StrictModel):
    timeWindow: Literal["00-06", "06-12", "12-18", "18-24"]
    ambientBand: Literal["below-16", "16-20", "20-24", "24-28", "28-plus"]
    ambientRange: tuple[float | None, float | None]
    status: Literal["learned", "suggested", "insufficient_evidence"]
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
    history = request.historyPoints
    room_actuators = sorted({(item.roomRef, item.actuatorRef) for item in [*usable, *history]})
    rooms: list[ClimateRoomProfile] = []
    for room_ref, actuator_ref in room_actuators:
        cells: list[ClimateProfileCell] = []
        room_items = [
            item for item in usable if item.roomRef == room_ref and item.actuatorRef == actuator_ref
        ]
        room_history = [
            item
            for item in history
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
                history_items = [
                    item
                    for item in room_history
                    if _window(item.localHour) == time_window
                    and _band(item.ambientTemperature)[0] == band_name
                ]
                manual_dates = {item.capturedAt.date() for item in items}
                evidence = [*items, *history_items]
                evidence_dates = {item.capturedAt.date() for item in evidence}
                learned = len(items) >= 6 and len(manual_dates) >= 3
                if learned:
                    newest = max(item.capturedAt for item in items)
                    values = []
                    for item in items:
                        age_days = max(0.0, (newest - item.capturedAt).total_seconds() / 86400)
                        values.append((item.targetTemperature, math.pow(0.5, age_days / 30)))
                    target = round(_weighted_median(values), 2)
                    modes = Counter(item.mode for item in items if item.mode is not None)
                    if modes:
                        top_mode, top_count = modes.most_common(1)[0]
                        mode = (
                            top_mode
                            if top_count >= 3 and list(modes.values()).count(top_count) == 1
                            else None
                        )
                    else:
                        mode = None
                    confidence = min(1.0, (len(items) / 10) * min(1.0, len(manual_dates) / 3))
                    first, last = min(item.capturedAt for item in items), newest
                    status = "learned"
                    support_count = len(items)
                    dates = manual_dates
                    explanation = f"基于 {len(items)} 次手动设置、{len(manual_dates)} 个日期计算。"
                elif evidence:
                    newest = max(item.capturedAt for item in evidence)
                    weighted_targets: list[tuple[float, float]] = []
                    weighted_modes: Counter[str] = Counter()
                    for item in evidence:
                        age_days = max(0.0, (newest - item.capturedAt).total_seconds() / 86400)
                        source_weight = 4.0 if item in items else 1.0
                        power_weight = 0.5 if item.powerOn is False else 1.0
                        weight = source_weight * power_weight * math.pow(0.5, age_days / 30)
                        weighted_targets.append((item.targetTemperature, weight))
                        if item.mode is not None:
                            weighted_modes[item.mode] += weight
                    target = round(_weighted_median(weighted_targets), 2)
                    if weighted_modes:
                        ranked_modes = weighted_modes.most_common()
                        mode = (
                            ranked_modes[0][0]
                            if len(ranked_modes) == 1 or ranked_modes[0][1] > ranked_modes[1][1]
                            else None
                        )
                    else:
                        mode = None
                    confidence = min(
                        0.65,
                        0.15
                        + min(0.35, len(evidence) * 0.025)
                        + min(0.15, len(evidence_dates) * 0.03),
                    )
                    first, last = min(item.capturedAt for item in evidence), newest
                    status = "suggested"
                    support_count = len(evidence)
                    dates = evidence_dates
                    explanation = (
                        f"临时建议：基于 {len(history_items)} 个周期状态点和 "
                        f"{len(items)} 次手动设置；更多手动证据可提升为已学习。"
                    )
                else:
                    target = mode = first = last = None
                    confidence = 0.0
                    status = "insufficient_evidence"
                    support_count = 0
                    dates = set()
                    explanation = "证据不足：尚无该时段和温度区间的历史状态或手动设置。"
                cells.append(
                    ClimateProfileCell(
                        timeWindow=time_window,
                        ambientBand=band_name,
                        ambientRange=(low, high),
                        status=status,
                        derivedTargetTemperature=target,
                        derivedMode=mode,
                        confidence=round(confidence, 3),
                        supportCount=support_count,
                        distinctDates=len(dates),
                        contributingFrom=first,
                        contributingTo=last,
                        explanation=explanation,
                    )
                )
        rooms.append(ClimateRoomProfile(roomRef=room_ref, actuatorRef=actuator_ref, cells=cells))
    canonical = json.dumps(
        [room.model_dump(mode="json") for room in rooms],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    revision = "climate_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return ClimateProfileResponse(revision=revision, generatedAt=generated_at, rooms=rooms)
