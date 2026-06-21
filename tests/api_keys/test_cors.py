"""CORS allowlist policy (PRD v4 §9.10 X6). Pure-function tests + a preflight
allow/deny against a throwaway app (no real network, no main.py import side effects).
"""

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.backend.cors import cors_allowed_origins


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("SERVER_BIND_HOST", raising=False)
    yield


def test_default_is_loopback_not_wildcard():
    origins = cors_allowed_origins()
    assert "http://localhost:5173" in origins and "http://127.0.0.1:5173" in origins
    assert "*" not in origins  # wildcard is illegal with credentials and re-opens CSRF


def test_origins_follow_bind_host(monkeypatch):
    monkeypatch.setenv("SERVER_BIND_HOST", "0.0.0.0")
    assert "http://0.0.0.0:5173" in cors_allowed_origins()  # bind + CORS are one knob


def test_override_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com, https://x.example.com")
    assert cors_allowed_origins() == ["https://app.example.com", "https://x.example.com"]


def test_preflight_allows_loopback_denies_foreign():
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/ping")
    def ping():
        return {"ok": True}

    client = TestClient(app)
    good = client.options("/ping", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert good.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.options("/ping", headers={"Origin": "https://evil.com", "Access-Control-Request-Method": "GET"})
    assert bad.headers.get("access-control-allow-origin") != "https://evil.com"
