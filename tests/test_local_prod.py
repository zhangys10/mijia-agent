import json
import stat
from pathlib import Path
from types import SimpleNamespace

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
        "AI_PREVIEW_MODE": "true",
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
    assert child["AI_PREVIEW_MODE"] == "false"
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
        "AI_PREVIEW_MODE": "false",
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
        "AI_PREVIEW_MODE": "false",
        "AI_GATEWAY_ALLOWED_MODELS": PROD_ENV["AI_GATEWAY_MODEL"],
        "MIJIA_CONSOLE_BASE_URL": f"https://{host}",
    }

    with pytest.raises(local_prod.CliError, match="MIJIA_CONSOLE_BASE_URL"):
        local_prod.production_settings(env)


def test_invalid_settings_error_does_not_echo_values():
    secret_value = "secret-invalid-model-value"
    env = PROD_ENV | {
        "AI_PREVIEW_MODE": "false",
        "AI_GATEWAY_MODEL": secret_value,
        "AI_GATEWAY_ALLOWED_MODELS": "other-model",
    }

    with pytest.raises(local_prod.CliError) as caught:
        local_prod.production_settings(env)

    assert secret_value not in str(caught.value)


def test_target_summary_contains_only_non_secret_targets():
    env = PROD_ENV | {
        "AI_PREVIEW_MODE": "false",
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


def test_confirmation_rejects_non_ascii_without_traceback():
    with pytest.raises(local_prod.CliError, match="nothing was started"):
        local_prod.confirm_production(False, input_fn=lambda _prompt: "使用生产服务")


def test_prepare_local_prod_runs_setup_and_reexecs_in_repository_venv(monkeypatch):
    repo_root = Path(local_prod.__file__).resolve().parents[2]
    setup_script = repo_root / "scripts" / "local-prod-setup.sh"
    interpreter = repo_root / ".venv" / "bin" / "python"
    setup_calls = []
    exec_calls = []
    original_is_file = Path.is_file

    def is_file(path):
        if path == interpreter:
            return True
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(local_prod.sys, "prefix", "/outside-repo-venv")
    monkeypatch.setattr(
        local_prod.subprocess,
        "run",
        lambda command, cwd, check: (
            setup_calls.append((command, cwd, check)) or SimpleNamespace(returncode=0)
        ),
    )
    monkeypatch.setattr(local_prod.os, "execv", lambda *args: exec_calls.append(args))

    env_file = repo_root / "custom-prod.env"
    local_prod.prepare_local_prod(
        ["run", "--message", "hello", "--env-file", "relative.env"], env_file
    )

    assert setup_calls == [(["bash", str(setup_script)], repo_root, False)]
    assert exec_calls == [
        (
            str(interpreter),
            [
                str(interpreter),
                "-m",
                "mijia_agent.local_prod",
                "run",
                "--message",
                "hello",
                "--env-file",
                str(env_file),
                "--i-understand-this-uses-production",
            ],
        )
    ]


def test_build_environment_preserves_default_model_allowlist(tmp_path):
    path = tmp_path / ".env"
    values = {key: value for key, value in PROD_ENV.items() if key != "AI_GATEWAY_ALLOWED_MODELS"}
    write_env(path, values)

    child = local_prod.build_environment(path, {})

    settings = local_prod.Settings.from_env(child)
    assert settings.allowed_models == (PROD_ENV["AI_GATEWAY_MODEL"],)


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


def test_load_cookie_hides_prompt_and_rejects_whitespace(tmp_path):
    cookie = local_prod.load_cookie(None, prompt_fn=lambda _prompt: " sealed-cookie-value ")
    assert cookie == "sealed-cookie-value"

    cookie_path = tmp_path / "cookie.txt"
    cookie_path.write_text("pasted-cookie\n", encoding="utf-8")
    cookie_path.chmod(0o644)
    with pytest.raises(local_prod.CliError, match="chmod 600"):
        local_prod.load_cookie(cookie_path)

    cookie_path.chmod(0o600)
    assert local_prod.load_cookie(cookie_path) == "pasted-cookie"

    multiline = tmp_path / "multiline.txt"
    multiline.write_text("a\nb\n", encoding="utf-8")
    multiline.chmod(0o600)
    with pytest.raises(local_prod.CliError, match="unexpected whitespace"):
        local_prod.load_cookie(multiline)


def test_generate_token_delegates_via_private_file(tmp_path):
    script_path = tmp_path / "fake-console" / "scripts" / "generate-automation-token.ts"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("console.log('v1.fake-token');\n", encoding="utf-8")

    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)

        class Proc:
            returncode = 0

            def communicate(self):
                return "prefix lines\nv1.generated-token\n", ""

        return Proc()

    token = local_prod.generate_token(
        tmp_path / "fake-console",
        "pasted-cookie-value",
        30,
        None,
        None,
        tmp_path / "production.env",
        popen=fake_popen,
    )

    assert token == "v1.generated-token"
    command = captured["command"]
    session_file = Path(command[command.index("--session-file") + 1])
    assert not session_file.exists()  # cookie temp file removed
    assert "pasted-cookie-value" not in " ".join(command)
    assert command[command.index("--env-file") + 1] == str((tmp_path / "production.env").resolve())
    # Node reads the selected token env file and the console's session env;
    # Python passes only the path and production token binding.
    child_env = captured["env"]
    assert set(child_env) <= {"PATH", "HOME", "NODE_ENV"}
    assert child_env["NODE_ENV"] == "production"
    assert "AI_AUTOMATION_TOKEN_SECRET" not in child_env
    assert "XIAOMI_SESSION_SECRET" not in child_env


