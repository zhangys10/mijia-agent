import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from mijia_agent.app import create_app
from mijia_agent.config import Settings
from mijia_agent.console import ConsoleTools
from mijia_agent.gateway import Gateway, payload
from mijia_agent.models import (
    AgentError,
    Decision,
    DeviceStatus,
    DeviceStatusItem,
    DeviceStatusRoom,
    Execution,
    HomeStatus,
    HomeStatusGroup,
    HomeStatusReading,
    Scene,
    Turn,
    Usage,
)
from mijia_agent.service import AgentService


def settings():
    return Settings(
        internal_secret="test-python-secret-" * 3,
        tools_secret="test-tools-secret-" * 3,
        gateway_key="fake-gateway-key",
        gateway_url="https://gateway.example/v1",
        console_url="https://console.example",
        model="test-model",
        allowed_models=("test-model",),
        legacy_router_enabled=True,
    )


def turn(**overrides):
    raw = {
        "requestId": "req_test_request_0001",
        "conversationId": "conv_example",
        "principalId": "usr_example",
        "homeId": "home-example",
        "message": "我回家了",
        "idempotencyKey": "test-idempotency-0001",
        "scopes": ["ai:chat", "scene:activate"],
        "sessionBinding": "fake-opaque-binding",
    }
    return Turn.model_validate(raw | overrides)


SCENE = Scene(
    alias="scene_0123456789abcdef",
    name="回家模式",
    description="已审核低风险场景",
    actionCount=1,
    risk="low",
    actionSummaries=[{"room": "客厅", "device": "客厅灯", "actions": [{"label": "电源", "value": "开启"}]}],
)
USAGE = Usage(promptTokens=10, completionTokens=5, totalTokens=15)
STATUS_READING = HomeStatusReading(
    value=25.5,
    unit="°C",
    sourceLabel="客厅温湿度计",
    roomName="客厅",
    capturedAt="2026-09-20T08:00:00Z",
)
STATUS = HomeStatus(
    capturedAt="2026-09-20T08:00:00Z",
    completeness="partial",
    groups=[
        HomeStatusGroup(
            metric="temperature",
            label="温度",
            unit="°C",
            latest=STATUS_READING,
            readings=[STATUS_READING],
        )
    ],
    warnings=["部分设备读取失败"],
)
DEVICE_STATUS = DeviceStatus(
    capturedAt="2026-09-21T08:00:00Z",
    completeness="partial",
    poweredOn=1,
    rooms=[
        DeviceStatusRoom(
            room="客厅",
            items=[
                DeviceStatusItem(name="客厅吸顶灯", kind="light", state="on", online=True),
                DeviceStatusItem(name="空气净化器", kind="airpurifier", state="off", online=True),
            ],
        )
    ],
    warnings=["部分设备状态暂时不可用。"],
)


class FakeProvider:
    def __init__(self, decision=None):
        self.decision = decision or Decision(
            tool="activate_scene", sceneId=SCENE.alias, usage=USAGE
        )
        self.calls = 0

    async def decide(self, turn, scenes):
        self.calls += 1
        return self.decision


class FakeTools:
    def __init__(self):
        self.calls = []
        self.result = Execution(status="success", message="已执行回家模式。")
        self.status = STATUS
        self.device_status = DEVICE_STATUS

    async def list_scenes(self, turn):
        self.calls.append(("list", turn.principalId, turn.homeId))
        return [SCENE]

    async def get_home_status(self, turn):
        self.calls.append(("status", turn.principalId, turn.homeId))
        return self.status

    async def get_device_status(self, turn):
        self.calls.append(("devices", turn.principalId, turn.homeId))
        return self.device_status

    async def activate_scene(self, turn, alias):
        self.calls.append(("activate", turn.principalId, turn.homeId, alias))
        return self.result


