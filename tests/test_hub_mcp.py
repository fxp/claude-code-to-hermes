"""Tests for the MCP server surface (hub/mcp/).

Covers:

    • Tool registry — build, list, dispatch
    • Individual tool implementations against a populated LocalBuffer
    • JSON-RPC 2.0 parsing and error codes
    • initialize / tools/list / tools/call end-to-end via handle_line()
    • ccm hub mcp-serve CLI wiring (--list path)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


# Repo-relative path to the src/ dir so subprocesses can import the package
# without needing `pip install -e .` to have run on this machine.
_REPO_SRC = str(Path(__file__).resolve().parent.parent / "src")


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _REPO_SRC + (os.pathsep + existing if existing else "")
    return env

from claude_code_migration.hub.buffer import LocalBuffer
from claude_code_migration.hub.mcp import (
    McpServer,
    build_default_registry,
)
from claude_code_migration.hub.mcp.server import _parse_message, PROTOCOL_VERSION
from claude_code_migration.hub.mcp.tools import Tool, ToolRegistry


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def populated_buffer(tmp_path: Path) -> LocalBuffer:
    """A LocalBuffer with a realistic spread across every mirror table."""
    buf = LocalBuffer(tmp_path / "buffer.db")
    now = int(time.time())

    # Identity
    buf.mirror_upsert("mirror_identity", {
        "id": "me", "source_platform": "claude-code",
        "email": "test@example.com", "display_name": "Test User",
        "updated_at": now,
    })

    # Memory items (5 across 3 kinds)
    buf.mirror_upsert("mirror_memory_items", {
        "id": "m1", "kind": "user_profile", "name": "style",
        "content": "prefers concise answers with code examples",
        "source_platform": "claude-code", "updated_at": now,
    })
    buf.mirror_upsert("mirror_memory_items", {
        "id": "m2", "kind": "user_profile", "name": "stack",
        "content": "python typescript rust",
        "source_platform": "claude-code", "updated_at": now,
    })
    buf.mirror_upsert("mirror_memory_items", {
        "id": "m3", "kind": "project_memory", "name": "hermes-notes",
        "content": "hermes migration pipeline uses Workspace Dossier",
        "source_platform": "claude-code", "updated_at": now,
    })
    buf.mirror_upsert("mirror_memory_items", {
        "id": "m4", "kind": "rule", "name": "no-force-push",
        "content": "never force push to main",
        "source_platform": "claude-code", "updated_at": now,
    })
    buf.mirror_upsert("mirror_memory_items", {
        "id": "m5", "kind": "scratch", "name": "todo",
        "content": "ship MCP server, document architecture",
        "source_platform": "claude-code", "updated_at": now,
    })

    # Projects
    buf.mirror_upsert("mirror_projects", {
        "id": "p1", "slug": "hermes-migration", "name": "Hermes Migration",
        "description": "migrate claude to hermes", "context": "ongoing work",
        "is_shared": 0, "updated_at": now,
    })
    buf.mirror_upsert("mirror_projects", {
        "id": "p2", "slug": "dossier-hub", "name": "Dossier Hub",
        "description": "always-on hub", "is_shared": 1, "updated_at": now - 100,
    })

    # Conversations + messages
    buf.mirror_upsert("mirror_conversations", {
        "id": "c1", "platform": "claude-code", "title": "building the hub",
        "model": "claude-sonnet-4", "project_id": "p2",
        "created_at": now - 3600, "updated_at": now - 1800,
    })
    buf.mirror_upsert("mirror_messages", {
        "id": "msg1", "conversation_id": "c1", "role": "user",
        "content_text": "how do I start the hub daemon",
        "timestamp_epoch": now - 3600,
    })
    buf.mirror_upsert("mirror_messages", {
        "id": "msg2", "conversation_id": "c1", "role": "assistant",
        "content_text": "run `ccm hub serve` to start the daemon in foreground",
        "timestamp_epoch": now - 3500,
    })
    buf.mirror_upsert("mirror_messages", {
        "id": "msg3", "conversation_id": "c1", "role": "user",
        "content_text": "what about MCP integration",
        "timestamp_epoch": now - 1900,
    })

    # Skills
    buf.mirror_upsert("mirror_skills", {
        "id": "s1", "name": "browse", "description": "web browsing",
        "body": "use Chrome MCP to navigate", "source_platform": "claude-code",
        "source_plugin": "gstack", "updated_at": now,
    })
    buf.mirror_upsert("mirror_skills", {
        "id": "s2", "name": "review", "description": "code review",
        "body": "check for bugs and style", "source_platform": "claude-code",
        "updated_at": now,
    })

    # Agents
    buf.mirror_upsert("mirror_agents", {
        "id": "a1", "name": "general-purpose", "description": "default research",
        "model": "sonnet", "updated_at": now,
    })

    # MCP endpoints
    buf.mirror_upsert("mirror_mcp_endpoints", {
        "id": "mcp1", "name": "notion", "scope": "user", "transport": "stdio",
        "command": "node", "args": json.dumps(["./notion-mcp.js"]),
    })

    # Hooks
    buf.mirror_upsert("mirror_hooks", {
        "id": "h1", "event": "PreToolUse", "matcher": "Bash",
        "type": "command", "command": "echo bash called", "scope": "user",
    })

    return buf


@pytest.fixture
def server(populated_buffer: LocalBuffer) -> McpServer:
    return McpServer(
        populated_buffer,
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )


# ── Registry ────────────────────────────────────────────────────────

def test_default_registry_has_14_tools():
    reg = build_default_registry()
    assert len(reg) == 14
    names = {t.name for t in reg}
    assert {
        "search_memory", "read_profile", "list_memory", "read_memory",
        "list_skills", "read_skill", "list_agents", "list_hooks",
        "list_mcp_endpoints", "list_projects", "get_project",
        "search_conversations", "get_conversation", "get_stats",
    }.issubset(names)


def test_registry_rejects_duplicate_names():
    reg = ToolRegistry()
    dummy = Tool(name="x", description="", input_schema={}, fn=lambda b, a: None)
    reg.add(dummy)
    with pytest.raises(ValueError, match="already registered"):
        reg.add(dummy)


def test_registry_to_mcp_list_shape():
    reg = build_default_registry()
    mcp_list = reg.to_mcp_list()
    assert all("name" in t and "description" in t and "inputSchema" in t for t in mcp_list)


# ── Tool implementations ────────────────────────────────────────────

def test_search_memory_finds_relevant_content(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    tool = reg.get("search_memory")
    out = tool.fn(populated_buffer, {"query": "hermes"})
    assert any(r["id"] == "m3" for r in out)


def test_search_memory_kind_filter(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    tool = reg.get("search_memory")
    out = tool.fn(populated_buffer, {"query": "python OR concise", "kind": "user_profile"})
    ids = {r["id"] for r in out}
    assert ids.issubset({"m1", "m2"})


def test_search_memory_requires_query(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    tool = reg.get("search_memory")
    with pytest.raises(ValueError, match="query"):
        tool.fn(populated_buffer, {})


def test_read_profile_returns_all_user_profile_items(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("read_profile").fn(populated_buffer, {})
    ids = {r["id"] for r in out}
    assert ids == {"m1", "m2"}


def test_list_memory_filter_by_kind(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("list_memory").fn(populated_buffer, {"kind": "rule"})
    assert len(out) == 1 and out[0]["id"] == "m4"


def test_read_memory_by_id(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("read_memory").fn(populated_buffer, {"id": "m3"})
    assert out["name"] == "hermes-notes"
    assert "Workspace Dossier" in out["content"]


def test_read_memory_missing_returns_none(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    assert reg.get("read_memory").fn(populated_buffer, {"id": "nope"}) is None


def test_list_skills(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("list_skills").fn(populated_buffer, {})
    names = {r["name"] for r in out}
    assert names == {"browse", "review"}


def test_read_skill_by_name(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("read_skill").fn(populated_buffer, {"name": "browse"})
    assert out["source_plugin"] == "gstack"
    assert "Chrome MCP" in out["body"]


def test_list_agents(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("list_agents").fn(populated_buffer, {})
    assert len(out) == 1 and out[0]["model"] == "sonnet"


def test_list_hooks(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("list_hooks").fn(populated_buffer, {})
    assert out[0]["event"] == "PreToolUse"


def test_list_mcp_endpoints_parses_args_json(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("list_mcp_endpoints").fn(populated_buffer, {})
    assert out[0]["name"] == "notion"


def test_list_projects(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("list_projects").fn(populated_buffer, {})
    assert {r["slug"] for r in out} == {"hermes-migration", "dossier-hub"}


def test_get_project_by_slug(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("get_project").fn(populated_buffer, {"slug": "hermes-migration"})
    assert out["name"] == "Hermes Migration"


def test_get_project_by_id(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("get_project").fn(populated_buffer, {"id": "p2"})
    assert out["slug"] == "dossier-hub"


def test_get_project_requires_selector(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    with pytest.raises(ValueError, match="slug.*id"):
        reg.get("get_project").fn(populated_buffer, {})


def test_search_conversations_returns_snippet(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("search_conversations").fn(populated_buffer, {"query": "daemon"})
    assert len(out) >= 1
    hit = out[0]
    assert hit["conversation_id"] == "c1"
    assert hit["conversation_title"] == "building the hub"
    assert "snippet" in hit


def test_get_conversation_returns_ordered_messages(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("get_conversation").fn(populated_buffer, {"id": "c1"})
    assert out["conversation"]["title"] == "building the hub"
    timestamps = [m["timestamp_epoch"] for m in out["messages"]]
    assert timestamps == sorted(timestamps)


def test_get_conversation_missing_returns_none(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    assert reg.get("get_conversation").fn(populated_buffer, {"id": "nope"}) is None


def test_get_stats_counts_are_right(populated_buffer: LocalBuffer):
    reg = build_default_registry()
    out = reg.get("get_stats").fn(populated_buffer, {})
    assert out["mirror_memory_items"] == 5
    assert out["mirror_projects"] == 2
    assert out["mirror_conversations"] == 1
    assert out["mirror_messages"] == 3
    assert out["mirror_skills"] == 2


# ── JSON-RPC parsing ────────────────────────────────────────────────

def test_parse_valid_request():
    req, err = _parse_message('{"jsonrpc":"2.0","id":1,"method":"ping"}')
    assert err is None and req is not None
    assert req.method == "ping" and req.id == 1 and not req.is_notification


def test_parse_notification_has_no_id():
    req, err = _parse_message(
        '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    )
    assert err is None and req.is_notification is True


def test_parse_malformed_json_returns_parse_error():
    req, err = _parse_message('{not json')
    assert req is None and err["error"]["code"] == -32700


def test_parse_missing_jsonrpc_version():
    req, err = _parse_message('{"id":1,"method":"ping"}')
    assert req is None and err["error"]["code"] == -32600


def test_parse_missing_method():
    req, err = _parse_message('{"jsonrpc":"2.0","id":1}')
    assert req is None and err["error"]["code"] == -32600


def test_parse_empty_line_returns_nothing():
    req, err = _parse_message("   \n")
    assert req is None and err is None


# ── Server handle_line ──────────────────────────────────────────────

def test_initialize_returns_protocol_version(server: McpServer):
    resp = server.handle_line(
        '{"jsonrpc":"2.0","id":1,"method":"initialize",'
        '"params":{"clientInfo":{"name":"pytest","version":"1.0"}}}'
    )
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"] == "ccm-hub-mcp"


def test_ping_returns_empty_result(server: McpServer):
    resp = server.handle_line('{"jsonrpc":"2.0","id":7,"method":"ping"}')
    assert resp == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_tools_list_returns_all_tools(server: McpServer):
    resp = server.handle_line('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
    tools = resp["result"]["tools"]
    assert len(tools) == 14
    assert all("inputSchema" in t for t in tools)


def test_tools_call_dispatches_and_wraps_result(server: McpServer):
    resp = server.handle_line(json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "list_skills", "arguments": {}},
    }))
    assert "content" in resp["result"]
    assert resp["result"]["content"][0]["type"] == "text"
    # structuredContent should be the raw list
    assert isinstance(resp["result"]["structuredContent"], list)
    assert {s["name"] for s in resp["result"]["structuredContent"]} == {"browse", "review"}


def test_tools_call_unknown_tool_errors(server: McpServer):
    resp = server.handle_line(json.dumps({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "no_such_tool", "arguments": {}},
    }))
    assert resp["error"]["code"] == -32602


def test_tools_call_missing_required_arg_errors(server: McpServer):
    resp = server.handle_line(json.dumps({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "read_skill", "arguments": {}},
    }))
    assert resp["error"]["code"] == -32602
    assert "name" in resp["error"]["message"]


def test_unknown_method_returns_method_not_found(server: McpServer):
    resp = server.handle_line(
        '{"jsonrpc":"2.0","id":9,"method":"resources/list"}'
    )
    assert resp["error"]["code"] == -32601


def test_notifications_produce_no_response(server: McpServer):
    resp = server.handle_line(
        '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    )
    # handle_line returns the internal dispatch result, but nothing is written
    # to stdout for notifications.
    out = server.stdout.getvalue()
    assert out == ""
    assert resp is None


def test_shutdown_stops_serve_loop(server: McpServer):
    server.handle_line('{"jsonrpc":"2.0","id":99,"method":"shutdown"}')
    assert server._shutdown is True


# ── End-to-end serve() over a StringIO pipe ─────────────────────────

def test_serve_processes_request_stream(populated_buffer: LocalBuffer):
    """Feed a canonical 3-message handshake and assert each reply."""
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"t","version":"1"}}}\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_stats","arguments":{}}}\n'
    )
    stdin = io.StringIO(requests)
    stdout = io.StringIO()
    stderr = io.StringIO()
    server = McpServer(populated_buffer, stdin=stdin, stdout=stdout, stderr=stderr)
    server.serve()

    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    # Expect exactly 3 responses: initialize, tools/list, tools/call
    # (notifications produce no response).
    assert len(lines) == 3
    assert lines[0]["id"] == 1 and "protocolVersion" in lines[0]["result"]
    assert lines[1]["id"] == 2 and len(lines[1]["result"]["tools"]) == 14
    stats = lines[2]["result"]["structuredContent"]
    assert stats["mirror_memory_items"] == 5


# ── CLI wiring: `ccm hub mcp-serve --list` ──────────────────────────

def test_cli_mcp_serve_list_dumps_tools(tmp_path: Path):
    """Exercise the CLI path end-to-end via subprocess."""
    buffer_path = tmp_path / "buffer.db"
    # `init` creates the buffer
    env = _subprocess_env()
    r = subprocess.run(
        [sys.executable, "-m", "claude_code_migration", "hub",
         "--buffer", str(buffer_path), "init"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert r.returncode == 0, r.stderr

    r = subprocess.run(
        [sys.executable, "-m", "claude_code_migration", "hub",
         "--buffer", str(buffer_path), "mcp-serve", "--list"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert r.returncode == 0, r.stderr
    tools = json.loads(r.stdout)
    assert len(tools) == 14
    assert any(t["name"] == "search_memory" for t in tools)


def test_cli_mcp_serve_refuses_missing_buffer(tmp_path: Path):
    missing = tmp_path / "nope.db"
    r = subprocess.run(
        [sys.executable, "-m", "claude_code_migration", "hub",
         "--buffer", str(missing), "mcp-serve", "--list"],
        capture_output=True, text=True, timeout=30, env=_subprocess_env(),
    )
    assert r.returncode == 2
    assert "no buffer" in r.stderr