def test_generate_token_rejects_bad_days(tmp_path):
    script_path = tmp_path / "scripts" / "generate-automation-token.ts"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("// stub\n", encoding="utf-8")

    with pytest.raises(local_prod.CliError, match="between 1 and 90"):
        local_prod.generate_token(
            tmp_path,
            "cookie",
            91,
            None,
            None,
            tmp_path / "production.env",
            popen=lambda *_a, **_k: None,
        )


def test_generate_token_reports_cookie_secret_mismatch_without_node_footer(tmp_path):
    script_path = tmp_path / "scripts" / "generate-automation-token.ts"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("// stub\n", encoding="utf-8")

    def fake_popen(_command, **_kwargs):
        class Proc:
            returncode = 1

            def communicate(self):
                return "", "XIAOMI_SESSION_INVALID: cookie mismatch\nNode.js v24.10.0\n"

        return Proc()

    with pytest.raises(local_prod.CliError, match="XIAOMI_SESSION_INVALID") as caught:
        local_prod.generate_token(
            tmp_path,
            "fake-cookie",
            30,
            None,
            None,
            tmp_path / "production.env",
            popen=fake_popen,
        )

    assert "Node.js" not in str(caught.value)


def test_generate_token_requires_out_file_and_never_prints(tmp_path, capsys):
    script_path = tmp_path / "fake-console" / "scripts" / "generate-automation-token.ts"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("console.log('v1.fake-token');\n", encoding="utf-8")

    def fake_popen(command, **kwargs):
        class Proc:
            returncode = 0

            def communicate(self):
                return "v1.generated-token\n", ""

        return Proc()

    token = local_prod.generate_token(
        tmp_path / "fake-console",
        "pasted-cookie-value",
        30,
        None,
        tmp_path / "token.txt",
        tmp_path / "production.env",
        popen=fake_popen,
    )

    assert token == "v1.generated-token"
    assert token not in capsys.readouterr().out
    written = tmp_path / "token.txt"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert written.read_text(encoding="utf-8").strip() == "v1.generated-token"


