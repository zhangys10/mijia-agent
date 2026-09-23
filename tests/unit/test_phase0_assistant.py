import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from typing import ClassVar, Literal

import httpx
import pytest
from fastapi.testclient import TestClient

from mijia_agent.app import create_app
from mijia_agent.config import Settings
from mijia_agent.gateway import Gateway
from mijia_agent.llm_log import LlmCallLogger
from mijia_agent.models import AgentError
from mijia_assistant.capabilities import (
    CaiyunWeatherCapability,
    CapabilityRegistry,
    CurrentDateTimeCapability,
    FakeWeatherCapability,
)
from mijia_assistant.capabilities.weather import amap_signature, caiyun_signature
from mijia_assistant.conversation import ConversationEngine
from mijia_assistant.models import (
    AssistantContext,
    AssistantError,
    CapabilityResult,
    ModelMessage,
    ModelTurn,
    ToolCall,
    Usage,
)
from mijia_assistant.providers import OpenAICompatibleProvider, OpenAIStreamNormalizer
from mijia_assistant.providers.openai_compatible import normalize_message


class ScriptedProvider:
    def __init__(self, *turns: ModelTurn):
        self.turns = list(turns)
        self.requests = []

    async def complete(self, messages, tools, ctx):
        self.requests.append((messages, tools, ctx))
        return self.turns.pop(0)


def context(**overrides):
    values = {
        "request_id": "req_phase0_test",
        "conversation_id": "conv_phase0",
        "principal_ref": "principal-must-not-reach-model",
        "home_ref": "home-must-not-reach-model",
    }
    return AssistantContext(**(values | overrides))


def run(engine, message="hello", ctx=None):
    return asyncio.run(engine.run(ctx or context(), message))


def test_generic_answer_completes_without_invoking_home_or_weather():
    weather = FakeWeatherCapability()
    provider = ScriptedProvider(
        ModelTurn(
            content="Relative humidity around 40–60% is comfortable.",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
    )
    result = run(ConversationEngine(provider, CapabilityRegistry([weather])))

    assert result.outcome == "direct_answer"
    assert weather.calls == 0
    serialized = json.dumps(
        [message.model_dump() for message in provider.requests[0][0]], ensure_ascii=False
    )
    assert "principal-must-not-reach-model" not in serialized
    assert "home-must-not-reach-model" not in serialized


def test_fake_weather_runs_two_model_steps_and_returns_structured_data():
    weather = FakeWeatherCapability()
    provider = ScriptedProvider(
        ModelTurn(
            tool_calls=[
                ToolCall(
                    id="call_weather",
                    name="get_weather",
                    arguments={"location": "Singapore", "days": 1},
                )
            ],
            usage=Usage(prompt_tokens=4, completion_tokens=2, total_tokens=6),
        ),
        ModelTurn(
            content="Singapore is hot with possible showers.",
            usage=Usage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
        ),
    )
    result = run(ConversationEngine(provider, CapabilityRegistry([weather])))

    assert result.outcome == "tool_answer"
    assert weather.calls == 1
    assert result.data["type"] == "weather"
    assert result.tool_events[0].name == "get_weather"
    assert result.usage.total_tokens == 17
    tool_messages = [message for message in provider.requests[1][0] if message.role == "tool"]
    assert len(tool_messages) == 1
    assert "Singapore" in tool_messages[0].content


def test_missing_weather_location_can_return_clarification_without_tool():
    weather = FakeWeatherCapability()
    provider = ScriptedProvider(
        ModelTurn(content="Which city should I check?", response_kind="clarification")
    )
    result = run(
        ConversationEngine(provider, CapabilityRegistry([weather])), "How is the weather today?"
    )

    assert result.outcome == "clarification"
    assert weather.calls == 0


def test_current_datetime_uses_the_trusted_context_timezone_and_rejects_arguments():
    capability = CurrentDateTimeCapability()
    result = asyncio.run(capability.invoke(context(timezone="Asia/Singapore"), {}))

    assert result.client_data["type"] == "datetime"
    assert result.model_content["timezone"] == "Asia/Singapore"
    with pytest.raises(AssistantError, match="INVALID_TOOL_ARGUMENTS"):
        asyncio.run(capability.invoke(context(), {"timezone": "UTC"}))


def test_caiyun_weather_fetches_and_caches_a_sanitized_snapshot():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v3/geocode/geo":
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "geocodes": [
                        {"location": "116.4074,39.9042", "formatted_address": "上海市长宁区"}
                    ],
                },
            )
        assert request.url.path == "/v2.6/caiyun-app-key/116.4074,39.9042/weather"
        assert dict(request.url.params) == {
            "lang": "zh_CN",
            "unit": "metric",
            "alert": "false",
            "dailysteps": "2",
        }
        assert request.headers["x-cy-nonce"].isalnum()
        assert len(request.headers["x-cy-nonce"]) == 32
        assert request.headers["x-cy-timestamp"].isdigit()
        assert request.headers["x-cy-signature"]
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "timezone": "Asia/Shanghai",
                "server_time": 1790125200,
                "result": {
                    "realtime": {
                        "temperature": 29.1,
                        "apparent_temperature": 34.0,
                        "humidity": 0.74,
                        "skycon": "LIGHT_RAIN",
                        "wind": {"speed": 12.4},
                    },
                    "daily": {
                        "temperature": [
                            {"date": "2026-09-23T00:00+08:00", "max": 31.2, "min": 26.1},
                            {"date": "2026-09-24T00:00+08:00", "max": 32.0, "min": 26.3},
                        ],
                        "skycon": [{"value": "LIGHT_RAIN"}, {"value": "CLOUDY"}],
                        "precipitation": [{"probability": 0.7}, {"probability": 0.4}],
                    },
                },
            },
        )

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            capability = CaiyunWeatherCapability(
                client,
                base_url="https://api.caiyunapp.com",
                app_key="caiyun-app-key",
                app_secret="caiyun-app-secret",
                geocoding_url="https://restapi.amap.com",
                geocoding_key="amap-key",
                geocoding_private_key="amap-private-key",
            )
            first = await capability.invoke(context(), {"location": "上海市长宁区", "days": 2})
            second = await capability.invoke(context(), {"location": "上海市长宁区", "days": 2})
            return first, second

    first, second = asyncio.run(execute())
    assert len(calls) == 3
    assert first.client_data["provider"] == "caiyun"
    assert first.client_data["alertsSupported"] is False
    assert first.client_data["attributionUrl"] == "https://www.caiyunapp.com/"
    assert "attribution" not in first.model_content
    assert "attributionUrl" not in first.model_content
    assert first.client_data["forecast"][0]["temperatureMax"] == 31.2
    assert first.client_data["freshness"] == "fresh"
    assert second.client_data["freshness"] == "cached"


