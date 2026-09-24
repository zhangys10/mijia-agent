import asyncio
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mijia_agent.app import create_app
from mijia_agent.command_console import ConsoleAgentTools
from mijia_agent.command_idempotency import IdempotencyStore, request_hash, valid_idempotency_key
from mijia_agent.command_models import CommandRequest
from mijia_agent.command_rules import (
    find_fallback_scene,
    recover_intent,
    sanitize_llm_output,
    sanitize_message,
)
from mijia_agent.command_service import CommandService
from mijia_agent.config import Settings
from mijia_agent.console import ConsoleTools
from mijia_agent.gateway import Gateway
from mijia_agent.llm_log import LlmCallLogger
from mijia_agent.models import AgentError, Scene, Turn
from mijia_agent.service import AgentService

TOKEN = "v1.fake.opaque.automation.token"
IDEMPOTENCY_KEY = "valid-idempotency-key-0001"

SCENES = [
    Scene(
        alias="scene_0123456789abcdef",
        name="回家模式",
        description="已审核低风险场景",
        actionCount=1,
    ),
    Scene(
        alias="scene_fedcba9876543210",
        name="明亮模式",
        description="已审核低风险场景",
        actionCount=2,
    ),
]
HOME_SCENE = SCENES[0]


def settings(environment="production"):
    return Settings(
        internal_secret="test-python-secret-" * 3,
        tools_secret="test-tools-secret-" * 3,
        gateway_key="fake-gateway-key",
        gateway_url="https://gateway.example/v1",
        console_url="https://console.example",
        model="test-model",
        allowed_models=("test-model",),
        environment=environment,
        legacy_router_enabled=True,
    )


def request(**overrides):
    raw = {"text": "我回家了"}
    return CommandRequest.model_validate(raw | overrides)


class FakeConsoleTools:
    def __init__(self):
        self.calls = []
        self.scenes = SCENES
        self.execution = {"status": "success", "succeeded": 2, "failed": 0}
        self.error = None

    async def list_scenes(self, token, request_id, home):
        if self.error:
            raise self.error
        self.calls.append(("list", token, request_id, home))
        return self.scenes

    async def activate_scene(self, token, request_id, home, alias, idempotency_key):
        self.calls.append(("activate", token, request_id, home, alias, idempotency_key))
        return self.execution


class FakeGateway:
    """Records model requests; returns scripted responses."""

    def __init__(self):
        self.settings = settings()
        self.requests = []
        self.contexts = []
        self.response = {"choices": [{"message": {"content": "好的"}}]}
        self.error = None

    async def chat(self, model_request, context):
        if self.error:
            raise self.error
        self.requests.append(model_request)
        self.contexts.append(context)
        return self.response, None


def tool_call_response(alias, reply="好的，已开启", content=None):
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "activate_scene",
                                "arguments": json.dumps({"sceneId": alias, "replyMessage": reply}),
                            },
                        }
                    ],
                }
            }
        ]
    }


def service(gateway=None, tools=None, idempotency=None, environment="production"):
    return CommandService(
        gateway or FakeGateway(),
        tools or FakeConsoleTools(),
        idempotency=idempotency,
        preview=environment == "preview",
    )


def run(service_instance, **request_overrides):
    return asyncio.run(
        service_instance.run(TOKEN, request(**request_overrides), IDEMPOTENCY_KEY, "hash")
    )


# --- command_rules: parity matrix -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "我还没回家",
        "不要开回家模式",
        "如果我回家了就开",
        "回家模式是什么",
        "他说他回家了",
        "今天天气怎么样",
        "关灯",
    ],
)
def test_negation_ambiguity_and_chit_chat_never_execute(text):
    assert find_fallback_scene(text, SCENES) is None
    assert recover_intent(text, None, SCENES) is None


@pytest.mark.parametrize("text", ["我回家了", "到家了", "开启回家模式"])
def test_home_phrases_find_fallback_scene(text):
    assert find_fallback_scene(text, SCENES) is HOME_SCENE


