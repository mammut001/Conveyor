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

单操作员私有 VPS：Codex 沙箱刻意设为 `danger-full-access`，不是多租户 SaaS。
安全靠 channel 白名单、低权用户、worktree 隔离、redaction，以及 `/diff` +
`/apply` 人工审查。收窄 sandbox 是未来加固项，不是当前行为。

| 触发 | JobMode | Codex `--sandbox` | 能力 |
|------|---------|-------------------|------|
| 纯文本 | `run` | `danger-full-access` | shell、web、读写 worktree、runner CLI |
| `/run` | `run` | 同上 | 同上 |
| `/fix` | `fix` | 同上 | 同上（与纯文本等价，保留命令兼容） |
| `记 xxx` / `/memo` | — | — | **不经过 Codex**，直接写 MEMORY.md |

**设计理由**：
- 个人 bot + 单 operator：read-only 边界带来的「查 IP 必须 `/fix`」不符合对话直觉
- 安全仍靠：channel 白名单、worktree 隔离、`/diff` + `/apply` 才合入主仓库、输出 redaction
- `/run` 与 `/fix` 保留仅为兼容旧习惯与 job 日志区分，**sandbox 已统一为 danger-full-access**

### 5.1 Prompt 注入顺序

每次 Codex 调用前的 prompt 拼接顺序（见 `runner/prefetch.py`）：

1. `<operator-profile>` — 身份、语言、风格
2. `<day-brief>` — 每天首个 job 的冷启动摘要
3. `<memory-context>` — 当日 `MEMORY.md`
4. `<tool-registry sandbox="danger-full-access">` — shell、memorize、recall 等
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

### Intent router (`handlers/intent.py` + `handlers/nl_router.py`)

- **Deterministic 优先**：显式 ops 请求（负载/htop/磁盘/日志）不走 hybrid
- **Hybrid**：`为什么服务器慢` / `分析一下 vps` → 默认采集 load+ps+disk+service_status，再把 facts 注入 Codex prompt
- **显式诊断**：`/diagnose [server|bot|logs|quick]`（`handlers/tools/diagnose.py` 定义各模式 tool 组合）；自然语言「诊断服务器」「帮我诊断 bot」保守匹配
- **NL 路由器回退**（P4.3）：intent.py 的模式匹配未命中时，回退到 `nl_router.classify_nl()` 处理额外领域（笔记搜索、提醒创建、日历忙闲、队列状态、设置状态）
- **工具目录**（P4.3）：`nl_router.get_catalog()` 从 host + personal registry 构建统一目录，用于路由和 `/nl_help`
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

### 6.7 Personal Tools Hub（P3.1 + P3.2 — 本地笔记/提醒 + 投递）

结构化个人工具基础层，为未来 Gmail / Calendar / Contacts / GitHub 预留扩展点。**OAuth token 不进入 Codex prompt**；Codex job 行为不变。

```
personal_tools/
  base.py      ToolResult / PersonalToolSpec / BasePersonalTool; DangerLevel 复用
  store.py     SQLite @ codex_memory_root/personal_tools.db（含 delivery 列迁移）
  registry.py  notes.* / reminders.* 注册 + 执行（传递 channel/chat_id 上下文）
  notes.py     笔记 CRUD
  reminders.py 提醒 CRUD + 简单时间解析
  reminder_parse.py  in 10m / in 2h / tomorrow HH:MM / ISO 解析
scripts/
  scheduler_tick.py   提醒投递调度器（每 60s 由 timer 触发）
  scheduler_probe.py  调度器探针（dry-run / live 模式）
systemd/
  conveyor-scheduler.service  oneshot: 运行 scheduler_tick.py
  conveyor-scheduler.timer    每 60s 触发
```

| 工具 | danger | 命令 | 说明 |
|---|---|---|---|
| notes.add | **WRITE_SAFE** | `/note` | 免确认；审计 |
| notes.search / notes.list_recent | READ | `/notes [query]` | |
| notes.delete | DESTRUCTIVE | （API；slash 未暴露） | 需确认 |
| reminders.create | **WRITE_SAFE** | `/remind` | 免确认；审计；存储 channel/chat_id |
| reminders.list / reminders.due | READ | `/reminders` | |
| reminders.cancel | WRITE | （API；slash 未暴露） | 需确认 |

**WRITE_SAFE 设计决策**：`notes.add` 和 `reminders.create` 是低风险 append/create 操作，
事后可通过 `notes.delete` / `reminders.cancel` 回退。强制确认会破坏「/remind in 10m X」的
流畅性。WRITE_SAFE = 不走确认流程，但 args + result preview 仍写入 `audit/tools.log` 并 redact。

