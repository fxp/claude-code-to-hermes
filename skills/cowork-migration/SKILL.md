---
name: cowork-migration
version: 1.0.0
description: |
  迁移 Claude Cowork（Team workspace）数据到 neuDrive / Hermes。包括团队共享项目、成员归属、Connected Tools 配置。
  当用户说"迁移cowork"、"迁移团队"、"cowork migration"、"team workspace export"时触发。
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

# Cowork Migration Skill v1.0

把 Claude Cowork（Team workspace）完整环境迁移出去。

---

## ⚠️ 重要前置说明

**Cowork 是基于 Claude.ai Team 订阅的团队协作空间**。当前（2026 年）迁移有以下限制：

1. **官方导出覆盖范围有限**: 同 Claude.ai 个人导出格式，ZIP 仅含 `conversations.json` / `projects.json` / `users.json`
2. **Admin API 未公开**: 团队管理员通过 Admin Console 操作，没有对外文档化的 REST API
3. **导出中缺失的数据**（需要手动补齐）:
   - Team Skills 定义
   - Connected Tools / MCP Server 配置
   - Shared Memory（团队级记忆）
   - Member roster（成员名单）+ roles
   - Usage logs / audit trail

4. **需要 admin 权限**: 只有团队 admin 能触发全量导出；member 自己的导出只含个人可见内容

---

## 两条迁移路径

### 路径 A: 官方 ZIP 导出（推荐首选）

Admin 在 Settings → Team settings → Export → Export all team data

输出和个人 Chat 导出**格式相同**，但额外字段：
- `conversations[].workspace_id` — 工作区 ID
- `conversations[].account.uuid` — 发送者账号（用于成员归属）
- `projects[].is_shared: true` — 团队共享标识

**优点**: 官方、安全、格式稳定
**缺点**: 缺少 skills / connectors / shared memory

### 路径 B: 浏览器自动化补齐（可选）

对于路径 A 不能覆盖的数据，用 `mcp__Claude_in_Chrome__*` 工具（需要已登录的浏览器 session）抓取：

- Admin Console → Tools 页面 → 抓取 MCP 连接列表
- Admin Console → Skills 页面 → 抓取团队 skills
- Admin Console → Members 页面 → 抓取成员和 roles

**注意**: 这是对 UI 的抓取，随时可能因 UI 改版失效，属于 best-effort 补丁。

---

## 执行流程

### Phase 0: 自检

1. WebFetch `https://support.claude.com/` 搜索 "team export" / "cowork export" 确认官方流程
2. 对比本 skill 的 workspace_id / is_shared 字段是否仍然有效
3. 检查 `chat-migration` skill 是否可用（本 skill 复用它的解析器）

---

### Phase 1: 权限与环境检查

```bash
echo "=== Phase 1: 环境检查 ==="
# Python + unzip + curl
command -v python3 &>/dev/null && echo "✅ Python" || exit 1
command -v unzip &>/dev/null && echo "✅ unzip" || exit 1

# 检查 chat-migration skill 是否存在
[ -f ~/.claude/skills/chat-migration/SKILL.md ] && echo "✅ chat-migration 可复用" || echo "⚠️  需先安装 chat-migration"
```

用 AskUserQuestion 确认:
- Q1: 你是团队的 admin 吗？
  - 是 → 继续 admin 全量导出流程
  - 否 → 只能导出你个人账号可见的对话（同 /chat-migration）
  - 不确定 → 展示判断方法

---

### Phase 2: 引导 admin 导出

**展示给用户的步骤**:

```
Admin 导出流程（2026 年）:

1. 以 admin 身份登录 https://claude.ai/
2. 打开 Settings → Team → Data export
3. 选择 "Export all team data"
4. 等待邮件（团队数据量大，可能几小时到 24 小时）
5. 下载 ZIP
6. 把 ZIP 路径告诉我

如果看不到 "Team" 设置项，说明不是 admin，退回路径 A 的个人导出。
```

---

### Phase 3: 解析与成员归属

调用 `chat-migration` 的解析逻辑，但额外处理 Cowork 特有字段：

```python
python3 << 'PYEOF'
import json, os

zip_dir = os.environ['WORK_DIR']
convs = json.load(open(f"{zip_dir}/conversations.json"))
users = json.load(open(f"{zip_dir}/users.json"))

# 用户名单（admin 导出时 users 通常是 list，含所有成员）
user_map = {}
if isinstance(users, list):
    for u in users:
        user_map[u['uuid']] = {
            'name': u.get('full_name', ''),
            'email': u.get('email_address', ''),
        }

# 按 workspace + member 分组
by_workspace = {}
for c in convs:
    ws = c.get('workspace_id') or 'default'
    owner = c.get('account', {}).get('uuid', 'unknown')
    by_workspace.setdefault(ws, {}).setdefault(owner, []).append(c)

# 统计
print(f"📊 Cowork 数据统计:")
for ws, members in by_workspace.items():
    print(f"  Workspace {ws[:8]}:")
    for member_uuid, mconvs in members.items():
        info = user_map.get(member_uuid, {'name': member_uuid[:8]})
        print(f"    👤 {info.get('name', '')} ({info.get('email', '?')}): {len(mconvs)} 对话")
PYEOF
```

