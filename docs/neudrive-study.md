# neuDrive 源码调研与 neudrive-sync 设计

> 深度阅读 [agi-bar/neuDrive](https://github.com/agi-bar/neuDrive) 全量源码后的调研报告 + 我们 `neudrive-sync` skill 的实现蓝图。

---

## 一、关键发现：neuDrive 已经内置了 Claude 导入器

**这改变了我们的实现策略。** 原计划我们自己写 `hermes-migration` → `neuDrive` 的同步逻辑，但实际上：

- `internal/platforms/claude_migration.go` 已实现 Claude Code 本地扫描
- `internal/platforms/agent_import.go` 已实现三种 import mode（`agent` / `files` / `all`）
- `neu import platform claude --mode agent` 是官方命令
- 已支持 `Claude Memory 导入` 和 `Claude exported data zip 导入`

所以我们的 skill 更多是**编排者**，不是重造轮子。

---

## 二、neuDrive API 契约（SDK 层）

### 2.1 认证

- Token 格式：`ndt_` + 40 位 hex
- 请求头：`Authorization: Bearer ndt_xxx`
- Scope 系统（19 种）：`read:*` / `write:*` + `admin`
- 我们主要用到：
  - `write:profile` — 写入身份档案
  - `write:memory` — 写入记忆
  - `write:tree` — 写入文件树
  - `write:bundle` — bundle 导入
  - `write:vault` — 秘钥保险柜
  - `write:skills` — 技能库

### 2.2 Canonical Paths（规范路径）

这是 neuDrive 的核心契约 — 所有数据按这个结构组织：

| Path | 内容 |
|------|------|
| `/identity/profile.json` | 身份主档案 |
| `/memory/profile/{category}.md` | 稳定偏好（preferences/relationships/principles） |
| `/memory/scratch/{YYYY-MM-DD}/{slug}.md` | 短期记忆 |
| `/projects/{name}/context.md` | 项目上下文 |
| `/projects/{name}/log.jsonl` | 项目日志 |
| `/skills/{name}/SKILL.md` | 技能定义 |
| `/conversations/{platform}/{key}/conversation.md` | 对话记录（markdown） |
| `/conversations/{platform}/{key}/conversation.json` | 对话记录（原始 JSON） |
| `/conversations/{platform}/{key}/resume-{target}.md` | 跨平台 resume 脚本 |
| `/conversations/{platform}/index.json` | 该平台对话索引 |
| `/roles/{name}/SKILL.md` | 角色技能 |
| `/inbox/{role}/{status}/{messageID}.json` | 收件箱 |

**关键**: `platform` 就是我们的数据来源（`claude-code` / `claude-chat` / `claude-cowork`）。

### 2.3 三种 Import 模式

| Mode | 含义 | 我们的映射 |
|------|------|-----------|
| `agent` | Agent 主动调用 neuDrive API 写入（推荐） | 从 Claude Code / Hermes 会话内触发 |
| `files` | 直接上传文件 | 从导出的 ZIP 包 |
| `all` | 两者都做 | 最保守的全量模式 |

**agent-mediated 限制**: 仅 `codex` 和 `claude-code` 支持 `agent` 模式。`chat` / `cowork` 只能走 `files`。

### 2.4 核心 HTTP 端点

**记忆/档案**:
```
GET  /agent/memory/profile?category=preferences
PUT  /agent/memory/profile  {category, content}
GET  /agent/search?q={query}&scope={all|memory|tree}
```

**项目**:
```
GET  /agent/projects
GET  /agent/projects/{name}
POST /agent/projects  {name}
POST /agent/projects/{name}/log  {action, summary, tags}
```

**文件树**:
```
GET  /agent/tree/{path}               # 读文件/目录
PUT  /agent/tree/{path}               # 写文件 (带 CAS: expected_version / checksum)
GET  /agent/tree/snapshot?path=/      # 全量快照
GET  /agent/tree/changes?cursor={n}   # 增量同步
```

**保险柜**:
```
GET  /agent/vault/scopes              # 列举 scope
GET  /agent/vault/{scope}             # 读取密钥
PUT  /agent/vault/{scope}  {data}     # 写入密钥
```

**Bundle Sync（大体积/迁移核心）**:
```
POST /agent/import/preview            # 导入前预览
POST /agent/import/bundle             # JSON bundle 导入（≤8MiB）
POST /agent/import/session            # 启动 archive 上传 session
PUT  /agent/import/session/{id}/parts/{idx}   # 分片上传
POST /agent/import/session/{id}/commit
GET  /agent/import/session/{id}
GET  /agent/export/bundle?format={json|archive}&filters=...
GET  /agent/export/all
```

**Claude 专属导入**:
```
POST /agent/import/claude-memory      { memories: [...] }
POST /agent/import/skill              { name, files }
POST /agent/import/profile            { preferences, relationships, principles }
```

### 2.5 Bundle 文件格式

- `.ndrv` — JSON 格式，小体积（≤ 8MiB 走 `/agent/import/bundle`）
- `.ndrvz` — ZIP 容器，大体积走 session + parts + commit（可断点续传）

CLI 侧已有完整工具：`neu sync export/push/pull/preview/resume/diff/history`。

---

## 三、neuDrive 内置的 Claude 导入器已实现什么

`internal/platforms/claude_migration.go` + `internal/platforms/agent_import.go`:

| 能力 | 实现路径 |
|------|---------|
| 扫描 `~/.claude/agent-memory/` | `scanClaudeMemoryTree()` |
| 扫描 `~/.claude/memory/` | `scanClaudeMemoryTree()` |
| Profile 规则提取 | `AgentProfileRule` |
| Memory items 提取 | `AgentMemoryItem` |
| 敏感信息检测 | `AgentSensitiveFinding` |
| Vault 候选识别 | `AgentVaultCandidate` |
| Inventory 统计 | `ClaudeInventory` |
| 三种 import mode | `ImportModeAgent` / `Files` / `All` |
| Dry-run 预览 | `PreviewImport()` |
| 消息类型处理 | text/thinking/tool_call/tool_result/attachment |
| 二进制内联限制 | 64 KB |

**已覆盖的 Claude 数据**: `agent-memory/` + `memory/` + 对话（部分）+ 敏感信息扫描。

**未覆盖的数据**（我们的 `hermes-migration` v4 覆盖了 47 项）:
- `~/.claude.json`（含全局 MCP + 项目配置 + OAuth）
- `history.jsonl` / `todos/` / `plans/` / `shell-snapshots/`
- `~/.claude/projects/*/memory/`（`agent-memory` 不含这个，是项目级独立 memory）
- `~/.claude/projects/*/*.jsonl`（完整聊天记录 → SQLite FTS5）
- `settings.json` 中的 `hooks` / `env` / `enableAllProjectMcpServers`
- Channels / teams / scheduled-tasks / plugins/data
- `.mcp.json` / `.worktreeinclude` / `REVIEW.md` / `CLAUDE.local.md`
- `.agents/` / `.cursor/` / `.claude-plugin/` 等非 `.claude/` 目录
- 项目级 `.claude/agents/*.md` + `.claude/agent-memory/` + 更多

**结论**: neuDrive 的 Claude 导入覆盖约 **10-15 项**，我们的 `hermes-migration` v4 覆盖 **47 项**。互补关系。

---

## 四、neudrive-sync 实现蓝图

### 4.1 定位

`~/.claude/skills/neudrive-sync/SKILL.md` 是一个**编排 skill**，不是重造轮子：

1. 读取 `hermes-migration` 的扫描结果（完整 47 项）
2. 按 neuDrive canonical paths 映射转换
3. 打包成 `.ndrvz` bundle
4. 用 `neu sync push` 或 SDK 调用推送
5. 对于 neuDrive 原生支持的部分（`agent-memory` / `memory`），直接用 `neu import platform claude --mode all` 让 neuDrive 自己扫

### 4.2 核心映射表：Claude 47 项 → neuDrive paths

| Claude Code 数据（我们的映射表） | neuDrive canonical path | neuDrive Scope | 上传方式 |
|----|----|---|---|
| `~/.claude/CLAUDE.md` | `/memory/profile/principles.md` | `write:profile` | `POST /agent/import/profile` |
| `CLAUDE.md`（项目） | `/projects/{name}/context.md` | `write:tree` | `PUT /agent/tree/...` |
| `.hermes.md` | `/projects/{name}/notes/hermes.md` | `write:tree` | `PUT` |
| 项目 memory | `/memory/scratch/{date}/{slug}.md` 或 `/projects/{name}/log.jsonl` | `write:memory` | `POST /agent/import/claude-memory` |
| 聊天记录 JSONL | `/conversations/claude-code/{session-uuid}/conversation.json` + `.md` | `write:tree` | `PUT` 或 bundle |
| 子 Agent JSONL | `/conversations/claude-code/{parent}/subagents/{agent}.json` | `write:tree` | `PUT` 或 bundle |
| 全局 Skills | `/skills/{name}/SKILL.md` + 附加文件 | `write:skills` | `POST /agent/import/skill` |
| 项目级 Skills | `/skills/{name}/SKILL.md` (带 cc- 前缀) | `write:skills` | `POST /agent/import/skill` |
| 自定义 Agents | `/roles/{name}/SKILL.md` | `write:skills` | `PUT /agent/tree/...` |
| `~/.claude.json` `mcpServers` Bearer Token | `/agent/vault/{scope}` | `write:vault` | `PUT /agent/vault/...` |
| `settings.local.json` 嵌入的密钥 | `/agent/vault/{scope}` | `write:vault` | `PUT /agent/vault/...` |
| `settings.json` hooks | `/projects/{name}/hooks.md`（作为文档） | `write:tree` | `PUT` |
| `.mcp.json` | `/projects/{name}/mcp.json` | `write:tree` | `PUT` |
| `history.jsonl` | `/memory/scratch/{date}/prompts.md`（精简后） | `write:memory` | `POST` |
| `plans/*.md` | `/projects/{name}/plans/{plan}.md` | `write:tree` | `PUT` |
| `todos/*.json` | `/projects/{name}/log.jsonl` 条目 | `write:memory` | `POST /agent/projects/{name}/log` |
| `output-styles/*.md` | `/memory/profile/output-style.md` | `write:profile` | `POST` |
| `loop.md` | `/skills/cc-loop/SKILL.md` | `write:skills` | `POST` |
| `rules/*.md` | `/memory/profile/rules/{name}.md` | `write:profile` | `PUT` |

### 4.3 Skill 工作流

```
Phase 0: 自检（含读取最新 neuDrive 文档检查 canonical paths 是否变化）

Phase 1: 环境检查
  - 检查是否已安装 neu CLI（~/.local/bin/neu 或 brew install）
  - 检查 ~/.config/neudrive/config.json
  - 如未登录：neu login --api-base https://www.neudrive.ai
  - 用户可选本地实例：neu login --api-base http://localhost:8080

Phase 2: 模式选择
  ┌─────────────────────────────────────────┐
  │ 选择迁移路径：                             │
  │  A) Hybrid（推荐）— 让 neuDrive 扫它能扫  │
  │     的，我们补 hermes-migration 覆盖的空白 │
  │  B) All-in-hermes — 先由 hermes-migration │
  │     全量扫描 → 转 bundle → push          │
  │  C) All-in-neu — 只用 neu import platform │
  │     claude (放弃 37 项 hermes 独有)       │
  └─────────────────────────────────────────┘

Phase 3: 执行迁移（以 Hybrid 为例）
  Step 3.1: neu import platform claude --dry-run --mode agent
    → 让 neuDrive 先 preview 它能扫描的
  Step 3.2: hermes-migration Phase 2 扫描（本地 skill 直接调用）
    → 得到 47 项完整清单
  Step 3.3: 差集计算
    → neuDrive 已覆盖: agent-memory / memory
    → 我们需要补: ~/.claude.json / JSONL 聊天记录 / hooks / MCP / ...
  Step 3.4: 密钥提取 → vault
    扫描 ~/.claude.json mcpServers, settings.local.json allow 规则
    → PUT /agent/vault/{scope} 逐个提交
  Step 3.5: 上下文转换 → /memory/profile/
    ~/.claude/CLAUDE.md → profile/principles.md
    output-styles → profile/output-style.md
    rules → profile/rules/*.md
  Step 3.6: 项目转换 → /projects/{name}/
    每个有数据的项目建一个 project
    PUT context.md + mcp.json + hooks.md + notes/
    log.jsonl 追加 todos
  Step 3.7: 对话转换 → /conversations/claude-code/{session}/
    读取每个 JSONL
    写入 conversation.json + conversation.md（markdown 版）
    生成 index.json
  Step 3.8: Skills 转换 → /skills/{name}/
    cc- 前缀复制（同我们 v4 的 Skill 5.5）
    POST /agent/import/skill 批量提交
  Step 3.9: neu import platform claude --mode agent
    让 neuDrive 再跑一遍，消化 agent-memory / memory
  Step 3.10: bundle 验证
    neu sync export --filters ... -o post-migration.ndrvz
    diff 一下确认一致

Phase 4: 完成报告
  - 推送了 N 项
  - Vault 秘钥 M 个
  - 会话 K 个
  - 可在 https://www.neudrive.ai 查看
  - 也可 neu sync pull 回本地备份
```

### 4.4 与 hermes-migration 的关系

```
┌───────────────────────────────────────┐
│  hermes-migration v4.0 (已完成)        │
│  覆盖 47 项本地扫描 + Hermes 输出      │
└────────────┬──────────────────────────┘
             │  复用扫描逻辑
             ▼
┌───────────────────────────────────────┐
│  neudrive-sync (本设计)                │
│  = hermes-migration 的 "另一种输出"     │
│    + neuDrive 官方 importer 的调度     │
└────────────┬──────────────────────────┘
             │
             ▼
┌───────────────────────────────────────┐
│  neuDrive Hub (agi-bar/neuDrive)       │
│  Hosted: https://www.neudrive.ai       │
│  Self-host: docker compose up          │
└───────────────────────────────────────┘
```

两者关系：
- `hermes-migration` 是**源端数据提取器**（多产出，可输出 Hermes 或 neuDrive 格式）
- `neudrive-sync` 是**目标端适配器**（把任意来源适配为 neuDrive canonical paths）
- `chat-migration` / `cowork-migration`（未来）走同样的适配器模式

### 4.5 安全处理

neuDrive 的 Vault 有 AES-256-GCM 加密 + 4 级信任等级，完美匹配我们的密钥扫描场景：

```
发现密钥                          → min_trust_level
────────────────────────────────────────────
~/.claude.json 的 Bearer Token    → L4（完全信任，给主力 Agent）
settings.local.json 的 Supabase   → L3（工作信任）
MCP stdio env 的 API Key          → L3
第三方 OAuth refresh_token        → L4
```

写入 vault：
```python
hub.write_secret("claude/web-search-prime/bearer", "ace80ee6...")
```

Agent 读取时：
```python
token = hub.read_secret("claude/web-search-prime/bearer")
# Agent 必须有 read:vault 权限 + 满足 min_trust_level
```

### 4.6 Bundle 策略

对于大量数据（如 GB 级聊天记录），用 `.ndrvz` bundle：

1. 本地生成 bundle（我们在 skill 里完成）：
   ```
   neu sync export --source /tmp/claude-migration-stage \
     --format archive \
     --include-domain memory,skills,profile \
     -o claude-migration.ndrvz
   ```

2. Push（自动分片 + 断点续传）：
   ```
   neu sync push --bundle claude-migration.ndrvz --transport auto
   ```

3. Web UI 亦可上传 + 预览。

---

## 五、实施优先级

### Phase A：MVP（1 周可做）
- [ ] `neudrive-sync` skill 骨架（Phase 0-2 + 简单 Hybrid 模式）
- [ ] 利用 neuDrive Python SDK 的 `write_file` / `import_claude_memory` / `import_skill`
- [ ] 密钥提取 → Vault 写入
- [ ] Dry-run 模式 + 完成报告

### Phase B：完整适配（2 周）
- [ ] 聊天 JSONL → neuDrive conversations 完整转换
- [ ] `.hermes-migration.json` 迁移快照格式对接 neuDrive `sync_jobs` 表
- [ ] Bundle 输出支持（.ndrvz）+ 大文件 session 上传

### Phase C：反向同步（后续）
- [ ] `neu sync pull` 从 neuDrive 拉到本地 → 重新生成 `~/.hermes/` 供 Hermes 使用
- [ ] 配合未来的 Cursor / Codex 导出适配器实现 n↔1↔m 闭环

---

## 六、推荐给 neuDrive 上游的补强

我们在 hermes-migration 中涵盖的 37 项 Claude Code 数据，是 neuDrive 当前 importer 未覆盖的。可以给 neuDrive 提 PR（或我们先以 skill 实现，稳定后推进上游）：

### 建议 PR：扩展 claude_migration.go

在 `scanLocalClaudeMigration()` 中补扫：
1. `~/.claude.json` mcpServers + projects + oauthAccount
2. `~/.claude/projects/*/*.jsonl` 完整聊天记录（现在只扫 memory）
3. `~/.claude/projects/*/memory/` （区别于 `agent-memory`）
4. `~/.claude/plans/`
5. `~/.claude/history.jsonl`
6. `~/.claude/todos/`（非空项）
7. `~/.claude/settings.json` + `settings.local.json` 完整权限/hooks/env
8. `~/.claude/plugins/installed_plugins.json`
9. `~/.claude/channels/`
10. `~/.claude/scheduled-tasks/`

### 建议 PR：支持更多来源目录

Claude Code 不只读 `.claude/`，还读 `.agents/`（Z School）、`.cursor/`（Next-Biz）、`.claude-plugin/`（DoItLater）、`.auto-memory/`（Presentation Generator），neuDrive 导入器可加扫这些路径。

---

## 七、下一步决策

1. **先出 `neudrive-sync` MVP**（Phase A）并测试
2. 还是**先给 neuDrive 上游提 PR** 扩展 importer
3. 或**两条腿走路**: 本仓库发 skill（快），同时给 neuDrive PR（慢但永久）

推荐 3，但从 1 开始。
