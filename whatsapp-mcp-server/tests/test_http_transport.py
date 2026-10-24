"""Tests for serving the MCP server over Streamable HTTP.

main.py builds its server at import time from the environment, so each test
imports a fresh copy with the HTTP variables set. Requests go straight to the
ASGI app -- nothing listens on a port and nothing contacts WhatsApp.
"""

import importlib
import secrets
import sys

import pytest
from sse_starlette.sse import AppStatus
from starlette.testclient import TestClient

PUBLIC_HOST = "mcp.example.com"
TOKEN = secrets.token_urlsafe(32)

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.fixture
def client(monkeypatch):
    # sse_starlette keeps a module-level Event bound to whichever event loop
    # first used it; each TestClient runs its own loop, so reset it per test.
    AppStatus.should_exit_event = None
    monkeypatch.setenv("WHATSAPP_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("WHATSAPP_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("WHATSAPP_MCP_PUBLIC_URL", f"https://{PUBLIC_HOST}")
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    with TestClient(main.mcp.streamable_http_app()) as c:
        yield c
    sys.modules.pop("main", None)


def initialize(client, host, token=TOKEN, origin=None):
    headers = {
        "Host": host,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/mcp", json=INITIALIZE, headers=headers)


@pytest.mark.parametrize(
    "host",
    [
        PUBLIC_HOST,              # what a reverse proxy forwards
        f"{PUBLIC_HOST}:443",     # a proxy that forwards an explicit port
        "127.0.0.1:9000",         # local health checks
    ],
)
def test_initialize_succeeds_for_allowed_hosts(client, host):
    response = initialize(client, host)
    assert response.status_code == 200, response.text
    assert '"serverInfo"' in response.text


def test_public_origin_is_allowed(client):
    response = initialize(client, PUBLIC_HOST, origin=f"https://{PUBLIC_HOST}")
    assert response.status_code == 200, response.text


def test_unknown_host_is_still_rejected(client):
    # DNS-rebinding protection must stay on for everything else.
    assert initialize(client, "evil.example.com").status_code == 421


def test_foreign_origin_is_rejected(client):
    response = initialize(client, PUBLIC_HOST, origin="https://evil.example.com")
    assert response.status_code == 403


def test_missing_token_is_rejected(client):
    assert initialize(client, PUBLIC_HOST, token=None).status_code == 401


def test_wrong_token_is_rejected(client):
    wrong = secrets.token_urlsafe(32)
    assert initialize(client, PUBLIC_HOST, token=wrong).status_code == 401
