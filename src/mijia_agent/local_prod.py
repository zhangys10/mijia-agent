"""Run the local Python agent against the configured production dependencies.

This is a deliberately explicit live-integration tool. It starts the real ASGI app on
loopback and calls its canonical ``POST /ai/assistant`` boundary; it never bypasses the
production console's home exposure or execution policy.
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

from .config import Settings

MAX_AUTOMATION_TOKEN = 8192

ACKNOWLEDGEMENT = "USE PRODUCTION SERVICES"
DEFAULT_ENV_FILE = Path("adapters/edgeone/.env")
CONSOLE_TOKEN_SCRIPT = Path("scripts") / "generate-automation-token.ts"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
READINESS_TIMEOUT_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 65.0
AGENT_ENV_NAMES = {
    "AI_PYTHON_INTERNAL_SECRET",
    "AI_TOOLS_INTERNAL_SECRET",
    "AI_GATEWAY_API_KEY",
    "AI_GATEWAY_BASE_URL",
    "MIJIA_CONSOLE_BASE_URL",
    "AI_GATEWAY_MODEL",
    "AI_GATEWAY_ALLOWED_MODELS",
    "AI_GATEWAY_TIMEOUT_MS",
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
        "and follows the current production device-execution policy. First use may install "
        "local dependencies, link the EdgeOne project, and pull its production environment."
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
    env_file: Path,
    popen=None,
) -> str:
    """Delegate token sealing to the console repo's offline generator.

    The cookie is passed through a private temporary file (never argv or env);
    the generator loads the pulled production env file for the token secret
    and its own checkout's .env for a missing Xiaomi session secret. Python
    never reads either secret or decrypts the session. The generated token
    returns through a pipe and is never echoed or logged.
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
            "--env-file",
            str(env_file.expanduser().resolve()),
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
            if "XIAOMI_SESSION_INVALID:" in (stderr or ""):
                hint = (
                    "XIAOMI_SESSION_INVALID: refresh the production xiaomi_session cookie "
                    "or pull the matching production XIAOMI_SESSION_SECRET"
                )
            elif "AI_AUTOMATION_TOKEN_SECRET must be set" in (stderr or ""):
                hint = "AI_AUTOMATION_TOKEN_SECRET is missing from the selected env file"
            elif "Cannot read env file:" in (stderr or ""):
                hint = "the selected env file could not be read by the console generator"
            else:
                hint = "unexpected Node error in the console token generator"
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


def resolve_console_repo(console_repo: Path | None) -> Path:
    """Resolve the explicitly selected web-console checkout."""

    if console_repo is None:
        raise CliError("Token generation needs the web-console checkout; pass --console-repo PATH")
    return console_repo.expanduser().resolve()


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
        return subprocess.Popen(command, cwd=package_root, env=child_env, stdin=subprocess.DEVNULL)
    except OSError as error:
        raise CliError("Could not start the local agent") from error


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
    channel: str = "web",
) -> tuple[dict, str]:
    key = "local-prod-" + secrets.token_hex(16)
    print(f"Request-Key: {key}")
    try:
        response = client.post(
            base_url + "/ai/assistant",
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": key},
            json=assistant_payload(text, conversation_id, home, channel),
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


def print_assistant_response(
    body: dict, channel: str = "web", expect_tool: str | None = None
) -> None:
    answer = body.get("answer")
    if not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
        raise CliError("Agent returned an invalid assistant response")
    speech_text = None
    if channel in {"siri", "voice"}:
        speech_text = answer.get("speechText")
        if not isinstance(speech_text, str) or len(speech_text) > 280:
            raise CliError("Agent did not return a bounded speechText for the selected channel")
    if expect_tool is not None:
        events = body.get("toolEvents")
        if not isinstance(events, list) or not any(
            isinstance(event, dict)
            and event.get("name") == expect_tool
            and event.get("status") in {"success", "partial"}
            for event in events
        ):
            raise CliError(f"Expected successful home read tool was not observed: {expect_tool}")
    if speech_text is not None:
        print(f"speechText: {speech_text}")
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
    channel: str = "web",
    expect_tool: str | None = None,
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
            body, _key = send_assistant(
                client, base_url, token, text, conversation_id, home, channel
            )
            print_assistant_response(body, channel, expect_tool)
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
        default=None,
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
        "--channel",
        choices=("web", "siri", "voice", "automation"),
        default="web",
        help="Assistant channel to include in the canonical turn request",
    )
    parser.add_argument(
        "--expect-tool",
        choices=("get_home_environment", "get_device_status"),
        help="Require this exposed home-read tool to complete successfully (requires --message)",
    )
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


