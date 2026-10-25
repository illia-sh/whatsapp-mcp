"""Every read tool, called through a real MCP client session.

A tool that raises, or whose result fails the SDK's output validation, comes
back as a normal JSON-RPC result with isError set -- so checking for a
"result" alone proves nothing. These tests check isError and the content.
"""

import importlib
import json
import sys

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from conftest import ALICE, GROUP

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(message_db, monkeypatch):
    monkeypatch.setenv("WHATSAPP_MCP_TRANSPORT", "stdio")
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    yield main.mcp
    sys.modules.pop("main", None)


async def call(server, name, **args):
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.call_tool(name, args)
    text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    assert not result.isError, f"{name} failed: {text}"
    return text, result.structuredContent


async def test_list_chats(server):
    text, _ = await call(server, "list_chats")
    assert "Alice" in text and "Test Group" in text


async def test_list_chats_without_last_message(server):
    text, _ = await call(server, "list_chats", include_last_message=False)
    assert "Alice" in text


async def test_list_messages(server):
    text, _ = await call(server, "list_messages", include_context=False)
    assert "hello" in text and "group note" in text


async def test_list_messages_with_context(server):
    text, _ = await call(server, "list_messages", query="hello")
    assert "hello" in text


async def test_search_contacts(server):
    text, _ = await call(server, "search_contacts", query="Alice")
    assert ALICE in text


async def test_get_chat(server):
    text, _ = await call(server, "get_chat", chat_jid=GROUP)
    assert "Test Group" in text


async def test_get_chat_missing(server):
    # A missing chat is a normal answer, not a tool failure.
    await call(server, "get_chat", chat_jid="0@s.whatsapp.net")


async def test_get_direct_chat_by_contact(server):
    text, _ = await call(server, "get_direct_chat_by_contact", sender_phone_number="15550000001")
    assert ALICE in text


async def test_get_contact_chats(server):
    text, _ = await call(server, "get_contact_chats", jid=ALICE)
    assert "Alice" in text


async def test_get_last_interaction(server):
    text, _ = await call(server, "get_last_interaction", jid=ALICE)
    assert "hi back" in text


async def test_get_message_context(server):
    text, _ = await call(server, "get_message_context", message_id="m2", before=1, after=1)
    assert "hello" in text and "hi back" in text


async def test_get_last_interaction_with_unknown_contact(server):
    # No messages is a normal answer (None), not a failed call.
    await call(server, "get_last_interaction", jid="0@s.whatsapp.net")


async def test_list_messages_reports_database_errors_as_text(server, monkeypatch, tmp_path):
    import whatsapp
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(tmp_path / "empty.db"))
    text, _ = await call(server, "list_messages")
    assert "database error" in text


async def test_booleans_survive_structured_output(server):
    _, structured = await call(server, "get_message_context", message_id="m2", before=1, after=0)
    assert structured["message"]["is_from_me"] is True
    assert structured["before"][0]["is_from_me"] is False