提醒时间：`in 10m` / `in 2h` / `tomorrow HH:MM` / ISO。解析失败返回用法说明。

**P3.2 提醒投递**：`reminders` 表通过迁移扩展 `channel`、`chat_id`、`delivered_at`、
`delivery_status`、`delivery_error`、`retry_count` 列。迁移自动处理已有数据库。
`/remind` 创建时存储 `msg.channel` + `msg.chat_id`。`scheduler_tick.py` 每 60s
查找 `delivery_status IN ('pending','failed') AND due_at <= now AND channel != ''`
的记录，Telegram 直接调 `send_message`，Feishu 暂跳过并记日志。
成功标记 `delivered`，失败标记 `failed` + 递增 `retry_count`（>=3 停止重试）。
支持 `--dry-run` 模式（不发消息不写 DB）用于 smoke 测试。

**P3.2.1 调度器可观测性**：三个确定性工具让运维者从聊天中验证投递管线，无需 SSH：

| 工具 | danger | 命令 | 说明 |
|---|---|---|---|
| scheduler_status | READ | `/scheduler_status` | Timer/Service 状态 + journal 尾部 + 提醒统计 + 通道支持 |
| scheduler_probe | READ | `/scheduler_probe` | Dry-run 探测：运行 scheduler_tick --dry-run，不发消息不写 DB |
| scheduler_probe_live | WRITE | `/scheduler_probe_live` | 实时探针：创建测试提醒并真正投递到 Telegram，需确认 |

`scheduler_status_report()` 在 `systemctl` 不可用时优雅降级（macOS/CI）。
`scheduler_probe_live()` 创建一条 `[probe]` 提醒，运行 `run_tick(dry_run=False)`，然后查询 DB 验证 `delivery_status=delivered`。
所有输出经 `redact_text()` + `truncate()` 处理，不暴露 `.env` 或 token。

`notes.delete` / `reminders.cancel` 走 `handlers/tools/runner.py` 同一确认 + `audit/tools.log` redaction 路径。

**P3.3 Gmail App Password MVP**：使用 IMAP + SMTP 的保守 Gmail 集成，**不使用 OAuth**。

```
personal_tools/
  gmail_imap.py   IMAP 读取工具（gmail.status/recent/search/read）
  email_smtp.py   SMTP 发送工具（email.send，需确认）
```

| 工具 | danger | 命令 | 说明 |
|---|---|---|---|
| gmail.status | READ | `/gmail_status` | Gmail 连接状态 |
| gmail.recent | READ | `/gmail_recent [n]` | 最近邮件 |
| gmail.search | READ | `/gmail_search <query>` | 搜索邮件 |
| gmail.read | READ | `/gmail_read <id>` | 读取邮件 |
| email.send | WRITE | `/email_send <to> \| <subject> \| <body>` | 发送邮件（需确认） |

环境变量：`GMAIL_BACKEND=imap_smtp`、`GMAIL_ADDRESS`、`GMAIL_APP_PASSWORD`（16 字符 App Password）。
可选：`GMAIL_IMAP_HOST`/`PORT`、`GMAIL_SMTP_HOST`/`PORT`。

安全：App Password **绝不**暴露在聊天回复、日志、审计日志或 `repr()` 中（`SENSITIVE_FIELDS` 集合）。
发送需要 WRITE 确认。此阶段不支持删除/归档/标签操作。不下载附件。
`gmail.status` 在配置缺失时优雅降级。

**P3.4 Google Calendar + Contacts**：Google OAuth broker + 只读日历/联系人工具。Gmail 仍用 App Password，OAuth 仅用于 Calendar/Contacts。

```
personal_tools/
  google_oauth.py      OAuth broker（status/auth/revoke，token 存 secrets/）
  calendar_google.py   日历工具（status/today/tomorrow/week/search/freebusy/create）
  contacts_google.py   联系人工具（search）
```

| 工具 | danger | 命令 | 说明 |
|---|---|---|---|
| google.status | READ | `/google_status` | OAuth token 状态 |
| google.auth | WRITE | `/auth_google` | OAuth 授权流程 |
| google.revoke | DESTRUCTIVE | `/google_revoke` | 撤销并删除 token |
| calendar.status | READ | `/calendar_status` | Calendar 连接状态 |
| calendar.today | READ | `/calendar_today` | 今日日程 |
| calendar.tomorrow | READ | `/calendar_tomorrow` | 明日日程 |
| calendar.week | READ | `/calendar_week` | 本周日程 |
| calendar.search | READ | `/calendar_search <query>` | 搜索日程 |
| calendar.freebusy | READ | `/calendar_freebusy <range>` | 查询忙闲 |
| calendar.create | WRITE | `/calendar_create <标题> \| <时间> \| <描述>` | 创建日程（需确认） |
| contacts.search | READ | `/contacts_search <query>` | 搜索联系人 |

