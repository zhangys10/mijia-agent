import asyncio
import base64
import copy
import hashlib
import hmac
import math
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import quote, quote_plus

import httpx
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


class CaiyunWeatherArguments(StrictModel):
    location: Annotated[str, Field(min_length=1, max_length=120)]
    days: Annotated[int, Field(ge=1, le=7)] = 1


class CaiyunWeatherCapability:
    """Mainland-China Caiyun Weather v2.6 capability with bounded local caching."""

    name = "get_weather"
    description = (
        "Get current mainland-China weather for a city or area name. "
        "Resolve the name server-side; never ask the user for coordinates."
    )
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

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        geocoding_url: str,
        geocoding_key: str,
        geocoding_private_key: str,
        timeout_seconds: float = 3,
        cache_ttl_seconds: int = 300,
        max_cache_entries: int = 256,
    ):
        self._client = client
        self._base_url = _versioned_base_url(base_url, "/v2.6")
        self._app_key = app_key
        self._app_secret = app_secret
        self._geocoding_url = _versioned_base_url(geocoding_url, "/v3")
        self._geocoding_key = geocoding_key
        self._geocoding_private_key = geocoding_private_key
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._max_cache_entries = max_cache_entries
        self._cache: dict[tuple[str, int, str], tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def is_available(self, ctx: AssistantContext) -> bool:
        return "ai:chat" in ctx.scopes

    async def invoke(self, ctx: AssistantContext, args: dict) -> CapabilityResult:
        try:
            request = CaiyunWeatherArguments.model_validate(args)
        except ValidationError:
            raise AssistantError("INVALID_TOOL_ARGUMENTS") from None
        location = await self._resolve_location(request.location)
        if location is None:
            return CapabilityResult(
                status="error",
                model_content={"provider": "caiyun", "status": "location_unavailable"},
                client_data={
                    "type": "weather",
                    "provider": "caiyun",
                    "status": "location_unavailable",
                },
            )
        language = "zh_CN" if ctx.locale.startswith("zh") else "en_US"
        key = (request.location.casefold().strip(), request.days, language)
        cached = await self._cached(key)
        if cached is not None:
            return self._result(cached, freshness="cached")

        snapshot = await self._forecast(location, request.days, language)
        await self._put_cached(key, snapshot)
        return self._result(snapshot, freshness="fresh")

    async def _resolve_location(self, name: str) -> dict[str, Any] | None:
        params = {"address": name, "key": self._geocoding_key}
        params["sig"] = amap_signature(params, self._geocoding_private_key)
        payload = await self._get_json(
            f"{self._geocoding_url}/geocode/geo",
            params,
            headers={},
        )
        items = payload.get("geocodes") if isinstance(payload, dict) else None
        if payload.get("status") != "1" or not isinstance(items, list) or not items:
            return None
        item = items[0]
        try:
            longitude, latitude = (float(part) for part in item["location"].split(",", 1))
        except (AttributeError, KeyError, ValueError):
            return None
        if not 73 <= longitude <= 135 or not 18 <= latitude <= 54:
            return None
        return {
            "longitude": longitude,
            "latitude": latitude,
            "label": _text(item.get("formatted_address"), 240) or name,
        }

    async def _forecast(self, location: dict[str, Any], days: int, language: str) -> dict[str, Any]:
        path = f"/v2.6/{quote(self._app_key, safe='')}/{location['longitude']},{location['latitude']}/weather"
        query = {
            "alert": "false",
            "dailysteps": str(days),
            "lang": language,
            "unit": "metric",
        }
        nonce = secrets.token_hex(16)
        timestamp = str(int(time.time()))
        headers = {
            "x-cy-nonce": nonce,
            "x-cy-timestamp": timestamp,
            "x-cy-signature": caiyun_signature(
                method="GET",
                path=path,
                query=query,
                app_key=self._app_key,
                app_secret=self._app_secret,
                nonce=nonce,
                timestamp=timestamp,
            ),
        }
        payload = await self._get_json(
            f"{self._base_url}{path.removeprefix('/v2.6')}", query, headers=headers
        )
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise AssistantError("WEATHER_PROVIDER_INVALID", 502)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AssistantError("WEATHER_PROVIDER_INVALID", 502)
        current = result.get("realtime")
        daily = result.get("daily")
        zone = _text(payload.get("timezone"), 64)
        if not isinstance(current, dict) or not isinstance(daily, dict) or zone is None:
            raise AssistantError("WEATHER_PROVIDER_INVALID", 502)
        required_current = {
            "temperature": _number(current.get("temperature")),
            "apparentTemperature": _number(current.get("apparent_temperature")),
            "humidity": _number(current.get("humidity")),
            "condition": _text(current.get("skycon"), 64),
        }
        wind = current.get("wind")
        required_current["windSpeed"] = (
            _number(wind.get("speed")) if isinstance(wind, dict) else None
        )
        if any(value is None for value in required_current.values()):
            raise AssistantError("WEATHER_PROVIDER_INVALID", 502)
        forecast = _daily_forecast(daily, days)
        server_time = _integer(payload.get("server_time"))
        observed_at = (
            datetime.fromtimestamp(server_time, timezone.utc).isoformat().replace("+00:00", "Z")
            if server_time is not None
            else None
        )
        return {
            "provider": "caiyun",
            "attribution": "Weather data by Caiyun Weather",
            "attributionUrl": "https://www.caiyunapp.com/",
            "location": location["label"],
            "coordinates": {"longitude": location["longitude"], "latitude": location["latitude"]},
            "timezone": zone,
            **({"observedAt": observed_at} if observed_at else {}),
            "capturedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "current": {
                **required_current,
                "humidityUnit": "ratio",
                "unit": "°C",
                "windUnit": "km/h",
            },
            "forecast": forecast,
            "forecastRequestedDays": days,
            "forecastDays": len(forecast),
            "advice": _advice(required_current, forecast),
            "alertsSupported": False,
        }

    async def _get_json(self, url: str, params: dict[str, Any], *, headers: dict[str, str]) -> Any:
        try:
            response = await self._client.get(
                url, params=params, headers=headers, timeout=self._timeout_seconds
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise AssistantError("WEATHER_PROVIDER_UNAVAILABLE", 502) from None

    async def _cached(self, key: tuple[str, int, str]) -> dict[str, Any] | None:
        async with self._lock:
            hit = self._cache.get(key)
            if hit is None or hit[0] <= time.monotonic():
                self._cache.pop(key, None)
                return None
            return copy.deepcopy(hit[1])

    async def _put_cached(self, key: tuple[str, int, str], snapshot: dict[str, Any]) -> None:
        async with self._lock:
            if key not in self._cache and len(self._cache) >= self._max_cache_entries:
                oldest = min(self._cache, key=lambda cache_key: self._cache[cache_key][0])
                self._cache.pop(oldest, None)
            self._cache[key] = (time.monotonic() + self._cache_ttl_seconds, copy.deepcopy(snapshot))

    @staticmethod
    def _result(snapshot: dict[str, Any], *, freshness: str) -> CapabilityResult:
        payload = {**snapshot, "freshness": freshness}
        model_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"attribution", "attributionUrl"}
        }
        return CapabilityResult(
            status="success",
            model_content=model_payload,
            client_data={"type": "weather", **payload},
        )


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit:
        return None
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _daily_forecast(daily: dict[str, Any], days: int) -> list[dict[str, Any]]:
    values = [daily.get(key) for key in ("temperature", "skycon", "precipitation")]
    if not all(isinstance(value, list) and value for value in values):
        raise AssistantError("WEATHER_PROVIDER_INVALID", 502)
    available_days = min(days, *(len(value) for value in values))
    forecast = []
    for index in range(available_days):
        temperature, skycon, precipitation = values[0][index], values[1][index], values[2][index]
        if not all(isinstance(value, dict) for value in (temperature, skycon, precipitation)):
            raise AssistantError("WEATHER_PROVIDER_INVALID", 502)
        date = _text(temperature.get("date"), 64)
        condition = _text(skycon.get("value"), 64)
        maximum = _number(temperature.get("max"))
        minimum = _number(temperature.get("min"))
        probability = _number(precipitation.get("probability"))
        if None in (date, condition, maximum, minimum, probability):
            raise AssistantError("WEATHER_PROVIDER_INVALID", 502)
        forecast.append(
            {
                "date": date,
                "condition": condition,
                "temperatureMax": maximum,
                "temperatureMin": minimum,
                "precipitationProbability": probability,
                "unit": "°C",
            }
        )
    return forecast