def test_fallback_requires_home_like_scene_in_catalog():
    assert find_fallback_scene("我回来了", [SCENES[1]]) is None


def test_recover_intent_when_model_claims_execution_without_tool_call():
    recovered = recover_intent("打开明亮模式", "好的，已经为您打开明亮模式", SCENES)
    assert recovered is not None
    assert recovered[0] is SCENES[1]
    assert recovered[1] == "已经开启「明亮模式」。"


def test_recover_intent_direct_scene_name_without_llm_claim():
    recovered = recover_intent("帮我打开明亮模式", None, SCENES)
    assert recovered is not None
    assert recovered[0] is SCENES[1]


def test_sanitize_message_replaces_alias_and_masks_ids():
    message = f'好的，已开启「回家模式」 sceneId: "{HOME_SCENE.alias}" 123456789012345678'
    cleaned = sanitize_message(message, SCENES)
    assert HOME_SCENE.alias not in cleaned
    assert "sceneId" not in cleaned
    assert "123456789012345678" not in cleaned
    assert "对应场景" in cleaned


def test_sanitize_llm_output_keeps_scene_reference_readable():
    output = f'call activate_scene(sceneId: "{HOME_SCENE.alias}")'
    cleaned = sanitize_llm_output(output, SCENES)
    assert HOME_SCENE.alias not in cleaned
    assert "回家模式" in cleaned
    assert sanitize_llm_output(None, SCENES) is None


# --- command_service: decision flow -----------------------------------------


def test_tool_call_response_shape_and_executor_wins():
    tools = FakeConsoleTools()
    gateway = FakeGateway()
    gateway.response = tool_call_response(HOME_SCENE.alias, "好的，已开启回家模式")
    result = run(service(gateway, tools))
    assert result.status == "completed"
    assert result.intent == "activate_scene"
    assert result.sceneName == "回家模式"
    assert result.message == "好的，已开启回家模式"
    assert result.execution == {"status": "success", "succeeded": 2, "failed": 0}
    assert result.decisionSource == "llm"
    assert tools.calls[-1][4] == HOME_SCENE.alias
    assert tools.calls[-1][5] == IDEMPOTENCY_KEY


def test_partial_execution_status_wins_over_model_success_text():
    tools = FakeConsoleTools()
    tools.execution = {"status": "partial_success", "succeeded": 1, "failed": 1}
    gateway = FakeGateway()
    gateway.response = tool_call_response(HOME_SCENE.alias, "已全部成功")
    result = run(service(gateway, tools))
    assert result.status == "partial_success"
    assert result.execution["failed"] == 1


def test_no_action_returns_not_understood_with_sanitized_reply():
    tools = FakeConsoleTools()
    gateway = FakeGateway()
    gateway.response = {"choices": [{"message": {"content": "好的，请问需要什么帮助？"}}]}
    result = run(service(gateway, tools), text="今天天气怎么样")
    assert result.status == "not_understood"
    assert result.intent == "none"
    assert result.message == "好的，请问需要什么帮助？"
    assert result.llmOutput == "好的，请问需要什么帮助？"
    assert len(tools.calls) == 1
    assert tools.calls[0][0] == "list" and tools.calls[0][1] == TOKEN


def test_model_claiming_execution_without_tool_call_is_recovered():
    gateway = FakeGateway()
    gateway.response = {"choices": [{"message": {"content": "好的，已经为您打开明亮模式"}}]}
    result = run(service(gateway), text="明亮模式")
    assert result.intent == "activate_scene"
    assert result.sceneName == "明亮模式"
    assert result.decisionSource == "llm"