环境变量：`GOOGLE_CLIENT_SECRET_PATH`（必需）、`GOOGLE_TOKEN_PATH`（默认 `secrets/google_token.json`）、`GOOGLE_OAUTH_SCOPES`、`GOOGLE_OAUTH_REDIRECT_PORT`（默认 8765）。

安全：OAuth token 存储在 `codex_memory_root/secrets/google_token.json`，chmod 600。
token 不暴露在聊天、日志、审计或 `repr()` 中。API 错误经脱敏处理。
`calendar.create` 需要 WRITE 确认。`google.revoke` 是 DESTRUCTIVE。
依赖：`google-auth`、`google-auth-oauthlib`、`google-api-python-client`。

**TODO（后续 phase）**：Gmail OAuth、`github.*` 工具；token 存 VPS 侧加密 vault，Codex 仅见 tool 结果摘要。

**P3.5 Daily Briefing**：每日简报系统，聚合 Calendar、提醒、Gmail、笔记。

```
personal_tools/
  briefing.py          简报构建与调度（status/today/tomorrow/enable/disable/probe）
```

| 工具 | danger | 命令 | 说明 |
|---|---|---|---|
| briefing.status | READ | `/brief_settings` | 简报设置状态 |
| briefing.today | READ | `/brief_today` | 今日简报 |
| briefing.tomorrow | READ | `/brief_tomorrow` | 明日简报 |
| briefing.enable | WRITE_SAFE | `/brief_enable [HH:MM]` | 启用每日简报 |
| briefing.disable | WRITE | `/brief_disable` | 禁用每日简报（需确认） |
| briefing.probe | READ | `/brief_probe` | 简报探针（dry-run） |

简报内容：日历事件（需 Google OAuth）、到期提醒、最近邮件摘要（需 Gmail）、最近笔记。缺少配置时显示降级提示。

调度器集成：`scripts/scheduler_tick.py` 每分钟检查已启用的简报设置，在用户本地时间到达时发送简报。`briefing_runs` 表记录已发送日期，避免重复。

存储：`briefing_settings`（operator_id 主键，enabled/local_time/channel/chat_id）和 `briefing_runs`（operator_id + local_date 唯一约束）。

安全：`briefing.enable` 是 `WRITE_SAFE`（仅启用本地设置，审计），`briefing.disable` 是 `WRITE`（需确认）。无原始邮件正文、无 OAuth token、无密码。输出经 `redact_text()` + `truncate()` 处理。

自然语言意图：`今日简报` → `briefing.today`，`启用每日简报` → `briefing.enable`，`禁用简报` → `briefing.disable`。

Smoke：`scripts/briefing_smoke.py`（15 项：settings CRUD、runs、graceful degradation、enable/disable、probe、registry、commands、help/tools、intent routing、dedup、redaction）。

**P3.6 GitHub Issues/PR Tools**：只读的 GitHub 项目工具，支持 Issues、PRs 和 CI 状态。**此阶段不支持 merge/close/delete 操作。**

```
personal_tools/
  github_tools.py      GitHub REST 客户端（status/issues/prs/ci/create/comment）
```

|| 工具 | danger | 命令 | 说明 |
||---|---|---|---|
|| github.status | READ | `/github_status` | GitHub 连接状态 |
|| github.issues | READ | `/github_issues [state|query]` | 列出 Issues |
|| github.issue | READ | `/github_issue <number>` | 查看 Issue 详情 |
|| github.prs | READ | `/github_prs [state]` | 列出 PRs |
|| github.pr | READ | `/github_pr <number>` | 查看 PR 详情 |
|| github.ci | READ | `/github_ci [ref]` | CI 状态 |
|| github.create_issue | WRITE_SAFE | `/github_create_issue <标题> \| <正文>` | 创建 Issue（审计） |
|| github.comment | WRITE | `/github_comment <编号> \| <正文>` | 评论（需确认） |

环境变量：`GITHUB_TOKEN`（必需）、`GITHUB_DEFAULT_REPO`（必需）、`GITHUB_API_BASE`（可选，默认 `https://api.github.com`）。

