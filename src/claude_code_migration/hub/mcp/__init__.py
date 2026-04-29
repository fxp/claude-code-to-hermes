"""MCP surface for hub-mode — serves the L4 mirror as tools.

Any MCP-capable agent (Claude Code, Cursor, Codex CLI, OpenCode, Windsurf…)
can point at `ccm hub mcp-serve` and query the user's Workspace Dossier:
memories, skills, agents, conversations, projects, hooks, MCP endpoints.

Design rules:

  • Read-only. MCP tools never mutate state directly — writes must go
    through a capture (fsnotify, browser extension, etc.) so every change
    flows through the redactor and is replicated via the outbox.
  • Offline-first. All tools read from L4 mirror tables; no network hop.
  • No secrets. The vault (dossier_vault_entries) is never mirrored
    locally, so MCP tools literally can't leak it.

Transport: JSON-RPC 2.0 over stdio with line-delimited framing. This is
the protocol every major MCP client speaks. HTTP transport is intentionally
not shipped here — a user who wants HTTP can front the stdio server with
an adapter of their choice.

Public surface::

    from claude_code_migration.hub.mcp import McpServer, ToolRegistry
    from claude_code_migration.hub.mcp.tools import build_default_registry

CLI::

    ccm hub mcp-serve             # stdio server, reads L4 buffer
    ccm hub mcp-serve --list      # list available tools, exit
"""
from __future__ import annotations

from .server import McpServer
from .tools import Tool, ToolRegistry, build_default_registry

__all__ = [
    "McpServer",
    "Tool",
    "ToolRegistry",
    "build_default_registry",
]
