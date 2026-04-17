---
name: chat-migration
version: 1.0.0
description: |
  迁移 Claude.ai 官方 Chat 数据（conversations / projects / artifacts）到 neuDrive / Hermes / 本地 Markdown / Obsidian。
  当用户说"迁移claude.ai"、"chat migration"、"claude对话导出"、"chat export"时触发。
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

# Chat Migration Skill v1.0

将 Claude.ai 官方数据导出（Settings → Privacy → Export data）转换为可迁移格式。

---

## Claude.ai 导出 ZIP 格式参考

**ZIP 内容**（2026 年官方格式）:
- `conversations.json` — 全部对话
- `projects.json` — Claude Projects（知识库 + Custom Instructions）
- `users.json` — 账号元信息

**关键事实**:
- 消息用 `content[]` 结构化存储，`text` 字段只是扁平映射（丢信息）
- Artifacts 作为 `tool_use` 内容项存储，`name:"artifacts"`，按 `input.id` 分组获取终稿
- 图片等二进制文件**不在 ZIP 里**，`files_v2[].preview_url` 是会过期的签名 URL，迁移时要立即下载
- Cowork（Team）额外有 `workspace_id` 和 `is_shared` 字段
- 没有 token 统计，没有每消息 model 字段，没有 Claude Memory 数据

**content[] 五种类型**:
| type | 字段 | 说明 |
|------|------|------|
| `text` | `text` | 普通文本 |
| `thinking` | `thinking` | 扩展思考 |
| `voice_note` | `title`, `text` | 语音转文字 |
| `tool_use` | `name`, `input` | 工具调用（含 Artifacts） |
| `tool_result` | `name`, `content[]` | 工具结果 |

**分支对话**: 通过 `parent_message_uuid` + `current_leaf_message_uuid` 重建，`index` 单独不够。

---

## 执行流程

### Phase 0: 自检与自更新

**每次迁移前必须执行。**

1. WebFetch `https://support.claude.com/en/articles/9450526-how-can-i-export-my-claude-data` 检查导出流程是否变化
2. WebFetch `https://github.com/osteele/claude-chat-viewer/blob/main/src/schemas/chat.ts` 检查 schema 是否更新
3. 对比本 skill 的字段列表，有变化则用 Edit 修改本文件再继续

---

### Phase 1: 环境检查

```bash
echo "=== Phase 1: 环境检查 ==="
command -v python3 &>/dev/null && echo "✅ Python $(python3 --version 2>&1 | awk '{print $2}')" || { echo "❌ Python 未安装"; exit 1; }
command -v unzip &>/dev/null && echo "✅ unzip" || { echo "❌ unzip 未安装"; exit 1; }
command -v curl &>/dev/null && echo "✅ curl (下载附件需要)" || echo "⚠️  curl 未安装，无法下载附件"
```

---

### Phase 2: 引导用户获取导出 ZIP

使用 AskUserQuestion 确认用户已完成导出：

**用户操作步骤**（skill 展示给用户）：

```
1. 打开 https://claude.ai/settings/data-privacy-controls
2. 点击 "Export data" 按钮
3. 等待邮件（通常 10 分钟到几小时）
4. 下载邮件中的 ZIP
5. 把 ZIP 路径告诉我
```

用 AskUserQuestion 问：
- Q1: 是否已完成导出？（是 / 否，先引导 / 稍后再做）
- Q2: ZIP 文件的绝对路径是什么？

---

### Phase 3: 解析 ZIP

```bash
ZIP_PATH="<用户提供的路径>"
WORK_DIR="/tmp/chat-migration-$(date +%s)"
mkdir -p "$WORK_DIR"
unzip -q "$ZIP_PATH" -d "$WORK_DIR"

echo "=== ZIP 内容 ==="
ls -la "$WORK_DIR/"
```

验证三个必需文件存在：
```bash
for f in conversations.json projects.json users.json; do
  [ -f "$WORK_DIR/$f" ] && echo "✅ $f ($(wc -c < "$WORK_DIR/$f" | tr -d ' ') bytes)" || echo "⚠️ $f 缺失"
done
```