安全：`github_token` 绝不暴露在聊天回复、日志、审计或 `repr()` 中。创建 Issue 是 `WRITE_SAFE`（审计），评论是 `WRITE`（需确认）。所有输出经 `redact_text()` + `truncate()` 处理。

自然语言意图：`看看 GitHub issue` → `github.issues`，`PR 状态` → `github.prs`，`CI 挂了吗` → `github.ci`，`创建 issue` → 引导输入详情。

Daily Briefing 集成：如果配置了 GitHub，简报会包含 open issue/PR 数量和默认分支 CI 状态。

Smoke：`scripts/github_smoke.py`（11 项：缺少配置、token 脱敏、命令解析、确认、registry、commands、help/tools、意图路由、简报降级、无网络）。

**P3.7 Natural Language Planner**：自然语言规划器，将现有确定性工具组合成有用的个人代理工作流。**不添加新的外部集成。所有 Planner profiles 都是只读的。**

```
personal_tools/
  planner.py             PlannerProfile dataclass + 5 profiles
```

| 工具 | danger | 命令 | 说明 |
|---|---|---|---|
| planner.list | READ | `/planners` | 列出所有 Planner |
| planner.today | READ | `/plan_today` | 今日优先级分析 |
| planner.dev | READ | `/plan_dev` | 开发计划 |
| planner.health | READ | `/planner_health` | Planner 健康检查 |
| planner.triage | READ | `/inbox_triage` | 邮件分类整理 |
| planner.schedule | READ | `/schedule_review` | 日程审查 |

每个 Planner profile 定义了：
- `tool_items`：要采集的 (tool_name, arg) 对列表（全部 READ）
- `prompt_template`：传递给 Codex 的分析模板
- `summary`：一句话描述

采集流程：`handle_hybrid()` → `run_tools_collected()` → Codex 分析。

安全：Planner profiles **只使用 READ 工具**。不发送邮件、不创建日历事件、不评论/创建 GitHub issue。所有采集事实经 `redact_text()` + `truncate()` 处理。

自然语言意图：`我今天应该先干啥` → `daily_priority`，`今天开发计划` → `dev_plan`，`项目健康状态` → `project_health`，`帮我整理邮件` → `inbox_triage`，`今天日程安排` → `schedule_review`。

Smoke：`scripts/planner_smoke.py`（9 项：registry、READ-only 验证、graceful degradation、prompt building、commands、自然语言路由、planner status）。

**P3.8 Codex Job Queue**：单并发 FIFO 队列，用于管理 Codex 任务。当 Codex 任务正在运行时，新任务会排队而不是被拒绝。**实际 Codex 执行仍然是单并发的。**

```
handlers/
  job_queue.py             JobQueue class + QueuedJob dataclass
  jobs.py                  Queue integration in handle_codex_job
```

| 命令 | 说明 |
|---|---|
| `/queue` | 查看队列状态 |
| `/queue_cancel <id>` | 取消队列任务 |
| `/queue_clear` | 清空队列 |
| `/queue_pause` | 暂停队列自动出队 |
| `/queue_resume` | 恢复队列自动出队 |

队列行为：
- 内存 FIFO 队列（bot 重启后丢失）。
- 最大队列长度：10 个任务。
- 当任务完成时，自动启动下一个队列任务。
- 队列仅存储 prompt 文本和路由元数据（无密钥）。
- 队列显示经过 `redact_text()` + `truncate()` 处理。
- 队列变更操作会记录审计日志。
- `/cancel` 仍然取消当前正在运行的任务。

安全性：**同一时间只有一个 Codex 进程。** 队列可通过 `/queue_pause` 暂停；暂停时完成的任务不会自动启动下一个。

确定性 READ 工具绕过队列，直接执行。

Smoke：`scripts/job_queue_smoke.py`（10 项：enqueue/dequeue、FIFO 顺序、最大长度、cancel、clear、pause/resume、状态显示、命令注册、help 文本、脱敏）。

**P3.9 通用项目管理（Project Profiles）**：通用的项目技能层，适用于任何用户的项目。用户定义项目配置文件，并运行通用项目命令。复用现有 Gmail、Calendar、GitHub、Notes、Reminders 工具。

```
personal_tools/
  store.py                 project_profiles + active_projects 表
  projects.py              项目工具实现
  registry.py              项目工具注册
  briefing.py              集成活跃项目到每日简报
handlers/
  commands.py              项目 slash 命令
  intent.py                项目自然语言路由
  tools/runner.py          handle_hybrid_project
```

