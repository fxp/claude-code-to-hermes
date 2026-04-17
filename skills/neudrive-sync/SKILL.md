---
name: neudrive-sync
version: 1.0.0
description: |
  把 Claude 生态（Code / Chat / Cowork）迁移数据推送到 neuDrive (agi-bar/neuDrive) 统一枢纽，
  让 Hermes / Cursor / Codex / Kimi / 飞书等任意 Agent 通过 MCP 共享同一份身份+记忆+技能。
  当用户说"推到neudrive"、"同步到hub"、"neudrive sync"、"统一枢纽"时触发。
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
  - Agent
  - WebFetch
---

# neuDrive Sync Skill v1.0

把 `hermes-migration` / `chat-migration` / `cowork-migration` 的产物适配到 [neuDrive](https://github.com/agi-bar/neuDrive) canonical paths 并推送。

---

## 定位：编排器，不是重造轮子

**关键事实**: neuDrive 已经内置 Claude 导入器（`internal/platforms/claude_migration.go`），通过 `neu import platform claude --mode agent` 可以扫描 `~/.claude/agent-memory/` + `~/.claude/memory/`，覆盖约 10-15 项数据。

本 skill 的工作：
1. **让 neuDrive 自己扫它能扫的** — 直接调 `neu import platform claude`
2. **我们补齐 neuDrive 没覆盖的 37+ 项** — 用 SDK API / Bundle 上传
3. **敏感数据 → Vault** — 密钥统一加密存储
4. **大体积数据 → .ndrvz bundle** — 断点续传

---

## neuDrive 契约速查

### Canonical Paths

| 路径 | 内容 |
|------|------|
| `/identity/profile.json` | 身份主档案 |
| `/memory/profile/{category}.md` | 稳定偏好（preferences/relationships/principles） |
| `/memory/scratch/{YYYY-MM-DD}/{slug}.md` | 短期记忆（默认 7 天衰减） |
| `/projects/{name}/context.md` | 项目上下文 |
| `/projects/{name}/log.jsonl` | 项目日志 |
| `/skills/{name}/SKILL.md` | 技能定义 |
| `/conversations/{platform}/{key}/conversation.{md,json}` | 对话归档 |
| `/roles/{name}/SKILL.md` | 角色技能 |
| `/inbox/{role}/{status}/{id}.json` | 收件箱 |

### Token & Scope

Token 格式: `ndt_` + 40 位 hex。常用 scope:
- `write:profile` / `write:memory` / `write:tree` / `write:skills` / `write:vault` / `write:bundle`

### 核心端点

- `PUT /agent/tree/{path}` — 写文件（支持 CAS: expected_version/checksum）
- `POST /agent/import/bundle` — JSON bundle ≤ 8MiB
- `POST /agent/import/session` — 大 bundle session 启动
- `PUT /agent/import/session/{id}/parts/{idx}` — 分片上传
- `POST /agent/import/session/{id}/commit` — 提交
- `POST /agent/import/claude-memory` — Claude memory 专用
- `POST /agent/import/skill` — Skill 专用
- `POST /agent/import/profile` — Profile 批量
- `PUT /agent/vault/{scope}` — 写入加密密钥

### Bundle 格式

- `.ndrv` — JSON 格式，≤ 8MiB 直传
- `.ndrvz` — ZIP 容器，大体积走 session + parts + commit（可 resume）

---

## 执行流程

### Phase 0: 自检

1. WebFetch `https://github.com/agi-bar/neuDrive/blob/main/docs/reference.md` 检查最新 canonical paths
2. WebFetch `https://github.com/agi-bar/neuDrive/blob/main/docs/sync.md` 检查 sync 协议
3. 对比本 skill 的映射表，有变化则 Edit 修改

---

### Phase 1: 环境检查

```bash
echo "=== Phase 1: 环境检查 ==="

# 检查 neu CLI
if command -v neu &>/dev/null; then
  NEU_VER=$(neu --version 2>/dev/null | head -1)
  echo "✅ neu CLI ($NEU_VER)"
else
  echo "⚠️  neu CLI 未安装，需要先安装："
  echo ""
  echo "  方式 A: 从源码编译"
  echo "    git clone https://github.com/agi-bar/neuDrive"
  echo "    cd neuDrive && go build -o neu ./cmd/neu"
  echo "    sudo cp neu /usr/local/bin/"
  echo ""
  echo "  方式 B: 直接用 Python SDK (无需 CLI)"
  echo "    pip install neudrive"
fi

# 检查 Python SDK (fallback 路径)
python3 -c "import neudrive" 2>/dev/null && echo "✅ neudrive Python SDK" || echo "⚪ neudrive SDK 未安装 (pip install neudrive)"

# 检查登录状态
if command -v neu &>/dev/null; then
  neu whoami 2>/dev/null && echo "✅ neu 已登录" || echo "⚠️  尚未登录，稍后引导"
fi
```

---

### Phase 2: 选择实例 + 登录

使用 AskUserQuestion:

| 选项 | 说明 |
|------|------|
| **Hosted (https://www.neudrive.ai)** | 官方托管，最省事 |
| **Self-hosted** | 自部署实例，输入 API URL |
| **Local dev** | 本机 `http://localhost:8080`（Docker 起的） |

登录：

```bash
# Hosted
neu login --api-base https://www.neudrive.ai

# 或手工粘贴 token
neu login --api-base https://www.neudrive.ai --token ndt_xxx

# 验证
neu whoami
```

未安装 CLI 的用户走 SDK 路径：
```python
from neudrive import NeuDrive
hub = NeuDrive("https://www.neudrive.ai", token="ndt_xxx")
hub.get_auth_info()  # 验证 token
```

---

### Phase 3: 选择来源

使用 AskUserQuestion (multi-select):

| 来源 | 对应 skill | 说明 |
|------|-----------|------|
| Claude Code (本机) | `hermes-migration` 扫描结果 | 47+ 项完整数据 |
| Claude.ai Chat | `chat-migration` 输出目录 | conversations + projects + artifacts |
| Claude Cowork | `cowork-migration` 输出目录 | 团队数据 + 成员归属 |
| neuDrive 内置扫描 | `neu import platform claude` | neuDrive 原生覆盖（~15 项） |

---

### Phase 4: 同步模式

使用 AskUserQuestion:

1. **Hybrid 推荐**: 先让 neuDrive 自扫 → 我们补差集
2. **All-in-our-tool**: 只用我们的映射，不调 neuDrive 内置
3. **Dry-run**: 先 preview 不实际写入
4. **Mirror mode**: 对齐到 bundle 状态（会删除 neuDrive 中 bundle 未声明的 skill 文件）

---

### Phase 5: 执行同步

#### 5.1 让 neuDrive 先扫（Hybrid 第 1 步）

```bash
# Dry-run 先看它要动什么
neu import platform claude --dry-run --mode agent

# 实际执行
neu import platform claude --mode all
```

这步会把 `~/.claude/agent-memory/` + `~/.claude/memory/` 写入 neuDrive。

#### 5.2 密钥提取 → Vault

```python
import re, json, os
from neudrive import NeuDrive

hub = NeuDrive(api_base, token=token)

# 扫描 hermes-migration 报告的密钥位置
SECRET_PATTERNS = [
    (r'Bearer\s+([A-Za-z0-9.\-_]+)', 'bearer_token'),
    (r'sk-[A-Za-z0-9]{40,}', 'openai_like'),
    (r'ndt_[a-f0-9]{40}', 'neudrive_token'),
    (r'sk-ant-[A-Za-z0-9_-]{80,}', 'anthropic'),
    (r'ghp_[A-Za-z0-9]{36}', 'github_pat'),
    (r'[A-Za-z0-9]{32}\.[A-Za-z0-9]{16}', 'zhipu_glm'),
]

# 1. ~/.claude.json mcpServers → vault
cj = json.load(open(os.path.expanduser('~/.claude.json')))
for name, cfg in cj.get('mcpServers', {}).items():
    headers = cfg.get('headers', {})
    for k, v in headers.items():
        if 'auth' in k.lower() or 'bearer' in str(v).lower():
            scope = f"claude/{name}/{k.lower()}"
            hub.write_secret(scope, str(v))
            print(f"🔐 Vault: {scope}")

# 2. settings.local.json 嵌入的密钥
sl_path = os.path.expanduser('~/.claude/settings.local.json')
if os.path.exists(sl_path):
    sl = open(sl_path).read()
    for pat, kind in SECRET_PATTERNS:
        for m in re.findall(pat, sl):
            scope = f"claude/settings/{kind}"
            hub.write_secret(scope, m)
            print(f"🔐 Vault: {scope}")
```

#### 5.3 Profile / 稳定偏好

```python
# ~/.claude/CLAUDE.md → /memory/profile/principles.md
cc_home_path = os.path.expanduser('~/.claude/CLAUDE.md')
if os.path.exists(cc_home_path):
    hub.update_profile('principles', open(cc_home_path).read())

# user-level memory (e.g. user_xiaopingfeng.md) → /memory/profile/preferences.md
# output-styles → /memory/profile/output-style.md
# rules → /memory/profile/rules.md（合并）
```

#### 5.4 项目上下文

```python
# 每个有 CLAUDE.md / .hermes.md 的项目建一个 neuDrive project
for proj_dir in hermes_scan_result['projects']:
    proj_name = os.path.basename(proj_dir)
    try:
        hub.create_project(proj_name)
    except: pass  # 已存在
    
    # context.md
    cm_path = f"{proj_dir}/CLAUDE.md"
    hm_path = f"{proj_dir}/.hermes.md"
    parts = []
    if os.path.exists(cm_path): parts.append(open(cm_path).read())
    if os.path.exists(hm_path): parts.append(open(hm_path).read())
    if parts:
        hub.write_file(f"/projects/{proj_name}/context.md", '\n\n---\n\n'.join(parts))
    
    # Hooks / MCP 作为 notes 写入
    settings_path = f"{proj_dir}/.claude/settings.json"
    if os.path.exists(settings_path):
        s = json.load(open(settings_path))
        if s.get('hooks'):
            hub.write_file(f"/projects/{proj_name}/notes/hooks.md",
                f"# Hooks\n\n```json\n{json.dumps(s['hooks'], indent=2)}\n```")
        if s.get('env'):
            hub.write_file(f"/projects/{proj_name}/notes/env.md",
                f"# Env\n\n```json\n{json.dumps(s['env'], indent=2)}\n```")
    
    mcp_path = f"{proj_dir}/.mcp.json"
    if os.path.exists(mcp_path):
        hub.write_file(f"/projects/{proj_name}/mcp.json", open(mcp_path).read())
    
    # Todos → log
    # ... (从 hermes-migration 读)
```

#### 5.5 对话记录（Claude Code JSONL）

从 `hermes-migration` 扫描结果导入到 `/conversations/claude-code/{uuid}/`：

```python
import glob
cc_projects = os.path.expanduser('~/.claude/projects')
for proj in os.listdir(cc_projects):
    for jsonl in glob.glob(f"{cc_projects}/{proj}/*.jsonl"):
        sid = os.path.basename(jsonl).replace('.jsonl', '')
        
        # 原始 JSON
        with open(jsonl) as f:
            raw = f.read()
        hub.write_file(f"/conversations/claude-code/{sid}/conversation.json", raw)
        
        # Markdown 版（调 chat-migration 的 content_to_md 逻辑）
        md = convert_cc_jsonl_to_md(raw)
        hub.write_file(f"/conversations/claude-code/{sid}/conversation.md", md)
```

对于从 `chat-migration` / `cowork-migration` 输出目录的迁移，直接按 canonical path 上传：
```python
# chat-migration 输出: output/conversations/{uuid}.md → /conversations/claude-chat/{uuid}/conversation.md
# cowork-migration 输出: output/members/{email}/conversations/{uuid}.md → /conversations/claude-cowork/{workspace}/{uuid}/conversation.md
```

#### 5.6 Skills

```python
# 全局 skills → /skills/cc-{name}/
import os, shutil
for skill_dir in os.listdir(os.path.expanduser('~/.claude/skills')):
    src = os.path.expanduser(f'~/.claude/skills/{skill_dir}')
    skill_md = f"{src}/SKILL.md"
    if not os.path.exists(skill_md):
        skill_md = f"{src}/skill.md"  # 大小写兼容
    if not os.path.exists(skill_md):
        continue
    
    files = {}
    files['SKILL.md'] = open(skill_md).read()
    # 附加 scripts / references / templates
    for sub in ['scripts', 'references', 'templates']:
        sub_dir = f"{src}/{sub}"
        if os.path.isdir(sub_dir):
            for root, _, names in os.walk(sub_dir):
                for name in names:
                    rel = os.path.relpath(os.path.join(root, name), src)
                    with open(os.path.join(root, name), 'rb') as f:
                        files[rel] = f.read().decode('utf-8', errors='replace')
    
    hub.import_skill(f"cc-{skill_dir}", files)
```

#### 5.7 Agent 定义（Claude Code agents）→ Roles

```python
# ~/.claude/agents/*.md → /roles/{name}/SKILL.md
# {project}/.claude/agents/*.md → /roles/{project}-{name}/SKILL.md
```

---

### Phase 6: 大体积数据走 Bundle

对于巨量聊天记录（GB 级），用 `.ndrvz` bundle 分片上传：

```bash
# 本地先生成 bundle
neu sync export \
  --source /tmp/neudrive-migration-stage \
  --format archive \
  --include-domain memory,skills \
  -o claude-migration.ndrvz

# 预览
neu sync preview --bundle claude-migration.ndrvz

# 推送（auto 自动选分片或直传）
neu sync push --bundle claude-migration.ndrvz --transport auto

# 断点续传
# 若中断会生成 claude-migration.ndrvz.session.json
neu sync resume --bundle claude-migration.ndrvz
```

---

### Phase 7: 验证

```bash
# 1. 列举推送后的文件树
neu stats

# 2. 拉回本地对比
neu sync pull -o /tmp/post-migration.ndrvz

# 3. diff（退出码 0=一致）
neu sync diff --left /tmp/original.ndrvz --right /tmp/post-migration.ndrvz
```

Python 侧:
```python
# 验证关键路径可读
profile = hub.get_profile()
print(f"Profile 条目: {len(profile)}")

skills = hub.list_skills()
print(f"Skills: {len(skills)}")

result = hub.search_memory("OpenClaw")
print(f"FTS 搜索 'OpenClaw': {len(result)} 条匹配")

vault_scopes = hub.list_secrets()
print(f"Vault 保存了: {len(vault_scopes)} 个密钥 scope")
```

---

### Phase 8: 完成报告

```
🎉 neuDrive Sync 完成!

实例: {api_base}
用户: {user_email}

推送统计:
  ✅ Profile: {N} 个条目
  ✅ Projects: {P} 个
  ✅ Conversations: {C} 个（Chat {c1} + Code {c2} + Cowork {c3}）
  ✅ Skills: {S} 个（cc- 前缀）
  ✅ Roles (Agents): {A} 个
  🔐 Vault 密钥: {V} 个
  📦 Bundle 上传: {B} MiB

访问方式:
  Web 管理后台: {api_base}
  MCP endpoint: {api_base}/mcp （给任意支持 MCP 的 Agent 配置）
  
MCP 接入示例（Cursor / Claude Desktop / Codex）:
  {
    "mcpServers": {
      "neudrive": {
        "type": "http",
        "url": "{api_base}/mcp",
        "headers": {
          "Authorization": "Bearer {scoped_token}"
        }
      }
    }
  }

下一步:
  • 在任意 Agent 中连接上面的 MCP endpoint
  • 该 Agent 会自动看到: identity/ + memory/ + projects/ + skills/
  • 敏感信息只通过 read_secret 获取，不明文暴露
```

---

## 注意事项

- **幂等**: 重复运行只会 upsert，不会重复；文件有 version/checksum CAS
- **权限隔离**: 给不同目标 Agent 颁发不同 scope 的 token（如 Hermes 拿 `admin`，第三方拿 `read:memory`）
- **信任等级**: 敏感 vault 设 `min_trust_level=L4`，只有完全信任的 Agent 能读
- **回退**: `neu sync pull` 任何时候可以把 hub 状态拉回本地 `.ndrvz`
- **审计**: 每次 sync 在 neuDrive `sync_jobs` 表有记录，可 `neu sync history` 查
- **安全**: 从不用 `--token` 明文命令行（留在 shell history），用 `neu login` 或环境变量
- **增量**: 第二次及以后只推变化的部分（通过 snapshot cursor）
- **本地 git mirror**（可选）: 如开启，推 neuDrive 的同时会写本地 git 仓库做双份备份