统计报告：
```python
python3 << PYEOF
import json, os
work = "$WORK_DIR"
convs = json.load(open(f"{work}/conversations.json"))
projs = json.load(open(f"{work}/projects.json"))
users = json.load(open(f"{work}/users.json"))

print(f"📊 导出统计:")
print(f"  对话数: {len(convs)}")
print(f"  项目数: {len(projs)}")
print(f"  用户数: {len(users) if isinstance(users, list) else 1}")

total_msgs = sum(len(c.get('chat_messages', [])) for c in convs)
print(f"  总消息数: {total_msgs}")

# 统计 workspace（Cowork 标记）
ws_conv = [c for c in convs if c.get('workspace_id')]
if ws_conv:
    print(f"  ⚠️  {len(ws_conv)} 个对话含 workspace_id，这是 Cowork 数据，建议用 cowork-migration")

# 统计 Artifacts
artifacts = 0
files_v2 = 0
for c in convs:
    for m in c.get('chat_messages', []):
        for item in m.get('content', []):
            if item.get('type') == 'tool_use' and item.get('name') == 'artifacts':
                artifacts += 1
        files_v2 += len(m.get('files_v2', []))
print(f"  Artifacts 次数: {artifacts}")
print(f"  二进制附件: {files_v2} (需要立即下载，签名 URL 会过期)")
PYEOF
```

---

### Phase 4: 选择输出模式

使用 AskUserQuestion（multi-select）:

| 选项 | 输出 | 适合场景 |
|------|------|---------|
| **Markdown Archive** | 每会话一个 .md 文件 + YAML frontmatter | 本地归档、Obsidian 导入 |
| **neuDrive Push** | 调用 neudrive-sync 推送 | 多 Agent 统一枢纽 |
| **SQLite FTS5** | 导入 Hermes `~/.hermes/state.db` | 在 Hermes 中 `--resume` / session_search |
| **Obsidian Vault** | 每会话 .md + 每 project 目录 + 附件文件夹 | Obsidian 用户 |
| **原始 JSON + 清洗** | 保留 JSON + 解析后的结构化 JSON | 自己写工具处理 |

允许多选。

---

### Phase 5: Markdown 转换核心逻辑

写入 `/tmp/chat-migration-*/output/`，每个对话一个 .md：