| 命令 | 说明 |
|---|---|
| `/projects` | 列出项目 |
| `/project_add <名称> \| <类型> \| <描述> \| [github] \| [关键词]` | 添加项目（WRITE_SAFE，审计） |
| `/project_use <id>` | 设置活跃项目（WRITE_SAFE，审计） |
| `/project_show [id]` | 查看项目详情 |
| `/project_remove <id>` | 删除项目（DESTRUCTIVE，需确认） |
| `/project_status [id]` | 项目状态分析（hybrid） |
| `/project_health [id]` | 项目健康检查（hybrid） |
| `/project_roadmap [id]` | 项目路线图（hybrid） |
| `/project_next [id]` | 项目下一步行动（hybrid） |
| `/project_release_checklist [id]` | 发布清单（hybrid） |
| `/project_brief [id]` | 项目简报（hybrid） |

支持的项目类型：`generic`、`mobile_app`、`web_app`、`bot`、`library`、`research`、`course`、`business`。

项目分析命令是 READ-only 的。它们从已配置的集成收集事实，并使用项目类型特定的 prompt 进行 Codex 分析。如果集成未配置，会优雅降级。

安全性：
- 项目分析命令是 READ-only 的。
- `/project_add` 和 `/project_use` 是 WRITE_SAFE 的，会记录审计日志。
- `/project_remove` 是 DESTRUCTIVE 的，需要确认。
- 不会发送邮件、创建 GitHub issue/评论、创建日历事件。
- 所有收集的事实和输出都经过 `redact_text()` + `truncate()` 处理。
- 不会暴露 token、app password、.env 值或原始密钥。

每日简报集成：显示最多 3 个已启用项目的简短状态。如果没有配置项目，会优雅降级。

Smoke：`scripts/project_profiles_smoke.py`（23 项：CRUD、operator 隔离、活跃项目回退、danger 级别、确认要求、briefing 集成、命令注册、help 文本、脱敏）。

**P3.10 设置向导（Setup Wizard）**：让新用户在部署后更容易配置 Conveyor。检查现有集成并引导用户完成设置。

```
personal_tools/
  setup.py                 设置工具实现
handlers/
  commands.py              设置 slash 命令
```

| 命令 | 说明 |
|---|---|
| `/setup` | 配置状态概览 |
| `/setup_status` | 同 /setup |
| `/setup_check` | 设置检查清单 |
| `/setup_project` | 项目配置指南 |
| `/setup_gmail` | Gmail App Password 配置指南 |
| `/setup_google` | Google OAuth 配置指南 |
| `/setup_github` | GitHub Token 配置指南 |

设置检查包括：
- Telegram Bot 已配置
- Allowed User ID 已配置
- Codex Binary 可用
- Workspace Root 存在
- Gmail (IMAP) 已配置
- Google OAuth 已配置
- GitHub Token/Repo 已配置
- Daily Briefing 已启用
- 活跃项目已配置

安全性：
- 所有设置命令都是 READ-only 的。
- 永远不会打印 token 值、app password、.env 内容或原始密钥。
- 所有输出经过 `redact_text()` + `truncate()` 处理。
- 无网络调用。

Smoke：`scripts/setup_smoke.py`（13 项：缺失集成、配置状态、项目示例、gmail 警告、github 无 token 泄漏、命令注册、help 文本、工具列表、无网络调用）。

**P3.11 项目导入/导出（Project Import/Export）**：使项目配置文件可移植，更容易设置。支持导入、导出和模板功能。

```
personal_tools/
  project_io.py              导入/导出/模板工具实现
handlers/
  commands.py                项目导入/导出命令
scripts/
  project_io_smoke.py        烟测
```

| 命令 | 说明 | Danger Level |
|------|------|--------------|
| `/project_export [id]` | 导出指定项目为 JSON | READ |
| `/project_export_all` | 导出所有项目 | READ |
| `/project_import <JSON>` | 从 JSON 导入项目 | WRITE_SAFE |
| `/project_template [type]` | 查看项目模板 | READ |

导出 JSON Schema：
```json
{
  "schema": "conveyor.project.v1",
  "projects": [
    {
      "name": "...",
      "type": "mobile_app|web_app|bot|library|research|course|business|generic",
      "description": "...",
      "github_repo": "...",
      "appstore_url": "...",
      "keywords": ["..."],
      "notes_query": "...",
      "gmail_query": "...",
      "default_branch": "...",
      "enabled": true
    }
  ]
}
```

安全性：
- 导出不包含内部 DB ID、operator_id、tokens、secrets、OAuth 路径或 .env 值。
- 导入验证 schema 和项目类型。
- 重复项目名称不覆盖（跳过）。
- 导入作用域为 operator_id。
- 导入的项目仅在无活跃项目时才设为活跃。
- 导出/模板为 READ-only，导入为 WRITE_SAFE。
- 所有输出经过 `redact_text()` + `truncate()` 处理。

