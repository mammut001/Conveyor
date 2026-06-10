# Conveyor — 架构与设计

> **状态**: Active
> **日期**: 2026-06-09
> **适用版本**: 通道解耦 P0+P1 完成（2026-06-09 commit `8828489`）

---

## 1. 一句话定位

**Conveyor 是 transport 层。** 在你和 `codex exec --json` 之间，按 channel（Telegram / Feishu）转发消息、鉴权、做轻量预处理。它**不**是 agent —— Agent 是 Codex CLI 自己（Brain + Hands），Conveyor 是路由和通讯的载体。

这意味着它和「Hermes 类 personal agent」是 **正交** 的：Hermes 处理 reasoning + tool loop，Conveyor 处理「消息从哪个 channel 来、怎么送到 Codex、回复怎么原路返回」。

---

## 2. 运行时架构

```text
Telegram                            Feishu
   │                                  │
   │ Update                           │ WebSocket
   ▼                                  ▼
 bot.py                          feishu_bot.py
 _TelegramOutbound               FeishuOutbound
   │                                  │
   └──────────┬───────────────────────┘
              ▼
       InboundMessage            ← channel-agnostic
              │
              ▼
       handlers.dispatch
         · is_allowed
         · parse command
         · detect memo intent
              │
       ┌──────┼──────┬──────────────┐
       ▼      ▼      ▼              ▼
  handlers/  handlers/ handlers/  handlers/
   commands    memo     jobs       (onboarding, t-only)
       │       │       │
       └───────┴───────┘
              │
              ▼
       OutboundPort (Protocol)
              │
              ▼
        CodexRunner (unchanged)
        · worktree
        · prefetch
        · streaming
        · memo · lifecycle · metadata
```

### 2.1 VPS 路径

| 用途 | 路径 |
|------|------|
| Conveyor 代码 | `/opt/conveyor/` |
| 用户仓库 | `/srv/<your-repo>/` |
| 任务根 | `/srv/conveyor/`（默认；可改） |
| 当日 worktree | `<task_root>/worktrees/day-YYYY-MM-DD/` |
| 飞书长连接 | `wss://msg-frontier.feishu.cn/ws/v2` |

### 2.2 边界规则

| 层 | 允许 import | 禁止 |
|---|-------------|------|
| `runner/` | `config`, `redaction`, `scripts/*` | `telegram`, `lark_oapi`, `handlers` |
| `handlers/` | `runner`, `channel.types`, `redaction` | `telegram`, `lark_oapi` |
| `channel/*.py` | 对应 SDK + `handlers` + `channel.types` | 业务命令逻辑 |
| `bot.py` / `feishu_bot.py` | channel adapter + `handlers.dispatch` | 直接调 `runner`（除 wire-up） |

---

## 3. 核心类型

```python
# channel/types.py — 通道无关，不 import telegram / lark_oapi

@dataclass(frozen=True)
class InboundMessage:
    channel: Literal["telegram", "feishu"]
    operator_id: str          # Telegram user id / Feishu open_id（统一为 str）
    chat_id: str              # 会话 ID（Telegram int → str）
    message_id: str | None    # 用于 reply/thread
    text: str
    chat_type: Literal["p2p", "group", "unknown"]
    mentioned_bot: bool = False

class OutboundPort(Protocol):
    supports_inline_buttons: bool
    async def reply(self, msg, text) -> str | None: ...
    async def send_new(self, msg, text) -> str | None: ...
    async def edit_progress(self, msg, placeholder_id, text) -> bool: ...
    async def reply_with_buttons(self, msg, text, buttons) -> str | None: ...
```

**设计要点**：
- `operator_id` 统一为 `str`，adapter 负责 `str(telegram_user_id)` / feishu `open_id`
- Handler 禁止 import `Update` / `FeishuChannel`
- `OutboundPort.reply` / `send_new` 返回**真实 message_id**（str），让 handler 能把它当 placeholder 传给 `edit_progress` 做就地编辑
- `OutboundPort.edit_progress` 返回 `bool`：
  - **Telegram** 用 `bot.edit_message_text` 真正就地编辑；遇到 `Message is not modified` 当成 success；其他异常记 log + 返回 `False`，handler latch 退化为 `send_new`
  - **Feishu** 首版返 `False`，所有进度走 `send_new`（计划 P2.2 用卡片流式或 throttle 升级）
- 重复发收尾已修复（2026-06-09 commit `09ab931`）

---

## 4. 命令表

