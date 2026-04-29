"""MCP server — JSON-RPC 2.0 over line-delimited JSON on stdio.

Implements the subset of the MCP protocol that agents actually call:

    initialize              handshake — returns server info + capabilities
    notifications/initialized  (client → server notification, ignored)
    tools/list              enumerate tools
    tools/call              invoke one tool
    ping                    keepalive
    shutdown                graceful close (some clients send this)

Framing: one JSON object per line. This is the default transport that
Claude Code, Codex CLI, and most SDKs speak. Content-Length / HTTP
transports are deliberately out of scope here — a user who wants those
can front the stdio server with an adapter.

The server is single-threaded (captures + drain live in the daemon, not
here). MCP requests are synchronous by design: read → dispatch → write.

Errors follow JSON-RPC 2.0:
  -32700 Parse error
  -32600 Invalid Request
  -32601 Method not found
  -32602 Invalid params
  -32603 Internal error
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable, IO

from ..buffer import LocalBuffer
from .tools import ToolRegistry, build_default_registry


# Protocol version we advertise in `initialize`. Real MCP clients also
# accept older versions; the string is informational.
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ccm-hub-mcp"


@dataclass
class _Request:
    id: Any
    method: str
    params: dict[str, Any]
    is_notification: bool


def _parse_message(raw: str) -> tuple[_Request | None, dict[str, Any] | None]:
    """Parse one line into (Request, None) or (None, error_response)."""
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, _error(None, -32700, f"Parse error: {e}")
    if not isinstance(msg, dict):
        return None, _error(None, -32600, "Invalid Request: not an object")
    if msg.get("jsonrpc") != "2.0":
        return None, _error(msg.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")
    method = msg.get("method")
    if not isinstance(method, str):
        return None, _error(msg.get("id"), -32600, "Invalid Request: missing method")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return None, _error(msg.get("id"), -32602, "Invalid params: must be object")
    return _Request(
        id=msg.get("id"),
        method=method,
        params=params,
        is_notification="id" not in msg,
    ), None


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str,
           data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ── Server ───────────────────────────────────────────────────────────

class McpServer:
    """Stdio MCP server backed by a LocalBuffer.

    Usage:

        buf = LocalBuffer("~/.dossier-hub/buffer.db")
        server = McpServer(buf)
        server.serve()    # blocks until stdin EOF or shutdown
    """

    def __init__(
        self,
        buffer: LocalBuffer,
        *,
        registry: ToolRegistry | None = None,
        stdin: IO[str] | None = None,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
    ) -> None:
        self.buffer = buffer
        self.registry = registry or build_default_registry()
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self._initialized = False
        self._shutdown = False
        # Dispatch table — keyed by MCP method name.
        self._methods: dict[str, Callable[[_Request], Any]] = {
            "initialize":                self._on_initialize,
            "ping":                      self._on_ping,
            "tools/list":                self._on_tools_list,
            "tools/call":                self._on_tools_call,
            "shutdown":                  self._on_shutdown,
            # Notifications (no response expected):
            "notifications/initialized": self._on_noop,
            "notifications/cancelled":   self._on_noop,
            "exit":                      self._on_exit,
        }

    # ── Public API ──────────────────────────────────────────────

    def serve(self) -> None:
        """Read-dispatch-write loop. Returns on stdin EOF or `exit`."""
        self._log(f"mcp server ready · tools={len(self.registry)}")
        while not self._shutdown:
            line = self.stdin.readline()
            if not line:          # EOF
                break
            self.handle_line(line)

    def handle_line(self, raw: str) -> dict[str, Any] | None:
        """Parse one line, dispatch, and write back any response.

        Exposed for tests: callers can drive the server without stdio.
        Returns the response dict (or None for notifications).
        """
        req, err = _parse_message(raw)
        if err is not None:
            self._write(err)
            return err
        if req is None:
            return None
        resp = self._dispatch(req)
        if resp is not None and not req.is_notification:
            self._write(resp)
        return resp

    # ── Dispatch ────────────────────────────────────────────────

    def _dispatch(self, req: _Request) -> dict[str, Any] | None:
        handler = self._methods.get(req.method)
        if handler is None:
            if req.is_notification:
                return None
            return _error(req.id, -32601, f"Method not found: {req.method}")
        try:
            result = handler(req)
        except ValueError as e:
            return _error(req.id, -32602, f"Invalid params: {e}")
        except Exception as e:
            self._log(f"internal error in {req.method}: {e}\n{traceback.format_exc()}")
            return _error(req.id, -32603, f"Internal error: {e}")
        # Notifications expect no response
        if req.is_notification:
            return None
        return _result(req.id, result)

    # ── Method handlers ────────────────────────────────────────

    def _on_initialize(self, req: _Request) -> dict[str, Any]:
        self._initialized = True
        client_info = req.params.get("clientInfo") or {}
        self._log(f"initialize · client={client_info.get('name', '?')}/"
                  f"{client_info.get('version', '?')}")
        try:
            from ... import __version__ as pkg_version
        except ImportError:
            pkg_version = "unknown"
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools":     {"listChanged": False},
                # No resources/prompts yet — advertise nothing so clients don't ask.
            },
            "serverInfo": {
                "name":    SERVER_NAME,
                "version": pkg_version,
            },
            "instructions": (
                "This is ccm hub — a read-only surface over the user's Workspace "
                "Dossier (memories, skills, agents, conversations, projects). All "
                "tools read from a local SQLite mirror; no network calls, no writes. "
                "Writes happen through captures (fsnotify, browser extension)."
            ),
        }

    def _on_ping(self, req: _Request) -> dict[str, Any]:
        return {}

    def _on_tools_list(self, req: _Request) -> dict[str, Any]:
        return {"tools": self.registry.to_mcp_list()}

    def _on_tools_call(self, req: _Request) -> dict[str, Any]:
        name = req.params.get("name")
        if not isinstance(name, str):
            raise ValueError("`name` must be a string")
        tool = self.registry.get(name)
        if tool is None:
            raise ValueError(f"unknown tool: {name}")
        arguments = req.params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("`arguments` must be an object")
        result = tool.fn(self.buffer, arguments)
        # MCP tool-call result convention: wrap JSON in a single `content`
        # block of type "text" with the serialized payload. Clients parse
        # that back. We also include the raw object under `structuredContent`
        # for clients that support it (newer SDKs do).
        text = json.dumps(result, ensure_ascii=False, default=str, indent=2)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": result,
            "isError": False,
        }

    def _on_shutdown(self, req: _Request) -> None:
        self._shutdown = True
        return None

    def _on_exit(self, req: _Request) -> None:
        self._shutdown = True
        return None

    def _on_noop(self, req: _Request) -> None:
        return None

    # ── I/O ─────────────────────────────────────────────────────

    def _write(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=str)
        self.stdout.write(line + "\n")
        self.stdout.flush()

    def _log(self, msg: str) -> None:
        print(f"[mcp] {msg}", file=self.stderr, flush=True)


__all__ = ["McpServer", "PROTOCOL_VERSION", "SERVER_NAME"]