Smoke：`scripts/project_io_smoke.py`（15 项：导出单个/全部项目、无 ids/operator_id、有效 JSON 导入、跳过重复、设置活跃项目、验证 schema/类型、模板显示、命令注册、help 文本、无网络调用、输出 redacted）。

**P4.1 Web 搜索 + 研究（Web Search + Research）**：为 Conveyor 添加外部 Web/研究能力。三层安全架构：Web Fetch → Web Search → Research。

```
personal_tools/
  web_fetch.py               Web Fetch MVP（curl 包装器）
  web_search.py              Web Search（多后端支持）
  research.py                Research（混合搜索+获取+Codex 综合）
handlers/
  commands.py                Web/Research 命令
scripts/
  web_tools_smoke.py         Web 工具烟测
  research_smoke.py          Research 烟测
```

**Phase A — Web Fetch MVP**：

| 命令 | 说明 | Danger Level |
|------|------|--------------|
| `/web_fetch <url>` | 获取网页内容 | READ |
| `/web_text <url>` | 获取网页文本 | READ |
| `/web_headers <url>` | 获取 HTTP headers | READ |

URL 验证：
- 拒绝非 http/https 协议（file://, ftp:// 等）
- 拒绝 localhost、127.0.0.0/8、0.0.0.0、::1
- 拒绝私有 IP：10.0.0.0/8、172.16.0.0/12、192.168.0.0/16
- 拒绝运营商级 NAT：100.64.0.0/10
- 拒绝基准测试范围：198.18.0.0/15
- 拒绝多播地址：224.0.0.0/4、ff00::/8
- 拒绝保留地址：240.0.0.0/4
- 拒绝链路本地：169.254.0.0/16、fe80::/10
- 拒绝 IPv6 ULA：fc00::/7
- 显式拦截元数据端点：169.254.169.254、metadata.google.internal
- 解析主机名并拒绝私有/保留 IP 结果

