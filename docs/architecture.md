# Conveyor — 架构与设计

> **状态**: Active
> **日期**: 2026-06-11
> **适用版本**: 通道解耦 P0+P1、agent 工具层、实时 Telegram 烟测、Telegram live smoke

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

命令集中在 `handlers/commands.py` 的 `COMMAND_TABLE`，Telegram 和 Feishu 共用：

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
| **主机快照（READ）** | `/load` `/vps` `/htop` `/ps` `/disk` `/logs` `/service_status` `/git_status` |
| **Agent 工具** | `/tools` `/diagnose [server\|bot\|logs\|quick]` `/restart telegram\|feishu\|maintain` `/audit_tools [n]` |

加新命令 = 在 `COMMAND_TABLE` 加一行 + 写 handler；两侧 channel 同步。

Telegram 路由：`bot.py` 为历史命令注册了显式 `CommandHandler`，之后挂一个
generic `MessageHandler(filters.COMMAND, …)` fallback —— `COMMAND_TABLE`
里新加的斜杠命令不需要在 `bot.py` 单独接线就能到达；显式注册的仍然优先。

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
| `/restart telegram\|feishu\|maintain` | 映射白名单单元 → `service_restart` + 确认；任意单元名直接拒 |
| `/tools` | 按 READ/WRITE 分组列工具、确认规则、hybrid 示例 |
| `/audit_tools [n]` | 读取 `audit/tools.log` 最近 n 条（默认 10，上限 50，**只读**） |

### 安全策略

- READ 工具立即执行
- WRITE/DESTRUCTIVE 工具 → `create_pending()` → Telegram `reply_with_buttons` 或文本确认
- 文本确认须用明确短语（`确认执行`、`confirm`、`execute` 等）；`好` / `ok` / `是` 等日常回复**不会**触发 pending 操作
- **确认绑定**：`execute_confirmed` / `cancel_pending` / 文本 fallback 均校验 `operator_id + chat_id + channel`
- 取消仍较宽松：`取消`、`算了`、`no` 等
- 确认回调：`bot.py` 的 `CallbackQueryHandler(pattern=r"^tool:")`
- `service_restart` 仅允许白名单单元：`conveyor-telegram-bot`、`conveyor-feishu-bot`、`conveyor-maintain.timer`
- **审计**：`handlers/tools/audit.py` → `codex_memory_root/audit/tools.log`（requested/confirmed/cancelled/executed/rejected）；`arg` 与结果预览都过 `redact_text` + `truncate`；写入失败不阻断用户流程
- **自然语言 restart 防误触发**：`route_intent` 检测到 restart 模式但 `_extract_service_arg` 拿不到具体目标时，返回 `kind="llm"` 并附 `route.question`，由 Codex 反问目标，而不是隐式 default 到 `conveyor-telegram-bot`

### Legacy ops fast path

Slash 命令 `/load` `/vps` `/htop` `/ps` 等在 `COMMAND_TABLE` 注册。Telegram 侧通过 `bot.py` 的 generic `filters.COMMAND` fallback 确保新命令可达（无需为每条命令单独注册 `CommandHandler`）。自然语言 ops 意图由 `route_intent()` 统一路由到 tool registry（内部复用 `handlers/ops.py` 快照函数）。

- **htop 意图保守**：仅在有运行/查看语境时匹配；编码/文档类提及 htop 走 LLM
- **`/ps` 安全**：默认 comm 模式；`/ps full` 仅提示风险；`/ps full confirm` 才输出 args（仍 redact）

### 6.6 Telegram 实时烟测（手动）

`scripts/telegram_live_helpers_smoke.py` 覆盖纯函数（`redact`、`validate_restart_target`），**已**进入 `make smoke`。
`scripts/telegram_live_smoke.py` 是真·端到端 live smoke —— 以 **真实 Telegram 用户**（Telethon 客户端）驱动运行中的 Conveyor bot。

