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


def is_unsupported_bulk_device_action(message: str) -> bool:
    """Recognize only explicit bulk physical-write commands rejected by V1 policy."""

    normalized = re.sub(r"[？?！!。.,，\s]+$", "", message.strip().lower())
    if (
        re.match(r"^(?:打开|开启|关闭|关掉|turn\s+(?:on|off)|switch\s+(?:on|off))", normalized)
        is None
    ):
        return False
    return re.search(r"(?:所有|全部|全屋|整屋|all\b|every\b)", normalized) is not None


def is_unsupported_compound_device_action(message: str) -> bool:
    """Recognize an explicit request for multiple physical writes.

    The Console action grammar intentionally authorizes one target/property/value
    only. Keep this recognizer narrow so ordinary conjunctions in general chat do
    not get intercepted before the model.
    """

    normalized = re.sub(r"[？?！!。.,，\s]+$", "", message.strip().lower())
    chinese_actions = re.findall(r"(?:打开|开启|关闭|关掉|设置|调到|设为)", normalized)
    if len(chinese_actions) >= 2 and re.search(r"(?:并|然后|再|同时|以及|和)", normalized):
        return True
    english_actions = re.findall(r"(?:turn\s+(?:on|off)|switch\s+(?:on|off)|set\b)", normalized)
    return len(english_actions) >= 2 and re.search(r"\b(?:and|then|also)\b", normalized) is not None


def is_unqualified_device_setting(message: str) -> bool:
    """Catch a mode/value write that names no device before invoking the model."""

    normalized = re.sub(r"[？?！!。.,，\s]+$", "", message.strip().lower())
    return bool(
        re.match(r"^(?:设置|调到|设为|set)\s*[^\s]+(?:模式|mode)$", normalized, re.IGNORECASE)
    )


def is_ungranted_device_action(message: str) -> bool:
    """Recognize a concrete device write that has no server write scope."""

    normalized = re.sub(r"[？?！!。.,，\s]+$", "", message.strip().lower())
    device_words = r"(?:灯|灯带|灯光|空调|窗帘|风扇|加湿器|净化器|插座|开关|设备|light|lamp|air conditioner|curtain|fan|device)"
    return bool(
        re.search(
            rf"(?:打开|开启|关闭|关掉|设置|调到|设为|turn\s+(?:on|off)|set) .*{device_words}",
            normalized,
            re.IGNORECASE,
        )
        or re.search(
            rf"{device_words}.*(?:打开|开启|关闭|关掉|设置|调到|设为|turn\s+(?:on|off)|set)",
            normalized,
            re.IGNORECASE,
        )
    )