def test_gateway_failure_falls_back_deterministically_for_home_text():
    gateway = FakeGateway()
    gateway.error = AgentError("AI_GATEWAY_TIMEOUT", 504)
    result = run(service(gateway))
    assert result.decisionSource == "deterministic_fallback"
    assert result.sceneName == "回家模式"
    assert result.message == "欢迎回家，已经开启回家模式。"


def test_gateway_failure_without_home_text_maps_to_public_codes():
    gateway = FakeGateway()
    gateway.error = AgentError("AI_GATEWAY_TIMEOUT", 504)
    with pytest.raises(AgentError) as caught:
        run(service(gateway), text="打开客厅的灯")
    assert (caught.value.code, caught.value.status) == ("LLM_TIMEOUT", 504)

    gateway.error = AgentError("AI_GATEWAY_UNAVAILABLE")
    with pytest.raises(AgentError) as caught:
        run(service(gateway), text="打开客厅的灯")
    assert (caught.value.code, caught.value.status) == ("LLM_PROVIDER_ERROR", 502)


def test_disallowed_tool_call_fails_closed_without_fallback():
    gateway = FakeGateway()
    gateway.response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "activate_scene",
                                "arguments": json.dumps({"sceneId": "scene_ffffffffffffffff"}),
                            },
                        }
                    ]
                }
            }
        ]
    }
    with pytest.raises(AgentError) as caught:
        run(service(gateway), text="我回家了")
    assert caught.value.code == "LLM_PROVIDER_ERROR"


def test_tool_call_requires_idempotency_key():
    gateway = FakeGateway()
    gateway.response = tool_call_response(HOME_SCENE.alias)
    with pytest.raises(AgentError) as caught:
        asyncio.run(service(gateway).run(TOKEN, request(), None, "hash"))
    assert (caught.value.code, caught.value.status) == ("INVALID_REQUEST", 400)


def test_conversation_reset_prefix_and_new_conversation_id():
    history = [
        message
        for i in range(5)
        for message in (
            {"role": "user", "content": f"问题{i}"},
            {"role": "assistant", "content": f"回答{i}"},
        )
    ]
    gateway = FakeGateway()
    gateway.response = {"choices": [{"message": {"content": "好的"}}]}
    result = run(
        service(gateway),
        text="今天天气怎么样",
        conversationId="conv_existing",
        history=history,
    )
    assert result.conversationReset is True
    assert result.turnIndex == 1
    assert result.conversationId != "conv_existing"
    assert result.message.startswith("已开启新一轮对话。")
    # Reset drops prior history from the model request.
    assert all(
        m["role"] != "assistant" or "回答" not in m["content"]
        for m in gateway.requests[0]["messages"]
    )


def test_preview_never_calls_model_or_tools():
    gateway, tools = FakeGateway(), FakeConsoleTools()
    result = run(service(gateway, tools, environment="preview"))
    assert result.status == "not_understood"
    assert "预览模式" in result.message
    assert not gateway.requests and not tools.calls


def test_model_request_pins_non_thinking_and_shared_prompt():
    gateway = FakeGateway()
    gateway.response = {"choices": [{"message": {"content": "好的"}}]}
    run(service(gateway))
    model_request = gateway.requests[0]
    assert model_request["enable_thinking"] is False
    assert model_request["temperature"] == 0
    assert "家庭控制意图路由器" in model_request["messages"][0]["content"]
    assert [t["function"]["name"] for t in model_request["tools"]] == ["activate_scene"]
    assert "replyMessage" in json.dumps(model_request["tools"])


def test_model_request_and_logs_contain_no_token_or_identity():
    sink = io.StringIO()
    logger = LlmCallLogger(sink=sink)
    http_gateway = Gateway(settings(), httpx.AsyncClient(transport=handler_transport()), logger)
    tools = FakeConsoleTools()
    asyncio.run(service(http_gateway, tools).run(TOKEN, request(), IDEMPOTENCY_KEY, "hash"))
    log_text = sink.getvalue()
    assert TOKEN not in log_text
    assert "fake-gateway-key" not in log_text
    assert "test-tools-secret" not in log_text


