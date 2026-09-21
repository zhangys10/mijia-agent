"""Run the local Python agent against the configured production dependencies.

This is a deliberately explicit live-integration tool. It starts the real ASGI app on
loopback and calls its public ``POST /ai/command`` boundary; it never bypasses the
production console's execution policy.
"""

import argparse
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
from pathlib import Path
from urllib.parse import urlsplit

import httpx

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
    "AI_ENVIRONMENT",
    "AI_LLM_LOG_PATH",
}
CONSOLE_ONLY_SECRETS = {
    "AI_AGENT_INTERNAL_SECRET",
    "AI_AUTOMATION_TOKEN_SECRET",
    "AI_PRINCIPAL_SECRET",
    "XIAOMI_SESSION_SECRET",
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
        elif any(token in value for token in ("$", "`", "$(`", ";")):
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
    allowed = {
        item.strip()
        for item in child.get("AI_GATEWAY_ALLOWED_MODELS", "").split(",")
        if item.strip()
    }
    if model and model not in allowed:
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
        url = urlsplit(env.get(name, ""))
        if url.scheme != "https" or local_hostname(url.hostname):
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
    if not secrets.compare_digest(answer, ACKNOWLEDGEMENT):
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
        return subprocess.Popen(command, env=dict(env), stdin=subprocess.DEVNULL)
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
) -> tuple[dict, str]:
    key = "local-prod-" + secrets.token_hex(16)
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
        raise CliError(f"Request is still processing (Idempotency-Key: {key}); do not retry")
    if response.status_code >= 500:
        raise CliError(
            f"Agent failed after dispatch (HTTP {response.status_code}, Idempotency-Key: {key}); "
            "check state before retrying"
        )
    if response.status_code >= 400 and not isinstance(body.get("code"), str):
        raise CliError(f"Agent returned an invalid error (HTTP {response.status_code})")
    return body, key


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
            if isinstance(body.get("message"), str) and not body.get("code"):
                history.extend(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": body["message"]},
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
        os.replace(source, destination)
        destination.chmod(0o600)
    except OSError as error:
        raise CliError("Could not retain the sensitive LLM log") from error
    print(f"Sensitive LLM log retained at {destination} (mode 0600).")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mijia-agent-local-prod",
        description="Run the local agent against live production dependencies.",
    )
    parser.add_argument("command", choices=("check", "run"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--host", default=DEFAULT_HOST, choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--message", help="Send one prompt and exit instead of opening a REPL")
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
        env = build_environment(args.env_file)
        settings = production_settings(env)
        print(target_summary(settings, args.host, args.port))
        if args.command == "check":
            print("Configuration is valid. No network calls were made.")
            return 0

        confirm_production(args.i_understand_this_uses_production)
        token = load_token(args.token_file)
        ensure_port_available(args.host, args.port)
        log_dir, log_path = private_log_path()
        env["AI_LLM_LOG_PATH"] = str(log_path)
        process = None
        try:
            process = start_agent(env, args.host, args.port)
            base_url = f"http://{args.host}:{args.port}"
            wait_until_ready(process, base_url, READINESS_TIMEOUT_SECONDS)
            run_repl(base_url, token, args.message, args.home)
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
            if cleanup_error is not None:
                raise cleanup_error
        return 0
    except (CliError, KeyboardInterrupt) as error:
        message = str(error) if str(error) else "Interrupted"
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
