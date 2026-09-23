"""Run the local Python agent against the configured production dependencies.

This is a deliberately explicit live-integration tool. It starts the real ASGI app on
loopback and calls its public ``POST /ai/command`` boundary; it never bypasses the
production console's execution policy.
"""

import argparse
import asyncio
import errno
import getpass
import ipaddress
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from mijia_assistant.api import AssistantRequest
from mijia_assistant.capabilities import CapabilityRegistry, FakeWeatherCapability
from mijia_assistant.conversation import ConversationEngine
from mijia_assistant.models import (
    AssistantContext,
    AssistantError,
    ModelMessage,
    ModelTurn,
    ToolCall,
)

from .command_models import (
    MAX_AUTOMATION_TOKEN,
    CommandRequest,
    CommandResponse,
    ProcessingResponse,
)
from .command_rules import DEFAULT_MAX_CONVERSATION_TURNS
from .config import Settings

ACKNOWLEDGEMENT = "USE PRODUCTION SERVICES"
DEFAULT_ENV_FILE = Path("adapters/edgeone/.env")
DEFAULT_CONSOLE_REPO = Path("..") / "mijia-web-console"
CONSOLE_TOKEN_SCRIPT = Path("scripts") / "generate-automation-token.ts"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
READINESS_TIMEOUT_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 65.0
MAX_HISTORY_MESSAGES = DEFAULT_MAX_CONVERSATION_TURNS * 2
AGENT_ENV_NAMES = {
    "AI_PYTHON_INTERNAL_SECRET",
    "AI_TOOLS_INTERNAL_SECRET",
    "AI_GATEWAY_API_KEY",
    "AI_GATEWAY_BASE_URL",
    "MIJIA_CONSOLE_BASE_URL",
    "AI_GATEWAY_MODEL",
    "AI_GATEWAY_ALLOWED_MODELS",
    "AI_GATEWAY_TIMEOUT_MS",
    "AI_GATEWAY_MAX_OUTPUT_TOKENS",
    "AI_ASSISTANT_MAX_OUTPUT_TOKENS",
    "AI_CAIYUN_BASE_URL",
    "AI_CAIYUN_APP_KEY",
    "AI_CAIYUN_APP_SECRET",
    "AI_AMAP_BASE_URL",
    "AI_AMAP_API_KEY",
    "AI_AMAP_PRIVATE_KEY",
    "AI_WEATHER_TIMEOUT_MS",
    "AI_WEATHER_CACHE_TTL_SECONDS",
    "AI_ENVIRONMENT",
    "AI_LLM_LOG_PATH",
}
CHILD_BASE_ENV_NAMES = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "VIRTUAL_ENV",
}


class CliError(Exception):
    """Safe error suitable for display without leaking upstream details."""


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse the simple KEY=VALUE format written by ``edgeone makers env pull``.

    Shell evaluation is intentionally unsupported. This avoids executing a production
    credential file during a development smoke test.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CliError(f"Cannot read env file: {path}") from error

    result: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise CliError(f"Unsupported env syntax at {path}:{number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            not key
            or not (key[0].isalpha() or key[0] == "_")
            or not all(char.isalnum() or char == "_" for char in key)
        ):
            raise CliError(f"Invalid env name at {path}:{number}")
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise CliError(f"Unclosed quoted value at {path}:{number}")
            value = value[1:-1]
            if quote == '"' and ("$" in value or "`" in value or "\\" in value):
                raise CliError(f"Shell interpolation is not supported at {path}:{number}")
        elif any(token in value for token in ("$", "`", ";")):
            raise CliError(f"Shell syntax is not supported at {path}:{number}")
        result[key] = value
    return result


