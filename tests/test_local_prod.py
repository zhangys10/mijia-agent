import stat
from pathlib import Path

import httpx
import pytest

from mijia_agent import local_prod

PROD_ENV = {
    "AI_PYTHON_INTERNAL_SECRET": "python-internal-secret-" * 2,
    "AI_TOOLS_INTERNAL_SECRET": "tools-internal-secret-" * 2,
    "AI_GATEWAY_API_KEY": "fake-production-key",
    "AI_GATEWAY_BASE_URL": "https://gateway.example/v1",
    "MIJIA_CONSOLE_BASE_URL": "https://console.example",
    "AI_GATEWAY_MODEL": "@makers/test-model",
    "AI_GATEWAY_ALLOWED_MODELS": "@makers/test-model",
}


def write_env(path: Path, values: dict[str, str] | None = None) -> None:
    values = PROD_ENV if values is None else values
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")


def test_parse_env_file_supports_comments_export_and_literal_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# generated\nexport ALPHA=one\nBETA='two words'\nGAMMA=\"three words\"\n",
        encoding="utf-8",
    )

    assert local_prod.parse_env_file(path) == {
        "ALPHA": "one",
        "BETA": "two words",
        "GAMMA": "three words",
    }


@pytest.mark.parametrize(
    "content",
    [
        "NO_EQUALS",
        "BAD-NAME=value",
        "VALUE=$HOME",
        "VALUE=$(whoami)",
        "VALUE=`whoami`",
        "VALUE=a;whoami",
        'VALUE="${HOME}"',
        "VALUE='unterminated",
    ],
)
def test_parse_env_file_rejects_shell_syntax(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(local_prod.CliError, match="env|syntax|interpolation|quoted"):
        local_prod.parse_env_file(path)


def test_build_environment_isolated_and_policy_preserving(tmp_path):
    path = tmp_path / ".env"
    write_env(path, PROD_ENV | {"XIAOMI_SESSION_SECRET": "must-not-reach-python"})
    inherited = {
        "PATH": "/bin",
        "AI_ENVIRONMENT": "development",
        "UNCHANGED": "yes",
        "AI_AUTOMATION_TOKEN_SECRET": "must-not-reach-python",
        "HTTPS_PROXY": "http://proxy.example:8080",
    }

    child = local_prod.build_environment(path, inherited)

    assert child["PATH"] == "/bin"
    assert "UNCHANGED" not in child
    assert "XIAOMI_SESSION_SECRET" not in child
    assert "AI_AUTOMATION_TOKEN_SECRET" not in child
    assert "HTTPS_PROXY" not in child
    assert child["AI_ENVIRONMENT"] == "production"
    assert child["AI_GATEWAY_ALLOWED_MODELS"] == PROD_ENV["AI_GATEWAY_ALLOWED_MODELS"]
    assert "AI_GATEWAY_API_KEY" not in inherited
    original = path.read_text(encoding="utf-8")
    assert "AI_GATEWAY_ALLOWED_MODELS=@makers/test-model" in original
    assert "XIAOMI_SESSION_SECRET=must-not-reach-python" in original


def test_build_environment_rejects_stale_model_allowlist(tmp_path):
    path = tmp_path / ".env"
    write_env(path, PROD_ENV | {"AI_GATEWAY_ALLOWED_MODELS": "stale-local-model"})

    with pytest.raises(local_prod.CliError, match="AI_GATEWAY_MODEL.*ALLOWED_MODELS"):
        local_prod.build_environment(path, {})


def test_production_settings_rejects_loopback_target_without_exposing_secret():
    env = PROD_ENV | {
        "AI_ENVIRONMENT": "production",
        "AI_GATEWAY_ALLOWED_MODELS": PROD_ENV["AI_GATEWAY_MODEL"],
        "MIJIA_CONSOLE_BASE_URL": "http://localhost:5173",
    }

    with pytest.raises(local_prod.CliError) as caught:
        local_prod.production_settings(env)

    assert "MIJIA_CONSOLE_BASE_URL" in str(caught.value)
    assert PROD_ENV["AI_GATEWAY_API_KEY"] not in str(caught.value)


@pytest.mark.parametrize("host", ["127.1", "0.0.0.0", "localhost.", "[::ffff:127.0.0.1]"])
def test_production_settings_rejects_other_explicit_local_hosts(host):
    env = PROD_ENV | {
        "AI_ENVIRONMENT": "production",
        "AI_GATEWAY_ALLOWED_MODELS": PROD_ENV["AI_GATEWAY_MODEL"],
        "MIJIA_CONSOLE_BASE_URL": f"https://{host}",
    }

    with pytest.raises(local_prod.CliError, match="MIJIA_CONSOLE_BASE_URL"):
        local_prod.production_settings(env)


def test_invalid_settings_error_does_not_echo_values():
    secret_value = "secret-invalid-model-value"
    env = PROD_ENV | {
        "AI_ENVIRONMENT": "production",
        "AI_GATEWAY_MODEL": secret_value,
        "AI_GATEWAY_ALLOWED_MODELS": "other-model",
    }

    with pytest.raises(local_prod.CliError) as caught:
        local_prod.production_settings(env)

    assert secret_value not in str(caught.value)


def test_target_summary_contains_only_non_secret_targets():
    env = PROD_ENV | {
        "AI_ENVIRONMENT": "production",
        "AI_GATEWAY_ALLOWED_MODELS": PROD_ENV["AI_GATEWAY_MODEL"],
    }
    settings = local_prod.production_settings(env)

    summary = local_prod.target_summary(settings, "127.0.0.1", 8000)

    assert "gateway.example" in summary
    assert "console.example" in summary
    assert "@makers/test-model" in summary
    assert PROD_ENV["AI_GATEWAY_API_KEY"] not in summary
    assert PROD_ENV["AI_TOOLS_INTERNAL_SECRET"] not in summary


def test_confirmation_requires_exact_phrase():
    with pytest.raises(local_prod.CliError, match="nothing was started"):
        local_prod.confirm_production(False, input_fn=lambda _prompt: "yes")

    local_prod.confirm_production(False, input_fn=lambda _prompt: local_prod.ACKNOWLEDGEMENT)
    local_prod.confirm_production(True, input_fn=lambda _prompt: pytest.fail("prompted"))


def test_token_file_must_be_owner_only_and_token_stays_out_of_child_env(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("v1.secret-token\n", encoding="utf-8")
    token_path.chmod(0o644)
    with pytest.raises(local_prod.CliError, match="chmod 600"):
        local_prod.load_token(token_path)

    token_path.chmod(0o600)
    token = local_prod.load_token(token_path)
    assert token == "v1.secret-token"

    env_path = tmp_path / ".env"
    write_env(env_path)
    assert token not in local_prod.build_environment(env_path).values()


def test_private_log_path_has_owner_only_permissions():
    directory, path = local_prod.private_log_path()
    try:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        path.unlink()
        directory.rmdir()


def test_command_payload_bounds_history_and_omits_optional_values():
    history = [{"role": "user", "content": str(index)} for index in range(20)]

    body = local_prod.command_payload("hello", None, history, None)

    assert "conversationId" not in body
    assert "home" not in body
    assert len(body["history"]) == local_prod.MAX_HISTORY_MESSAGES
    assert body["history"][0]["content"] == str(20 - local_prod.MAX_HISTORY_MESSAGES)


def test_command_payload_reuses_request_validation():
    with pytest.raises(local_prod.CliError, match="Prompt.*invalid"):
        local_prod.command_payload("x" * 201, None, [], None)


def test_send_command_uses_unique_keys_and_does_not_retry(monkeypatch, capsys):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "requestId": "req_test",
                "conversationId": "conv_test",
                "status": "not_understood",
                "intent": "none",
                "message": "ok",
            },
        )

    keys = iter(("a" * 32, "b" * 32))
    monkeypatch.setattr(local_prod.secrets, "token_hex", lambda _size: next(keys))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first, first_key = local_prod.send_command(
            client, "http://local", "secret-token", "one", None, [], None
        )
        _second, second_key = local_prod.send_command(
            client, "http://local", "secret-token", "two", first["conversationId"], [], None
        )

    assert len(requests) == 2
    assert first_key != second_key
    assert requests[0].headers["idempotency-key"] == first_key
    assert requests[1].headers["idempotency-key"] == second_key
    assert requests[0].headers["authorization"] == "Bearer secret-token"
    assert "secret-token" not in capsys.readouterr().out