def handler_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "好的"}}]})

    return httpx.MockTransport(handler)


def test_llm_call_is_logged_with_request_response_and_latency():
    sink = io.StringIO()
    logger = LlmCallLogger(sink=sink)

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["enable_thinking"] is False
        return httpx.Response(200, json=tool_call_response(HOME_SCENE.alias, content=None))

    gateway = Gateway(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)), logger)
    asyncio.run(service(gateway).run(TOKEN, request(), IDEMPOTENCY_KEY, "hash"))
    record = json.loads(sink.getvalue().splitlines()[0])
    assert record["event"] == "llm_call"
    assert record["request"]["messages"]
    assert record["response"]["toolCalls"][0]["name"] == "activate_scene"
    assert record["latencyMs"] >= 0
    assert "usage" in record


def test_llm_call_failure_is_logged():
    sink = io.StringIO()
    logger = LlmCallLogger(sink=sink)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream-secret")

    gateway = Gateway(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)), logger)
    with pytest.raises(AgentError, match="LLM_PROVIDER_ERROR"):
        asyncio.run(
            service(gateway).run(TOKEN, request(text="打开客厅的灯"), IDEMPOTENCY_KEY, "hash")
        )
    record = json.loads(sink.getvalue().splitlines()[0])
    assert record["event"] == "llm_call_failed"
    assert record["code"] == "AI_GATEWAY_UNAVAILABLE"
    assert "upstream-secret" not in sink.getvalue()


# --- idempotency -------------------------------------------------------------


def test_idempotency_replays_completed_response():
    store = IdempotencyStore()
    gateway = FakeGateway()
    gateway.response = tool_call_response(HOME_SCENE.alias)
    tools = FakeConsoleTools()
    svc = service(gateway, tools, idempotency=store)
    first = asyncio.run(svc.run(TOKEN, request(), IDEMPOTENCY_KEY, "hash"))
    second = asyncio.run(svc.run(TOKEN, request(), IDEMPOTENCY_KEY, "hash"))
    assert second.model_dump() == first.model_dump()
    assert len(tools.calls) == 2  # one list + one activate


def test_idempotency_conflict_on_different_body():
    store = IdempotencyStore()
    gateway = FakeGateway()
    gateway.response = tool_call_response(HOME_SCENE.alias)
    svc = service(gateway, FakeConsoleTools(), idempotency=store)
    asyncio.run(svc.run(TOKEN, request(), IDEMPOTENCY_KEY, "hash1"))
    with pytest.raises(AgentError) as caught:
        asyncio.run(svc.run(TOKEN, request(text="到家了"), IDEMPOTENCY_KEY, "hash2"))
    assert (caught.value.code, caught.value.status) == ("IDEMPOTENCY_CONFLICT", 409)


def test_idempotency_reports_processing_then_fails_closed_on_retry():
    store = IdempotencyStore()
    store.start(IDEMPOTENCY_KEY, "hash")
    svc = service(FakeGateway(), FakeConsoleTools(), idempotency=store)
    result = asyncio.run(svc.run(TOKEN, request(), IDEMPOTENCY_KEY, "hash"))
    assert result.status == "processing"
    store.fail(IDEMPOTENCY_KEY, {})
    # failed records allow retry, like the console store
    assert store.lookup(IDEMPOTENCY_KEY, "hash") == "failed"


def test_valid_idempotency_key_bounds():
    assert valid_idempotency_key("a" * 16)
    assert valid_idempotency_key("a" * 128)
    assert not valid_idempotency_key("a" * 15)
    assert not valid_idempotency_key("a" * 129)
    assert not valid_idempotency_key(None)


def test_request_hash_is_stable_and_order_insensitive():
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})


# --- console client ----------------------------------------------------------


