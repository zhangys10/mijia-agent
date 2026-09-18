import importlib.util
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


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

    entry_path = Path(__file__).parents[1] / "adapters/edgeone/cloud-functions/api/index.py"
    spec = importlib.util.spec_from_file_location("edgeone_cloud_function_entry", entry_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    assert isinstance(module.app, FastAPI)
    assert {route.path for route in module.app.routes if hasattr(route, "path")} == {
        "/healthz",
        "/internal/v1/turn",
    }

    with TestClient(module.app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.post("/internal/v1/turn").status_code == 401