23 条命令，集中在 `handlers/commands.py` 的 `COMMAND_TABLE`。Telegram 和 Feishu 共用：

| 类别 | 命令 |
|------|------|
| 状态 | `/status` `/last` `/jobs [n]` |
| 任务控制 | `/run` `/fix` `/cancel` `/apply` `/discard` |
| 改动 | `/diff` |
| 记忆 | `/memo` `/memory [date] [cat]` `/journal [n]` |
| 健康 | `/health [full] [json] [nosecurity]` `/doctor` `/diag [since]` |
| 审计 | `/audit [stale-min]` `/security [since]` `/ratelimit [n]` |
| 报告 | `/metrics [n]` `/log [sel]` `/meta [sel]` |
| 自检 | `/smoke` `/editcheck` |
| 维护 | `/maintain [keep]` `/clean [keep]` |
| 帮助 | `/help` |

加新命令 = 在 `COMMAND_TABLE` 加一行 + 写 handler；两侧 channel 同步。

---

## 5. 对话模式（Chat-first）

| 触发 | JobMode | Codex `--sandbox` | 能力 |
|------|---------|-------------------|------|
| 纯文本 | `run` | `workspace-write` | shell、web、读写 worktree、runner CLI |
| `/run` | `run` | 同上 | 同上 |
| `/fix` | `fix` | 同上 | 同上（与纯文本等价，保留命令兼容） |
| `记 xxx` / `/memo` | — | — | **不经过 Codex**，直接写 MEMORY.md |

**设计理由**：
- 个人 bot + 单 operator：read-only 边界带来的「查 IP 必须 `/fix`」不符合对话直觉
- 安全仍靠：channel 白名单、worktree 隔离、`/diff` + `/apply` 才合入主仓库、输出 redaction
- `/run` 与 `/fix` 保留仅为兼容旧习惯与 job 日志区分，**sandbox 已统一**

### 5.1 Prompt 注入顺序

每次 Codex 调用前的 prompt 拼接顺序（见 `runner/prefetch.py`）：

1. `<operator-profile>` — 身份、语言、风格
2. `<day-brief>` — 每天首个 job 的冷启动摘要
3. `<memory-context>` — 当日 `MEMORY.md`
4. `<tool-registry sandbox="workspace-write">` — shell、memorize、recall 等
5. 用户消息

---

## 6. 与 Hermes 类 personal agent 的边界

| 维度 | Hermes | Conveyor |
|------|--------|----------|
| Agent 内核 | Python `AIAgent` 循环 | **Codex CLI** |
| Tool 调用 | JSON Schema + dispatch | Prompt `<tool-registry>` + Codex shell |
| 多轮 | SQLite SessionDB | 每消息一 job（P0 backlog：session 摘要） |
| 通道 | 多 platform | Telegram + Feishu（同一份业务逻辑） |
| 记忆 | 可插拔 + Skills | MEMORY.md → JOURNAL |

**借鉴**：onboarding、day-brief、streaming 聊天感、MEMORY 归档。

**不重复造**：Conveyor 不维护自己的 tool loop、自己的 session DB、自己的 reasoning step —— 这些都让 Codex CLI 做。Conveyor 负责「在 user 和 Codex 之间搬运消息 + 鉴权 + 渲染回复」。

---

## 6.5 Agent tool layer

Conveyor 在 transport 之上增加了 **结构化 tool registry** 和 **轻量 intent router**，不是纯 hardcoded 命令 bot：

```
用户消息
  → route_intent()
      ├─ deterministic → handlers/tools/runner.run_tool(s)
      ├─ hybrid        → run_tools() 采集事实 → handle_codex_job(带 facts 的 prompt)
      └─ llm           → handle_codex_job(原始 prompt)
```

### 已注册工具 (`handlers/tools/registry.py`)

| name | danger | 说明 |
|---|---|---|
| load, ps, htop, disk, logs, service_status, git_status | READ | 主机快照，0 token |
| service_restart | WRITE | 重启 conveyor 单元，**需确认** |

### Intent router (`handlers/intent.py`)

- **Deterministic 优先**：显式 ops 请求（负载/htop/磁盘/日志）不走 hybrid
- **Hybrid**：`为什么服务器慢` / `分析一下 vps` → 默认采集 load+ps+disk+service_status，再把 facts 注入 Codex prompt
- **显式诊断**：`/diagnose [server|bot|logs|quick]`（`handlers/tools/diagnose.py` 定义各模式 tool 组合）；自然语言「诊断服务器」「帮我诊断 bot」保守匹配
- **LLM fallback**：编码/调试类开放式任务