def _advice(current: dict[str, Any], forecast: list[dict[str, Any]]) -> dict[str, str]:
    temperature = current["apparentTemperature"]
    if temperature < 8:
        clothing = "穿保暖外套，注意防风。"
    elif temperature < 18:
        clothing = "建议穿夹克或薄外套。"
    elif temperature < 28:
        clothing = "短袖加一件薄外套通常合适。"
    else:
        clothing = "天气较热，穿轻薄透气衣物并注意补水。"
    rain = max(item["precipitationProbability"] for item in forecast)
    umbrella = "建议携带雨伞。" if rain >= 0.4 else "通常不需要带伞。"
    return {"clothing": clothing, "umbrella": umbrella}


def _versioned_base_url(value: str, version_path: str) -> str:
    """Accept either a provider host or a base URL already ending in its API version."""

    normalized = value.rstrip("/")
    return normalized if normalized.endswith(version_path) else normalized + version_path


def caiyun_signature(
    *,
    method: str,
    path: str,
    query: dict[str, str],
    app_key: str,
    app_secret: str,
    nonce: str,
    timestamp: str,
) -> str:
    """Produce Caiyun v2.6's URL-safe Base64 HMAC-SHA256 request signature."""

    query_string = "&".join(
        f"{quote_plus(key, safe='')}={quote_plus(query[key], safe='')}" for key in sorted(query)
    )
    material = f"{method}:{path}:{query_string}:{app_key}:{nonce}:{timestamp}"
    digest = hmac.new(app_secret.encode(), material.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode()


def amap_signature(params: dict[str, str], private_key: str) -> str:
    """Produce AMap ``sig`` before HTTP URL encoding using sorted UTF-8 parameters."""

    material = "&".join(f"{key}={params[key]}" for key in sorted(params))
    material += private_key
    return hashlib.md5(material.encode("utf-8")).hexdigest()