---

### Phase 4: 密钥扫描（Cowork 特别重要）

Cowork 常包含团队共享的 API Key。扫描：

```python
import re

SECRET_PATTERNS = [
    (r'sk-[A-Za-z0-9]{40,}', 'openai'),
    (r'ndt_[a-f0-9]{40}', 'neudrive'),
    (r'sk-ant-[A-Za-z0-9_-]{80,}', 'anthropic'),
    (r'ghp_[A-Za-z0-9]{36}', 'github_pat'),
    (r'gho_[A-Za-z0-9]{36}', 'github_oauth'),
    (r'xox[baprs]-[A-Za-z0-9-]+', 'slack'),
    (r'[A-Za-z0-9]{32}\.[A-Za-z0-9]{16}', 'zhipu_glm'),  # BigModel
]

# 对每个 message content / attachment 扫描
# 记录到 secrets.txt 并提示用户转移到 vault
```

**警告处理**: 任何发现的密钥 → 提醒用户通过 `/neudrive-sync` 写入 neuDrive Vault（AES-256-GCM 加密），不要明文留在 markdown 里。

---

### Phase 5: 可选 — 浏览器自动化补齐

如果用户想要团队 Skills / Connectors 等，引导用 Chrome MCP：

```
要补齐以下数据需要浏览器自动化:
- Team Skills 列表
- Connected Tools (MCP servers)
- Member roster + roles
- Usage logs

是否启动 Chrome 自动抓取？(Y/N)
```

若 Y：
1. 检查 `mcp__Claude_in_Chrome__*` 工具是否可用
2. 用 navigate 打开 Admin Console 对应页面
3. 用 `read_page` / `get_page_text` 提取结构化数据
4. 解析 → 写入 `output/team-config/`

**每个抓取前都要用 AskUserQuestion 确认**，避免 UI 改版时乱抓。

---

### Phase 6: 按成员分目录输出

```
output/
├── team-info/
│   ├── members.json         # 成员名单
│   ├── workspaces.json      # workspace 列表
│   ├── connectors.md        # MCP 连接（path B 补齐）
│   └── skills/              # 团队 skills（path B 补齐）
├── shared-projects/
│   ├── {project-name}/
│   │   ├── PROJECT.md
│   │   ├── docs/
│   │   └── conversations/   # 此项目下的跨成员对话
│   └── ...
├── members/
│   ├── {member-email}/
│   │   ├── profile.md       # 姓名/邮箱/role（from users.json）
│   │   └── conversations/   # 此成员的私人对话
│   └── ...
└── audit/
    ├── secrets-found.txt    # 扫到的明文密钥（建议删除）
    └── migration-audit.json
```

---

### Phase 7: 路由到其他 skill

使用 AskUserQuestion 让用户选择推送目的地:

- **neuDrive Hub** → 调用 `/neudrive-sync --source output --platform claude-cowork`
- **个人 Hermes** → 只推自己的 `members/{self-email}/` 到本地 Hermes state.db
- **Team Hermes** → 每个成员都各自执行迁移（skill 生成 per-member 指令）
- **只归档** → 打包成 ZIP

---

### Phase 8: 团队迁移专属提示

```
🎉 Cowork Migration 完成!

统计:
  ✅ {N} 个成员的对话已按成员分目录
  ✅ {K} 个共享项目已提取
  ⚠️  {M} 个 workspace 数据
  🔐 {S} 个密钥被扫出 (建议删除并用 /neudrive-sync 写入 Vault)

⚠️  重要提醒:
  1. 团队数据含他人隐私，分发前请与所有成员确认
  2. 如某成员要单独迁到自己的 Agent:
     把 members/{他的email}/ 目录给他，他用 /chat-migration --local 即可
  3. Team Skills / Connectors / Shared Memory 如未补齐:
     admin 进 Admin Console 手动抄录
  4. 建议 admin 把 ZIP 原件保存 180 天，以便二次迁移
```

---

## 注意事项

- **隐私边界**: Cowork 含多成员数据，按成员分目录是**必须**的，不要把所有对话混在一起
- **Secrets 优先**: 团队 API Key 外泄风险远大于个人，密钥扫描阶段不能跳过
- **admin vs member**: 两种身份导出范围不同，skill 在 Phase 1 问清楚并走不同路径
- **UI 抓取脆弱**: path B 依赖当前 UI，半年一审
- **Cross-tenant 隔离**: 不同 workspace_id 严格分开，不做 merge
- **官方方案优先**: 始终先用官方 ZIP 导出，浏览器抓取只补不可缺的部分
