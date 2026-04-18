"""Tests Cowork Projects + Scheduled Tasks + Archive propagation.

Uses a synthetic Cowork ZIP (built in memory) since we can't ship real
team-workspace data. Verifies each adapter produces Project-shaped output
and the _archive/ contains the raw unmigratable artifacts.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from claude_code_migration import scan_claude_code, parse_cowork_zip
from claude_code_migration.adapters import get_adapter


PROJ = Path(os.environ.get(
    "CCM_TEST_PROJECT",
    "/Users/xiaopingfeng/Library/Mobile Documents/iCloud~md~obsidian/Documents/Projects/IdeaToProd",
))


def _build_synthetic_cowork_zip(path: Path) -> None:
    """Build a minimal Cowork-flavored export ZIP."""
    convs = [
        {
            "uuid": "conv-1",
            "name": "Research Q2 roadmap",
            "workspace_id": "ws-alpha",
            "account": {"uuid": "user-alice"},
            "project_uuid": "proj-vision",
            "created_at": "2026-02-01T10:00:00Z",
            "updated_at": "2026-02-01T11:00:00Z",
            "model": "claude-sonnet-4-5",
            "chat_messages": [
                {"uuid": "m1", "sender": "human",
                 "content": [{"type": "text", "text": "What's our Q2 priority?"}],
                 "created_at": "2026-02-01T10:00:00Z", "attachments": [], "files_v2": []},
                {"uuid": "m2", "sender": "assistant",
                 "content": [{"type": "text", "text": "Based on the vision doc, it's platform expansion."}],
                 "created_at": "2026-02-01T10:01:00Z", "attachments": [], "files_v2": []},
            ],
        },
    ]
    projs = [
        {
            "uuid": "proj-vision",
            "name": "Vision 2026",
            "description": "Strategic planning space for the year",
            "prompt_template": "You are a strategy advisor. Respond concisely with bullet points.",
            "created_at": "2026-01-15T00:00:00Z",
            "is_shared": True,
            "docs": [
                {"filename": "north-star.md",
                 "content": "# North Star\n\nWe aim to become the de-facto platform for knowledge work."},
                {"filename": "metrics.md",
                 "content": "# Metrics\n\n- MRR growth 20% QoQ\n- NPS > 50"},
            ],
        },
        {
            "uuid": "proj-ops",
            "name": "Operations",
            "description": "Runbooks + on-call",
            "prompt_template": "",
            "created_at": "2026-01-20T00:00:00Z",
            "is_shared": False,
            "docs": [],
        },
    ]
    users = [
        {"uuid": "user-alice", "full_name": "Alice A", "email_address": "alice@example.com"},
        {"uuid": "user-bob", "full_name": "Bob B", "email_address": "bob@example.com"},
    ]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("conversations.json", json.dumps(convs))
        z.writestr("projects.json", json.dumps(projs))
        z.writestr("users.json", json.dumps(users))


@pytest.fixture(scope="session")
def synthetic_cowork_zip(tmp_path_factory):
    p = tmp_path_factory.mktemp("cowork-synth") / "export.zip"
    _build_synthetic_cowork_zip(p)
    return p


@pytest.fixture(scope="session")
def scan_dict():
    if not PROJ.exists():
        pytest.skip(f"Test project not available: {PROJ}")
    return scan_claude_code(project_dir=PROJ, include_sessions=False).to_dict()


@pytest.fixture(scope="session")
def cowork_dict(synthetic_cowork_zip):
    return parse_cowork_zip(synthetic_cowork_zip).to_dict()


# ─────────── Parser tests ───────────

def test_parse_cowork_zip_shape(cowork_dict):
    """Parser extracts Cowork source + projects + workspace_ids."""
    assert cowork_dict["source"] == "cowork"  # workspace_id present
    assert cowork_dict["workspace_ids"] == ["ws-alpha"]
    assert len(cowork_dict["projects"]) == 2
    assert len(cowork_dict["conversations"]) == 1


def test_project_retains_prompt_template_and_docs(cowork_dict):
    """prompt_template and docs must be preserved through parsing."""
    p = next(p for p in cowork_dict["projects"] if p["name"] == "Vision 2026")
    assert "strategy advisor" in p["prompt_template"]
    assert len(p["docs"]) == 2
    assert any("north-star" in d["filename"] for d in p["docs"])
    assert p["is_shared"] is True


# ─────────── Adapter propagation tests ───────────

def test_hermes_cowork_projects_propagate(scan_dict, cowork_dict, tmp_path):
    adapter = get_adapter("hermes")
    proj = tmp_path / "proj"; proj.mkdir()
    out = tmp_path / "out"; out.mkdir()
    result = adapter.apply(scan_dict, out, project_dir=proj, cowork_export=cowork_dict)

    # Each project should become a memories/projects/<slug>/context.md
    projs_dir = out / ".hermes" / "memories" / "projects"
    assert projs_dir.exists()
    project_dirs = list(projs_dir.iterdir())
    assert len(project_dirs) == 2
    # Prompt template must appear in context
    vision_ctx = next(d for d in project_dirs if "Vision" in d.name).joinpath("context.md").read_text()
    assert "strategy advisor" in vision_ctx
    assert "north-star" in vision_ctx  # docs inlined


def test_opencode_cowork_projects_propagate(scan_dict, cowork_dict, tmp_path):
    adapter = get_adapter("opencode")
    proj = tmp_path / "proj"; proj.mkdir()
    out = tmp_path / "out"; out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj, cowork_export=cowork_dict)

    projs_dir = proj / ".opencode" / "projects"
    assert projs_dir.exists()
    # Each project should have AGENTS.md + docs/ subfolder
    vision = next(d for d in projs_dir.iterdir() if "Vision" in d.name)
    assert (vision / "AGENTS.md").exists()
    # Docs should be extracted as separate files
    docs = list((vision / "docs").glob("*.md"))
    assert len(docs) == 2


def test_cursor_cowork_projects_become_rules(scan_dict, cowork_dict, tmp_path):
    adapter = get_adapter("cursor")
    proj = tmp_path / "proj"; proj.mkdir()
    out = tmp_path / "out"; out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj, cowork_export=cowork_dict)

    rules = list((proj / ".cursor" / "rules").glob("cowork-project-*.mdc"))
    assert len(rules) == 2
    vision_rule = next(r for r in rules if "vision" in r.name.lower() or "Vision" in r.name)
    text = vision_rule.read_text()
    assert text.startswith("---\n")
    assert "Cowork project" in text.split("---\n")[1]
    assert "strategy advisor" in text


def test_windsurf_cowork_projects_become_rules(scan_dict, cowork_dict, tmp_path):
    adapter = get_adapter("windsurf")
    proj = tmp_path / "proj"; proj.mkdir()
    out = tmp_path / "out"; out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj, cowork_export=cowork_dict)

    rules = list((proj / ".windsurf" / "rules").glob("cowork-project-*.md"))
    assert len(rules) == 2


# ─────────── Archive tests ───────────

@pytest.mark.parametrize("target", ["hermes", "opencode", "cursor", "windsurf"])
def test_archive_written_for_every_target(scan_dict, cowork_dict, target, tmp_path):
    """Every adapter must write an _archive/ with raw data."""
    adapter = get_adapter(target)
    proj = tmp_path / "proj"; proj.mkdir()
    out = tmp_path / "out"; out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj, cowork_export=cowork_dict)

    archive = out / "_archive"
    assert archive.exists(), f"{target}: _archive/ missing"
    assert (archive / "raw-cowork-export.json").exists()
    assert (archive / "plugin-inventory.json").exists()
    assert (archive / "MIGRATION_NOTES.md").exists()


def test_archive_contains_full_raw_cowork(scan_dict, cowork_dict, tmp_path):
    """Raw cowork export must be losslessly preserved in archive."""
    adapter = get_adapter("hermes")
    proj = tmp_path / "proj"; proj.mkdir()
    out = tmp_path / "out"; out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj, cowork_export=cowork_dict)

    raw = json.loads((out / "_archive" / "raw-cowork-export.json").read_text())
    assert raw["source"] == "cowork"
    assert len(raw["projects"]) == 2
    # prompt_template + docs round-trip through archive
    vision = next(p for p in raw["projects"] if p["name"] == "Vision 2026")
    assert "strategy advisor" in vision["prompt_template"]
    assert any("north-star" in d["filename"] for d in vision["docs"])


def test_migration_notes_explains_unmigratable(scan_dict, cowork_dict, tmp_path):
    """MIGRATION_NOTES.md explicitly mentions the known-unmigratable features."""
    adapter = get_adapter("opencode")
    proj = tmp_path / "proj"; proj.mkdir()
    out = tmp_path / "out"; out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj, cowork_export=cowork_dict)

    notes = (out / "_archive" / "MIGRATION_NOTES.md").read_text()
    # Key terms the doc should teach users about
    for term in ("Custom styles", "Connected apps", "OTel", "Artifact version"):
        assert term in notes, f"MIGRATION_NOTES.md missing: '{term}'"


def test_secrets_manifest_has_no_plaintext(scan_dict, cowork_dict, tmp_path):
    """secrets-manifest.json must contain only hashes, no raw values."""
    adapter = get_adapter("cursor")
    proj = tmp_path / "proj"; proj.mkdir()
    out = tmp_path / "out"; out.mkdir()
    adapter.apply(scan_dict, out, project_dir=proj, cowork_export=cowork_dict)

    mp = out / "_archive" / "secrets-manifest.json"
    if not mp.exists():
        pytest.skip("No secrets in test project")
    manifest = json.loads(mp.read_text())
    # Every entry should have sha256_prefix but NOT raw_value
    for entry in manifest:
        assert "sha256_prefix" in entry
        assert "raw_value" not in entry, "Secrets manifest must never contain plaintext"