```python
python3 << 'PYEOF'
import json, os, re
from datetime import datetime

work = os.environ.get('WORK_DIR', '/tmp/chat-migration')
out = f"{work}/output"
os.makedirs(f"{out}/conversations", exist_ok=True)
os.makedirs(f"{out}/projects", exist_ok=True)
os.makedirs(f"{out}/artifacts", exist_ok=True)

convs = json.load(open(f"{work}/conversations.json"))
projs = json.load(open(f"{work}/projects.json"))

def safe_name(s, max_len=80):
    return re.sub(r'[^\w\-一-龥]+', '-', s)[:max_len].strip('-') or 'untitled'

def content_to_md(content_items, artifact_store):
    """把 content[] 数组转成 markdown"""
    parts = []
    for item in content_items:
        t = item.get('type')
        if t == 'text':
            parts.append(item.get('text', ''))
        elif t == 'thinking':
            thinking = item.get('thinking', '')
            if thinking:
                parts.append(f"<details><summary>💭 Thinking</summary>\n\n{thinking}\n\n</details>")
        elif t == 'voice_note':
            parts.append(f"🎙️ **Voice**: {item.get('title', '')}\n\n{item.get('text', '')}")
        elif t == 'tool_use':
            name = item.get('name', '')
            inp = item.get('input', {})
            if name == 'artifacts':
                # Artifact — 收集到 artifact_store（按 id 分组）
                art_id = inp.get('id', 'unknown')
                art_type = inp.get('type', 'text/plain')
                art_title = inp.get('title', art_id)
                art_content = inp.get('content', '')
                cmd = inp.get('command', 'create')
                artifact_store.setdefault(art_id, {
                    'type': art_type, 'title': art_title, 'versions': []
                })
                artifact_store[art_id]['versions'].append({
                    'command': cmd, 'content': art_content,
                    'version_uuid': inp.get('version_uuid', '')
                })
                parts.append(f"📎 **Artifact** [{cmd}]: `{art_title}` ({art_type}) → see `/artifacts/{art_id}.{_ext(art_type)}`")
            else:
                # 其他工具调用（web_search / repl / google_drive_search 等）
                parts.append(f"🔧 **Tool**: `{name}`\n```json\n{json.dumps(inp, ensure_ascii=False, indent=2)[:500]}\n```")
        elif t == 'tool_result':
            name = item.get('name', '')
            result = item.get('content', [])
            text = ''.join(r.get('text', '') for r in result if isinstance(r, dict))
            parts.append(f"📤 **{name} result**:\n```\n{text[:1000]}\n```")
    return '\n\n'.join(p for p in parts if p)

def _ext(mime):
    m = {
        'text/markdown': 'md', 'text/html': 'html',
        'application/vnd.ant.code': 'txt', 'application/vnd.ant.react': 'tsx',
        'image/svg+xml': 'svg', 'application/vnd.ant.mermaid': 'mmd',
    }
    return m.get(mime, 'txt')

# 建立 project_uuid → project 映射
proj_by_uuid = {p['uuid']: p for p in projs}

# 写入每个对话
for c in convs:
    artifact_store = {}
    uuid = c['uuid']
    name = c.get('name', 'untitled')
    created = c.get('created_at', '')[:10]
    updated = c.get('updated_at', '')
    proj_uuid = c.get('project_uuid')
    workspace = c.get('workspace_id', '')
    
    proj_name = ''
    if proj_uuid and proj_uuid in proj_by_uuid:
        proj_name = proj_by_uuid[proj_uuid].get('name', '')
    
    # Frontmatter
    fm = [
        '---',
        f'uuid: {uuid}',
        f'name: "{name.replace(chr(34), chr(39))}"',
        f'created_at: {c.get("created_at", "")}',
        f'updated_at: {updated}',
    ]
    if proj_name:
        fm.append(f'project: "{proj_name}"')
        fm.append(f'project_uuid: {proj_uuid}')
    if workspace:
        fm.append(f'workspace_id: {workspace}  # Cowork 数据')
    if c.get('model'):
        fm.append(f'model: {c["model"]}')
    fm.append('---\n')
    
    # 消息
    msg_md = []
    for m in c.get('chat_messages', []):
        sender = m.get('sender', '')
        ts = m.get('created_at', '')[:19]
        content = m.get('content', [])
        if not content and m.get('text'):
            # 兜底用 text
            body = m['text']
        else:
            body = content_to_md(content, artifact_store)
        
        # 附件
        attachments = m.get('attachments', [])
        for att in attachments:
            extracted = att.get('extracted_content', '')
            fname = att.get('file_name', 'attachment')
            if extracted:
                body += f"\n\n<details><summary>📎 Attachment: {fname}</summary>\n\n```\n{extracted[:3000]}\n```\n\n</details>"
        
        # 二进制文件（签名 URL）
        files = m.get('files_v2') or m.get('files') or []
        for f in files:
            fname = f.get('file_name', 'unknown')
            url = f.get('preview_url', '')
            if url:
                body += f"\n\n🖼️ Binary: `{fname}` — ⚠️ URL 会过期，需立即下载: {url[:80]}..."
        
        icon = '🧑' if sender == 'human' else '🤖'
        msg_md.append(f'## {icon} {sender} — {ts}\n\n{body}')
    
    # 写文件
    fname = f"{created}_{safe_name(name)}_{uuid[:8]}.md"
    out_path = f"{out}/conversations/{fname}"
    with open(out_path, 'w') as f:
        f.write('\n'.join(fm) + '\n'.join(msg_md))
    
    # 写 Artifacts（每 artifact 的最终版本）
    for art_id, art_data in artifact_store.items():
        ext = _ext(art_data['type'])
        # 取最后一个版本（或按 command 重建）
        final_content = ''
        for v in art_data['versions']:
            if v['command'] == 'rewrite' or v['command'] == 'create':
                final_content = v['content']
            elif v['command'] == 'update':
                # update 的 content 通常是完整替换（Claude 的 artifact 机制）
                final_content = v['content']
        
        art_path = f"{out}/artifacts/{art_id}.{ext}"
        with open(art_path, 'w') as f:
            f.write(final_content)

# 写入 Projects
for p in projs:
    pname = safe_name(p.get('name', 'project'))
    pdir = f"{out}/projects/{pname}"
    os.makedirs(f"{pdir}/docs", exist_ok=True)
    
    # PROJECT.md
    prj_md = [
        '# ' + p.get('name', ''),
        '',
        f"**Description**: {p.get('description', '')}",
        f"**Created**: {p.get('created_at', '')}",
        '',
        '## Custom Instructions (prompt_template)',
        '',
        p.get('prompt_template', '(empty)'),
    ]
    with open(f"{pdir}/PROJECT.md", 'w') as f:
        f.write('\n'.join(prj_md))
    
    # Docs
    for doc in p.get('docs', []):
        dname = safe_name(doc.get('filename', 'doc'))
        with open(f"{pdir}/docs/{dname}.md", 'w') as f:
            f.write(doc.get('content', ''))

print(f"✅ 完成: {out}")
PYEOF
```