@pytest.mark.parametrize(
    "message",
    [
        "我还没回家",
        "如果我回家了",
        "我回家了吗？",
        "他说我回家了",
        "不要执行回家模式",
        "执行回家模式吗",
        "关灯",
    ],
)
def test_model_cannot_turn_negation_or_ambiguity_into_execution(message):
    tools = FakeTools()
    result = asyncio.run(AgentService(FakeProvider(), tools).run(turn(message=message)))
    assert result.intent == "none"
    assert len(tools.calls) == 1


@pytest.mark.parametrize("message", ["我回家了", "执行回家模式", "请开启回家模式！"])
def test_explicit_scene_command(message):
    tools = FakeTools()
    result = asyncio.run(AgentService(FakeProvider(), tools).run(turn(message=message)))
    assert result.tool.status == "success"
    assert tools.calls[-1] == ("activate", "usr_example", "home-example", SCENE.alias)


def test_executor_result_wins_over_model_success():
    tools = FakeTools()
    tools.result = Execution(status="partial_success", message="状态待确认。")
    provider = FakeProvider(
        Decision(tool="activate_scene", sceneId=SCENE.alias, message="已全部成功")
    )
    result = asyncio.run(AgentService(provider, tools).run(turn()))
    assert result.message == "状态待确认。"
    assert result.tool.status == "partial_success"


def test_missing_scope_blocks_action_and_retains_usage():
    tools = FakeTools()
    with pytest.raises(AgentError) as caught:
        asyncio.run(AgentService(FakeProvider(), tools).run(turn(scopes=["ai:chat"])))
    assert caught.value.code == "AI_SCOPE_FORBIDDEN"
    assert caught.value.usage == USAGE
    assert len(tools.calls) == 1


def test_preview_does_not_touch_model_or_tools():
    provider, tools = FakeProvider(), FakeTools()
    asyncio.run(AgentService(provider, tools, preview=True).run(turn()))
    assert not provider.calls and not tools.calls


def test_model_payload_has_no_credentials_or_identity():
    request = payload(turn(), [SCENE], settings())
    raw = json.dumps(request)
    for secret in [
        "fake-opaque-binding",
        "fake-gateway-key",
        "usr_example",
        "home-example",
        "req_test_request_0001",
    ]:
        assert secret not in raw
    assert SCENE.alias in raw


def test_readonly_payload_does_not_advertise_activation():
    request = payload(turn(scopes=["ai:chat"]), [SCENE], settings())
    assert [tool["function"]["name"] for tool in request["tools"]] == [
        "list_scenes",
        "get_home_status",
        "get_device_status",
    ]
    assert payload(turn(), [SCENE], settings())["tools"][-1]["function"]["name"] == (
        "activate_scene"
    )


def test_chat_payload_shares_command_router_prompt_and_schema():
    request = payload(turn(), [SCENE], settings())
    system = request["messages"][0]
    assert system["role"] == "system"
    assert "家庭控制意图路由器" in system["content"]
    assert "replyMessage" in json.dumps(request["tools"])
    assert request["enable_thinking"] is False
    assert request["temperature"] == 0


def test_home_status_query_fetches_after_decision_and_returns_structured_result():
    tools = FakeTools()
    provider = FakeProvider(Decision(tool="get_home_status", usage=USAGE))
    result = asyncio.run(AgentService(provider, tools).run(turn(message="现在室内温度多少")))
    assert result.intent == "get_home_status"
    assert result.message == "已读取当前家庭环境状态。"
    assert result.homeStatus == STATUS
    assert result.tool is not None and result.tool.name == "get_home_status"
    assert result.tool.status == "partial_success"
    # Readings stay out of the reply text, so they never enter later model history.
    assert "25.5" not in result.message
    assert tools.calls[-1] == ("status", "usr_example", "home-example")


def test_home_status_query_needs_only_chat_scope():
    tools = FakeTools()
    provider = FakeProvider(Decision(tool="get_home_status", usage=USAGE))
    result = asyncio.run(AgentService(provider, tools).run(turn(scopes=["ai:chat"])))
    assert result.intent == "get_home_status"
    assert tools.calls[-1] == ("status", "usr_example", "home-example")


