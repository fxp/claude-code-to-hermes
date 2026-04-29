"""Tool registry — what the L4 mirror exposes as MCP tools.

Every tool takes a ``LocalBuffer`` and a JSON ``arguments`` dict, and returns
a JSON-serializable result. Tools never touch the network: all reads go
through ``buffer.mirror_*`` helpers, so the whole surface keeps working
offline.

Adding a tool is deliberately low-ceremony: write a function with
signature ``fn(buffer, arguments) -> dict | list``, then register it
with ``@register`` or pass it to ``ToolRegistry.add``.

Tool list (v1):

    ── memory ────
    search_memory          FTS over memory items (notes, rules, styles, profile)
    read_profile           user_profile kind memory items
    list_memory            enumerate memory items (optionally filter by kind)
    read_memory            full content of one memory item by id

    ── skills / agents / hooks / mcp ────
    list_skills            all known skills
    read_skill             one skill body + frontmatter
    list_agents            all subagents
    list_hooks             all hooks
    list_mcp_endpoints     configured MCP servers

    ── projects / conversations ────
    list_projects          enumerate project cards
    get_project            one project by slug or id
    search_conversations   FTS over messages — returns matching messages
    get_conversation       messages of one conversation, ordered

    ── introspection ────
    get_stats              buffer + mirror counters
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..buffer import LocalBuffer

# ── Types ────────────────────────────────────────────────────────────

ToolFn = Callable[[LocalBuffer, dict[str, Any]], Any]


@dataclass
class Tool:
    """One MCP tool: name, description, JSON-schema input, implementation."""
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn

    def to_mcp(self) -> dict[str, Any]:
        """Serialize for `tools/list`."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolRegistry:
    """Dict-like registry of Tools, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def to_mcp_list(self) -> list[dict[str, Any]]:
        return [t.to_mcp() for t in self._tools.values()]


# ── Helpers ──────────────────────────────────────────────────────────

def _row_to_dict(row: Any) -> dict[str, Any]:
    """Normalize sqlite3.Row → plain dict, parse JSON-ish columns."""
    d = dict(row) if not isinstance(row, dict) else dict(row)
    for k in ("frontmatter", "content_blocks", "tools", "args", "env", "headers"):
        v = d.get(k)
        if isinstance(v, str) and v:
            try:
                d[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def _clamp_limit(raw: Any, default: int = 20, maximum: int = 200) -> int:
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, maximum)


def _require_str(args: dict[str, Any], key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"missing required argument: {key}")
    return v.strip()


# ── Tool implementations ─────────────────────────────────────────────

def _search_memory(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = _require_str(args, "query")
    kind = args.get("kind")
    limit = _clamp_limit(args.get("limit"), default=20, maximum=100)
    rows = buf.mirror_search_memory(query, kind=kind, limit=limit)
    return [_row_to_dict(r) for r in rows]


def _read_profile(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    category = args.get("category")
    rows = buf.mirror_read_profile(category)
    return [_row_to_dict(r) for r in rows]


def _list_memory(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    kind = args.get("kind")
    limit = _clamp_limit(args.get("limit"), default=50, maximum=500)
    if kind:
        rows = buf._conn.execute(
            "select id, kind, name, source_platform, updated_at "
            "from mirror_memory_items where kind = ? "
            "order by updated_at desc limit ?",
            (kind, limit),
        ).fetchall()
    else:
        rows = buf._conn.execute(
            "select id, kind, name, source_platform, updated_at "
            "from mirror_memory_items order by updated_at desc limit ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _read_memory(buf: LocalBuffer, args: dict[str, Any]) -> dict[str, Any] | None:
    mid = _require_str(args, "id")
    row = buf._conn.execute(
        "select * from mirror_memory_items where id = ?", (mid,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def _list_skills(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [_row_to_dict(r) for r in buf.mirror_list_skills()]


def _read_skill(buf: LocalBuffer, args: dict[str, Any]) -> dict[str, Any] | None:
    name = _require_str(args, "name")
    row = buf.mirror_read_skill(name)
    return _row_to_dict(row) if row else None


def _list_agents(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    rows = buf._conn.execute(
        "select id, name, description, model from mirror_agents order by name"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _list_hooks(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    rows = buf._conn.execute(
        "select id, event, matcher, type, command, scope from mirror_hooks order by event, matcher"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _list_mcp_endpoints(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    rows = buf._conn.execute(
        "select id, name, scope, transport, url, command, plugin_owner "
        "from mirror_mcp_endpoints order by scope, name"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _list_projects(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    rows = buf._conn.execute(
        "select id, slug, name, description, is_shared, updated_at "
        "from mirror_projects order by updated_at desc"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_project(buf: LocalBuffer, args: dict[str, Any]) -> dict[str, Any] | None:
    slug = args.get("slug")
    pid = args.get("id")
    if slug:
        row = buf._conn.execute(
            "select * from mirror_projects where slug = ?", (slug,)
        ).fetchone()
    elif pid:
        row = buf._conn.execute(
            "select * from mirror_projects where id = ?", (pid,)
        ).fetchone()
    else:
        raise ValueError("provide either `slug` or `id`")
    return _row_to_dict(row) if row else None


def _search_conversations(buf: LocalBuffer, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = _require_str(args, "query")
    limit = _clamp_limit(args.get("limit"), default=20, maximum=100)
    rows = buf._conn.execute(
        """
        select m.id, m.conversation_id, m.role, m.timestamp_epoch,
               c.title as conversation_title, c.platform,
               snippet(mirror_messages_fts, 1, '<b>', '</b>', '...', 16) as snippet
          from mirror_messages_fts f
          join mirror_messages m on m.id = f.id
          left join mirror_conversations c on c.id = m.conversation_id
         where mirror_messages_fts match ?
         order by rank
         limit ?
        """,
        (query, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_conversation(buf: LocalBuffer, args: dict[str, Any]) -> dict[str, Any] | None:
    cid = _require_str(args, "id")
    conv_row = buf._conn.execute(
        "select * from mirror_conversations where id = ?", (cid,)
    ).fetchone()
    if not conv_row:
        return None
    limit = _clamp_limit(args.get("limit"), default=200, maximum=1000)
    msg_rows = buf._conn.execute(
        "select id, role, content_text, thinking, timestamp_epoch "
        "from mirror_messages where conversation_id = ? "
        "order by timestamp_epoch asc nulls last, id asc limit ?",
        (cid, limit),
    ).fetchall()
    return {
        "conversation": _row_to_dict(conv_row),
        "messages": [_row_to_dict(m) for m in msg_rows],
    }


def _get_stats(buf: LocalBuffer, args: dict[str, Any]) -> dict[str, Any]:
    def _count(table: str) -> int:
        row = buf._conn.execute(f"select count(*) as n from {table}").fetchone()
        return int(row["n"]) if row else 0

    return {
        "outbox":          buf.outbox_size(),
        "dead_letter":     buf.dead_letter_count(),
        "mirror_identity":      _count("mirror_identity"),
        "mirror_memory_items":  _count("mirror_memory_items"),
        "mirror_projects":      _count("mirror_projects"),
        "mirror_conversations": _count("mirror_conversations"),
        "mirror_messages":      _count("mirror_messages"),
        "mirror_skills":        _count("mirror_skills"),
        "mirror_agents":        _count("mirror_agents"),
        "mirror_hooks":         _count("mirror_hooks"),
        "mirror_mcp_endpoints": _count("mirror_mcp_endpoints"),
        "last_mirror_sync_epoch": int(buf.get_state("last_mirror_sync_epoch", "0") or 0),
    }


# ── Registry factory ─────────────────────────────────────────────────

def build_default_registry() -> ToolRegistry:
    """Assemble the canonical v1 tool set.

    Keep the descriptions agent-readable: one line, verb-first, no
    implementation detail. Agents pick tools by their description.
    """
    reg = ToolRegistry()

    reg.add(Tool(
        name="search_memory",
        description=(
            "Full-text search over the user's memory items (CLAUDE.md notes, "
            "project memory, rules, output-styles, user profile). Ranked by relevance."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "FTS5 query string."},
                "kind":  {"type": "string",
                          "description": "Optional filter: user_profile|project_memory|scratch|rule|output_style|agent_memory"},
                "limit": {"type": "integer", "default": 20, "maximum": 100},
            },
            "required": ["query"],
        },
        fn=_search_memory,
    ))

    reg.add(Tool(
        name="read_profile",
        description=(
            "Return the user's profile memory items (from ~/.claude/CLAUDE.md and "
            "similar). Pass `category` to filter to one category like 'style' or 'stack'."
        ),
        input_schema={
            "type": "object",
            "properties": {"category": {"type": "string"}},
        },
        fn=_read_profile,
    ))

    reg.add(Tool(
        name="list_memory",
        description="List memory items (id, kind, name, updated_at). Optionally filter by kind.",
        input_schema={
            "type": "object",
            "properties": {
                "kind":  {"type": "string"},
                "limit": {"type": "integer", "default": 50, "maximum": 500},
            },
        },
        fn=_list_memory,
    ))

    reg.add(Tool(
        name="read_memory",
        description="Return the full content + frontmatter of one memory item by id.",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        fn=_read_memory,
    ))

    reg.add(Tool(
        name="list_skills",
        description="List all Claude Code skills (id, name, description, source_plugin).",
        input_schema={"type": "object", "properties": {}},
        fn=_list_skills,
    ))

    reg.add(Tool(
        name="read_skill",
        description="Return the full body + frontmatter of one skill by name.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        fn=_read_skill,
    ))

    reg.add(Tool(
        name="list_agents",
        description="List configured subagents (id, name, description, model).",
        input_schema={"type": "object", "properties": {}},
        fn=_list_agents,
    ))

    reg.add(Tool(
        name="list_hooks",
        description="List Claude Code hooks (event, matcher, type, command, scope).",
        input_schema={"type": "object", "properties": {}},
        fn=_list_hooks,
    ))

    reg.add(Tool(
        name="list_mcp_endpoints",
        description="List configured MCP endpoints (scope, transport, url/command, plugin_owner).",
        input_schema={"type": "object", "properties": {}},
        fn=_list_mcp_endpoints,
    ))

    reg.add(Tool(
        name="list_projects",
        description="List project cards (id, slug, name, description, is_shared, updated_at).",
        input_schema={"type": "object", "properties": {}},
        fn=_list_projects,
    ))

    reg.add(Tool(
        name="get_project",
        description="Return one project by `slug` or `id`, including context and prompt template.",
        input_schema={
            "type": "object",
            "properties": {"slug": {"type": "string"}, "id": {"type": "string"}},
        },
        fn=_get_project,
    ))

    reg.add(Tool(
        name="search_conversations",
        description=(
            "Full-text search over conversation messages. Returns matching messages "
            "with a highlight snippet, plus the parent conversation's title and platform."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20, "maximum": 100},
            },
            "required": ["query"],
        },
        fn=_search_conversations,
    ))

    reg.add(Tool(
        name="get_conversation",
        description="Return one conversation's metadata + ordered messages by id.",
        input_schema={
            "type": "object",
            "properties": {
                "id":    {"type": "string"},
                "limit": {"type": "integer", "default": 200, "maximum": 1000},
            },
            "required": ["id"],
        },
        fn=_get_conversation,
    ))

    reg.add(Tool(
        name="get_stats",
        description="Return L4 buffer + mirror counters (row counts per table, outbox size, water-mark).",
        input_schema={"type": "object", "properties": {}},
        fn=_get_stats,
    ))

    return reg


__all__ = [
    "Tool",
    "ToolRegistry",
    "build_default_registry",
]