def test_console_agent_tools_sends_token_header_and_home_field():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"scenes": [s.model_dump() for s in SCENES]})

    tools = ConsoleAgentTools(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    scenes = asyncio.run(tools.list_scenes(TOKEN, "req_test", "我的家"))
    assert scenes == SCENES
    assert seen[0].url.path == "/api/ai/tools"
    assert seen[0].headers["x-ai-user-token"] == TOKEN
    assert seen[0].headers["authorization"] == "Bearer " + "test-tools-secret-" * 3
    body = json.loads(seen[0].content)
    assert body == {
        "requestId": "req_test",
        "home": "我的家",
        "tool": "list_scenes",
        "arguments": {},
    }


@pytest.mark.parametrize(
    "code,status",
    [
        ("AUTOMATION_TOKEN_EXPIRED", 401),
        ("AUTOMATION_TOKEN_INVALID", 401),
        ("AI_HOME_NOT_FOUND", 404),
        ("AI_SCENE_EXECUTION_DISABLED", 403),
    ],
)
def test_console_token_errors_pass_through(code, status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": code})

    tools = ConsoleAgentTools(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AgentError) as caught:
        asyncio.run(tools.list_scenes(TOKEN, "req_test", None))
    assert (caught.value.code, caught.value.status) == (code, status)


def test_console_v1_token_environment_error_is_not_collapsed_to_agent_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/internal/assistant/v1/tools:invoke"
        return httpx.Response(500, json={"code": "AI_AUTOMATION_TOKEN_ENV_NOT_CONFIGURED"})

    tools = ConsoleAgentTools(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AgentError) as caught:
        asyncio.run(tools.invoke_home_environment(TOKEN, "req_test", None, {}))
    assert (caught.value.code, caught.value.status) == (
        "AI_AUTOMATION_TOKEN_ENV_NOT_CONFIGURED",
        500,
    )


def test_console_v1_failure_keeps_public_error_safe_and_records_diagnostic_category():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"code": "AI_AGENT_UNAVAILABLE"})

    tools = ConsoleAgentTools(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AgentError) as caught:
        asyncio.run(tools.invoke_home_environment(TOKEN, "req_test", None, {}))
    assert (caught.value.code, caught.value.status, caught.value.diagnostic_code) == (
        "AI_AGENT_UNAVAILABLE",
        502,
        "CONSOLE_HTTP_502",
    )


def test_console_v1_records_only_allowlisted_handler_diagnostic_codes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "code": "AI_AGENT_UNAVAILABLE",
                "diagnosticCode": "ASSISTANT_AUTHORIZATION_EXCEPTION",
            },
        )

    tools = ConsoleAgentTools(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AgentError) as caught:
        asyncio.run(tools.invoke_home_environment(TOKEN, "req_test", None, {}))
    assert (caught.value.code, caught.value.status, caught.value.diagnostic_code) == (
        "AI_AGENT_UNAVAILABLE",
        502,
        "CONSOLE_ASSISTANT_AUTHORIZATION_EXCEPTION",
    )


def test_console_v1_ignores_unrecognized_console_diagnostic_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "code": "AI_AGENT_UNAVAILABLE",
                "diagnosticCode": "SECRET\nresponse-body",
            },
        )

    tools = ConsoleAgentTools(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AgentError) as caught:
        asyncio.run(tools.invoke_home_environment(TOKEN, "req_test", None, {}))
    assert caught.value.diagnostic_code == "CONSOLE_HTTP_502"


def test_console_activate_timeout_maps_to_device_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret", request=request)

    tools = ConsoleAgentTools(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AgentError) as caught:
        asyncio.run(
            tools.activate_scene(TOKEN, "req_test", None, HOME_SCENE.alias, IDEMPOTENCY_KEY)
        )
    assert caught.value.code == "DEVICE_TIMEOUT"


# --- HTTP route contract -----------------------------------------------------


def route_app(gateway=None, console=None):
    config = settings()
    gateway = gateway or FakeGateway()
    console = console or FakeConsoleTools()
    return create_app(config, command_service=service(gateway, console))


