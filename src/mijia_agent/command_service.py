"""Deprecated scene command router retained for controlled retirement.

It may classify legacy scene intent for compatibility, but it never dispatches
physical writes. New assistant behavior belongs in the canonical conversation
engine after the action-scope and deployment gates in the design docs pass.
"""

import json
import uuid

from .command_console import ConsoleAgentTools
from .command_idempotency import FAILED, IdempotencyStore
from .command_models import CommandDecision, CommandRequest, CommandResponse, ProcessingResponse
from .command_rules import (
    DEFAULT_MAX_CONVERSATION_TURNS,
    SYSTEM_PROMPT,
    ConversationState,
    activate_scene_tool,
    default_reply,
    explicit_current_scene_command,
    fallback_reply,
    find_fallback_scene,
    recover_intent,
    sanitize_llm_output,
    sanitize_message,
    user_content,
)
from .gateway import Gateway
from .models import AgentError, Scene

# Only genuine model-call failures may trigger the deterministic fallback.
_GATEWAY_FAILURE_CODES = {
    "AI_GATEWAY_TIMEOUT",
    "AI_GATEWAY_UNAVAILABLE",
    "AI_GATEWAY_RATE_LIMITED",
    "AI_GATEWAY_RESPONSE_INVALID",
}

# Malformed model output (missing choices, unparseable arguments) mirrors the
# console provider, which surfaces those as provider failures and may fall
# back deterministically. Semantically rejected calls (unknown tool or scene,
# extra arguments) fail closed without fallback, like UNSUPPORTED_INTENT.
_PARSE_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError)


class _ModelResponseRejected(Exception):
    """Model produced a parseable but disallowed tool call."""


