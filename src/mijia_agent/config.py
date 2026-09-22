import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def endpoint(value: str, development: bool = False) -> str:
    url = urlsplit(value)
    local = development and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
    if (
        (url.scheme != "https" and not local)
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Service URL must use HTTPS without credentials, query, or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    internal_secret: str = field(repr=False)
    tools_secret: str = field(repr=False)
    gateway_key: str = field(repr=False)
    gateway_url: str
    console_url: str
    model: str
    allowed_models: tuple[str, ...]
    timeout_ms: int = 5000
    max_output_tokens: int = 256
    environment: str = "production"
    llm_log_path: str = field(repr=False, default="")

    def __post_init__(self):
        if len(self.internal_secret) < 32 or len(self.tools_secret) < 32:
            raise ValueError(
                "Independent internal and tool secrets must each be at least 32 characters"
            )
        if self.internal_secret == self.tools_secret:
            raise ValueError("Internal and tool secrets must be distinct")
        if not self.gateway_key or not self.model or self.model not in self.allowed_models:
            raise ValueError("Gateway key and explicitly allowed model are required")
        if self.environment not in {"development", "production", "preview"}:
            raise ValueError("Invalid environment")
        if not 1 <= self.timeout_ms <= 60000 or not 1 <= self.max_output_tokens <= 4096:
            raise ValueError("Invalid Gateway timeout or token limit")
        endpoint(self.gateway_url, self.environment == "development")
        endpoint(self.console_url, self.environment == "development")

    @classmethod
    def from_env(cls, env=None):
        """Build settings from a ``os.environ``-like mapping (default: the process env)."""
        env = os.environ if env is None else env
        model = env.get("AI_GATEWAY_MODEL", "")
        return cls(
            internal_secret=env.get("AI_PYTHON_INTERNAL_SECRET", ""),
            tools_secret=env.get("AI_TOOLS_INTERNAL_SECRET", ""),
            gateway_key=env.get("AI_GATEWAY_API_KEY", ""),
            gateway_url=env.get("AI_GATEWAY_BASE_URL", ""),
            console_url=env.get("MIJIA_CONSOLE_BASE_URL", ""),
            model=model,
            allowed_models=tuple(
                x.strip()
                for x in env.get("AI_GATEWAY_ALLOWED_MODELS", model).split(",")
                if x.strip()
            ),
            timeout_ms=int(env.get("AI_GATEWAY_TIMEOUT_MS", "5000")),
            max_output_tokens=int(env.get("AI_GATEWAY_MAX_OUTPUT_TOKENS", "256")),
            environment=env.get("AI_ENVIRONMENT", "production"),
            llm_log_path=env.get("AI_LLM_LOG_PATH", ""),
        )
