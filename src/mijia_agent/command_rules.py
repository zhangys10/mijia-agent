"""Shared ai/command decision rules, ported from the console's lib/ai.

One implementation serves both agent pipelines:
- ``POST /internal/v1/turn`` (chat) reuses the system prompt, the
  ``activate_scene`` tool schema, and the sanitization helpers so the chat
  brain matches ai/command-grade routing;
- ``POST /ai/command`` additionally uses the deterministic fallback, intent
  recovery, and conversation-state semantics.

Behavior parity baseline is the console's ``lib/ai`` (intent-orchestrator,
fallback, qwen-openai-provider, tool-validator, security/conversation). Scenes
here are the console's sanitized projections — an opaque ``scene_<hex16>``
alias plus a display name — so real scene IDs, DIDs, principal and home
identifiers never enter this module.
"""

import json
import re

from .models import Scene

# Intent-router system prompt, verbatim parity with the console provider.
SYSTEM_PROMPT = """你是家庭控制意图路由器，你的核心职责是识别用户想要执行的家庭场景并调用工具。

【核心规则】
1. 当用户表达要开启、激活、执行、运行、切换到某个场景（例如“打开明亮模式”、“切换到离家”、“回家了”），或者在多轮对话中指代具体场景时，你【必须且只能】调用 activate_scene 工具！
2. 绝对禁止在纯文本回复中声称或假装“已经打开/已经开启/已为您执行”了某个场景！纯文本回复没有任何执行能力，声称已执行但未调用工具是严重故障！
3. availableScenes 列出了当前家庭所有可用场景及其 ID 和名称。调用 activate_scene 时，sceneId 必须严格取自 availableScenes 中的 id；replyMessage 为向用户反馈的自然口语（如“好的，已开启明亮模式”），且 replyMessage 中严禁出现任何纯数字场景 ID！
4. 否定、假设、询问等意图不执行。例如“我还没回家”、“不要开明亮模式”、“明亮模式是什么”。
5. 只有当指令完全与场景无关、指令含糊不清无法确认场景、或者用户在闲聊/提问时，才不要调用工具，直接输出简短文本答复引导用户。
6. 结合上下文历史理解用户的代词指代（例如“那离家呢”、“帮我打开它”、“换成那个”）或后续澄清（例如上一轮询问场景，这一轮回复具体模式名），在明确意图后调用 activate_scene。
7. 每个请求最多调用一次工具。
8. 严禁在任何文本或回复中向用户透露、引用或输出任何纯数字场景 ID 代码，回复中只能使用场景的自然中文显示名称。
9. 禁止输出任何思考过程或思维链，只能调用工具或输出简短答复。"""

# Chat pipeline addendum: the read-only tools the chat contract offers in
# addition to activate_scene (list_scenes, get_home_status, get_device_status).
CHAT_TOOLS_ADDENDUM = """

【只读工具补充（仅网页对话）】
10. 当用户想了解当前家庭有哪些可用场景时，调用 list_scenes 工具。
11. 当用户询问温度、湿度、空气质量、甲醛、二氧化碳等环境数据时，调用 get_home_status 工具；
它只读且无参数，读数由系统返回，不得自行编造任何数值或单位。
12. 当用户询问哪些设备或灯开着、某个房间的设备开关状态时，调用 get_device_status 工具；
它只读且无参数，状态由系统返回，不得自行编造任何设备名称或开关状态。
13. list_scenes、get_home_status 与 get_device_status 是只读查询，不会执行任何设备操作；除查询外的场景意图仍必须调用 activate_scene。"""

FALLBACK_PHRASES = (
    "我回家了",
    "我到家了",
    "我回来了",
    "开启回家模式",
    "打开回家模式",
    "回家",
    "到家",
    "进门",
)
FORBIDDEN_MARKERS = re.compile(
    r"不|别|没|未|如果|假如|假设|是否|吗|呢|什么|怎么|为什么|教程|密码|他说|她说|转述|刚才说|问到|听说"
)
CLAIM_EXECUTION = re.compile(r"(已(经)?|好[的，, ]*已(经)?)(为?您?)?(打开|开启|启动|切换|执行)")
NUMERIC_SCENE_ID = re.compile(r"\b\d{15,22}\b")
SCENE_ID_JSON = re.compile(r"""["']?sceneId["']?\s*:\s*["'][^"']+["']""", re.IGNORECASE)
EMPTY_QUOTE_PAIR = re.compile(r"""[「“”」"']\s*[「“”」"']""")

DEFAULT_MAX_CONVERSATION_TURNS = 5
MAX_CONVERSATION_TURNS_LIMIT = 20
MAX_REPLY_MESSAGE = 200


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


def default_reply(scene_name: str) -> str:
    """Console route default when the model omits replyMessage."""
    if re.search(r"回家|到家|进门", scene_name):
        return f"欢迎回家，已经开启「{scene_name}」。"
    return f"已经开启「{scene_name}」。"


def find_fallback_scene(text: str, scenes: list[Scene]) -> Scene | None:
    """Deterministic home-scene fallback for model-call failures, console parity."""
    normalized = normalize(text)
    if not normalized or FORBIDDEN_MARKERS.search(normalized):
        return None
    if not any(phrase in normalized for phrase in FALLBACK_PHRASES):
        return None
    return next((s for s in scenes if any(phrase in s.name for phrase in FALLBACK_PHRASES)), None)


