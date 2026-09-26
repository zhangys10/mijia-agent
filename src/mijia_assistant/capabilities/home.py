from typing import ClassVar, Literal

from mijia_agent.console_tools import ConsoleAgentTools
from mijia_agent.models import AgentError, HomeCapabilities
from mijia_assistant.models import AssistantContext, AssistantError, CapabilityResult

HOME_METRICS = (
    "temperature",
    "humidity",
    "co2",
    "formaldehyde",
    "pm25",
    "pm10",
    "tvoc",
    "pressure",
    "battery",
)


class _HomeReadCapability:
    risk: Literal["home_read"] = "home_read"
    input_schema: ClassVar[dict]

    def __init__(self, tools: ConsoleAgentTools, manifest: HomeCapabilities | None = None):
        self.tools = tools
        self.manifest = manifest

    def bind(self, manifest: HomeCapabilities):
        if not any(item.name == self.name and item.available for item in manifest.capabilities):
            return None
        return type(self)(self.tools, manifest)

    async def is_available(self, ctx: AssistantContext) -> bool:
        return ctx.automation_token is not None and "ai:chat" in ctx.scopes

    @staticmethod
    def _token(ctx: AssistantContext) -> str:
        if ctx.automation_token is None:
            raise AssistantError("HOME_CONTEXT_UNAVAILABLE", 403)
        return ctx.automation_token.get_secret_value()

    @staticmethod
    def _validate_args(args: dict, allowed: set[str]) -> dict:
        if not isinstance(args, dict) or set(args) - allowed:
            raise AssistantError("INVALID_TOOL_ARGUMENTS")
        normalized = {}
        for key in allowed:
            value = args.get(key)
            if value is None:
                continue
            limit = 40 if key == "kinds" else 20 if key == "rooms" else 9 if key == "metrics" else 3
            max_length = 40 if key == "kinds" else 200
            options = (
                set(HOME_METRICS)
                if key == "metrics"
                else {"on", "off", "unknown"}
                if key == "states"
                else None
            )
            if (
                not isinstance(value, list)
                or len(value) > limit
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item) > max_length
                    or (options is not None and item not in options)
                    for item in value
                )
            ):
                raise AssistantError("INVALID_TOOL_ARGUMENTS")
            normalized[key] = list(dict.fromkeys(value))
        return normalized


class HomeExposureDiscoveryCapability(_HomeReadCapability):
    name = "discover_home_exposure"
    description = "Discover the home's approved read-only rooms, measurements, and device kinds before requesting home data."
    input_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        if args:
            raise AssistantError("INVALID_TOOL_ARGUMENTS", 400)
        try:
            manifest = await self.tools.capabilities_v1(
                self._token(ctx), ctx.request_id, ctx.home_selector
            )
        except AgentError as error:
            raise AssistantError(error.code, error.status, error.diagnostic_code) from None
        return CapabilityResult(status="success", model_content=manifest.model_dump())


class HomeEnvironmentCapability(_HomeReadCapability):
    name = "get_home_environment"
    description = "Read the exposed home's current environmental measurements."
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rooms": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 20,
            },
            "metrics": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(HOME_METRICS),
                },
                "maxItems": 9,
            },
        },
    }

    def __init__(self, tools: ConsoleAgentTools, manifest: HomeCapabilities | None = None):
        super().__init__(tools, manifest)
        if manifest is not None:
            rooms = list(manifest.projection.roomMetrics)
            metrics = manifest.projection.measurementTypes
            room_metric_choices = "; ".join(
                f"{room}: {', '.join(manifest.projection.roomMetrics[room])}" for room in rooms
            )
            self.description = (
                "Read the exposed home's current environmental measurements. Only request "
                f"room/metric pairs from the discovered exposure list: {room_metric_choices}."
            )
            self.input_schema = {
                **self.input_schema,
                "properties": {
                    "rooms": {
                        "type": "array",
                        "items": {"type": "string", "enum": rooms},
                        "maxItems": 20,
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string", "enum": metrics},
                        "maxItems": 9,
                    },
                },
            }

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        filters = self._validate_args(args, {"rooms", "metrics"})
        if self.manifest is not None:
            allowed_rooms = self.manifest.projection.roomMetrics
            if any(room not in allowed_rooms for room in filters.get("rooms", [])) or any(
                metric not in self.manifest.projection.measurementTypes
                for metric in filters.get("metrics", [])
            ):
                raise AssistantError("INVALID_TOOL_ARGUMENTS", 400)
            selected_rooms = filters.get("rooms", list(allowed_rooms))
            selected_metrics = filters.get("metrics")
            if selected_metrics is not None and any(
                metric
                not in {
                    exposed_metric
                    for room in selected_rooms
                    for exposed_metric in allowed_rooms[room]
                }
                for metric in selected_metrics
            ):
                raise AssistantError("INVALID_TOOL_ARGUMENTS", 400)
        try:
            status = await self.tools.invoke_home_environment(
                self._token(ctx), ctx.request_id, ctx.home_selector, filters
            )
        except AgentError as error:
            raise AssistantError(error.code, error.status, error.diagnostic_code) from None
        readings = []
        group_sources = [
            group.readings or ([group.latest] if group.latest is not None else [])
            for group in status.groups
        ]
        for index in range(max((len(source) for source in group_sources), default=0)):
            for group, source in zip(status.groups, group_sources, strict=True):
                if len(readings) >= 32:
                    break
                if index >= len(source):
                    continue
                reading = source[index]
                readings.append(
                    {
                        "metric": group.metric,
                        "label": group.label,
                        "value": reading.value,
                        "unit": reading.unit,
                        "roomName": reading.roomName[:80] if reading.roomName else None,
                        "capturedAt": reading.capturedAt,
                        "freshness": reading.freshness,
                    }
                )
            if len(readings) >= 32:
                break
        content = {
            "completeness": status.completeness,
            "capturedAt": status.capturedAt,
            "readings": readings,
            "truncated": sum(len(source) for source in group_sources) > len(readings),
            "warnings": status.warnings,
        }
        display = status.model_dump(exclude_none=True)
        return CapabilityResult(
            status="partial" if status.completeness == "partial" else "success",
            model_content=content,
            client_data={"type": "home_environment", **display},
            display_text=(
                "当前家庭暂无可用的环境读数。"
                if status.completeness == "empty"
                else "已读取当前家庭环境状态。"
            ),
        )