def test_empty_home_status_reports_empty():
    tools = FakeTools()
    tools.status = HomeStatus(capturedAt="2026-09-20T08:00:00Z", completeness="empty")
    provider = FakeProvider(Decision(tool="get_home_status", usage=USAGE))
    result = asyncio.run(AgentService(provider, tools).run(turn(message="家里空气质量如何")))
    assert result.message == "当前家庭暂无可用的环境读数。"
    assert result.homeStatus is not None and result.homeStatus.groups == []
    assert result.tool.status == "success"


def test_home_status_tool_failure_retains_usage():
    class FailingTools(FakeTools):
        async def get_home_status(self, turn):
            self.calls.append(("status", turn.principalId, turn.homeId))
            raise AgentError("AI_AGENT_UNAVAILABLE")

    tools = FailingTools()
    with pytest.raises(AgentError) as caught:
        asyncio.run(
            AgentService(FakeProvider(Decision(tool="get_home_status", usage=USAGE)), tools).run(
                turn()
            )
        )
    assert caught.value.usage == USAGE


def test_home_status_result_never_carries_identifiers():
    tools = FakeTools()
    provider = FakeProvider(Decision(tool="get_home_status", usage=USAGE))
    result = asyncio.run(AgentService(provider, tools).run(turn(message="家里湿度如何")))
    raw = json.dumps(result.model_dump(exclude_none=True), ensure_ascii=False)
    for forbidden in [
        "usr_example",
        "home-example",
        "fake-opaque-binding",
        "scene_0123456789abcdef",
        "did",
        "siid",
        "piid",
    ]:
        assert forbidden not in raw


def test_device_status_query_fetches_after_decision_and_returns_structured_result():
    tools = FakeTools()
    provider = FakeProvider(Decision(tool="get_device_status", usage=USAGE))
    result = asyncio.run(AgentService(provider, tools).run(turn(message="客厅哪些灯开着")))
    assert result.intent == "get_device_status"
    assert result.message == "已读取当前家庭设备状态。"
    assert result.deviceStatus == DEVICE_STATUS
    assert result.tool is not None and result.tool.name == "get_device_status"
    assert result.tool.status == "partial_success"
    # Device states stay out of the reply text, so they never enter later model history.
    assert "客厅吸顶灯" not in result.message
    assert tools.calls[-1] == ("devices", "usr_example", "home-example")


def test_device_status_query_needs_only_chat_scope():
    tools = FakeTools()
    provider = FakeProvider(Decision(tool="get_device_status", usage=USAGE))
    result = asyncio.run(AgentService(provider, tools).run(turn(scopes=["ai:chat"])))
    assert result.intent == "get_device_status"
    assert tools.calls[-1] == ("devices", "usr_example", "home-example")


def test_empty_device_status_reports_empty():
    tools = FakeTools()
    tools.device_status = DeviceStatus(
        capturedAt="2026-09-21T08:00:00Z", completeness="empty", poweredOn=0
    )
    provider = FakeProvider(Decision(tool="get_device_status", usage=USAGE))
    result = asyncio.run(AgentService(provider, tools).run(turn(message="家里还有灯亮着吗")))
    assert result.message == "当前家庭暂无可用的设备状态。"
    assert result.deviceStatus is not None and result.deviceStatus.rooms == []
    assert result.tool.status == "success"


def test_device_status_tool_failure_retains_usage():
    class FailingTools(FakeTools):
        async def get_device_status(self, turn):
            self.calls.append(("devices", turn.principalId, turn.homeId))
            raise AgentError("AI_AGENT_UNAVAILABLE")

    tools = FailingTools()
    with pytest.raises(AgentError) as caught:
        asyncio.run(
            AgentService(FakeProvider(Decision(tool="get_device_status", usage=USAGE)), tools).run(
                turn()
            )
        )
    assert caught.value.usage == USAGE