def recover_intent(
    text: str, llm_output: str | None, scenes: list[Scene]
) -> tuple[Scene, str] | None:
    """Recover a concrete scene intent when the model claims execution without
    calling the tool, or when the user names a scene directly (console parity)."""
    norm_user = normalize(text)
    if not norm_user or FORBIDDEN_MARKERS.search(norm_user):
        return None
    norm_llm = normalize(llm_output or "")
    claims = CLAIM_EXECUTION.search(norm_llm) is not None
    for scene in scenes:
        scene_name = normalize(scene.name)
        if not scene_name:
            continue
        if claims and (scene_name in norm_llm or scene_name in norm_user):
            return scene, fallback_reply(scene.name)
        direct = norm_user in {
            scene_name,
            f"打开{scene_name}",
            f"开启{scene_name}",
            f"切换到{scene_name}",
            f"执行{scene_name}",
            f"启动{scene_name}",
            f"运行{scene_name}",
            f"帮我开{scene_name}",
            f"帮我打开{scene_name}",
            f"开一下{scene_name}",
        }
        if direct:
            return scene, fallback_reply(scene.name)
    return None


def fallback_reply(scene_name: str) -> str:
    # Verbatim console parity: the recovery/fallback path uses a fixed reply
    # for home-like scenes rather than interpolating the scene name.
    if re.search(r"回家|到家|进门", scene_name):
        return "欢迎回家，已经开启回家模式。"
    return f"已经开启「{scene_name}」。"


def sanitize_message(message: str, scenes: list[Scene]) -> str:
    """User-facing message sanitizer: alias→name, strip sceneId JSON, mask IDs."""
    result = message
    for scene in scenes:
        result = result.replace(scene.alias, scene.name)
    result = SCENE_ID_JSON.sub("", result)
    result = NUMERIC_SCENE_ID.sub("对应场景", result)
    result = EMPTY_QUOTE_PAIR.sub("", result)
    return re.sub(r"\s+", " ", result).strip()[: MAX_REPLY_MESSAGE * 2]


def sanitize_llm_output(output: str | None, scenes: list[Scene]) -> str | None:
    if not output:
        return None
    result = output
    for scene in scenes:
        result = result.replace(scene.alias, scene.name)
    result = re.sub(r"""["']?sceneId["']?\s*:\s*["']([^"']+)["']""", r'scene: "\1"', result)
    return NUMERIC_SCENE_ID.sub("对应场景", result)


def activate_scene_tool(scenes: list[Scene]) -> dict:
    """The single write tool, verbatim console schema parity."""
    return {
        "type": "function",
        "function": {
            "name": "activate_scene",
            "description": (
                "激活一个已配置且可用的家庭手动场景（如回家模式、明亮模式、离家模式、"
                "观影模式、睡眠模式等）。当用户想要开启或切换到某个场景时必须调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sceneId": {
                        "type": "string",
                        "enum": [s.alias for s in scenes],
                        "description": "从 availableScenes 中选择最贴合用户意图的场景 ID。",
                    },
                    "replyMessage": {
                        "type": "string",
                        "description": (
                            "执行该场景后向用户回复的一句自然口语表达（不超过25字）。"
                            "严禁包含任何数字ID或代码，只能使用自然的场景中文名称。"
                            "例如离家时说“好的，已开启离家模式，路上注意安全”；"
                            "回家时说“欢迎回家，已为您打开回家模式”；"
                            "明亮时说“好的，已为您开启明亮模式”。"
                        ),
                    },
                },
                "required": ["sceneId", "replyMessage"],
            },
        },
    }


def chat_tools(scenes: list[Scene], allow_activate: bool) -> list[dict]:
    """Model tools for the chat pipeline: read-only tools plus activate_scene."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_scenes",
                "description": "列出当前家庭允许的场景。",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_home_status",
                "description": "查询当前家庭的环境状态（温度、湿度、空气质量等只读数据）。",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_device_status",
                "description": "查询当前家庭各房间的设备开关状态（例如哪些灯开着、空调是否运行，只读数据）。",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
    ]
    if allow_activate and scenes:
        tools.append(activate_scene_tool(scenes))
    return tools


def user_content(text: str, locale: str, timezone: str, scenes: list[Scene]) -> str:
    return json.dumps(
        {
            "text": text,
            "locale": locale,
            "timezone": timezone,
            "availableScenes": [
                {"id": s.alias, "name": s.name, "description": s.description} for s in scenes
            ],
        },
        ensure_ascii=False,
    )


class ConversationState:
    """Port of the console's evaluateConversationState/normalizeHistory window."""

    def __init__(self, prior: list, max_turns: int):
        self.max_turns = max(1, min(max_turns, MAX_CONVERSATION_TURNS_LIMIT))
        bounded = prior[-(self.max_turns * 2) :]
        completed = len(bounded) // 2
        self.is_reset = completed >= self.max_turns
        self.effective_prior = [] if self.is_reset else bounded
        self.turn_index = 1 if self.is_reset else completed + 1