class DeviceStatusCapability(_HomeReadCapability):
    name = "get_device_status"
    description = "Read the exposed home's current per-room device power status."
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rooms": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 20,
            },
            "kinds": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 40},
                "maxItems": 40,
            },
            "states": {
                "type": "array",
                "items": {"type": "string", "enum": ["on", "off", "unknown"]},
                "maxItems": 3,
            },
        },
    }

    def __init__(self, tools: ConsoleAgentTools, manifest: HomeCapabilities | None = None):
        super().__init__(tools, manifest)
        if manifest is not None:
            rooms = list(manifest.projection.roomDeviceKinds)
            kinds = manifest.projection.deviceKinds
            room_device_kind_choices = "; ".join(
                f"{room}: {', '.join(manifest.projection.roomDeviceKinds[room])}" for room in rooms
            )
            self.description = (
                "Read the exposed home's current device states. Only request "
                f"room/device-kind pairs from the discovered exposure list: {room_device_kind_choices}."
            )
            self.input_schema = {
                **self.input_schema,
                "properties": {
                    "rooms": {
                        "type": "array",
                        "items": {"type": "string", "enum": rooms},
                        "maxItems": 20,
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": kinds},
                        "maxItems": 40,
                    },
                    "states": self.input_schema["properties"]["states"],
                },
            }

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        filters = self._validate_args(args, {"rooms", "kinds", "states"})
        if self.manifest is not None and (
            any(
                room not in self.manifest.projection.roomDeviceKinds
                for room in filters.get("rooms", [])
            )
            or any(
                kind not in self.manifest.projection.deviceKinds
                for kind in filters.get("kinds", [])
            )
        ):
            raise AssistantError("INVALID_TOOL_ARGUMENTS", 400)
        if self.manifest is not None:
            selected_rooms = filters.get("rooms", list(self.manifest.projection.roomDeviceKinds))
            selected_kinds = filters.get("kinds")
            if selected_kinds is not None and any(
                kind
                not in {
                    exposed_kind
                    for room in selected_rooms
                    for exposed_kind in self.manifest.projection.roomDeviceKinds[room]
                }
                for kind in selected_kinds
            ):
                raise AssistantError("INVALID_TOOL_ARGUMENTS", 400)
        try:
            status = await self.tools.invoke_device_status(
                self._token(ctx), ctx.request_id, ctx.home_selector, filters
            )
        except AgentError as error:
            raise AssistantError(error.code, error.status, error.diagnostic_code) from None
        devices = []
        for room in status.rooms:
            for item in room.items:
                if len(devices) >= 40:
                    break
                devices.append(
                    {
                        "room": room.room[:80],
                        "name": item.name[:80],
                        "kind": item.kind,
                        "state": item.state,
                        "online": item.online,
                    }
                )
            if len(devices) >= 40:
                break
        total_devices = sum(len(room.items) for room in status.rooms)
        content = {
            "completeness": status.completeness,
            "capturedAt": status.capturedAt,
            "poweredOn": status.poweredOn,
            "devices": devices,
            "truncated": total_devices > len(devices),
            "warnings": status.warnings,
        }
        display = status.model_dump(exclude_none=True)
        return CapabilityResult(
            status="partial" if status.completeness == "partial" else "success",
            model_content=content,
            client_data={"type": "device_status", **display},
            display_text=(
                "当前家庭暂无可用的设备状态。"
                if status.completeness == "empty"
                else "已读取当前家庭设备状态。"
            ),
        )