def test_caiyun_rejects_coordinates_outside_the_mainland_china_mvp_without_a_request():
    async def execute():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "1", "geocodes": []})
            )
        ) as client:
            capability = CaiyunWeatherCapability(
                client,
                base_url="https://api.caiyunapp.com",
                app_key="caiyun-app-key",
                app_secret="caiyun-app-secret",
                geocoding_url="https://restapi.amap.com",
                geocoding_key="amap-key",
                geocoding_private_key="amap-private-key",
            )
            return await capability.invoke(context(), {"location": "unknown"})

    result = asyncio.run(execute())
    assert result.status == "error"
    assert result.model_content["status"] == "location_unavailable"


def test_caiyun_signature_follows_the_documented_sorted_query_contract():
    signature = caiyun_signature(
        method="GET",
        path="/v2.6/your_app_key/116.3176,39.9760/weather",
        query={"hourlysteps": "24", "alert": "true", "dailysteps": "1"},
        app_key="your_app_key",
        app_secret="your_app_secret",
        nonce="0195c68a-42e7-7243-bff2-ac97a78b837d",
        timestamp="1742791910",
    )

    assert signature == "KfHsk3z2XfX6Yxox4Uf_VgyM0wHk6bWEyRqZ9QOJUYw="


def test_amap_signature_follows_the_documented_sorted_query_contract():
    assert (
        amap_signature(
            {"f": "8", "a": "23", "d": "48", "c": "67", "b": "12"},
            "bbbbb",
        )
        == "a89e8c2266d888860c46672d77d069f3"
    )