def test_device_status_result_never_carries_identifiers():
    tools = FakeTools()
    provider = FakeProvider(Decision(tool="get_device_status", usage=USAGE))
    result = asyncio.run(AgentService(provider, tools).run(turn(message="卧室灯关了吗")))
    raw = json.dumps(result.model_dump(exclude_none=True), ensure_ascii=False)
    for forbidden in [
        "usr_example",
        "home-example",
        "fake-opaque-binding",
        "scene_0123456789abcdef",
        "did",
        "siid",
        "piid",
    ]:
        assert forbidden not in raw


def gateway_response(message, usage=None):
    body = {"choices": [{"message": message}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def gateway_run(handler):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Gateway(settings(), client).decide(turn(), [SCENE])

    return asyncio.run(execute())


def test_gateway_contract_and_exact_usage():
    def handler(request):
        assert str(request.url) == "https://gateway.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-gateway-key"
        assert json.loads(request.content)["enable_thinking"] is False
        return gateway_response(
            {"content": "你好"}, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        )

    result = gateway_run(handler)
    assert result.usage == USAGE


def test_gateway_accepts_exact_home_status_call():
    result = gateway_run(
        lambda _: gateway_response(
            {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": "get_home_status", "arguments": "{}"},
                    }
                ]
            }
        )
    )
    assert result.tool == "get_home_status"


def test_gateway_accepts_exact_device_status_call():
    result = gateway_run(
        lambda _: gateway_response(
            {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": "get_device_status", "arguments": "{}"},
                    }
                ]
            }
        )
    )
    assert result.tool == "get_device_status"


def test_gateway_missing_usage_estimates_full_tool_call():
    result = gateway_run(
        lambda _: gateway_response(
            {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "activate_scene",
                            "arguments": json.dumps({"sceneId": SCENE.alias}),
                        },
                    }
                ]
            }
        )
    )
    assert result.usage.estimated and result.usage.completionTokens > 0


@pytest.mark.parametrize(
    "name,args",
    [
        ("shell", {}),
        ("activate_scene", {"sceneId": "invented"}),
        ("activate_scene", {"sceneId": SCENE.alias, "homeId": "other"}),
        ("list_scenes", {"unexpected": 1}),
        ("get_home_status", {"metric": "temperature"}),
        ("get_device_status", {"room": "客厅"}),
    ],
)
def test_gateway_rejects_invented_tools_and_extra_arguments(name, args):
    with pytest.raises(AgentError) as caught:
        gateway_run(
            lambda _: gateway_response(
                {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ]
                }
            )
        )
    assert caught.value.code == "AI_GATEWAY_RESPONSE_INVALID"
    assert caught.value.usage is not None


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "AI_GATEWAY_UNAVAILABLE"),
        (302, "AI_GATEWAY_UNAVAILABLE"),
        (429, "AI_GATEWAY_RATE_LIMITED"),
        (500, "AI_GATEWAY_UNAVAILABLE"),
    ],
)
def test_gateway_error_sanitization(status, code):
    with pytest.raises(AgentError) as caught:
        gateway_run(lambda _: httpx.Response(status, text="secret-upstream-response"))
    assert caught.value.code == code
    assert "secret" not in str(caught.value)


