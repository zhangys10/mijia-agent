"""Capability-neutral Mijia Assistant core.

The existing :mod:`mijia_agent` package is the deprecated scene-router stack.
New product behavior belongs here and must not import router decisions or schemas.
"""

from .models import AssistantContext, AssistantResponse

__all__ = ["AssistantContext", "AssistantResponse"]
