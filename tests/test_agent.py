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
from mijia_agent.models import AgentError, Decision, Execution, Scene, Turn, Usage
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
    alias="scene_0123456789abcdef", name="回家模式", description="已审核低风险场景", actionCount=1
)
USAGE = Usage(promptTokens=10, completionTokens=5, totalTokens=15)


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

    async def list_scenes(self, turn):
        self.calls.append(("list", turn.principalId, turn.homeId))
        return [SCENE]

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
    assert [tool["function"]["name"] for tool in request["tools"]] == ["list_scenes"]


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
        assert "history" not in body and "message" not in body
        return httpx.Response(200, json={"scenes": [SCENE.model_dump()]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ConsoleTools(settings(), client).list_scenes(turn())

    assert asyncio.run(execute()) == [SCENE]
    assert len(requests) == 1


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
    app = create_app(config, AgentService(FakeProvider(), FakeTools()))
    data = turn().model_dump()
    data["sessionBinding"] = "fake-opaque-binding"
    with TestClient(app) as client:
        assert client.post("/internal/v1/turn", json={}).status_code == 401
        headers = {"Authorization": "Bearer " + config.internal_secret}
        response = client.post("/internal/v1/turn", headers=headers, json=data)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
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
