import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def runtime_environment(env) -> str:
    environment = env.get("AI_ENVIRONMENT", "production").lower()
    if environment not in {"development", "preview", "production"}:
        raise ValueError("AI_ENVIRONMENT must be development, preview, or production")
    return environment


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
    assistant_max_output_tokens: int = 512
    caiyun_base_url: str = ""
    caiyun_app_key: str = field(repr=False, default="")
    caiyun_app_secret: str = field(repr=False, default="")
    amap_base_url: str = ""
    amap_api_key: str = field(repr=False, default="")
    amap_private_key: str = field(repr=False, default="")
    weather_timeout_ms: int = 3000
    weather_cache_ttl_seconds: int = 300
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
        if (
            not 1 <= self.timeout_ms <= 60000
            or not 1 <= self.assistant_max_output_tokens <= 4096
            or not 100 <= self.weather_timeout_ms <= 3000
            or not 60 <= self.weather_cache_ttl_seconds <= 600
        ):
            raise ValueError("Invalid timeout, token limit, or weather cache configuration")
        endpoint(self.gateway_url, self.environment == "development")
        endpoint(self.console_url, self.environment == "development")
        if bool(self.caiyun_base_url) != bool(self.caiyun_app_key) or bool(
            self.caiyun_base_url
        ) != bool(self.caiyun_app_secret):
            raise ValueError("Caiyun base URL, App Key, and App Secret must be configured together")
        if self.caiyun_base_url:
            endpoint(self.caiyun_base_url, self.environment == "development")
        if bool(self.amap_base_url) != bool(self.amap_api_key) or bool(self.amap_base_url) != bool(
            self.amap_private_key
        ):
            raise ValueError("Amap base URL, API key, and private key must be configured together")
        if self.amap_base_url:
            endpoint(self.amap_base_url, self.environment == "development")

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
            assistant_max_output_tokens=int(env.get("AI_ASSISTANT_MAX_OUTPUT_TOKENS", "512")),
            caiyun_base_url=env.get("AI_CAIYUN_BASE_URL", ""),
            caiyun_app_key=env.get("AI_CAIYUN_APP_KEY", ""),
            caiyun_app_secret=env.get("AI_CAIYUN_APP_SECRET", ""),
            amap_base_url=env.get("AI_AMAP_BASE_URL", ""),
            amap_api_key=env.get("AI_AMAP_API_KEY", ""),
            amap_private_key=env.get("AI_AMAP_PRIVATE_KEY", ""),
            weather_timeout_ms=int(env.get("AI_WEATHER_TIMEOUT_MS", "3000")),
            weather_cache_ttl_seconds=int(env.get("AI_WEATHER_CACHE_TTL_SECONDS", "300")),
            environment=runtime_environment(env),
            llm_log_path=env.get("AI_LLM_LOG_PATH", ""),
        )
