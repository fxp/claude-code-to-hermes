"""Cursor adapter.

Cursor uses:
- .cursor/rules/*.mdc (Markdown with YAML frontmatter)
- .cursor/mcp.json (global at ~/.cursor/mcp.json, project at .cursor/mcp.json)
- User Rules live in Settings DB — can't automate, generate paste-ready file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import (Adapter, MigrationResult, ensure_dir,
                   build_universal_agents_md, render_cowork_project_markdown,
                   safe_slug, write_archive)


class CursorAdapter(Adapter):
    name = "cursor"

    def apply(self, scan, out_dir, project_dir=None, cowork_export=None):
        r = MigrationResult(target=self.name)
        out_dir = ensure_dir(Path(out_dir))

        target_root = project_dir if project_dir else out_dir

        # 1. .cursor/rules/ — main rule file from CLAUDE.md + memory
        rules_dir = ensure_dir(target_root / ".cursor" / "rules")
        main_body = scan.get("claude_md") or build_universal_agents_md(scan)
        main_rule = (
            "---\n"
            "description: Project main guidelines (migrated from Claude Code)\n"
            "alwaysApply: true\n"
            "---\n\n"
            + main_body
        )
        (rules_dir / "cc-main.mdc").write_text(main_rule, encoding="utf-8")
        r.files_written.append(str(rules_dir / "cc-main.mdc"))

        # 2. User profile → alwaysApply rule
        if scan.get("home_claude_md"):
            user_rule = (
                "---\n"
                "description: User preferences (from ~/.claude/CLAUDE.md)\n"
                "alwaysApply: true\n"
                "---\n\n"
                + scan["home_claude_md"]
            )
            (rules_dir / "cc-user.mdc").write_text(user_rule, encoding="utf-8")
            r.files_written.append(str(rules_dir / "cc-user.mdc"))

        # 3. Feedback memory → optional rules
        for m in (scan.get("memory") or []):
            if m.get("type") == "feedback":
                name = (m.get("file") or "mem").replace(".md", "")
                rule = (
                    "---\n"
                    f"description: {m.get('frontmatter', {}).get('name') or name}\n"
                    "alwaysApply: false\n"
                    'globs: "**/*"\n'
                    "---\n\n"
                    + (m.get("content") or "")
                )
                path = rules_dir / f"cc-{name}.mdc"
                path.write_text(rule, encoding="utf-8")
                r.files_written.append(str(path))

        # 4. Agents → manual-invoke rules (Cursor doesn't have first-class agents)
        for a in (scan.get("agents") or []):
            name = (a.get("name") or "agent").lower().replace(" ", "-")
            rule = (
                "---\n"
                f"description: {a.get('description','').splitlines()[0] if a.get('description') else 'Migrated agent'}\n"
                "---\n\n"
                f"# {a.get('name')}\n\n"
                + (a.get("instructions") or "")
            )
            path = rules_dir / f"cc-agent-{name}.mdc"
            path.write_text(rule, encoding="utf-8")
            r.files_written.append(str(path))

        # 5. .cursor/mcp.json — merge global + project-level MCP
        servers: dict[str, Any] = {}

        def _convert(name: str, srv: dict[str, Any], prefix: str) -> None:
            if srv.get("url"):
                cfg: dict[str, Any] = {"url": srv["url"]}
                if srv.get("headers"):
                    clean: dict[str, str] = {}
                    for k, v in srv["headers"].items():
                        if "auth" in k.lower() or "token" in k.lower():
                            env_var = f"CC_MCP_{name.upper().replace('-', '_')}_TOKEN"
                            clean[k] = "Bearer ${env:" + env_var + "}"
                            r.env_vars_needed[env_var] = f"From {name} mcpServer"
                        else:
                            clean[k] = v
                    cfg["headers"] = clean
            else:
                cfg = {
                    "type": "stdio",
                    "command": srv.get("command") or "npx",
                    "args": list(srv.get("args") or []),
                }
                if srv.get("env"):
                    cfg["env"] = {k: "${env:" + k + "}" for k in srv["env"]}
                    for k in srv["env"]:
                        r.env_vars_needed[k] = f"From {name} mcpServer env"
            servers[f"{prefix}{name}"] = cfg

        for name, srv in (scan.get("mcp_servers_global") or {}).items():
            _convert(name, srv, "cc-")
        for name, srv in (scan.get("mcp_servers_project") or {}).items():
            _convert(name, srv, "cc-proj-")
        # Plugin-bundled MCPs (Cowork plugin system)
        for p in (scan.get("plugins") or []):
            for mname, msrv in (p.get("mcp_servers") or {}).items():
                _convert(f"{p['plugin_name']}-{mname}", msrv, "cc-plugin-")

        if servers:
            mcp_path = target_root / ".cursor" / "mcp.json"
            mcp_path.write_text(json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False), encoding="utf-8")
            r.files_written.append(str(mcp_path))

        # 6. User Rules paste-ready file (can't automate Cursor Settings)
        if scan.get("home_claude_md"):
            ur_dir = ensure_dir(out_dir / ".migration")
            (ur_dir / "cursor-user-rules.md").write_text(
                "# Cursor User Rules — paste into Settings → Rules → User Rules\n\n"
                "(Cursor User Rules are stored in Settings DB and can't be written to disk.)\n\n"
                "---\n\n"
                + scan["home_claude_md"],
                encoding="utf-8",
            )
            r.files_written.append(str(ur_dir / "cursor-user-rules.md"))
            r.warnings.append("User Rules: copy from .migration/cursor-user-rules.md into Cursor Settings")

        # 6b. Cowork Projects → .cursor/rules/cowork-project-<slug>.mdc
        #     (each Cowork Project becomes a manual-invoke rule)
        if cowork_export and (cowork_export.get("projects") or []):
            for cproj in cowork_export["projects"]:
                slug = safe_slug(cproj.get("name", "project"))
                rule = (
                    "---\n"
                    f"description: Cowork project · {cproj.get('name', slug)}\n"
                    "alwaysApply: false\n"
                    "---\n\n"
                    + render_cowork_project_markdown(cproj, include_docs=True)
                )
                path = rules_dir / f"cowork-project-{slug}.mdc"
                path.write_text(rule, encoding="utf-8")
                r.files_written.append(str(path))

        # 6c. Scheduled tasks preserved as manual-invoke rules
        sched = scan.get("scheduled_tasks") or []
        if sched:
            for st in sched:
                slug = safe_slug(st["name"])
                rule = (
                    "---\n"
                    f"description: Scheduled task · {st['name']}\n"
                    "alwaysApply: false\n"
                    "---\n\n"
                    f"# {st['name']}\n\n"
                    + (st.get("body") or "")
                )
                path = rules_dir / f"scheduled-{slug}.mdc"
                path.write_text(rule, encoding="utf-8")
                r.files_written.append(str(path))
            r.warnings.append(
                f"{len(sched)} scheduled tasks preserved as manual rules (Cursor has no cron)"
            )

        # 7. Cowork conversations: archive to .migration/conversations (not Cursor-readable, just preserved)
        if cowork_export:
            arch = ensure_dir(out_dir / ".migration" / "conversations")
            for conv in (cowork_export.get("conversations") or [])[:50]:  # cap
                f = arch / f"{conv['uuid'][:8]}.md"
                lines = [f"# {conv['name']}\n"]
                for m in conv.get("messages") or []:
                    lines.append(f"## {m['sender']} — {m['timestamp']}\n\n{m.get('text','')}")
                f.write_text("\n\n".join(lines), encoding="utf-8")
                r.files_written.append(str(f))
            r.warnings.append("Cursor has no session-resume feature; conversations archived to markdown")

        # 8. Archive: unmigrateable raw data
        archive_files = write_archive(out_dir, scan, cowork_export)
        r.files_written.extend(archive_files)

        r.post_install_hint = (
            "Cursor setup:\n"
            "  1. Rules are auto-loaded from .cursor/rules/\n"
            "  2. Copy cc-user.mdc contents → Settings → Rules → User Rules\n"
            "  3. Set env vars: " + (", ".join(r.env_vars_needed.keys()) or "(none)") + "\n"
            "  4. Restart Cursor"
        )
        return r
