"""OpenCode adapter.

OpenCode (sst/opencode) natively reads CLAUDE.md + .claude/skills/ so
migration is mostly writing an opencode.json with providers + MCP.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import Adapter, MigrationResult, ensure_dir, build_universal_agents_md


DEFAULT_MODEL = "bigmodel/glm-5"
DEFAULT_PROVIDER_ID = "bigmodel"


class OpenCodeAdapter(Adapter):
    name = "opencode"

    def apply(self, scan, out_dir, project_dir=None, cowork_export=None):
        r = MigrationResult(target=self.name)
        out_dir = ensure_dir(Path(out_dir))

        # 1. Global opencode.json (in out_dir / .config/opencode/opencode.json for testing;
        #    real path is ~/.config/opencode/opencode.json).
        cfg_dir = ensure_dir(out_dir / ".config" / "opencode")
        cfg_path = cfg_dir / "opencode.json"

        opencode_cfg: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "model": DEFAULT_MODEL,
            "autoupdate": True,
            "provider": {
                DEFAULT_PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "BigModel GLM",
                    "options": {
                        "baseURL": "https://open.bigmodel.cn/api/paas/v4",
                        "apiKey": "{env:GLM_API_KEY}",
                    },
                    "models": {
                        "glm-5": {
                            "name": "GLM-5",
                            "limit": {"context": 128000, "output": 8192},
                        },
                    },
                },
            },
            "mcp": {},
        }
        r.env_vars_needed["GLM_API_KEY"] = "From https://open.bigmodel.cn/ API keys"

        # Merge global MCP servers
        for name, srv in (scan.get("mcp_servers_global") or {}).items():
            key = f"cc-{name}"
            if srv.get("transport") == "http" or srv.get("url"):
                entry: dict[str, Any] = {
                    "type": "remote",
                    "url": srv["url"],
                    "enabled": True,
                }
                hdrs = srv.get("headers") or {}
                if hdrs:
                    # Replace bearer secrets with env interpolation
                    clean: dict[str, str] = {}
                    for k, v in hdrs.items():
                        if "auth" in k.lower() or "bearer" in str(v).lower() or "token" in k.lower():
                            env_var = f"CC_MCP_{name.upper().replace('-', '_')}_TOKEN"
                            clean[k] = "Bearer {env:" + env_var + "}"
                            r.env_vars_needed[env_var] = f"Extracted from {name} mcpServer headers"
                        else:
                            clean[k] = v
                    entry["headers"] = clean
            else:
                cmd = [srv.get("command") or "npx"] + list(srv.get("args") or [])
                entry = {
                    "type": "local",
                    "command": cmd,
                    "enabled": True,
                }
                if srv.get("env"):
                    env_map: dict[str, str] = {}
                    for k in srv["env"].keys():
                        env_map[k] = "{env:" + k + "}"
                        r.env_vars_needed[k] = f"Extracted from {name} mcpServer env"
                    entry["environment"] = env_map
            opencode_cfg["mcp"][key] = entry

        # Merge project-level MCP (if project dir given)
        if project_dir:
            for name, srv in (scan.get("mcp_servers_project") or {}).items():
                key = f"cc-proj-{name}"
                if srv.get("url"):
                    opencode_cfg["mcp"][key] = {
                        "type": "remote", "url": srv["url"],
                        "headers": srv.get("headers") or {}, "enabled": True,
                    }
                else:
                    opencode_cfg["mcp"][key] = {
                        "type": "local",
                        "command": [srv.get("command") or "npx"] + list(srv.get("args") or []),
                        "enabled": True,
                    }

        # Plugin-bundled MCP servers (from ~/.claude/plugins/cache/*/*/*/.mcp.json)
        # This is Cowork plugin data — each installed plugin can bring its own MCP.
        for p in (scan.get("plugins") or []):
            for mname, msrv in (p.get("mcp_servers") or {}).items():
                key = f"cc-plugin-{p['plugin_name']}-{mname}"
                if msrv.get("url"):
                    entry: dict[str, Any] = {
                        "type": "remote", "url": msrv["url"], "enabled": True,
                    }
                    hdrs = msrv.get("headers") or {}
                    if hdrs:
                        clean: dict[str, str] = {}
                        for k, v in hdrs.items():
                            if "auth" in k.lower() or "bearer" in str(v).lower() or "token" in k.lower():
                                env_var = f"CC_PLUGIN_{p['plugin_name'].upper()}_{mname.upper()}_TOKEN"
                                clean[k] = "Bearer {env:" + env_var + "}"
                                r.env_vars_needed[env_var] = f"From plugin {p['plugin_name']} MCP {mname}"
                            else:
                                clean[k] = v
                        entry["headers"] = clean
                    opencode_cfg["mcp"][key] = entry
                elif msrv.get("command"):
                    cmd = [msrv.get("command")] + list(msrv.get("args") or [])
                    opencode_cfg["mcp"][key] = {
                        "type": "local", "command": cmd, "enabled": True,
                    }

        # Write global config
        cfg_path.write_text(json.dumps(opencode_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        r.files_written.append(str(cfg_path))

        # 2. Project AGENTS.md (OpenCode will read CLAUDE.md if AGENTS.md absent,
        #    but writing both makes intent explicit + round-trippable).
        if project_dir:
            agents_path = project_dir / "AGENTS.md"
            if not agents_path.exists() or scan.get("_force_overwrite"):
                agents_path.write_text(
                    build_universal_agents_md(
                        scan,
                        header_note=(
                            "Migrated by claude-code-migration → OpenCode target. "
                            "OpenCode natively reads CLAUDE.md too — both files coexist."
                        ),
                    ),
                    encoding="utf-8",
                )
                r.files_written.append(str(agents_path))

        # 3. Copy skills into ~/.config/opencode/skills/cc-*
        skills_out = ensure_dir(out_dir / ".config" / "opencode" / "skills")
        # Merge global skills + plugin-bundled skills (Cowork)
        all_skills = list(scan.get("skills_global") or []) + list(scan.get("plugins_skills") or [])
        for skill in all_skills:
            # Plugin skills already have "plugin:skill" names — normalize
            raw_name = str(skill.get('name', 'unknown')).replace(':', '-')
            skill_dir = ensure_dir(skills_out / f"cc-{raw_name}")
            skill_md = skill_dir / "SKILL.md"
            # Rebuild frontmatter to match OpenCode requirements
            # OpenCode name regex: ^[a-z0-9]+(-[a-z0-9]+)*$
            safe_name = f"cc-{raw_name}".lower()
            import re as _re
            safe_name = _re.sub(r"[^a-z0-9-]", "-", safe_name)[:64].strip("-")
            fm = {
                "name": safe_name,
                "description": (skill.get("description") or f"Migrated from Claude Code skill {raw_name}")[:1024],
            }
            fm_text = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n\n"
            skill_md.write_text(fm_text + (skill.get("body") or ""), encoding="utf-8")
            r.files_written.append(str(skill_md))

        # 4. Agents → .opencode/agents/ (OpenCode has native custom agents)
        if project_dir:
            agents_dir = ensure_dir(project_dir / ".opencode" / "agents")
            for a in (scan.get("agents") or []):
                name = (a.get("name") or "agent").lower().replace(" ", "-")
                fm_lines = [
                    "---",
                    f"description: {a.get('description','').splitlines()[0] if a.get('description') else 'Migrated from Claude Code agent'}",
                    "mode: subagent",
                ]
                if a.get("model"):
                    fm_lines.append(f"model: {a['model']}")
                fm_lines.append("---")
                agent_path = agents_dir / f"cc-{name}.md"
                agent_path.write_text("\n".join(fm_lines) + "\n\n" + (a.get("instructions") or ""), encoding="utf-8")
                r.files_written.append(str(agent_path))

        # 5. Cowork conversations → OpenCode session export format
        if cowork_export and project_dir:
            sess_dir = ensure_dir(project_dir / ".opencode" / "sessions-imported")
            count = 0
            for conv in cowork_export.get("conversations") or []:
                if count >= 100:  # Cap to avoid runaway writes
                    break
                safe_name = conv["name"][:50].replace("/", "-")
                f = sess_dir / f"{conv['uuid'][:8]}_{safe_name}.md"
                body = [f"# {conv['name']}\n", f"_uuid: {conv['uuid']}_", f"_created: {conv['created_at']}_", ""]
                for m in conv.get("messages") or []:
                    body.append(f"### {m['sender']} — {m['timestamp']}\n\n{m.get('text','')}")
                f.write_text("\n\n".join(body), encoding="utf-8")
                r.files_written.append(str(f))
                count += 1

        r.post_install_hint = (
            "Install OpenCode: https://opencode.ai/docs/installation\n"
            "  Copy out_dir/.config/opencode/ → ~/.config/opencode/\n"
            "  Set env vars: " + ", ".join(r.env_vars_needed.keys()) + "\n"
            "  Run: opencode"
        )
        return r