def test_live_read_profile_clarifies_location_without_asking_the_model():
    provider = ScriptedProvider(ModelTurn(content="must not be consumed"))
    result = run(
        ConversationEngine(provider, CapabilityRegistry()),
        "今天天气怎么样？",
    )

    assert result.outcome == "clarification"
    assert result.answer.text == "你想查询哪个城市的天气？"
    assert provider.requests == []


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"location": "Singapore", "unknown": True},
        {"location": "Singapore", "days": 8},
    ],
)
def test_malformed_tool_arguments_fail_closed(arguments):
    provider = ScriptedProvider(
        ModelTurn(tool_calls=[ToolCall(id="bad", name="get_weather", arguments=arguments)])
    )
    with pytest.raises(AssistantError, match="INVALID_TOOL_ARGUMENTS"):
        run(ConversationEngine(provider, CapabilityRegistry([FakeWeatherCapability()])))


def test_invented_or_unavailable_tool_fails_closed():
    provider = ScriptedProvider(
        ModelTurn(tool_calls=[ToolCall(id="bad", name="activate_scene", arguments={})])
    )
    with pytest.raises(AssistantError, match="TOOL_NOT_AVAILABLE"):
        run(ConversationEngine(provider, CapabilityRegistry()))


class TerminalWrite:
    name = "activate_scene"
    description = "Test-only terminal action"
    risk: Literal["home_write_low"] = "home_write_low"
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }

    def __init__(self):
        self.calls = 0

    async def is_available(self, ctx):
        return True

    async def invoke(self, ctx, args):
        self.calls += 1
        return CapabilityResult(
            status="success",
            display_text="The scene was activated.",
            client_data={"type": "action", "status": "success"},
            is_terminal=True,
        )


class SlowWrite(TerminalWrite):
    async def invoke(self, ctx, args):
        self.calls += 1
        await asyncio.sleep(0.1)
        return CapabilityResult(status="success", display_text="too late", is_terminal=True)


def test_write_result_is_terminal_and_model_is_not_called_again():
    capability = TerminalWrite()
    provider = ScriptedProvider(
        ModelTurn(tool_calls=[ToolCall(id="write", name="activate_scene", arguments={})]),
        ModelTurn(content="must not be consumed"),
    )
    result = run(ConversationEngine(provider, CapabilityRegistry([capability])))

    assert result.outcome == "action_result"
    assert result.answer.text == "The scene was activated."
    assert capability.calls == 1
    assert len(provider.requests) == 1


def test_write_cannot_be_parallelized_with_read():
    provider = ScriptedProvider(
        ModelTurn(
            tool_calls=[
                ToolCall(id="write", name="activate_scene", arguments={}),
                ToolCall(id="read", name="get_weather", arguments={"location": "Singapore"}),
            ]
        )
    )
    registry = CapabilityRegistry([TerminalWrite(), FakeWeatherCapability()])
    with pytest.raises(AssistantError, match="WRITE_MUST_BE_EXCLUSIVE"):
        run(ConversationEngine(provider, registry))


def test_write_timeout_is_unknown_and_never_retried():
    capability = SlowWrite()
    provider = ScriptedProvider(
        ModelTurn(tool_calls=[ToolCall(id="write", name="activate_scene", arguments={})])
    )
    short_deadline = context(deadline=datetime.now(timezone.utc) + timedelta(milliseconds=10))
    result = run(ConversationEngine(provider, CapabilityRegistry([capability])), ctx=short_deadline)
    assert result.outcome == "outcome_unknown"
    assert result.tool_events[0].status == "outcome_unknown"
    assert capability.calls == 1
    assert len(provider.requests) == 1


class ReadWithFallback:
    name = "get_status"
    description = "Test-only read"
    risk: Literal["general_read"] = "general_read"
    input_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }

    async def is_available(self, ctx):
        return True

    async def invoke(self, ctx, args):
        return CapabilityResult(
            status="success",
            model_content={"value": 1},
            client_data={"value": 1},
            display_text="已读取当前状态。",
        )


def test_truncated_tool_follow_up_uses_complete_deterministic_fallback():
    provider = ScriptedProvider(
        ModelTurn(tool_calls=[ToolCall(id="read", name="get_status", arguments={})]),
        ModelTurn(content="未完成", truncated=True),
    )
    result = run(ConversationEngine(provider, CapabilityRegistry([ReadWithFallback()])))

    assert result.outcome == "tool_answer"
    assert result.answer.text == "已读取当前状态。"
    assert result.data == {"value": 1}