### 命令别名

| 命令 | 说明 |
|---|---|
| `/diagnose [mode]` | hybrid 主机诊断 → Codex 分析（≠ `/diag` harness） |
| `/restart telegram\|feishu\|maintain` | 映射白名单单元 → `service_restart` + 确认 |
| `/tools` | 按 READ/WRITE 分组列出工具、确认规则、hybrid 示例 |
| `/audit_tools [n]` | 读取 `audit/tools.log` 最近 n 条（只读） |

### 安全策略

- READ 工具立即执行
- WRITE/DESTRUCTIVE 工具 → `create_pending()` → Telegram `reply_with_buttons` 或文本确认
- 文本确认须用明确短语（`确认执行`、`confirm`、`execute` 等）；`好` / `ok` / `是` 等日常回复**不会**触发 pending 操作
- **确认绑定**：`execute_confirmed` / `cancel_pending` / 文本 fallback 均校验 `operator_id + chat_id + channel`
- 取消仍较宽松：`取消`、`算了`、`no` 等
- 确认回调：`bot.py` 的 `CallbackQueryHandler(pattern=r"^tool:")`
- `service_restart` 仅允许白名单单元：`conveyor-telegram-bot`、`conveyor-feishu-bot`、`conveyor-maintain.timer`
- **审计**：`handlers/tools/audit.py` → `codex_memory_root/audit/tools.log`（requested/confirmed/cancelled/executed/rejected）；写入失败不阻断用户流程

### Legacy ops fast path

Slash 命令 `/load` `/vps` `/htop` `/ps` 等在 `COMMAND_TABLE` 注册。Telegram 侧通过 `bot.py` 的 generic `filters.COMMAND` fallback 确保新命令可达（无需为每条命令单独注册 `CommandHandler`）。自然语言 ops 意图由 `route_intent()` 统一路由到 tool registry（内部复用 `handlers/ops.py` 快照函数）。

- **htop 意图保守**：仅在有运行/查看语境时匹配；编码/文档类提及 htop 走 LLM
- **`/ps` 安全**：默认 comm 模式；`/ps full` 仅提示风险；`/ps full confirm` 才输出 args（仍 redact）

---

## 7. Harness 矩阵

```text
make smoke
  ├── runner smokes（不变）
  │   auto_maintain / compress_day / clean_* / classify_memo /
  │   memo_flow / memo_fastpath / progress
  ├── handlers smokes（channel-agnostic）
  │   handlers_smoke / jobs_dedupe_smoke
  │   ops_intent_smoke / ops_smoke / ops_run_smoke / telegram_outbound_smoke
  │   tools_intent_smoke / tools_runner_smoke
  │   telegram_command_fallback_smoke / confirm_strict_smoke / ps_full_smoke
  │   diagnose_command_smoke / restart_alias_smoke / tools_output_smoke
  │   confirmation_context_smoke / tool_audit_smoke / audit_tools_smoke
  └── command_harness
      38 用例，驱动 handlers.dispatch + FakeOutbound + FakeRunner
      （不再用 FakeUpdate / FakeMessage / FakeContext）
```

`channel/telegram_smoke.py` / `channel/feishu_smoke.py` 是 P2.1 的目标——目前两个 channel adapter 还在 `bot.py` / `feishu_bot.py` 内联。

---

## 8. 阶段进度

| 阶段 | 状态 | commit |
|------|------|--------|
| P0 抽 handler、零行为变化 | ✅ | `8828489` |
| P1 命令表统一 + harness 迁移 | ✅ | `8828489` |
| P1.x dedupe 收尾（不重复发 summary） | ✅ | `09ab931` |
| P2.1 Adapter 独立文件 | ⏳ backlog | — |
| P2.2 飞书 progress 卡片 / throttle | ⏳ backlog | — |
| P2.3 onboarding 移入 `handlers/onboarding.py` | ⏳ backlog | — |
| P2.4 单进程双通道 | ⏳ backlog | — |
| Session 摘要（多轮接着聊） | ⏳ backlog | — |

---

## 9. 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| 1.0 | 2026-06-09 | 合并原 `001` + `003`；改名 Conveyor |
| 0.9 | 2026-06-09 | 原 `003-channel-decoupling.md`（P0+P1 设计稿 + 落地） |
| 0.1 | 2026-06-09 | 原 `001-hermes-learning-and-chat-mode.md`（Hermes 对照 + chat-first） |