def command_post(client, json_body, token=TOKEN, key=IDEMPOTENCY_KEY):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/ai/command", headers=headers, json=json_body)


def test_route_requires_bearer_token():
    with TestClient(route_app()) as client:
        response = command_post(client, {"text": "我回家了"}, token=None)
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/ai/command").json()["status"] == "ok"


def test_route_rejects_oversized_token_and_invalid_body():
    with TestClient(route_app()) as client:
        response = command_post(client, {"text": "我回家了"}, token="x" * 8193)
        assert response.status_code == 401
        assert response.json()["code"] == "AUTOMATION_TOKEN_INVALID"
        response = command_post(client, {"text": ""})
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"
        response = command_post(client, {"text": "x" * 201})
        assert response.status_code == 400
        response = command_post(client, {"text": "好的", "locale": "en-US"})
        assert response.status_code == 400


def test_route_error_bodies_never_echo_input_or_token():
    with TestClient(route_app()) as client:
        response = command_post(client, {"text": "secret-input-value", "extra": 1})
        assert response.status_code == 400
        assert "secret-input-value" not in response.text
        assert TOKEN not in response.text


def test_route_maps_service_errors_to_public_codes():
    tools = FakeConsoleTools()
    tools.error = AgentError("AUTOMATION_TOKEN_EXPIRED", 401)
    with TestClient(route_app(console=tools)) as client:
        response = command_post(client, {"text": "我回家了"})
        assert response.status_code == 401
        assert response.json()["code"] == "AUTOMATION_TOKEN_EXPIRED"

    tools = FakeConsoleTools()
    tools.error = AgentError("AI_HOME_NOT_FOUND", 404)
    with TestClient(route_app(console=tools)) as client:
        response = command_post(client, {"text": "我回家了"})
        assert response.status_code == 404
        assert response.json()["code"] == "AI_HOME_NOT_FOUND"

    tools = FakeConsoleTools()
    tools.error = AgentError("AI_SCENE_NOT_FOUND", 400)
    with TestClient(route_app(console=tools)) as client:
        response = command_post(client, {"text": "我回家了"})
        assert response.status_code == 400
        assert response.json()["code"] == "AI_SCENE_NOT_FOUND"


def test_route_returns_202_while_processing():
    store = IdempotencyStore()
    store.start(IDEMPOTENCY_KEY, request_hash({"text": "我回家了"}))
    config = settings()
    app = create_app(
        config, command_service=service(FakeGateway(), FakeConsoleTools(), idempotency=store)
    )
    with TestClient(app) as client:
        response = command_post(client, {"text": "我回家了"})
        assert response.status_code == 202
        assert response.json()["status"] == "processing"


def test_route_requires_key_only_when_action_is_selected():
    gateway = FakeGateway()
    gateway.response = {"choices": [{"message": {"content": "好的，需要什么帮助？"}}]}
    with TestClient(route_app(gateway=gateway)) as client:
        response = command_post(client, {"text": "今天天气怎么样"}, key=None)
        assert response.status_code == 200
        assert response.json()["status"] == "not_understood"
    gateway.response = tool_call_response(HOME_SCENE.alias)
    with TestClient(route_app(gateway=gateway)) as client:
        response = command_post(client, {"text": "我回家了"}, key="short")
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


def test_route_replays_idempotent_completed_response():
    gateway = FakeGateway()
    gateway.response = tool_call_response(HOME_SCENE.alias)
    app = route_app(gateway=gateway)
    with TestClient(app) as client:
        first = command_post(client, {"text": "我回家了"})
        second = command_post(client, {"text": "我回家了"})
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == first.json()


def test_route_oversized_body_is_rejected():
    with TestClient(route_app()) as client:
        response = client.post(
            "/ai/command",
            headers={"Authorization": f"Bearer {TOKEN}"},
            content=b'{"text": "' + b"x" * 70000 + b'"}',
        )
        assert response.status_code == 400