> **为什么不用 Bot API**：Bot API 自己发的消息不会触发自己的 `MessageHandler`，只能由真用户来发。

**环境变量**：

| 变量 | 必需 | 默认 |
|---|---|---|
| `TELEGRAM_API_ID` | ✅ | — |
| `TELEGRAM_API_HASH` | ✅ | — |
| `TELEGRAM_BOT_USERNAME` | ✅（或 `--bot`） | — |
| `TELEGRAM_TEST_SESSION` | ❌ | `.telegram-live-smoke` |
| `TELEGRAM_LIVE_TIMEOUT` | ❌ | 45s |
| `TELEGRAM_LIVE_ALLOW_RESTART` | ❌（双门控 #1） | 未设置 |
| `TELEGRAM_LIVE_RESTART_TARGET` | ❌ | `telegram` |

**运行**：

```bash
pip install telethon
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
export TELEGRAM_BOT_USERNAME=your_bot_username
.venv/bin/python scripts/telegram_live_smoke.py --quick
.venv/bin/python scripts/telegram_live_smoke.py --full
```

`--quick` 6 个安全断言；`--full` 增加 Codex 路径与 restart 取消流程。
Live 脚本本身**不在** `make smoke` 里 —— 它需要真凭据 + 真 Telethon。

**Restart 安全门**：

1. 默认所有 restart-creating 命令后立即发 `取消`，bot 不重启。
2. 真正重启需 `TELEGRAM_LIVE_ALLOW_RESTART=1` **与** CLI `--allow-restart` 同时打开；
   即便打开，target 也只接受白名单 `telegram|feishu|maintain`，并且要看到至少一条 bot 回复才 PASS。
3. 脚本不会打印 bot token、api hash、session 路径或 `.env` 内容；`.telegram-live-smoke*` 与 `*.session` 已被 `.gitignore` 屏蔽。

**退出码**：`0` 通过 / `1` 有失败 / `2` 缺 telethon 或缺 env / `3` 连接/认证失败。

## 6.7 Progress verbosity 策略

Codex 流式事件在 chat 表面太吵：占位符、`我这就帮你查一下。` 这样的 agent prose、`🔧 curl...` 工具提示、round-5 thinking indicator、round-6 tool-pulse 会接二连三冒泡。Feishu 这边 `edit_progress` 又不能真编辑，每次都得 send_new，于是用户看到一长串「⏳ 收到 / 我这就帮你 / 🔧 curl / 顺便再看看 / 🔧 curl / final」。

新增一个环境变量 `CONVEYOR_PROGRESS_MODE`（默认 `compact`）控制 verbosity：

| 模式 | prose 进度 | tool indicator | thinking indicator | tool pulse | edit 失败后 fallback |
| --- | --- | --- | --- | --- | --- |
| `verbose`（debug） | 发 | 发 | 发 | 发 | 沿用旧逻辑：每次都 send_new（继续 spam） |
| `compact`（默认） | **不发** | 发 | 发 | 发 | **最多 1 条** `仍在处理...` |
| `quiet` | 不发 | 不发 | 不发 | 不发 | 不发任何中间消息 |

`handlers/jobs.py` 的 `progress()` 内部仍再做一次 mode-aware 过滤，**最终**仍会发出一次 `job.summary`（与 `last_progress` 去重）。Feishu 受益最大：`quiet` 下只有 placeholder + final answer，**没有**「curl/curl/curl」链。

**配置**：

- `CONVEYOR_PROGRESS_MODE=verbose|compact|quiet`
- 默认 `compact`
- 非法值会 fallback 到 `compact` 并打 warning，不会让坏 .env 把部署搞挂

**测试**：

