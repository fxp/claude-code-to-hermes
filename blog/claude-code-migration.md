# 当你担心 Claude 账号被封：先备份，再迁移

> 一份开源 CLI + Claude Code Skills，把 Claude Code / Chat / Cowork 的本地数据完整搬到 Hermes / OpenCode / Cursor / Windsurf。
> 仓库：[github.com/fxp/claude-code-migration](https://github.com/fxp/claude-code-migration) · 在线介绍：[fxp.github.io/claude-code-migration](https://fxp.github.io/claude-code-migration/)

---

## 一、你的 Claude 资产，比你以为的多得多

打开你的 `~/.claude/`，数一数那里到底有什么：

CLAUDE.md、自定义 agents、自己写的 skills、hooks、MCP 配置、scheduled-tasks、`.claude/rules/`、output-styles、自定义 slash 命令、主题、键位绑定、所有 session 的 JSONL 历史、子 agent 嵌套对话、tool-result 缓存、shell 快照、file-history 撤销栈、每个项目的 trust dialog 状态和 token 计数、Cowork plugin 清单、企业 managed-policy CLAUDE.md……

我做了一遍清点，**60+ 种数据类型**。这些是你和 Claude 共同积累的工作记忆——它们让 Claude 知道你的代码风格、你团队的术语、你昨天告诉它"这个测试不要再忘了跑"。

风控收紧的时候，你要么换号、要么换 agent。换 agent 这条路上，**绝大多数迁移工具只搬 CLAUDE.md**——其他全部静默丢失。

这就是 [`claude-code-migration`](https://github.com/fxp/claude-code-migration) 想解决的事。

---

## 二、不是点对点适配，是中间层

Anthropic 的 Claude Code、Cursor、OpenCode、Hermes Agent、Windsurf——每个 agent 有自己的本地数据格式。如果做点对点迁移，N 个源 × M 个目标 = N×M 个适配器。新增一个平台就要写 N+M 个新对子。

我们做了 **N+M**：所有源 → 一个中间表示（Workspace Dossier，对外术语；代码里叫 `CanonicalData`/IR）→ 所有目标。

```
SOURCES (N)                 WORKSPACE DOSSIER             TARGETS (M)
─────────────────           ─────────────────             ─────────────────
💻 Claude Code     ┐                                      ┌  🔱 Hermes Agent
💬 Claude.ai Chat  │       ┌────────────────┐             │  ◇ OpenCode
👥 Claude Cowork   ├──────▶│ dossier.json   │─────────────┤  ✎ Cursor
✎ Cursor          │       │ (vendor-neutral)│             │  ⚡ Windsurf
◇ OpenCode        │       │ (你拥有它)     │             │  🔱 neuDrive Hub
🔱 Hermes         │       └────────────────┘             │  (任意新平台)
⚡ Windsurf       ┘                                      └
```

这个 `dossier.json` 是**你拥有的**：纯 JSON、可 diff、可 commit、可加密备份。哪天再想换 agent，再 apply 一次就行。它不绑任何厂商。

新加一个 Agent 平台，**只需要写一个 parser 或 writer**，自动获得它和其他所有 Agent 的互通能力。

---

## 三、3 步使用，对齐你的心智模型

```bash
pip install claude-code-migration

# Step 1: 告诉工具项目在哪
# Step 2: 自动识别 + 导出为 Workspace Dossier
ccm export --project /path/to/your-project --out ./out/dossier.json

# Step 3: 把同一份档案生成为任意目标（可反复跑，不必重扫）
ccm apply --dossier ./out/dossier.json --target hermes   --out ./out
ccm apply --dossier ./out/dossier.json --target opencode --out ./out
ccm apply --dossier ./out/dossier.json --target cursor,windsurf --out ./out

# 一把梭也行
ccm migrate --project /path --target hermes,opencode,cursor,windsurf
```

为什么分两步？大项目的 session JSONL 常常几十 MB，Claude Code 会话历史动辄上万条 message。一次 `export` 写盘后，`apply` 切不同 target 不必重扫——`dossier.json` 是 cache，也是审计材料。

---

## 四、安全是默认的，不是选项

`dossier.json` / `scan.json` 写盘自动完成：

- **明文密钥 redact**：MCP headers 里的 Bearer、`*_API_KEY` 环境变量、会话正文里粘贴的 `sk-ant-*` / `ghp_*` / `AKIA*` / PEM 私钥 / BigModel `32hex.16alnum` 全替换成 `${CC_<PATH>}` 占位符，adapter 把占位符原样传到目标 config（运行时读 env）
- **0o600 文件权限**：user-only，防止共享机器其他账户 `cat` 走
- **同目录 `*.secrets-manifest.json`**：只含 SHA256 前缀和建议的 env var 名，可以审计"这次脱敏了哪些"

默认**不动你真实项目目录**——所有 `.cursor/rules/` `.windsurfrules` `AGENTS.md` `.hermes.md` 都先 staging 到 `<out>/<target>-target/<project-name>/` 下。确认无误才用 `--in-place`，且必须在干净 git 分支上。

---

## 五、对齐 2026 Claude Code 规格

Claude Code 的本地数据模型每周都在演化。我们对照官方文档（[code.claude.com/docs/en/memory](https://code.claude.com/docs/en/memory) + [claude-directory](https://code.claude.com/docs/en/claude-directory)）做了两轮覆盖扩展：

**Round 1 · CLAUDE.md 五处加载点**（之前都是静默丢失）：
- `./.claude/CLAUDE.md`（2026 新增的项目级替代位置）
- 祖先目录全部 CLAUDE.md + CLAUDE.local.md（Claude Code 会拼接整链）
- 子目录 CLAUDE.md（Claude Code 懒加载）
- `@path` import 递归展开（最多 5 跳，跳过 fenced code block 里的伪 import）
- 企业 managed-policy CLAUDE.md（macOS / Linux / Windows 三套 OS 路径）

**Round 2 · `~/.claude/` 剩余表面**：
- `~/.claude/commands/**/*.md` 自定义 slash 命令（子目录做 namespace）
- `<proj>/.claude/commands/**/*.md` 项目级 slash 命令
- `~/.claude/themes/*` 用户主题
- `~/.claude/keybindings.json` 键位
- Plugin `bin/` 可执行（Week 14 新功能：plugin 启用时注入 PATH）
- Plugin-bundled commands / agents

每个桶不强行翻译成目标 agent 原生格式（Cursor 没主题、Windsurf 没 slash 命令）——而是落到 `_archive/claude-md-tree/` 和 `_archive/claude-extras/`，附 `INDEX.md` 说明每个文件来自哪里。**用户决定哪些值得手动 port，哪些丢就丢**——但至少不会沉默地丢。

---

## 六、还有 always-on hub 模式（v1.1+）

上面的 3 步是**一次性**迁移。如果你想让多个 agent **持续共享同一份记忆**——Claude Code 改了一段，Cursor 这边马上也能读到——v1.1 加了 hub 模式：

```bash
pip install 'claude-code-migration[hub]'
ccm hub init                   # 本地 SQLite buffer
ccm hub serve --remote         # captures → outbox → Supabase
```

四层架构：**L1+L2 Supabase（pgvector + tsvector）** · **L3 hub-agent 守护进程** · **L4 SQLite outbox + mirror**。

- **离线优先**：captures 毫秒级写进 L4 outbox，断网不丢
- **redactor 全程过滤**：每个 capture payload 先脱敏再上行
- **MCP 暴露**：`ccm hub mcp-serve` 让任何 agent 通过 MCP 读 L4 mirror，零网络往返
- **dead-letter**：重试 ≥10 次的行挪到死信表，人工复盘

---

## 七、不止备份——这是 Agent 自由

我做这个项目最初的动机，是看到朋友 Claude 账号被风控、几年的 CLAUDE.md 和上千条对话一夜归零。

但写着写着发现，"换 agent 不丢东西"本身就是个值得长期存在的能力。当你的 dossier 是一份独立的、归你拥有的文件：

- 你可以把它备份到自己的存储
- 你可以在 Claude Code 和 Cursor 之间无痛来回切
- 你可以邮件给同事让他用同样的设置开始新项目
- 你可以让企业内部的 5 个不同 agent 共享同一套团队规则

**Agent 锁定不是 LLM 时代的必然**。把数据归一到 vendor-neutral 的 dossier，你每天用什么 agent 是你的事，不是平台的事。

---

## 八、现状

- **184 个端到端测试通过**，覆盖每个 source / 每个 target / round-trip 互迁 / 真实子进程执行
- **覆盖 60+ 种 Claude Code 数据类型**
- 已在 50+ 真实项目上跑通完整迁移
- MIT 协议
- v1.2.0 已发，[Unreleased] 段含两轮 2026 spec 更新

如果你正担心账号、想换 agent、或纯粹想给自己的 Claude 工作记忆做个备份：

```bash
pip install claude-code-migration
ccm export --project . --out dossier.json
```

剩下的事你说了算。

---

**仓库**：<https://github.com/fxp/claude-code-migration>
**主页**：<https://fxp.github.io/claude-code-migration/>
**反馈 / PR**：欢迎在 issue 里说 — 新增任何 agent 平台只需要一个 parser 或 writer。