def test_tool_client_passes_only_scoped_envelope():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/ai/tools"
        body = json.loads(request.content)
        assert body["sessionBinding"] == "fake-opaque-binding"
        assert body["principalId"] == "usr_example"
        assert body["tool"] == "list_scenes"
        assert "history" not in body and "message" not in body
        return httpx.Response(200, json={"scenes": [SCENE.model_dump()]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ConsoleTools(settings(), client).list_scenes(turn())

    assert asyncio.run(execute()) == [SCENE]
    assert len(requests) == 1


def test_status_tool_client_validates_strictly():
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["tool"] == "get_home_status" and body["arguments"] == {}
        return httpx.Response(200, json=STATUS.model_dump())

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ConsoleTools(settings(), client).get_home_status(turn())

    assert asyncio.run(execute()) == STATUS


def test_status_tool_client_rejects_malformed_or_leaky_payloads():
    leaked = STATUS.model_dump()
    leaked["deviceIds"] = ["did-123456789"]
    malformed = [
        {"completeness": "complete"},  # missing capturedAt
        {**STATUS.model_dump(), "extra": True},
        {**STATUS.model_dump(), "groups": [{"metric": "hackers"}]},
        {
            **STATUS.model_dump(),
            "groups": [{"metric": "temperature", "readings": [{"value": "25"}]}],
        },
        leaked,
    ]
    for payload_json in malformed:

        def handler(request, payload_json=payload_json):
            return httpx.Response(200, json=payload_json)

        async def execute():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await ConsoleTools(settings(), client).get_home_status(turn())

        with pytest.raises(AgentError, match="AI_AGENT_UNAVAILABLE"):
            asyncio.run(execute())


def test_device_status_tool_client_validates_strictly():
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["tool"] == "get_device_status" and body["arguments"] == {}
        return httpx.Response(200, json=DEVICE_STATUS.model_dump())

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ConsoleTools(settings(), client).get_device_status(turn())

    assert asyncio.run(execute()) == DEVICE_STATUS
    assert len(requests) == 1


def test_device_status_tool_client_rejects_malformed_or_leaky_payloads():
    leaked = DEVICE_STATUS.model_dump()
    leaked["deviceIds"] = ["did-123456789"]
    malformed = [
        {"completeness": "complete"},  # missing capturedAt/poweredOn
        {**DEVICE_STATUS.model_dump(), "extra": True},
        {
            **DEVICE_STATUS.model_dump(),
            "rooms": [
                {
                    "room": "客厅",
                    "items": [{"name": "灯", "kind": "light", "state": "halfway", "online": True}],
                }
            ],
        },
        {
            **DEVICE_STATUS.model_dump(),
            "rooms": [
                {
                    "room": "客厅",
                    "items": [{"name": "灯", "kind": "light", "state": "on", "online": "yes"}],
                }
            ],
        },
        leaked,
    ]
    for payload_json in malformed:

        def handler(request, payload_json=payload_json):
            return httpx.Response(200, json=payload_json)

        async def execute():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await ConsoleTools(settings(), client).get_device_status(turn())

        with pytest.raises(AgentError, match="AI_AGENT_UNAVAILABLE"):
            asyncio.run(execute())


def test_tool_timeout_never_retries():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("fake upstream secret", request=request)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await ConsoleTools(settings(), client).activate_scene(turn(), SCENE.alias)

    with pytest.raises(AgentError, match="AI_SCENE_TIMEOUT"):
        asyncio.run(execute())
    assert count == 1


def test_http_auth_validation_no_store_and_response():
    config = settings()
    provider = FakeProvider(Decision(tool="get_home_status", usage=USAGE))
    app = create_app(config, AgentService(provider, FakeTools()))
    data = turn().model_dump()
    data["sessionBinding"] = "fake-opaque-binding"
    with TestClient(app) as client:
        assert client.post("/internal/v1/turn", json={}).status_code == 401
        headers = {"Authorization": "Bearer " + config.internal_secret}
        response = client.post("/internal/v1/turn", headers=headers, json=data)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["intent"] == "get_home_status"
        assert body["homeStatus"]["groups"][0]["metric"] == "temperature"
        response = client.post(
            "/internal/v1/turn", headers=headers, json=data | {"userId": "sensitive-value"}
        )
        assert response.status_code == 400
        assert "sensitive-value" not in response.text and "fake-opaque-binding" not in response.text
        assert (
            client.post("/internal/v1/turn", headers=headers, content=b"x" * 65537).status_code
            == 400
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:password@example.com",
        "https://example.com?token=x",
        "file:///tmp/foo",
    ],
)
def test_config_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        replace(settings(), console_url=url)


def test_config_rejects_unknown_model_and_shared_secrets():
    with pytest.raises(ValueError):
        replace(settings(), model="unapproved")
    with pytest.raises(ValueError):
        replace(settings(), tools_secret=settings().internal_secret)
    assert "fake-gateway-key" not in repr(settings())