def test_route_end_to_end_with_fake_console_and_gateway():
    """Full /ai/command round trip: gateway + console /api/ai/tools fakes."""

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        content = json.loads(json.loads(request.content)["messages"][-1]["content"])
        if "回家" in content["text"]:
            return httpx.Response(
                200, json=tool_call_response(HOME_SCENE.alias, "好的，已开启回家模式")
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "好的，需要什么帮助？"}}]}
        )

    def console_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ai/tools"
        assert request.headers["x-ai-user-token"] == TOKEN
        body = json.loads(request.content)
        if body["tool"] == "list_scenes":
            return httpx.Response(200, json={"scenes": [s.model_dump() for s in SCENES]})
        return httpx.Response(403, json={"code": "AI_SCENE_EXECUTION_DISABLED"})

    config = settings()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                gateway_handler(request)
                if request.url.host == "gateway.example"
                else console_handler(request)
            )
        )
    )
    gateway = Gateway(config, client, LlmCallLogger(sink=io.StringIO()))
    app = create_app(
        config, command_service=CommandService(gateway, ConsoleAgentTools(config, client))
    )
    with TestClient(app) as test_client:
        response = command_post(test_client, {"text": "我回家了"})
        assert response.status_code == 403
        assert response.json()["code"] == "AI_SCENE_EXECUTION_DISABLED"
        assert response.headers["cache-control"] == "no-store"
        # A second, different message still routes through the fake console.
        response = command_post(test_client, {"text": "今天天气怎么样"}, key=None)
        assert response.status_code == 200
        assert response.json()["status"] == "not_understood"


# --- chat pipeline: get_home_status end-to-end -------------------------------


def test_internal_turn_get_home_status_end_to_end_with_fake_console():
    snapshot = {
        "capturedAt": "2026-09-20T08:00:00Z",
        "completeness": "partial",
        "groups": [
            {
                "metric": "temperature",
                "label": "温度",
                "unit": "°C",
                "latest": {
                    "value": 25.5,
                    "unit": "°C",
                    "sourceLabel": "客厅温湿度计",
                    "roomName": "客厅",
                    "capturedAt": "2026-09-20T08:00:00Z",
                    "freshness": "fresh",
                },
                "readings": [],
            }
        ],
        "warnings": [],
    }
    seen = []

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {"name": "get_home_status", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
        )

    def console_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["tool"] == "list_scenes":
            return httpx.Response(200, json={"scenes": [s.model_dump() for s in SCENES]})
        return httpx.Response(200, json=snapshot)

    config = settings()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                gateway_handler(request)
                if request.url.host == "gateway.example"
                else console_handler(request)
            )
        )
    )
    app = create_app(
        config,
        service=AgentService(
            Gateway(config, client, LlmCallLogger(sink=io.StringIO())),
            ConsoleTools(config, client),
        ),
    )
    data = turn().model_dump()
    data["sessionBinding"] = "fake-opaque-binding"
    with TestClient(app) as test_client:
        response = test_client.post(
            "/internal/v1/turn",
            headers={"Authorization": "Bearer " + config.internal_secret},
            json=data,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "get_home_status"
    assert body["homeStatus"] == snapshot
    assert body["tool"]["name"] == "get_home_status"
    assert [call["tool"] for call in seen] == ["list_scenes", "get_home_status"]
    assert seen[-1]["principalId"] == "usr_example"


def turn():
    return Turn.model_validate(
        {
            "requestId": "req_test_request_0001",
            "conversationId": "conv_example",
            "principalId": "usr_example",
            "homeId": "home-example",
            "message": "家里现在温度多少",
            "idempotencyKey": "test-idempotency-0001",
            "scopes": ["ai:chat"],
            "sessionBinding": "fake-opaque-binding",
        }
    )
