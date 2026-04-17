"""Cowork-specific tests.

Cowork's data footprint goes beyond conversations ZIP:
- Plugin inventory at ~/.claude/plugins/
- Plugin-bundled MCP servers (from each plugin's .mcp.json)
- Plugin-bundled skills (from each plugin's skills/)
- Marketplaces at ~/.claude/plugins/marketplaces/
- Org metadata in ~/.claude.json oauthAccount

This suite verifies all of that flows through to each target adapter.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from claude_code_migration import scan_claude_code
from claude_code_migration.adapters import get_adapter


PROJ = Path(os.environ.get(
    "CCM_TEST_PROJECT",
    "/Users/xiaopingfeng/Library/Mobile Documents/iCloud~md~obsidian/Documents/Projects/IdeaToProd",
))


@pytest.fixture(scope="session")
def scan_dict():
    if not PROJ.exists():
        pytest.skip(f"Test project not available: {PROJ}")
    return scan_claude_code(project_dir=PROJ, include_sessions=False).to_dict()


# ──────── Plugin inventory ────────

def test_plugins_captured(scan_dict):
    """~/.claude/plugins/ should yield PluginInstall records."""
    plugins = scan_dict.get("plugins") or []
    if not plugins:
        pytest.skip("No plugins installed")
    # Each record must have structured fields
    for p in plugins:
        assert p["id"], "Plugin id required"
        assert "@" in p["id"], f"Plugin id should be name@marketplace: {p['id']}"
        assert p["plugin_name"]
        assert p["marketplace"]
        assert p["version"]
        assert p["scope"] in ("user", "project", "local")


def test_plugin_bundled_mcp_captured(scan_dict):
    """Plugins bundling .mcp.json should contribute MCP servers."""
    plugins = scan_dict.get("plugins") or []
    total_mcp = sum(len(p.get("mcp_servers") or {}) for p in plugins)
    # If no plugin has MCP, skip (not a test failure)
    if total_mcp == 0:
        pytest.skip("No plugin-bundled MCP servers in test environment")
    # At least one should be http
    has_http = any(
        (srv.get("transport") == "http" or srv.get("url"))
        for p in plugins for srv in (p.get("mcp_servers") or {}).values()
    )
    assert has_http, "Expected at least one HTTP plugin-MCP (e.g. Figma)"


def test_plugin_bundled_skills_captured(scan_dict):
    """Plugins bundling skills/ should contribute skills."""
    plugin_skills = scan_dict.get("plugins_skills") or []
    plugins = scan_dict.get("plugins") or []
    total_declared = sum(len(p.get("skill_names") or []) for p in plugins)
    if total_declared == 0:
        pytest.skip("No plugin-bundled skills in test environment")
    assert len(plugin_skills) >= total_declared, \
        f"plugins_skills list ({len(plugin_skills)}) should match or exceed declared skill_names ({total_declared})"
    # Plugin skills use "plugin:name" naming convention
    for s in plugin_skills:
        assert ":" in s.get("name", ""), f"Plugin skill should be 'plugin:name': {s.get('name')}"


def test_marketplaces_captured(scan_dict):
    """known_marketplaces.json should yield Marketplace records."""
    mps = scan_dict.get("marketplaces") or []
    if not mps:
        pytest.skip("No marketplaces registered")
    for m in mps:
        assert m["name"]
        assert m["source_type"] in ("github", "url", "git-subdir", "npm", "path", "unknown")
        assert m["install_location"]


# ──────── Org metadata ────────

def test_org_metadata_captured(scan_dict):
    """oauthAccount fields should populate OrgMetadata."""
    org = scan_dict.get("org")
    if not org:
        pytest.skip("No ~/.claude.json oauthAccount (not signed in)")
    # Required fields should be present (even if None for non-Cowork)
    for f in ("account_uuid", "organization_uuid", "organization_role",
              "workspace_role", "billing_type", "email_address"):
        assert f in org, f"OrgMetadata missing field: {f}"


# ──────── Adapter propagation ────────

@pytest.mark.parametrize("target", ["hermes", "opencode", "cursor", "windsurf"])
def test_plugin_mcp_propagates_to_target(scan_dict, target, tmp_path):
    """Plugin-bundled MCP should end up in target's MCP config with cc-plugin- prefix."""
    plugins = scan_dict.get("plugins") or []
    total_plugin_mcp = sum(len(p.get("mcp_servers") or {}) for p in plugins)
    if total_plugin_mcp == 0:
        pytest.skip("No plugin-bundled MCP in this environment")

    adapter = get_adapter(target)
    proj = tmp_path / "proj"
    proj.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj)

    # Locate target's MCP config
    if target == "hermes":
        cfg_path = out / ".hermes" / "config.yaml"
        text = cfg_path.read_text()
        # We embed plugin MCPs as "cc-plugin-<plugin>-<mname>" in YAML
        assert "cc-plugin-" in text, \
            f"Hermes config.yaml missing plugin-bundled MCPs:\n{text[:500]}"
    elif target == "opencode":
        cfg_path = out / ".config" / "opencode" / "opencode.json"
        cfg = json.loads(cfg_path.read_text())
        keys = list((cfg.get("mcp") or {}).keys())
        assert any(k.startswith("cc-plugin-") for k in keys), \
            f"OpenCode mcp missing cc-plugin-* keys: {keys}"
    elif target == "cursor":
        mcp_path = proj / ".cursor" / "mcp.json"
        if not mcp_path.exists():
            pytest.skip("No Cursor mcp.json")
        cfg = json.loads(mcp_path.read_text())
        keys = list((cfg.get("mcpServers") or {}).keys())
        assert any(k.startswith("cc-plugin-") for k in keys), \
            f"Cursor mcpServers missing cc-plugin-* keys: {keys}"
    elif target == "windsurf":
        mcp_path = out / ".codeium" / "windsurf" / "mcp_config.json"
        if not mcp_path.exists():
            pytest.skip("No Windsurf mcp_config.json")
        cfg = json.loads(mcp_path.read_text())
        keys = list((cfg.get("mcpServers") or {}).keys())
        assert any(k.startswith("cc-plugin-") for k in keys), \
            f"Windsurf mcpServers missing cc-plugin-* keys: {keys}"


