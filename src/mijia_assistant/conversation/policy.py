import re

_NO_LOCATION_WEATHER_QUERIES = {
    "今天天气怎么样",
    "今天的天气怎么样",
    "天气怎么样",
    "天气如何",
    "how is the weather today",
    "weather today",
}


def needs_weather_location_clarification(message: str) -> bool:
    """Handle only unmistakable location-free weather questions without an installed provider.

    This is intentionally narrow. It keeps a missing native weather capability from causing a
    model to invent a tool call, while leaving broader language to the general assistant.
    """

    normalized = re.sub(r"[？?！!。.,，\s]+$", "", message.strip().lower())
    return normalized in _NO_LOCATION_WEATHER_QUERIES
