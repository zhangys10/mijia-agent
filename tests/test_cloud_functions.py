import importlib.util
import sys
import types
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import mijia_agent
from mijia_agent import app as canonical_app
from mijia_agent import config as canonical_config


def test_edgeone_cloud_functions_entry_exposes_the_internal_asgi_app(monkeypatch):
    environment = {
        "AI_PYTHON_INTERNAL_SECRET": "fake-python-secret-" * 3,
        "AI_TOOLS_INTERNAL_SECRET": "fake-tools-secret-" * 3,
        "AI_GATEWAY_API_KEY": "fake-gateway-key",
        "AI_GATEWAY_BASE_URL": "https://gateway.example/v1",
        "MIJIA_CONSOLE_BASE_URL": "https://console.example",
        "AI_GATEWAY_MODEL": "test-model",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    project_root = Path(__file__).parents[1]
    entry_path = project_root / "adapters/edgeone/cloud-functions/api/index.py"
    runtime_package = types.ModuleType("api")
    runtime_package.mijia_agent = mijia_agent
    monkeypatch.setitem(sys.modules, "api", runtime_package)
    monkeypatch.setitem(sys.modules, "api.mijia_agent", mijia_agent)
    monkeypatch.setitem(sys.modules, "api.mijia_agent.app", canonical_app)
    monkeypatch.setitem(sys.modules, "api.mijia_agent.config", canonical_config)
    spec = importlib.util.spec_from_file_location("edgeone_cloud_function_entry", entry_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    assert isinstance(module.app, FastAPI)
    assert {route.path for route in module.app.routes if hasattr(route, "path")} == {
        "/healthz",
        "/internal/v1/assistant",
        "/ai/assistant",
    }

    with TestClient(module.app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.post("/internal/v1/turn").status_code == 404
        assert client.post("/ai/command").status_code == 404
        assert client.get("/ai/command").status_code == 404