---

### Phase 6: 附件下载（可选，但强烈建议）

签名 URL 会过期（通常几小时到几天），必须在迁移期间下载：

```python
python3 << 'PYEOF'
import json, os, urllib.request, hashlib
# 从 conversations.json 提取所有 files_v2 preview_url
# 并发下载到 output/binaries/{file_uuid}_{file_name}
# 超时 30s，失败记录到 failed_downloads.txt
PYEOF
```

如果 URL 已过期（HTTP 403 / 404），记录到 `failed_downloads.txt` 供用户参考。

---

### Phase 7: 多路输出

按 Phase 4 用户选择：

**7.1 Markdown Archive**: 直接把 `output/` 打 ZIP 给用户。

**7.2 neuDrive Push**: 调用 `neudrive-sync` skill：
```
/neudrive-sync --source /tmp/chat-migration-*/output --platform claude-chat
```

**7.3 SQLite FTS5** (for Hermes):
```python
# 把每个对话 INSERT 到 ~/.hermes/state.db sessions + messages 表
# session_id = f"cc_{created[:10].replace('-','')}_xxx_{uuid[:8]}"
# source = "cli"（让 hermes --resume 能恢复）
```

**7.4 Obsidian**: 把 `output/` 目录名包一层 `vault/`，用户复制到 Obsidian vault 即可。

**7.5 原始 JSON**: 直接把 `output/normalized.json`（清洗后的结构化数据）给用户。

---

### Phase 8: 完成报告

```
🎉 Chat Migration 完成!

统计:
  ✅ {N} 个对话 → Markdown
  ✅ {K} 个项目 → Projects/
  ✅ {A} 个 Artifacts 提取（按 id 去重后）
  ✅ {B} 个附件下载（{F} 个过期）
  ⚠️  {W} 个 Cowork 数据（建议补跑 /cowork-migration）

输出位置: {output_dir}

下一步:
  • 本地阅读: open {output_dir}
  • 推到 neuDrive: /neudrive-sync ...
  • 导入 Hermes: /hermes-migration 检测到 state.db
  • Obsidian: 把 output/ 复制到 vault
```

---

## 注意事项

- **无损转换**: 保留原始 ZIP，`output/raw/` 保留 conversations.json 清洗前版本
- **分支对话**: 默认用线性顺序展示，可选 `--branches full` 重建完整分支树
- **账号隐私**: `users.json` 含 email/手机号，默认不写入 output，仅在 `.meta.json` 中保留 uuid
- **Artifacts 版本**: 只保留最终版本；历史版本在 `artifacts/_history/{id}/v{n}.{ext}` 可选保留
- **二进制过期**: `files_v2.preview_url` 过期后只能通过重新下载 ZIP 获取
- **Cowork 分流**: 检测到 `workspace_id` 的对话自动标记，建议配合 `/cowork-migration` 做成员归属处理