Curl 安全：
- `--fail --silent --show-error --no-location`（禁用自动重定向）
- `--connect-timeout 5`
- `--max-time`（默认 10 秒）
- `--max-filesize`（默认 2MB）
- `--proto =http,https`（无 cookies、无 auth headers、无文件写入）
- `shell=False`（subprocess 安全）
- Content-Type 验证：仅允许 text/*、application/json、application/xml（HEAD 和 GET 响应均验证）
- WEB_SEARCH_ENDPOINT 验证：拒绝 localhost/私有 IP/链路本地/元数据端点

Web Search 安全（P4.1.1）：
- 使用 urllib.request 替代 curl 子进程，避免 API 密钥暴露在进程参数中
- API 密钥通过 HTTP 头传递，不在 URL 或命令行参数中
- 所有错误信息经过 redact_text() 处理

**Phase B — Web Search**：

| 命令 | 说明 | Danger Level |
|------|------|--------------|
| `/web_search <查询>` | Web 搜索 | READ |

支持后端（`WEB_SEARCH_BACKEND`）：
- `disabled`（默认）— 禁用搜索
- `brave` — Brave Search API
- `tavily` — Tavily Search API
- `serper` — Serper.dev API
- `searxng` — 自托管 SearXNG 实例

**Phase C — Research**：

| 命令 | 说明 | Danger Level |
|------|------|--------------|
| `/research <问题>` | Web 研究 | READ |
| `/project_research [id] <问题>` | 项目相关研究 | READ |

Research 流程：
1. 运行 web.search 获取搜索结果
2. 去重域名
3. 获取 top N 安全 URL 的内容
4. 构建证据包（source title/url/snippet/text excerpt）
5. 返回 `[HYBRID_PROMPT]` 标记，由 Codex 混合合成生成研究报告
6. 不使用 WRITE 工具

项目研究（`/project_research`）：
- 使用项目名称、类型、描述、关键词、github_repo 作为搜索上下文
- 不修改项目配置
- 无活跃项目时优雅降级

自然语言路由：
- `搜索 Python asyncio` → web.search
- `研究一下 AI 编程助手` → research.run
- `获取网页 https://example.com` → web.fetch
- 无 URL/查询时会用中文提示用户提供

安全性：
- 所有工具都是 READ-only
- 不发送邮件、不创建日历事件、不写 GitHub
- 无文件写入、无任意 curl、无 JS 执行
- 所有输出经过 `redact_text()` + `truncate()` 处理
- 不暴露 tokens、API keys、cookies、auth headers
- Smoke 测试中无真实网络调用

配置变量：
| 变量 | 默认 | 说明 |
|------|------|------|
| `WEB_FETCH_ENABLED` | true | 启用 Web Fetch |
| `WEB_FETCH_TIMEOUT_SECONDS` | 10 | 超时秒数 |
| `WEB_FETCH_MAX_BYTES` | 2000000 | 最大字节数 |
| `WEB_FETCH_MAX_REDIRECTS` | 3 | 最大重定向 |
| `WEB_USER_AGENT` | ConveyorBot/0.1 | User-Agent |
| `WEB_SEARCH_BACKEND` | disabled | 搜索后端 |
| `WEB_SEARCH_API_KEY` | — | 搜索 API key |
| `WEB_SEARCH_ENDPOINT` | — | 自定义端点 |
| `WEB_SEARCH_MAX_RESULTS` | 8 | 最大结果数 |
| `RESEARCH_MAX_SOURCES` | 5 | 最大来源数 |
| `RESEARCH_FETCH_TOP_N` | 5 | 获取 top N |
| `RESEARCH_MAX_CHARS_PER_SOURCE` | 6000 | 每来源最大字符 |

Smoke：
- `scripts/web_tools_smoke.py`（31 项：URL 验证、curl 安全、html_to_text、输出 redacted、工具 danger level、命令注册、help 文本、禁用降级、重定向安全、Content-Type 验证、endpoint 验证、URL 编码、API 密钥安全、扩展 IP 拦截）
- `scripts/research_smoke.py`（14 项：搜索禁用降级、结果规范化、证据包、READ-only 工具、项目研究降级、域名去重、输出 redacted、混合提示词）

**P4.2 文件搜索 + 知识库（File Search / Knowledge Base）**：自然语言优先的文件搜索，自动收集 READ-only 事实。斜杠命令作为后备/调试。

```
personal_tools/
  file_search.py             文件搜索（安全边界）
  kb.py                      知识库（SQLite FTS5）
handlers/
  commands.py                文件搜索/KB 命令
  intent.py                  自然语言路由
scripts/
  file_search_smoke.py       文件搜索烟测
```

**文件搜索（files.search / files.read）：**

| 命令 | 说明 | Danger Level |
|------|------|--------------|
| `/files_roots` | 列出搜索根目录 | READ |
| `/files_search <查询词>` | 搜索文件 | READ |
| `/files_read <文件路径>` | 读取文件 | READ |

安全边界：
- 仅允许搜索配置的根目录：CODEX_WORKSPACE_ROOT、CODEX_MEMORY_ROOT/notes、KB_ROOT、FILE_SEARCH_ALLOWED_ROOTS
- 拒绝敏感文件：.env、secrets/、.ssh/、私钥、token 文件、google_token.json、client_secret.json
- 拒绝二进制文件（.png、.pdf、.zip 等）
- 拒绝超大文件（超过 FILE_SEARCH_MAX_FILE_BYTES）
- 无路径遍历（使用 resolve() 验证）
- 所有输出经过 redact_text() + truncate() 处理

**知识库（kb.index / kb.search）：**

| 命令 | 说明 | Danger Level |
|------|------|--------------|
| `/kb_index` | 索引知识库 | WRITE_SAFE |
| `/kb_status` | 知识库状态 | READ |
| `/kb_search <查询词>` | 搜索知识库 | READ |

索引存储：
- `indexed_files` 表：path, root, size, mtime, sha256, ext, updated_at
- `file_chunks` 表：file_id, chunk_index, text, text_hash
- 使用 SQLite FTS5（如果可用），否则 LIKE 回退
- 增量索引：仅索引新/修改的文件（基于 SHA256）

**项目文档搜索（/project_docs）：**

| 命令 | 说明 | Danger Level |
|------|------|--------------|
| `/project_docs <查询词>` | 搜索项目文档 | READ |
| `/project_kb_search [id] <查询词>` | 搜索项目知识库 | READ |

- 使用项目名称、类型、描述、关键词作为搜索上下文
- 不修改项目配置
- 无活跃项目时优雅降级

**自然语言路由：**
- `找一下文档里关于 deploy 的说明` → files.search "deploy"
- `README 里有没有 Gmail 配置步骤` → files.search "Gmail 配置步骤"
- `项目文档怎么说 scheduler` → files.search "scheduler"
- `根据本地文档总结安装流程` → files.search "安装流程"
- `查一下我 notes 里关于 OAuth 的内容` → files.search "OAuth"
- 无查询词时会用中文提示用户提供

**自动事实收集（collect_file_facts）：**
1. 先搜索 KB（如果已索引）
2. 回退到直接文件搜索
3. 读取 top N 安全代码片段
4. 构建证据包（path + excerpt）
5. 返回混合提示词给 Codex 综合

安全性：
- 所有文件/KB 分析命令为 READ-only
- `kb.index` 为 WRITE_SAFE（审计）
- 不暴露 secrets、tokens、API keys
- 不包含完整大文件在提示词中
- 不发送邮件、不创建 GitHub issues、不写日历事件
- Smoke 测试中无真实网络调用

配置变量：
| 变量 | 默认 | 说明 |
|------|------|------|
| `FILE_SEARCH_ENABLED` | true | 启用文件搜索 |
| `FILE_SEARCH_ALLOWED_ROOTS` | — | 额外允许的搜索根目录 |
| `FILE_SEARCH_MAX_FILE_BYTES` | 1000000 | 最大文件大小 |
| `FILE_SEARCH_MAX_RESULTS` | 10 | 最大结果数 |
| `FILE_SEARCH_EXTENSIONS` | .md,.txt,.py,.ts,.tsx,.js,.json,.yaml,.yml,.toml | 允许的扩展名 |
| `KB_ROOT` | CODEX_MEMORY_ROOT/kb | 知识库根目录 |
| `KB_INDEX_PATH` | CODEX_MEMORY_ROOT/kb_index.sqlite | 索引数据库路径 |

Smoke：
- `scripts/file_search_smoke.py`（14 项：允许根目录、路径遍历拒绝、.env 拒绝、secrets 目录拒绝、私钥模式 redacted、二进制跳过、超大跳过、files.search 返回片段、files.read 返回截断、kb.index 创建索引、kb.search 工作、NL 路由触发收集器、项目文档降级、无网络调用）

**P4.3 自然语言 Agent 路由器（Natural Language Agent Router）**：自然语言优先，斜杠命令作为后备。用户可以用正常语言调用大多数注册工具。

```
handlers/
  nl_router.py                 NL 路由层 + 工具目录
  intent.py                    集成 nl_router 作为回退
  commands.py                  /nl_help 命令
scripts/
  nl_router_smoke.py           NL 路由器烟测
```

**工具目录（Tool Catalog）：**
- 从 host TOOL_REGISTRY + personal PERSONAL_TOOL_REGISTRY 构建
- 每个条目包含：name, summary, danger, keywords, examples_zh, examples_en, domain
- 用于路由匹配和 /nl_help 输出

**路由分类：**

| 分类 | 说明 | 执行策略 |
|------|------|----------|
| READ_DETERMINISTIC | 直接读取工具 | 自动执行 |
| READ_HYBRID | 收集事实 + Codex 综合 | 自动收集，Codex 综合 |
| WRITE_PREVIEW | 写入操作 | 预览 + 确认 |
| CLARIFY | 缺少参数 | 自然语言追问 |
| CODEX_LLM | 编码/开放任务 | Codex 处理 |

**扩展 NL 覆盖（P4.3 新增）：**
- 笔记搜索：`搜索笔记里的 deploy` → notes.search
- 提醒创建：`提醒我明天9点开会` → reminders.create
- 日历忙闲：`下午有空吗` → calendar.freebusy
- 队列状态：`队列状态` → scheduler_status
- 设置状态：`配置状态` → setup.status

**安全策略：**
- READ 工具可自动执行
- WRITE_SAFE 工具（notes.add、reminders.create）自动执行但有审计日志
- WRITE/DESTRUCTIVE 工具必须先预览再确认，不会自动执行
- 模糊的编码请求优先走 Codex LLM
- 缺少参数时用自然语言追问，不建议斜杠格式
- 确认消息不包含斜杠命令格式建议

**/nl_help 输出：**
按领域分组列出自然语言示例，包括：运维、笔记、提醒、邮件、日历、联系人、简报、GitHub、规划、项目、设置、Web、研究、文件、知识库、调度。

Smoke：
- `scripts/nl_router_smoke.py`（25 项：目录构建、目录字段、NL 路由日历/邮件/GitHub/KB/研究/笔记/提醒/队列/设置、模糊编码走 LLM、确认消息无斜杠、/nl_help 输出、/nl_help 领域分组、/nl_help 注册、WRITE_SAFE 标记、READ 标记、项目模式、NL 示例、斜杠命令可导入、编码守卫、笔记添加、Web 搜索、Gmail 搜索）

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