def test_main_generate_token_writes_owner_only_file(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    write_env(env_path)
    script_path = tmp_path / "fake-console" / "scripts" / "generate-automation-token.ts"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("console.log('v1.fake-token');\n", encoding="utf-8")

    def fake_popen(command, **kwargs):
        class Proc:
            returncode = 0

            def communicate(self):
                return "v1.generated-token\n", ""

        return Proc()

    monkeypatch.setattr(local_prod.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)
    cookie_path = tmp_path / "cookie.txt"
    cookie_path.write_text("pasted-cookie\n", encoding="utf-8")
    cookie_path.chmod(0o600)

    result = local_prod.main(
        [
            "generate-token",
            "--env-file",
            str(env_path),
            "--console-repo",
            str(tmp_path / "fake-console"),
            "--cookie-file",
            str(cookie_path),
            "--token-out",
            str(tmp_path / "token.txt"),
        ]
    )

    assert result == 0
    assert "v1.generated-token" not in capsys.readouterr().out
    assert stat.S_IMODE((tmp_path / "token.txt").stat().st_mode) == 0o600


def test_generate_token_requires_explicit_console_repo(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    write_env(env_path)
    cookie_path = tmp_path / "cookie.txt"
    cookie_path.write_text("pasted-cookie\n", encoding="utf-8")
    cookie_path.chmod(0o600)
    monkeypatch.chdir(tmp_path)

    result = local_prod.main(
        ["generate-token", "--env-file", str(env_path), "--cookie-file", str(cookie_path)]
    )

    assert result == 2
    assert "pass --console-repo PATH" in capsys.readouterr().err


def test_private_log_path_has_owner_only_permissions():
    directory, path = local_prod.private_log_path()
    try:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        path.unlink()
        directory.rmdir()


def test_malformed_bracketed_url_is_safe_cli_error():
    env = PROD_ENV | {"MIJIA_CONSOLE_BASE_URL": "https://[::1"}

    with pytest.raises(local_prod.CliError, match="MIJIA_CONSOLE_BASE_URL"):
        local_prod.production_settings(env)


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


def test_main_requires_message_for_explicit_idempotency_key():
    assert local_prod.main(["run", "--idempotency-key", "valid-key-0000001"]) == 2


def test_run_requires_acknowledgement_before_token_or_process(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    write_env(path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    monkeypatch.setattr(local_prod, "load_token", lambda *_args: pytest.fail("read token"))
    monkeypatch.setattr(local_prod, "start_agent", lambda *_args: pytest.fail("started process"))
    monkeypatch.setattr(local_prod, "prepare_local_prod", lambda *_args: pytest.fail("ran setup"))

    assert local_prod.main(["run", "--env-file", str(path)]) == 2


def test_run_prompts_for_cookie_without_echo(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    write_env(env_path)
    captured = {}
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("visible cookie prompt"))
    monkeypatch.setattr(local_prod.getpass, "getpass", lambda _prompt: "fake-cookie")
    monkeypatch.setattr(local_prod, "prepare_local_prod", lambda _args, _env_file: None)

    def fake_generate(_repo, cookie, _days, _home, _out, _env_file):
        captured["cookie"] = cookie
        return "v1.fake-token"

    monkeypatch.setattr(local_prod, "generate_token", fake_generate)
    monkeypatch.setattr(
        local_prod,
        "ensure_port_available",
        lambda *_args: (_ for _ in ()).throw(local_prod.CliError("stop before network")),
    )

    result = local_prod.main(
        [
            "run",
            "--env-file",
            str(env_path),
            "--console-repo",
            str(tmp_path),
            "--i-understand-this-uses-production",
        ]
    )

    assert result == 2
    assert captured["cookie"] == "fake-cookie"
    assert "fake-cookie" not in str(capsys.readouterr())


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
    monkeypatch.setattr(local_prod, "prepare_local_prod", lambda _args, _env_file: None)
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
    local_prod.start_agent(PROD_ENV | {"AUTOMATION_TOKEN": "v1.sentinel-token"}, "127.0.0.1", 8123)

    serialized = repr(captured)
    assert "v1.sentinel-token" not in serialized
    assert "automation-token" not in serialized
    assert "--port" in captured["command"]
    assert "-P" not in captured["command"]
    assert captured["cwd"] == str(Path(local_prod.__file__).resolve().parents[1])
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


def test_restore_signal_handlers_skips_none_and_ignores_restore_errors(monkeypatch):
    calls = []

    def fail_restore(signum, handler):
        calls.append((signum, handler))
        raise TypeError("unsupported handler")

    monkeypatch.setattr(local_prod.signal, "signal", fail_restore)
    local_prod.restore_signal_handlers(
        {local_prod.signal.SIGTERM: None, local_prod.signal.SIGHUP: "old"}
    )

    assert calls == [(local_prod.signal.SIGHUP, "old")]


def test_print_assistant_response_renders_bounded_environment_data(capsys):
    local_prod.print_assistant_response(
        {
            "outcome": "tool_answer",
            "answer": {"text": "已读取当前家庭环境状态。"},
            "toolEvents": [{"name": "get_home_environment", "status": "success"}],
            "data": {
                "type": "home_environment",
                "groups": [
                    {
                        "label": "甲醛",
                        "latest": {"value": 0.048, "unit": "mg/m³", "roomName": "客厅"},
                    }
                ],
            },
            "usage": {"totalTokens": 1},
        }
    )

    output = capsys.readouterr().out
    assert "answer: 已读取当前家庭环境状态。" in output
    assert "data: 甲醛 0.048mg/m³（客厅）" in output


def test_send_assistant_forwards_channel_and_expectations_check_read_tool(capsys):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "outcome": "tool_answer",
                "answer": {"text": "已读取。", "speechText": "客厅温度正常。"},
                "toolEvents": [{"name": "get_home_environment", "status": "success"}],
                "usage": {"totalTokens": 12, "estimated": False},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        body, _key = local_prod.send_assistant(
            client, "http://local", "opaque-token", "客厅温度是多少？", None, None, "siri"
        )

    assert requests[0].url.path == "/ai/assistant"
    assert json.loads(requests[0].content)["channel"] == "siri"
    local_prod.print_assistant_response(body, "siri", "get_home_environment")
    output = capsys.readouterr().out
    assert "speechText: 客厅温度正常。" in output
    assert "tool: get_home_environment (success)" in output


def test_assistant_live_read_expectations_fail_closed(capsys):
    body = {
        "answer": {"text": "暂时无法读取。", "speechText": "暂时无法读取。"},
        "toolEvents": [{"name": "get_home_environment", "status": "error"}],
    }
    with pytest.raises(local_prod.CliError, match="Expected successful home read tool"):
        local_prod.print_assistant_response(body, "siri", "get_home_environment")

    with pytest.raises(local_prod.CliError, match="bounded speechText"):
        local_prod.print_assistant_response(
            {"answer": {"text": "ok", "speechText": "x" * 281}}, "siri"
        )

    assert capsys.readouterr().out == ""


def test_expect_tool_requires_a_single_live_read_message(tmp_path, capsys):
    env_path = tmp_path / ".env"
    write_env(env_path)

    result = local_prod.main(
        ["run", "--env-file", str(env_path), "--expect-tool", "get_home_environment"]
    )

    assert result == 2
    assert "--expect-tool requires run --message" in capsys.readouterr().err
