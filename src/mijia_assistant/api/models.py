from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from mijia_assistant.models import AssistantResponse, StrictModel


class AssistantRequest(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=500)]
    home: Annotated[str | None, Field(default=None, min_length=1, max_length=100)] = None
    locale: Literal["zh-CN", "en-US"] = "zh-CN"
    timezone: Annotated[str, Field(min_length=1, max_length=64)] = "Asia/Shanghai"
    channel: Literal["web", "siri", "voice", "automation"] = "web"
    conversationId: Annotated[str | None, Field(default=None, min_length=6, max_length=128)] = None

    @field_validator("text")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("empty text")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("invalid timezone") from None
        return value


def public_response(response: AssistantResponse) -> dict:
    data = response.model_dump(exclude_none=True)
    return {
        "requestId": data["request_id"],
        "conversationId": data["conversation_id"],
        "status": data["status"],
        "outcome": data["outcome"],
        "answer": {
            "text": data["answer"]["text"],
            "speak": data["answer"]["speak"],
            "continueConversation": data["answer"]["continue_conversation"],
        },
        **({"data": data["data"]} if "data" in data else {}),
        "toolEvents": [
            {"name": event["name"], "status": event["status"]} for event in data["tool_events"]
        ],
        "usage": {
            "promptTokens": data["usage"]["prompt_tokens"],
            "completionTokens": data["usage"]["completion_tokens"],
            "totalTokens": data["usage"]["total_tokens"],
            "estimated": data["usage"]["estimated"],
        },
    }