def test_expired_deadline_prevents_model_call():
    provider = ScriptedProvider(ModelTurn(content="too late"))
    expired = context(deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(AssistantError, match="DEADLINE_EXCEEDED"):
        run(ConversationEngine(provider, CapabilityRegistry()), ctx=expired)
    assert provider.requests == []


def test_history_allows_only_normalized_messages():
    provider = ScriptedProvider(ModelTurn(content="follow-up"))
    engine = ConversationEngine(provider, CapabilityRegistry())
    result = asyncio.run(
        engine.run(
            context(),
            "and the bedroom?",
            [ModelMessage(role="assistant", content="I answered the living-room query.")],
        )
    )
    assert result.answer.text == "follow-up"


def app_settings(**overrides):
    values = {
        "internal_secret": "internal-secret-" * 3,
        "tools_secret": "tools-secret-" * 3,
        "gateway_key": "fake-key",
        "gateway_url": "https://gateway.example/v1",
        "console_url": "https://console.example",
        "model": "test-model",
        "allowed_models": ("test-model",),
        "legacy_router_enabled": False,
    }
    return Settings(**(values | overrides))


def test_canonical_http_response_and_legacy_gate():
    provider = ScriptedProvider(
        ModelTurn(
            content="A direct answer.",
            usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )
    )
    engine = ConversationEngine(provider, CapabilityRegistry())

    class FakeAuth:
        def __init__(self):
            self.calls = []

        async def call(self, token, request_id, tool, home, arguments):
            self.calls.append((token, tool, home, arguments))
            return {"ok": True}

    auth = FakeAuth()
    app = create_app(
        app_settings(),
        service=object(),
        command_service=object(),
        assistant_engine=engine,
        assistant_tools=auth,
    )

    with TestClient(app) as client:
        response = client.post(
            "/ai/assistant",
            headers={"Authorization": "Bearer opaque-token"},
            json={"text": "hello", "channel": "web"},
        )
        legacy = client.post("/ai/command")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["outcome"] == "direct_answer"
    assert body["answer"]["text"] == "A direct answer."
    assert body["usage"]["totalTokens"] == 5
    assert legacy.status_code == 410
    assert auth.calls == [("opaque-token", "authorize", None, {})]
    model_payload = json.dumps(
        [message.model_dump() for message in provider.requests[0][0]], ensure_ascii=False
    )
    assert "opaque-token" not in model_payload


def test_canonical_ingress_authenticates_token_before_model_call():
    provider = ScriptedProvider(ModelTurn(content="must not run"))

    class RejectingAuth:
        async def call(self, token, request_id, tool, home, arguments):
            raise AgentError("AUTOMATION_TOKEN_INVALID", 401)

    app = create_app(
        app_settings(),
        service=object(),
        command_service=object(),
        assistant_engine=ConversationEngine(provider, CapabilityRegistry()),
        assistant_tools=RejectingAuth(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/ai/assistant",
            headers={"Authorization": "Bearer rejected-token"},
            json={"text": "hello"},
        )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTOMATION_TOKEN_INVALID"
    assert provider.requests == []


def test_canonical_ingress_keeps_bounded_redacted_history_for_follow_up():
    provider = ScriptedProvider(ModelTurn(content="上海市目前没有配置可用的天气数据源。"))

    class FakeAuth:
        async def call(self, token, request_id, tool, home, arguments):
            return {"ok": True}

    app = create_app(
        app_settings(),
        service=object(),
        command_service=object(),
        assistant_engine=ConversationEngine(provider, CapabilityRegistry()),
        assistant_tools=FakeAuth(),
    )
    with TestClient(app) as client:
        first = client.post(
            "/ai/assistant",
            headers={"Authorization": "Bearer same-token"},
            json={"text": "今天天气怎么样？"},
        ).json()
        second = client.post(
            "/ai/assistant",
            headers={"Authorization": "Bearer same-token"},
            json={"text": "上海市", "conversationId": first["conversationId"]},
        )

    assert second.status_code == 200
    assert second.json()["answer"]["text"] == "上海市目前没有配置可用的天气数据源。"
    messages = provider.requests[0][0]
    assert [message.content for message in messages if message.role == "user"] == [
        "今天天气怎么样？",
        "locale=zh-CN; timezone=Asia/Shanghai; channel=web\n上海市",
    ]
    assert "你想查询哪个城市的天气？" in [
        message.content for message in messages if message.role == "assistant"
    ]


def test_stream_normalizer_orders_tool_deltas_and_has_one_terminal_event():
    normalizer = OpenAIStreamNormalizer()
    normalizer.push(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_weather",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location":"Sing',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        }
    )
    normalizer.push(
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'apore"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )

    turn = normalizer.finish(Usage(prompt_tokens=2, completion_tokens=1, total_tokens=3))
    assert turn.tool_calls[0].id == "call_weather"
    assert turn.tool_calls[0].arguments == {"location": "Singapore"}
    assert turn.usage.total_tokens == 3
    with pytest.raises(AssistantError, match="MODEL_STREAM_INVALID"):
        normalizer.push({"choices": []})


def test_stream_without_terminal_event_fails_closed():
    normalizer = OpenAIStreamNormalizer()
    normalizer.push({"choices": [{"delta": {"content": "partial"}}]})
    with pytest.raises(AssistantError, match="MODEL_STREAM_INCOMPLETE"):
        normalizer.finish()


def test_openai_adapter_requests_redacted_gateway_logging_and_normalizes_usage():
    class FakeGateway:
        settings = SimpleNamespace(model="validated-model", max_output_tokens=256)

        def __init__(self):
            self.call = None

        async def chat(self, request, log_context, **kwargs):
            self.call = (request, log_context, kwargs)
            legacy_usage = SimpleNamespace(
                promptTokens=7, completionTokens=2, totalTokens=9, estimated=False
            )
            return {"choices": [{"message": {"content": "hello"}}]}, legacy_usage

    gateway = FakeGateway()
    provider = OpenAICompatibleProvider(gateway)
    turn = asyncio.run(
        provider.complete([ModelMessage(role="user", content="private prompt")], [], context())
    )

    assert turn.content == "hello"
    assert turn.usage.total_tokens == 9
    assert gateway.call[2] == {"log_content": False}


def test_openai_adapter_uses_the_assistant_completion_limit():
    class FakeGateway:
        settings = SimpleNamespace(
            model="validated-model", max_output_tokens=256, assistant_max_output_tokens=512
        )

        def __init__(self):
            self.request = None

        async def chat(self, request, log_context, **kwargs):
            self.request = request
            usage = SimpleNamespace(
                promptTokens=7, completionTokens=2, totalTokens=9, estimated=False
            )
            return {"choices": [{"message": {"content": "hello"}}]}, usage

    gateway = FakeGateway()
    asyncio.run(
        OpenAICompatibleProvider(gateway).complete(
            [ModelMessage(role="user", content="private")], [], context()
        )
    )

    assert gateway.request["max_tokens"] == 512


def test_normalizer_accepts_gateway_object_tool_arguments():
    turn = normalize_message(
        {
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": {"location": "Singapore"},
                    },
                }
            ]
        },
        Usage(),
    )

    assert turn.tool_calls[0].arguments == {"location": "Singapore"}


def test_openai_adapter_marks_length_limited_response_as_truncated():
    class FakeGateway:
        settings = SimpleNamespace(model="validated-model", max_output_tokens=256)

        async def chat(self, request, log_context, **kwargs):
            usage = SimpleNamespace(
                promptTokens=7, completionTokens=256, totalTokens=263, estimated=False
            )
            return {
                "choices": [{"message": {"content": "未完成"}, "finish_reason": "length"}]
            }, usage

    turn = asyncio.run(
        OpenAICompatibleProvider(FakeGateway()).complete(
            [ModelMessage(role="user", content="private")], [], context()
        )
    )

    assert turn.truncated is True


def test_canonical_gateway_log_omits_prompt_response_and_tool_arguments():
    sink = StringIO()

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "private model response",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"location":"private place"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = Gateway(app_settings(), client, LlmCallLogger(sink=sink))
            await gateway.chat(
                {
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "private prompt"}],
                    "tools": [{"type": "function", "function": {"name": "get_weather"}}],
                },
                {"source": "assistant_turn"},
                log_content=False,
            )

    asyncio.run(execute())
    record = sink.getvalue()
    assert "get_weather" in record
    assert "private prompt" not in record
    assert "private model response" not in record
    assert "private place" not in record