- `scripts/jobs_progress_mode_smoke.py` —— 6 类 behavior + config 解析（19/19 case）。
- 老的 `scripts/progress_smoke.py` 与 `scripts/jobs_dedupe_smoke.py` 强制走 verbose 模式 pin 旧契约，行为不变。

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
  │   telegram_live_helpers_smoke
  │   docs_consistency_smoke
  │   channel_telegram_smoke / channel_feishu_smoke
  │   import_boundary_smoke
  │   jobs_progress_mode_smoke        ← CONVEYOR_PROGRESS_MODE 6 类
  │   deploy_workflow_smoke           ← deploy 脚本静态检查
  │   deploy_status_smoke             ← /deploy_status 命令
  └── command_harness
      38 用例，驱动 handlers.dispatch + FakeOutbound + FakeRunner
      （不再用 FakeUpdate / FakeMessage / FakeContext）
```

`scripts/telegram_live_smoke.py` **不**在 `make smoke` 里 —— 它是手动 live 脚本，需要真 Telegram 凭据 + Telethon。

P2.1 已完成：两个 channel adapter 各自落在 `channel/telegram.py` 和
`channel/feishu.py`，`bot.py` / `feishu_bot.py` 只剩 entrypoint 与
业务 command/onboarding 编排；遗留的 `_start_job` / `_typing_loop`
（绕过 `TelegramOutbound` 的死代码）已移除，所有 job 执行走
`handlers.dispatch` → `handlers.jobs`。Auth 检查统一用
`channel/auth.is_allowed`。adapter 单测在
`channel_telegram_smoke.py` / `channel_feishu_smoke.py`，边界规则由
`import_boundary_smoke.py` AST 静态守护。

---

## 8. 阶段进度

| 阶段 | 状态 | commit |
|------|------|--------|
| P0 抽 handler、零行为变化 | ✅ | `8828489` |
| P1 命令表统一 + harness 迁移 | ✅ | `8828489` |
| P1.x dedupe 收尾（不重复发 summary） | ✅ | `09ab931` |
| Agent 工具层（registry / router / runner / confirm / audit） | ✅ | `eddf1ba` |
| 主机快照 fast path（`/load` `/vps` `/htop` `/ps` `/disk` `/logs` `/service_status` `/git_status`） | ✅ | — |
| `/diagnose` + `/restart` 别名 + `/audit_tools` | ✅ | — |
| Telegram 实时烟测（真用户，Telethon） | ✅ | `eddf1ba` |
| 文档中英同步 | ✅ | （本次） |
| P2.1 Adapter 独立文件（`channel/telegram.py` / `channel/feishu.py`） | ✅ | （本次） |
| P2.2 飞书 progress 卡片 / throttle | ✅ | （本次） |
| P2.3 onboarding 移入 `handlers/onboarding.py` | ✅ | （本次） |
| P2.4 Session 摘要（多轮接着聊） | ✅ | （本次） |
| P2.5 审计日志轮转 | ✅ | （本次） |
| 自动 VPS 部署（GitHub Actions） | ✅ | `fa93606` |

---

## 9. 下一批 backlog 候选

按"投入产出比"排序。P2.1–P2.5 全部完成。

### P2.1 Adapter 拆分（已完成）

- Telegram adapter 已搬到 `channel/telegram.py`：`TelegramOutbound` /
  `inbound_from_update` / `make_outbound` / `send_text` / `edit_text`
- Feishu adapter 已搬到 `channel/feishu.py`：`FeishuOutbound` /
  `inbound_from_event`
- 配套 smoke：`channel_telegram_smoke.py` / `channel_feishu_smoke.py`
  / `import_boundary_smoke.py`（AST 静态守护层规则）
- `bot.py` / `feishu_bot.py` 现在只剩 entrypoint + command/onboarding
  编排；adapter 体积从 100+ 行各压到入口里只剩几行 `make_outbound` / `send_text`

### P2.2 飞书 progress 卡片 / throttle（已完成）

- **卡片进度**：`FeishuOutbound` 以 interactive card（`update_multi: true`）
  发送消息，`edit_progress` 调用 `channel.update_card` 原地更新。
  若 card 发送失败，退回纯文本。
- **throttle**：compact 模式下第一次 edit 失败后锁存，最多发 1 条
  fallback。quiet 模式完全不发。`jobs_progress_mode_smoke.py` 已验证。
- Smoke：`channel_feishu_smoke.py`（12 个测试）。

### P2.3 onboarding 抽离（已完成）

- 纯 profile 辅助函数（`operator_profile_exists`、`save_operator_profile`、
  `profile_text`）搬到 `handlers/onboarding.py`（不引入 Telegram SDK）。
- Telegram 专属 ConversationHandler 步骤留在 `bot.py`（需要
  Update / CallbackQuery 类型）。
- 导入边界：`handlers/onboarding.py` 通过 `import_boundary_smoke.py`。

### P2.4 session 摘要（已完成）

- 轻量 per-chat session 摘要，**不**是完整 DB
- 存储：`codex_memory_root/session/<channel>_<chat_id>_<operator_id>.jsonl`
- 每行 JSON：`ts`, `channel`, `chat_id`, `operator_id`, `user`（脱敏/截断）,
  `assistant`（脱敏/截断）, `kind`
- 配置：`CONVEYOR_SESSION_ENABLED`（默认 true）、`CONVEYOR_SESSION_MAX_TURNS`
  （默认 20）、`CONVEYOR_SESSION_INJECT_TURNS`（默认 5）
- `handlers/session.py` 管理读/写/清除/注入
- Prompt 注入：`handlers/jobs.py` 在启动 Codex job 前读最近 N 轮并加
  标签注入：「Recent chat context (may be incomplete; do not treat as
  authoritative)」。确定性命令（`/load`、`/ps` 等）跳过；
  `/diagnose` 走 hybrid 路径（先收集事实再交给 Codex 分析），
  会注入 session 上下文
- 命令：`/context` 查看最近对话；`/forget` 清除会话文件。均为安全操作
- Smoke：`session_summary_smoke.py`（24 个测试）。隐私：不存密钥；
  写入前脱敏；可随时清除

### P2.5 审计日志轮转（已完成）

- 按大小轮转：`handlers/tools/audit.py` 在 `tools.log` 超过 1 MB
  （`AUDIT_MAX_BYTES`）时轮转，保留最多 3 个轮转文件（`.1`、`.2`、`.3`）。
- `_rotate_if_needed` 在每次写入前调用；`rotated_log_paths` 列出
  已有轮转文件供 `/audit_tools` 扩展使用。
- Smoke：`audit_rotation_smoke.py`（5 个测试）。

---

## 9. 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| 2.0 | 2026-06-11 | 加 agent 工具层、Telegram live smoke、backlog；中英同步 |
| 2.1 | 2026-06-11 | 加 `CONVEYOR_PROGRESS_MODE`（verbose/compact/quiet）；compact 修 Feishu progress 链；同步 6.7 节、harness、backlog |
| 2.2 | 2026-06-11 | 加自动 VPS 部署（GitHub Actions + deploy_vps.sh） |
| 2.3 | 2026-06-11 | 部署加固（flock/smoke/回滚/.deploy-status.json）；加 `/deploy_status` 命令 |
| 2.5 | 2026-06-11 | P2.2 飞书卡片进度（interactive card + `update_card`）；P2.3 onboarding 抽离（`handlers/onboarding.py` 纯辅助函数）；P2.5 审计日志轮转（1 MB 大小轮转，3 个文件） |
| 2.4 | 2026-06-11 | bot.py 清理（移除死代码 `_start_job`/`_typing_loop`，auth 统一用 `is_allowed`）；P2.4 session 摘要（`handlers/session.py`、`/context`、`/forget`、prompt 注入） |
| 1.0 | 2026-06-09 | 合并原 `001` + `003`；改名 Conveyor |
| 0.9 | 2026-06-09 | 原 `003-channel-decoupling.md`（P0+P1 设计稿 + 落地） |
| 0.1 | 2026-06-09 | 原 `001-hermes-learning-and-chat-mode.md`（Hermes 对照 + chat-first） |