class CommandService:
    def __init__(
        self,
        gateway: Gateway,
        tools: ConsoleAgentTools,
        idempotency: IdempotencyStore | None = None,
        max_conversation_turns: int = DEFAULT_MAX_CONVERSATION_TURNS,
        preview: bool = False,
    ):
        self.gateway = gateway
        self.tools = tools
        self.idempotency = idempotency or IdempotencyStore()
        self.max_turns = max(1, min(max_conversation_turns, 20))
        self.preview = preview

    async def run(
        self,
        token: str,
        request: CommandRequest,
        idempotency_key: str | None,
        body_hash: str,
    ) -> CommandResponse | ProcessingResponse:
        request_id = "req_" + uuid.uuid4().hex
        conversation_id = request.conversationId or "conv_" + uuid.uuid4().hex
        state = ConversationState(request.history, self.max_turns)
        if state.is_reset:
            conversation_id = "conv_" + uuid.uuid4().hex
        reset_prefix = "已开启新一轮对话。" if state.is_reset else ""

        if self.preview:
            return CommandResponse(
                requestId=request_id,
                conversationId=conversation_id,
                conversationReset=state.is_reset,
                turnIndex=state.turn_index,
                status="not_understood",
                intent="none",
                message=reset_prefix + "预览模式：不会调用模型或控制真实设备。",
            )

        if idempotency_key and body_hash:
            lookup = self.idempotency.lookup(idempotency_key, body_hash)
            if lookup == "conflict":
                raise AgentError("IDEMPOTENCY_CONFLICT", 409)
            if lookup == "processing":
                return ProcessingResponse(requestId=request_id)
            replay = self.idempotency.completed(idempotency_key)
            if replay is not None:
                return CommandResponse.model_validate(replay)
            if lookup == FAILED:
                failure = self.idempotency.failure(idempotency_key)
                if (
                    failure
                    and isinstance(failure.get("code"), str)
                    and type(failure.get("status")) is int
                ):
                    raise AgentError(failure["code"], failure["status"])
                raise AgentError("AI_EXECUTION_STATUS_UNKNOWN", 409)

        scenes = await self.tools.list_scenes(token, request_id, request.home)
        decision = await self._decide(request_id, request, scenes, state)

        if decision.type == "no_action":
            reply = decision.reply or "未找到匹配的场景，您可以告诉我具体的场景名称，例如回家模式。"
            return CommandResponse(
                requestId=request_id,
                conversationId=conversation_id,
                conversationReset=state.is_reset,
                turnIndex=state.turn_index,
                status="not_understood",
                intent="none",
                message=reset_prefix + sanitize_message(reply, scenes),
                decisionSource=decision.decisionSource,
                llmOutput=sanitize_llm_output(decision.llm_output, scenes),
            )

        scene = decision.scene
        assert scene is not None  # narrowed by decision.type == "tool_call"
        if not explicit_current_scene_command(request.text, scene):
            return CommandResponse(
                requestId=request_id,
                conversationId=conversation_id,
                conversationReset=state.is_reset,
                turnIndex=state.turn_index,
                status="not_understood",
                intent="none",
                message=reset_prefix + "如需执行场景，请直接说出场景名称和执行指令。",
                decisionSource=decision.decisionSource,
                llmOutput=sanitize_llm_output(decision.llm_output, scenes),
            )
        # This router is deprecated and frozen. Phase 3 physical writes remain
        # unavailable here until the canonical assistant action-scope flow is
        # deployed and its operational gates in docs/TODO.md have passed.
        raise AgentError("AI_SCENE_EXECUTION_DISABLED", 403)

    async def _decide(
        self,
        request_id: str,
        request: CommandRequest,
        scenes: list[Scene],
        state: ConversationState,
    ) -> CommandDecision:
        model_request = self._model_request(request, scenes, state)
        context = {
            "requestId": request_id,
            "conversationId": request.conversationId,
            "source": "ai_command",
            "home": request.home,
        }
        try:
            body, _usage = await self.gateway.chat(model_request, context)
            message = body["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            llm_output = str(message.get("content") or "").strip()
            if not calls:
                recovered = recover_intent(request.text, llm_output, scenes)
                if recovered:
                    scene, reply = recovered
                    return CommandDecision(
                        type="tool_call", scene=scene, reply=reply, llm_output=llm_output
                    )
                return CommandDecision(type="no_action", reply=llm_output, llm_output=llm_output)
            call = calls[0]
            if call.get("type") != "function":
                raise _ModelResponseRejected("non-function call")
            function = call.get("function") or {}
            if function.get("name") != "activate_scene":
                raise _ModelResponseRejected("unsupported tool")
            args = json.loads(function.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise _ModelResponseRejected("invalid arguments")
            if not {"sceneId"} <= set(args) <= {"sceneId", "replyMessage"}:
                raise _ModelResponseRejected("argument keys")
            reply = args.get("replyMessage")
            if reply is not None and not isinstance(reply, str):
                raise _ModelResponseRejected("invalid replyMessage")
            scene = next(
                (s for s in scenes if s.risk == "low" and s.alias == args.get("sceneId")), None
            )
            if scene is None:
                raise _ModelResponseRejected("unknown scene")
            return CommandDecision(
                type="tool_call",
                scene=scene,
                reply=str(reply or "").strip() or default_reply(scene.name),
                llm_output=llm_output,
            )
        except _ModelResponseRejected:
            # Parseable but disallowed: fail closed, no deterministic fallback.
            raise AgentError("LLM_PROVIDER_ERROR", 502) from None
        except AgentError as error:
            # Provider/model failures fall back deterministically only for
            # home-like scenes with clean text, exactly like the console. Any
            # non-gateway AgentError is re-raised unmapped: only the console
            # client can raise those, and it runs before this decision.
            if error.code not in _GATEWAY_FAILURE_CODES:
                raise
            fallback = find_fallback_scene(request.text, scenes)
            if fallback is not None:
                return CommandDecision(
                    type="tool_call",
                    scene=fallback,
                    reply=fallback_reply(fallback.name),
                    llm_output=f"fallback to scene: {fallback.name}",
                    decisionSource="deterministic_fallback",
                )
            code = "LLM_TIMEOUT" if error.code == "AI_GATEWAY_TIMEOUT" else "LLM_PROVIDER_ERROR"
            status = 504 if code == "LLM_TIMEOUT" else 502
            raise AgentError(code, status) from error
        except _PARSE_ERRORS:
            # Malformed model output mirrors the console provider error path.
            fallback = find_fallback_scene(request.text, scenes)
            if fallback is not None:
                return CommandDecision(
                    type="tool_call",
                    scene=fallback,
                    reply=fallback_reply(fallback.name),
                    llm_output=f"fallback to scene: {fallback.name}",
                    decisionSource="deterministic_fallback",
                )
            raise AgentError("LLM_PROVIDER_ERROR", 502) from None

    def _model_request(
        self, request: CommandRequest, scenes: list[Scene], state: ConversationState
    ) -> dict:
        settings = self.gateway.settings
        history = [m.model_dump() for m in state.effective_prior]
        return {
            "model": settings.model,
            "temperature": 0,
            "enable_thinking": False,
            "max_tokens": settings.max_output_tokens,
            "tools": [activate_scene_tool(scenes)],
            "tool_choice": "auto",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}]
            + history
            + [
                {
                    "role": "user",
                    "content": user_content(request.text, request.locale, request.timezone, scenes),
                }
            ],
        }