def test_send_command_reports_unknown_outcome_without_retry():
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection lost")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(local_prod.CliError, match="outcome is unknown.*do not retry"),
    ):
        local_prod.send_command(client, "http://local", "secret-token", "one", None, [], None)
    assert calls == 1


def test_send_command_treats_server_error_as_post_dispatch_failure():
    def handler(_request):
        return httpx.Response(502, json={"code": "MI_CLOUD_ERROR"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(local_prod.CliError, match="failed after dispatch.*check state"),
    ):
        local_prod.send_command(client, "http://local", "secret-token", "one", None, [], None)


class FakeProcess:
    def __init__(self, statuses=None):
        self.statuses = iter(statuses or [None])
        self.returncode = None
        self.signals = []
        self.killed = False

    def poll(self):
        try:
            value = next(self.statuses)
        except StopIteration:
            value = self.returncode
        if value is not None:
            self.returncode = value
        return value

    def send_signal(self, sent):
        self.signals.append(sent)
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_stop_process_terminates_child():
    process = FakeProcess([None])

    local_prod.stop_process(process)

    assert process.signals == [local_prod.signal.SIGTERM]
    assert not process.killed


def test_check_mode_makes_no_network_or_process_calls(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    write_env(path)
    monkeypatch.setattr(local_prod, "start_agent", lambda *_args: pytest.fail("started process"))
    monkeypatch.setattr(local_prod.httpx, "get", lambda *_args: pytest.fail("network call"))

    result = local_prod.main(["check", "--env-file", str(path)])

    output = capsys.readouterr()
    assert result == 0
    assert "No network calls were made" in output.out
    assert PROD_ENV["AI_GATEWAY_API_KEY"] not in output.out + output.err


def test_run_requires_acknowledgement_before_token_or_process(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    write_env(path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    monkeypatch.setattr(local_prod, "load_token", lambda *_args: pytest.fail("read token"))
    monkeypatch.setattr(local_prod, "start_agent", lambda *_args: pytest.fail("started process"))

    assert local_prod.main(["run", "--env-file", str(path)]) == 2


def test_run_cleans_private_log_when_child_start_fails(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    write_env(env_path)
    token_path = tmp_path / "token"
    token_path.write_text("v1.secret-token", encoding="utf-8")
    token_path.chmod(0o600)
    log_dir = tmp_path / "private-log"
    log_dir.mkdir(mode=0o700)
    log_path = log_dir / "llm-calls.jsonl"
    log_path.touch(mode=0o600)

    monkeypatch.setattr(local_prod, "private_log_path", lambda: (log_dir, log_path))
    monkeypatch.setattr(local_prod, "ensure_port_available", lambda *_args: None)
    monkeypatch.setattr(
        local_prod, "start_agent", lambda *_args: (_ for _ in ()).throw(local_prod.CliError("no"))
    )

    result = local_prod.main(
        [
            "run",
            "--env-file",
            str(env_path),
            "--token-file",
            str(token_path),
            "--i-understand-this-uses-production",
        ]
    )

    assert result == 2
    assert not log_dir.exists()


def test_retain_log_rejects_directory_and_keeps_source(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text("sensitive", encoding="utf-8")
    destination = tmp_path / "logs"
    destination.mkdir()

    with pytest.raises(local_prod.CliError, match="must name a file"):
        local_prod.retain_log(source, destination)

    assert source.read_text(encoding="utf-8") == "sensitive"
    assert stat.S_IMODE(destination.stat().st_mode) & stat.S_IXUSR


def test_retain_log_moves_to_owner_only_file(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text("sensitive", encoding="utf-8")
    destination = tmp_path / "nested" / "kept.jsonl"

    local_prod.retain_log(source, destination)

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "sensitive"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_start_agent_argv_and_env_never_include_automation_token(monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(local_prod.subprocess, "Popen", fake_popen)
    local_prod.start_agent(PROD_ENV, "127.0.0.1", 8123)

    serialized = repr(captured)
    assert "automation-token" not in serialized
    assert "--port" in captured["command"]
    assert captured["stdin"] is local_prod.subprocess.DEVNULL
    assert captured["env"]["PYTHONPATH"].split(local_prod.os.pathsep)[0] == str(
        Path(local_prod.__file__).resolve().parents[1]
    )


def test_termination_handlers_raise_keyboard_interrupt(monkeypatch):
    handlers = {}
    monkeypatch.setattr(local_prod.signal, "getsignal", lambda signum: f"old-{signum}")
    monkeypatch.setattr(
        local_prod.signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler)
    )

    previous = local_prod.install_termination_handlers()

    assert previous[local_prod.signal.SIGTERM] == f"old-{local_prod.signal.SIGTERM}"
    with pytest.raises(KeyboardInterrupt):
        handlers[local_prod.signal.SIGTERM](local_prod.signal.SIGTERM, None)