def prepare_local_prod(argv: list[str], env_file: Path | None = None) -> None:
    """Bootstrap first-run local dependencies, then continue inside the repo venv."""
    repo_root = Path(__file__).resolve().parents[2]
    setup_script = repo_root / "scripts" / "local-prod-setup.sh"
    if not setup_script.is_file():
        raise CliError("Local setup script is missing from this repository checkout")
    completed = subprocess.run(["bash", str(setup_script)], cwd=repo_root, check=False)
    if completed.returncode:
        raise CliError("Local setup did not complete; see the setup error above")

    interpreter = repo_root / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        raise CliError("Local setup did not create the Python environment")
    if Path(sys.prefix).resolve() != interpreter.parent.parent.resolve():
        resumed_args = list(argv)
        if env_file is not None:
            normalized_args: list[str] = []
            skip_env_file_value = False
            for argument in resumed_args:
                if skip_env_file_value:
                    skip_env_file_value = False
                    continue
                if argument == "--env-file":
                    skip_env_file_value = True
                    continue
                if argument.startswith("--env-file="):
                    continue
                normalized_args.append(argument)
            resumed_args = [*normalized_args, "--env-file", str(env_file)]
        if "--i-understand-this-uses-production" not in resumed_args:
            resumed_args.append("--i-understand-this-uses-production")
        os.execv(
            str(interpreter),
            [str(interpreter), "-m", "mijia_agent.local_prod", *resumed_args],
        )


def main(argv: list[str] | None = None) -> int:
    original_argv = list(sys.argv[1:] if argv is None else argv)
    args = create_parser().parse_args(original_argv)
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
        if args.expect_tool and (args.command != "run" or not args.message):
            raise CliError("--expect-tool requires run --message")
        if args.channel != "web" and args.command != "run":
            raise CliError("--channel is only supported by run")
        if args.command == "run":
            confirm_production(args.i_understand_this_uses_production)
            repo_root = Path(__file__).resolve().parents[2]
            if args.env_file == DEFAULT_ENV_FILE:
                args.env_file = repo_root / DEFAULT_ENV_FILE
            elif not args.env_file.is_absolute():
                args.env_file = args.env_file.resolve()
            prepare_local_prod(original_argv, args.env_file)
        env = build_environment(args.env_file)
        settings = production_settings(env)
        print(target_summary(settings, args.host, args.port))
        if args.command == "check":
            print("Configuration is valid. No network calls were made.")
            return 0
        if args.command == "generate-token":
            cookie = load_cookie(args.cookie_file)
            generate_token(
                resolve_console_repo(args.console_repo),
                cookie,
                args.token_days,
                args.home,
                args.token_out,
                args.env_file,
            )
            if args.token_out is None:
                print(
                    "Token generated successfully. It was not printed or written anywhere; "
                    "use run without --token-file to generate and use one in memory."
                )
            return 0

        if args.token_file is not None:
            token = load_token(args.token_file)
        elif args.cookie_file is not None:
            # Non-interactive: generate the token from the provided cookie file.
            cookie = load_cookie(args.cookie_file)
            token = generate_token(
                resolve_console_repo(args.console_repo),
                cookie,
                args.token_days,
                args.home,
                None,
                args.env_file,
            )
        else:
            cookie = load_cookie(None)
            token = generate_token(
                resolve_console_repo(args.console_repo),
                cookie,
                args.token_days,
                args.home,
                None,
                args.env_file,
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
            run_assistant_repl(
                base_url,
                token,
                args.message,
                args.home,
                args.channel,
                args.expect_tool,
            )
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