def test_plugin_skills_propagate_to_opencode(scan_dict, tmp_path):
    """OpenCode should receive plugin-bundled skills as cc-<plugin>-<skill>."""
    plugin_skills = scan_dict.get("plugins_skills") or []
    if not plugin_skills:
        pytest.skip("No plugin-bundled skills")
    adapter = get_adapter("opencode")
    proj = tmp_path / "proj"
    proj.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj)

    skills_dir = out / ".config" / "opencode" / "skills"
    assert skills_dir.exists()
    all_skill_dirs = list(skills_dir.glob("cc-*"))
    # Plugin skills have names like "frontend-design:frontend-design" →
    # sanitized to "cc-frontend-design-frontend-design"
    # Check at least one cc- directory was created from a plugin skill
    assert len(all_skill_dirs) >= len(plugin_skills), \
        f"OpenCode skills dir has {len(all_skill_dirs)} entries, expected >= {len(plugin_skills)}"


def test_cowork_org_info_in_agents_md(scan_dict, tmp_path):
    """If org metadata present, AGENTS.md should include a 'Cowork Organization' section."""
    org = scan_dict.get("org") or {}
    if not org.get("organization_name"):
        pytest.skip("No org name in test account")
    adapter = get_adapter("opencode")
    proj = tmp_path / "proj"
    proj.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj)

    agents_md = proj / "AGENTS.md"
    if not agents_md.exists():
        pytest.skip("AGENTS.md not generated (e.g. already existed)")
    text = agents_md.read_text()
    assert "Cowork Organization" in text or "Organization" in text, \
        "AGENTS.md missing Cowork org section"


def test_installed_plugins_listed_in_agents_md(scan_dict, tmp_path):
    """Installed plugins should appear in AGENTS.md inventory."""
    plugins = scan_dict.get("plugins") or []
    if not plugins:
        pytest.skip("No plugins installed")
    adapter = get_adapter("opencode")
    proj = tmp_path / "proj"
    proj.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj)
    agents_md = proj / "AGENTS.md"
    if not agents_md.exists():
        pytest.skip("AGENTS.md not generated")
    text = agents_md.read_text()
    # At least one plugin name should appear
    assert any(p["plugin_name"] in text for p in plugins), \
        f"AGENTS.md missing plugin inventory:\n{text[:1000]}"