def build_environment(path: Path, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the isolated child environment without mutating the parent process."""

    base = os.environ if inherited is None else inherited
    child = {key: value for key, value in base.items() if key in CHILD_BASE_ENV_NAMES}
    loaded = parse_env_file(path)
    child.update({key: value for key, value in loaded.items() if key in AGENT_ENV_NAMES})
    child["AI_ENVIRONMENT"] = "production"
    model = child.get("AI_GATEWAY_MODEL", "").strip()
    if model and "AI_GATEWAY_ALLOWED_MODELS" in loaded:
        allowed = {
            item.strip()
            for item in child.get("AI_GATEWAY_ALLOWED_MODELS", "").split(",")
            if item.strip()
        }
        if model not in allowed:
            raise CliError("AI_GATEWAY_MODEL is not present in AI_GATEWAY_ALLOWED_MODELS")
    return child


def local_hostname(hostname: str | None) -> bool:
    if not hostname:
        return True
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            packed = socket.inet_aton(normalized)
            address = ipaddress.ip_address(packed)
        except OSError:
            return False
    return address.is_loopback or address.is_unspecified or address.is_link_local


def production_settings(env: Mapping[str, str]) -> Settings:
    """Validate runtime settings and reject explicit local production targets."""

    for name in ("AI_GATEWAY_BASE_URL", "MIJIA_CONSOLE_BASE_URL"):
        value = env.get(name, "")
        try:
            url = urlsplit(value)
            hostname = url.hostname
        except ValueError as error:
            raise CliError(f"{name} must identify the HTTPS production service") from error
        if url.scheme != "https" or local_hostname(hostname):
            raise CliError(f"{name} must identify the HTTPS production service")
    try:
        return Settings.from_env(env)
    except (TypeError, ValueError) as error:
        raise CliError("Agent settings are incomplete or invalid") from error


def target_summary(settings: Settings, host: str, port: int) -> str:
    """Return a secret-free description of the live target."""

    return "\n".join(
        (
            "Live production target:",
            f"  environment: {settings.environment}",
            f"  gateway: {urlsplit(settings.gateway_url).hostname}",
            f"  console: {urlsplit(settings.console_url).hostname}",
            f"  model: {settings.model}",
            f"  local agent: http://{host}:{port}",
        )
    )


def confirm_production(skip_prompt: bool, input_fn=None) -> None:
    if skip_prompt:
        return
    print(
        "WARNING: this uses real production account/home data, incurs real model cost, "
        "and follows the current production device-execution policy."
    )
    reader = input if input_fn is None else input_fn
    answer = reader(f'Type "{ACKNOWLEDGEMENT}" to continue: ').strip()
    try:
        matched = secrets.compare_digest(answer, ACKNOWLEDGEMENT)
    except TypeError:
        matched = False
    if not matched:
        raise CliError("Production acknowledgement did not match; nothing was started")


def load_token(token_file: Path | None, prompt_fn=None) -> str:
    if token_file is None:
        reader = getpass.getpass if prompt_fn is None else prompt_fn
        token = reader("Production automation token: ").strip()
    else:
        try:
            mode = stat.S_IMODE(token_file.stat().st_mode)
            if mode & 0o077:
                raise CliError("Token file must be owner-only (chmod 600)")
            token = token_file.read_text(encoding="utf-8").strip()
        except CliError:
            raise
        except OSError as error:
            raise CliError(f"Cannot read token file: {token_file}") from error
    if not token or len(token) > MAX_AUTOMATION_TOKEN:
        raise CliError("Automation token is empty or too long")
    return token


def load_cookie(cookie_file: Path | None, prompt_fn=None) -> str:
    """Read the pasted ``xiaomi_session`` cookie without echoing it by default."""

    if cookie_file is None:
        reader = getpass.getpass if prompt_fn is None else prompt_fn
        cookie = reader("xiaomi_session cookie value (paste and press Enter): ").strip()
    else:
        try:
            mode = stat.S_IMODE(cookie_file.stat().st_mode)
            if mode & 0o077:
                raise CliError("Cookie file must be owner-only (chmod 600)")
            cookie = cookie_file.read_text(encoding="utf-8").strip()
        except CliError:
            raise
        except OSError as error:
            raise CliError(f"Cannot read cookie file: {cookie_file}") from error
    if not cookie:
        raise CliError("Pasted cookie is empty")
    if any(character in cookie for character in "\n\r\t"):
        raise CliError("Pasted cookie contains unexpected whitespace")
    return cookie


def generate_token(
    console_repo: Path,
    cookie: str,
    days: int,
    home: str | None,
    token_out: Path | None,
    popen=None,
) -> str:
    """Delegate token sealing to the console repo's offline generator.

    The cookie is passed through a private temporary file (never argv or env);
    the generator auto-loads the console project's own `.env` for the sealing
    secrets, so the Python process never touches Xiaomi session decryption or
    the console's token secrets. The generated token returns through a pipe and
    is never echoed or logged.
    """

    if not (1 <= days <= 90):
        raise CliError("--token-days must be between 1 and 90")
    script = console_repo / CONSOLE_TOKEN_SCRIPT
    if not script.is_file():
        raise CliError(f"Console token script not found: {script}")

    directory = Path(tempfile.mkdtemp(prefix="mijia-agent-cookie-"))
    directory.chmod(0o700)
    cookie_path = directory / "cookie.txt"
    try:
        with open(cookie_path, "w", encoding="utf-8") as handle:
            handle.write(cookie)
        cookie_path.chmod(0o600)

        command = [
            "node",
            "--experimental-strip-types",
            str(script),
            "--session-file",
            str(cookie_path),
            "--days",
            str(days),
        ]
        if home:
            command.extend(["--home", home])
        runner = subprocess.Popen if popen is None else popen
        try:
            process = runner(
                command,
                env={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": os.environ.get("HOME", ""),
                    # The token's AES-GCM AAD binds the environment name; the
                    # production console resolves APP_ENV || NODE_ENV, so the
                    # generator must seal with the same env or verification fails.
                    "NODE_ENV": "production",
                },
                cwd=str(console_repo),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            raise CliError("Could not run the console token generator") from error
        _stdout, stderr = process.communicate()
        if process.returncode != 0:
            detail = (stderr or "").strip().splitlines()
            hint = detail[-1] if detail else "unknown error"
            raise CliError(f"Console token generator failed: {hint}")
        token = _stdout.strip().splitlines()[-1] if _stdout.strip() else ""
        if not token.startswith("v1.") or len(token) > MAX_AUTOMATION_TOKEN:
            raise CliError("Console token generator returned an unexpected response")
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    if token_out is not None:
        try:
            token_out.parent.mkdir(parents=True, exist_ok=True)
            token_out.write_text(token + "\n", encoding="utf-8")
            token_out.chmod(0o600)
        except OSError as error:
            raise CliError(f"Could not write token file: {token_out}") from error
        print(f"Token written to {token_out} (mode 0600).")
    return token


def ensure_port_available(host: str, port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, port))
    except OSError as error:
        raise CliError(f"Local address {host}:{port} is already in use") from error


def private_log_path() -> tuple[Path, Path]:
    directory = Path(tempfile.mkdtemp(prefix="mijia-agent-live-"))
    directory.chmod(0o700)
    path = directory / "llm-calls.jsonl"
    path.touch(mode=0o600)
    return directory, path


def wait_until_ready(process: subprocess.Popen, base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CliError("Local agent exited before becoming ready")
        try:
            response = httpx.get(base_url + "/healthz", timeout=0.5, trust_env=False)
            if response.status_code == 200 and response.json() == {"status": "ok"}:
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.1)
    raise CliError("Timed out waiting for the local agent")


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise CliError("Local agent did not stop; inspect the child process") from error


def start_agent(env: Mapping[str, str], host: str, port: int) -> subprocess.Popen:
    allowed_names = CHILD_BASE_ENV_NAMES | AGENT_ENV_NAMES
    child_env = {key: value for key, value in env.items() if key in allowed_names}
    package_root = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = (
        package_root + os.pathsep + existing_pythonpath if existing_pythonpath else package_root
    )
    command = [
        sys.executable,
        "-P",
        "-m",
        "uvicorn",
        "mijia_agent.app:create_app",
        "--factory",
        "--host",
        host,
        "--port",
        str(port),
        "--no-access-log",
        "--log-level",
        "warning",
    ]
    try:
        return subprocess.Popen(command, env=child_env, stdin=subprocess.DEVNULL)
    except OSError as error:
        raise CliError("Could not start the local agent") from error


def command_payload(
    text: str, conversation_id: str | None, history: list[dict[str, str]], home: str | None
) -> dict:
    try:
        request = CommandRequest.model_validate(
            {
                "text": text,
                "conversationId": conversation_id,
                "history": history[-MAX_HISTORY_MESSAGES:],
                "home": home,
            }
        )
    except ValueError as error:
        raise CliError("Prompt, home, or conversation history is invalid") from error
    return request.model_dump(exclude_none=True)


def send_command(
    client: httpx.Client,
    base_url: str,
    token: str,
    text: str,
    conversation_id: str | None,
    history: list[dict[str, str]],
    home: str | None,
    idempotency_key: str | None = None,
) -> tuple[dict, str]:
    key = idempotency_key or "local-prod-" + secrets.token_hex(16)
    print(f"Idempotency-Key: {key}")
    try:
        response = client.post(
            base_url + "/ai/command",
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": key},
            json=command_payload(text, conversation_id, history, home),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise CliError(
            f"Request outcome is unknown (Idempotency-Key: {key}); do not retry blindly"
        ) from error
    try:
        body = response.json()
    except ValueError as error:
        raise CliError(
            f"Agent returned an invalid response (HTTP {response.status_code})"
        ) from error
    if not isinstance(body, dict):
        raise CliError(f"Agent returned an invalid response (HTTP {response.status_code})")
    if response.status_code == 202:
        raise CliError(
            f"Request is still processing (Idempotency-Key: {key}); the result is unknown"
        )
    if response.status_code >= 500:
        code = body.get("code")
        detail = f", code: {code}" if isinstance(code, str) and code else ""
        raise CliError(
            f"Agent failed after dispatch (HTTP {response.status_code}{detail}, "
            f"Idempotency-Key: {key}); the result is unknown"
        )
    if response.status_code >= 400 and not isinstance(body.get("code"), str):
        raise CliError(f"Agent returned an invalid error (HTTP {response.status_code})")
    return body, key


def assistant_payload(
    text: str, conversation_id: str | None, home: str | None, channel: str = "web"
) -> dict:
    try:
        request = AssistantRequest.model_validate(
            {
                "text": text,
                "conversationId": conversation_id,
                "home": home,
                "channel": channel,
            }
        )
    except ValueError as error:
        raise CliError("Prompt, home, or conversation identifier is invalid") from error
    return request.model_dump(exclude_none=True)


def send_assistant(
    client: httpx.Client,
    base_url: str,
    token: str,
    text: str,
    conversation_id: str | None,
    home: str | None,
) -> tuple[dict, str]:
    key = "local-prod-" + secrets.token_hex(16)
    print(f"Request-Key: {key}")
    try:
        response = client.post(
            base_url + "/ai/assistant",
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": key},
            json=assistant_payload(text, conversation_id, home),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise CliError(
            f"Request outcome is unknown (Request-Key: {key}); do not retry blindly"
        ) from error
    try:
        body = response.json()
    except ValueError as error:
        raise CliError(
            f"Agent returned an invalid response (HTTP {response.status_code})"
        ) from error
    if not isinstance(body, dict):
        raise CliError(f"Agent returned an invalid response (HTTP {response.status_code})")
    if response.status_code >= 400:
        code = body.get("code")
        if not isinstance(code, str):
            raise CliError(f"Agent returned an invalid error (HTTP {response.status_code})")
        raise CliError(f"Agent request failed (HTTP {response.status_code}, code: {code})")
    return body, key


def print_assistant_response(body: dict) -> None:
    answer = body.get("answer")
    if not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
        raise CliError("Agent returned an invalid assistant response")
    print(f"outcome: {body.get('outcome')}")
    print(f"answer: {answer['text']}")
    for event in body.get("toolEvents") or []:
        if isinstance(event, dict):
            print(f"tool: {event.get('name')} ({event.get('status')})")
    _print_structured_data(body.get("data"))
    usage = body.get("usage")
    if isinstance(usage, dict):
        print(f"usage: {json.dumps(usage, ensure_ascii=False)}")


def _print_structured_data(data: object) -> None:
    """Render a compact, safe fallback when a client cannot display a home card."""

    if not isinstance(data, dict):
        return
    if data.get("type") == "home_environment":
        rendered = 0
        for group in data.get("groups") or []:
            if not isinstance(group, dict) or not isinstance(group.get("latest"), dict):
                continue
            latest = group["latest"]
            label = group.get("label")
            value = latest.get("value")
            unit = latest.get("unit")
            room = latest.get("roomName")
            if isinstance(label, str) and isinstance(value, (int, float)) and isinstance(unit, str):
                suffix = f"（{room}）" if isinstance(room, str) and room else ""
                print(f"data: {label} {value}{unit}{suffix}")
                rendered += 1
            if rendered >= 8:
                break
    elif data.get("type") == "device_status":
        rendered = 0
        for room in data.get("rooms") or []:
            if not isinstance(room, dict) or not isinstance(room.get("room"), str):
                continue
            for item in room.get("items") or []:
                if not isinstance(item, dict):
                    continue
                name, state = item.get("name"), item.get("state")
                if isinstance(name, str) and isinstance(state, str):
                    print(f"data: {room['room']} · {name}：{state}")
                    rendered += 1
                if rendered >= 12:
                    return


def run_assistant_repl(
    base_url: str,
    token: str,
    one_shot: str | None,
    home: str | None,
    input_fn=input,
    client_factory=httpx.Client,
) -> None:
    conversation_id: str | None = None
    with client_factory(follow_redirects=False, trust_env=False) as client:
        while True:
            if one_shot is not None:
                text = one_shot.strip()
            else:
                try:
                    text = input_fn("mijia> ").strip()
                except EOFError:
                    break
            if not text or text in {"/quit", "/exit"}:
                break
            body, _key = send_assistant(client, base_url, token, text, conversation_id, home)
            print_assistant_response(body)
            if isinstance(body.get("conversationId"), str):
                conversation_id = body["conversationId"]
            if one_shot is not None:
                break


class _SmokeProvider:
    def __init__(self, turns: list[ModelTurn]):
        self.turns = list(turns)

    async def complete(self, messages: list[ModelMessage], tools: list[dict], ctx):
        if not self.turns:
            raise AssertionError("unexpected model iteration")
        return self.turns.pop(0)


async def _smoke_case(turns: list[ModelTurn], message: str):
    engine = ConversationEngine(
        _SmokeProvider(turns), CapabilityRegistry([FakeWeatherCapability()])
    )
    context = AssistantContext(request_id="req_phase0_smoke", conversation_id="conv_smoke")
    return await engine.run(context, message)


def run_fake_smoke() -> None:
    direct = asyncio.run(
        _smoke_case([ModelTurn(content="舒适湿度通常约为 40%–60%。")], "舒适湿度是多少？")
    )
    weather = asyncio.run(
        _smoke_case(
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall(
                            id="weather_1",
                            name="get_weather",
                            arguments={"location": "新加坡", "days": 1},
                        )
                    ]
                ),
                ModelTurn(content="新加坡目前炎热，有阵雨可能。"),
            ],
            "今天新加坡天气怎么样？",
        )
    )
    clarification = asyncio.run(
        _smoke_case(
            [ModelTurn(content="你想查询哪个城市的天气？", response_kind="clarification")],
            "今天天气怎么样？",
        )
    )
    for name, result in (
        ("direct-answer", direct),
        ("weather-tool-loop", weather),
        ("clarification", clarification),
    ):
        print(f"{name}: {result.outcome}")

    rejected = 0
    cases = [
        (
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall(
                            id="bad_1",
                            name="get_weather",
                            arguments={"location": "新加坡", "unexpected": True},
                        )
                    ]
                )
            ],
            "malformed-tool",
        ),
        (
            [ModelTurn(tool_calls=[ToolCall(id="write_1", name="activate_scene", arguments={})])],
            "physical-write-disabled",
        ),
    ]
    for turns, name in cases:
        try:
            asyncio.run(_smoke_case(turns, name))
        except AssistantError:
            rejected += 1
            print(f"{name}: rejected")
    expected_rejections = len(cases) + 1
    expired_engine = ConversationEngine(
        _SmokeProvider([ModelTurn(content="late")]), CapabilityRegistry()
    )
    expired_context = AssistantContext(
        request_id="req_phase0_expired",
        conversation_id="conv_expired",
        deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    try:
        asyncio.run(expired_engine.run(expired_context, "deadline"))
    except AssistantError:
        rejected += 1
        print("deadline: rejected before model call")
    if rejected != expected_rejections:
        raise CliError("Fake smoke policy cases did not fail closed")
    print("fake smoke: passed (local only; no credentials, network, or physical writes)")


def print_response(body: dict) -> None:
    if body.get("code"):
        print(f"error: {body['code']}")
        if body["code"] == "AI_SCENE_EXECUTION_DISABLED":
            print("Production physical execution remains disabled by the current M2 gate.")
        return
    try:
        response = CommandResponse.model_validate(body)
    except ValueError:
        try:
            processing = ProcessingResponse.model_validate(body)
        except ValueError as error:
            raise CliError("Agent returned an invalid command response") from error
        print(f"status: {processing.status}")
        print(f"message: {processing.message}")
        return
    rendered = response.model_dump(exclude_none=True)
    for key in ("status", "intent", "sceneName", "message", "execution", "decisionSource"):
        value = rendered.get(key)
        if value is not None:
            value = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            print(f"{key}: {value}")


def run_repl(
    base_url: str,
    token: str,
    one_shot: str | None,
    home: str | None,
    input_fn=input,
    client_factory=httpx.Client,
) -> None:
    conversation_id: str | None = None
    history: list[dict[str, str]] = []
    with client_factory(follow_redirects=False, trust_env=False) as client:
        while True:
            if one_shot is not None:
                text = one_shot.strip()
            else:
                try:
                    text = input_fn("mijia> ").strip()
                except EOFError:
                    break
            if not text or text in {"/quit", "/exit"}:
                break
            body, _key = send_command(client, base_url, token, text, conversation_id, history, home)
            print_response(body)
            if isinstance(body.get("conversationId"), str):
                conversation_id = body["conversationId"]
            if body.get("conversationReset") is True:
                history.clear()
                print("conversation: reset by server")
            reply = body.get("message")
            # History entries must be non-empty after strip (server contract
            # trims to 300 chars); a whitespace-only sanitized reply would
            # poison the next turn's validation.
            if isinstance(reply, str) and reply.strip() and not body.get("code"):
                history.extend(
                    [
                        {"role": "user", "content": text.strip()[:300]},
                        {"role": "assistant", "content": reply.strip()[:300]},
                    ]
                )
                history = history[-MAX_HISTORY_MESSAGES:]
            if one_shot is not None:
                break


def retain_log(source: Path, destination: Path) -> None:
    if destination.exists() and destination.is_dir():
        raise CliError("--keep-log must name a file, not a directory")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, destination)
        except OSError as error:
            if getattr(error, "errno", None) != errno.EXDEV:
                raise
            shutil.move(str(source), str(destination))
        destination.chmod(0o600)
    except OSError as error:
        raise CliError("Could not retain the sensitive LLM log") from error
    print(f"Sensitive LLM log retained at {destination} (mode 0600).")


def install_termination_handlers() -> dict[signal.Signals, object]:
    previous = {}

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    return previous


def restore_signal_handlers(previous: Mapping[signal.Signals, object]) -> None:
    for signum, handler in previous.items():
        if handler is None:
            continue
        try:
            signal.signal(signum, handler)
        except (OSError, TypeError, ValueError):
            pass


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mijia-agent-local-prod",
        description="Run the local agent against live production dependencies.",
    )
    parser.add_argument("command", choices=("check", "smoke", "run", "generate-token"))
    parser.add_argument("--profile", choices=("fake", "live-read"), default="live-read")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--console-repo",
        type=Path,
        default=DEFAULT_CONSOLE_REPO,
        help="Path to the mijia-web-console checkout that owns token generation",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        help="File holding the pasted xiaomi_session cookie (owner-only file recommended)",
    )
    parser.add_argument("--token-days", type=int, default=30, help="Token validity in days (1-90)")
    parser.add_argument("--token-out", type=Path, help="Write the generated token to this file")
    parser.add_argument("--host", default=DEFAULT_HOST, choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--message", help="Send one prompt and exit instead of opening a REPL")
    parser.add_argument(
        "--idempotency-key",
        help=(
            "Use an explicit idempotency key with --message; only for the same live agent "
            "process. Replay across a restarted CLI is not exactly-once"
        ),
    )
    parser.add_argument("--home", help="Optional production home name or id")
    parser.add_argument("--keep-log", type=Path, help="Retain the sensitive LLM JSONL log here")
    parser.add_argument(
        "--i-understand-this-uses-production",
        action="store_true",
        help="Skip the typed acknowledgement (for deliberate automation only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise CliError("Port must be between 1 and 65535")
        if args.idempotency_key:
            raise CliError(
                "--idempotency-key cannot resume a prior CLI process; investigate the printed "
                "key and ask for a fresh explicit action instead"
            )
        if args.command == "smoke":
            if args.profile != "fake":
                raise CliError("smoke currently requires --profile fake")
            run_fake_smoke()
            return 0
        if args.command == "run" and args.profile != "live-read":
            raise CliError("run currently requires --profile live-read")
        env = build_environment(args.env_file)
        settings = production_settings(env)
        print(target_summary(settings, args.host, args.port))
        if args.command == "check":
            print("Configuration is valid. No network calls were made.")
            return 0
        if args.command == "generate-token":
            cookie = load_cookie(args.cookie_file)
            generate_token(
                args.console_repo,
                cookie,
                args.token_days,
                args.home,
                args.token_out,
            )
            if args.token_out is None:
                print(
                    "Token generated successfully. It was not printed or written anywhere; "
                    "use run without --token-file to generate and use one in memory."
                )
            return 0

        confirm_production(args.i_understand_this_uses_production)
        if args.token_file is not None:
            token = load_token(args.token_file)
        elif args.cookie_file is not None:
            # Non-interactive: generate the token from the provided cookie file.
            cookie = load_cookie(args.cookie_file)
            token = generate_token(
                args.console_repo,
                cookie,
                args.token_days,
                args.home,
                None,
            )
        else:
            # Offer cookie-based generation inline; a path or 'token' pastes a ready-made token.
            choice = input(
                "Paste the xiaomi_session cookie to generate a token, or enter a token file "
                "path, or type 'token' to paste a ready-made token: "
            ).strip()
            if choice == "token":
                token = load_token(None)
            elif choice and Path(choice).expanduser().is_file():
                token = load_token(Path(choice).expanduser())
            else:
                cookie = load_cookie(None, prompt_fn=lambda _prompt: choice)
                token = generate_token(
                    args.console_repo,
                    cookie,
                    args.token_days,
                    args.home,
                    None,
                )
        ensure_port_available(args.host, args.port)
        log_dir, log_path = private_log_path()
        env["AI_LLM_LOG_PATH"] = str(log_path)
        process = None
        previous_handlers = install_termination_handlers()
        try:
            process = start_agent(env, args.host, args.port)
            base_url = f"http://{args.host}:{args.port}"
            wait_until_ready(process, base_url, READINESS_TIMEOUT_SECONDS)
            run_assistant_repl(base_url, token, args.message, args.home)
        except (CliError, KeyboardInterrupt) as error:
            primary_error = error
            raise
        finally:
            cleanup_error = None
            if process is not None:
                try:
                    stop_process(process)
                except CliError as error:
                    cleanup_error = error
            try:
                if args.keep_log and log_path.exists():
                    retain_log(log_path, args.keep_log)
            finally:
                shutil.rmtree(log_dir, ignore_errors=True)
            restore_signal_handlers(previous_handlers)
            if cleanup_error is not None and primary_error is None:
                raise cleanup_error
            if cleanup_error is not None:
                print(f"error: {cleanup_error}", file=sys.stderr)
        return 0
    except (CliError, KeyboardInterrupt) as error:
        message = str(error) if str(error) else "Interrupted"
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
