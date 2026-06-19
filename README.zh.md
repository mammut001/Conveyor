# Conveyor

一个小巧的个人 transport 层，桥接一个白名单用户（Telegram 和/或 Feishu）
到 VPS 上运行的 [`codex`](https://github.com/openai/codex) CLI。在手机上
敲一句话，把 Codex 的回答拿回来；`记 xxx`（写进记忆）、`/status`、`/diff`、
`/run` —— 所有命令都行。单 operator、单 VPS、不是 SaaS。

> Conveyor 是 **transport 层**，不是 agent。Agent 是 Codex CLI 自己。
> 完整设计见 [`docs/architecture.md`](docs/architecture.md)。

---

## 1. 快速开始（10 分钟）

**前置条件：** Ubuntu VPS、SSH 访问、已安装 [`codex` CLI](https://github.com/openai/codex)。

### 1.1 安装（在笔记本上操作）

```bash
git clone https://github.com/mammut001/conveyor.git && cd conveyor
sudo bash scripts/install.sh
```

安装脚本会：
1. 安装系统依赖
2. 同步代码到 `/opt/conveyor`
3. 创建 Python `.venv`
4. 提示配置 `.env`
5. 安装并启动 systemd 服务

### 1.2 配置 `.env`

编辑 `/opt/conveyor/.env`（完整选项见 [`.env.example`](.env.example)）：

```bash
sudo nano /opt/conveyor/.env
```

**最低配置：**
```dotenv
TELEGRAM_BOT_TOKEN=123456789:从BotFather获取
TELEGRAM_ALLOWED_USER_ID=你的用户ID
CODEX_WORKSPACE_ROOT=/path/to/your/repo
```

### 1.3 重启并测试

```bash
sudo systemctl restart conveyor-telegram-bot
sudo systemctl status conveyor-telegram-bot
```

打开 Telegram，给机器人发 `/start`，搞定！

### 1.4 更新（后续）

```bash
cd conveyor && git pull
sudo bash scripts/install.sh --update
```

### 1.5 可选：飞书机器人

见 [§2. 飞书机器人接入](#2-飞书机器人--完整接入) 添加第二个 channel。

---

## 2. 飞书机器人 — 完整接入

`conveyor` 装好之后，只要再配一下飞书这一侧。

### 2.1 拿凭证

打开[飞书开放平台](https://open.feishu.cn/app) → 创建应用 →

- **凭证与基础信息** → 抄下 `App ID` 和 `App Secret`

这两个值会写到 `.env` 的 `LARK_APP_ID` 和 `LARK_APP_SECRET`（见 §2.5）。

### 2.2 启用机器人能力

**应用能力 → 机器人 → 启用**（默认是关的——不打开的话事件列表里不会有
`im.message.receive_v1`）。

### 2.3 开权限（按需）

**权限管理** → 搜索并添加：

| 搜索关键字 | 权限 scope | 用途 |
|------------|-----------|------|
| `p2p` | `im:message.p2p_msg:readonly` | 收私聊消息 |
| `send_as_bot` | `im:message:send_as_bot` | 以机器人身份发消息 |
| `group_at` | `im:message.group_at_msg:readonly` | 群 @ 机器人（可选） |
| `user.id` | `contact:user.id:readonly` | 解析发送者（推荐） |

### 2.4 订阅事件

- **事件订阅** → 订阅方式 = **长连接 / persistent connection**
- **保存订阅前 bot 必须先跑起来**（本地 `feishu_bot.py` 在跑）
- 添加事件：`im.message.receive_v1`（接收消息），订阅身份选 **应用身份**
  （不是机器人身份 —— 那是另一种长连接）

### 2.5 配 `.env`

编辑 `/opt/conveyor/.env`，加这几行（占位符，别写真值）：

```dotenv
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=replace_me
# LARK_ALLOWED_OPEN_ID=ou_xxx
```

`LARK_ALLOWED_OPEN_ID` 不写的时候，飞书 bot 处于 **bootstrap 模式**：任意
发送者都能收到回信，回信里带发送者的 `open_id`，让你填进 `.env` 然后
重启。这是一次性握手，省去你从日志里捞 ID 的麻烦。

写好 open_id 之后：

```bash
sudo systemctl restart conveyor-feishu-bot
sudo journalctl -u conveyor-feishu-bot -f
```

看到 `connected to wss://msg-frontier.feishu.cn/ws/v2...` 就连上了。

### 2.6 发版并安装到企业

**版本管理与发布** → 新建版本 → 审核（内部应用一般秒过）→ **申请发布**
→ **安装到企业**。**加了权限 scope 必须重新发版**才能在现网生效。

### 2.7 一次性核对表

- [ ] 凭证：`LARK_APP_ID` 和 `LARK_APP_SECRET` 在 `.env` 里
- [ ] **应用能力 → 机器人 → 启用**
- [ ] `im:message.p2p_msg:readonly` 已开
- [ ] `im:message:send_as_bot` 已开
- [ ] （可选）`im:message.group_at_msg:readonly` 已开
- [ ] （可选）`contact:user.id:readonly` 已开
- [ ] 事件订阅：方式 = **长连接**，身份 = **应用身份**，事件 = `im.message.receive_v1`
- [ ] 保存订阅前本地 bot 在跑
- [ ] 新版本创建、发布、安装到企业
- [ ] VPS `.env` 有 `LARK_*`（仓库里没有真实秘钥）
- [ ] VPS `pip install -r requirements.txt` 跑过（含 `lark-oapi>=1.4.0`）
- [ ] VPS 3 个 systemd unit 装好且 active
- [ ] `journalctl -u conveyor-feishu-bot -f` 显示 `wss://msg-frontier.feishu.cn/...` 已连
- [ ] 给 bot 发私聊 → 收到 bootstrap 回信 → 把 `LARK_ALLOWED_OPEN_ID` 填进 `.env` → 重启

---

## 3. `.env` 全字段

同一份 `.env` 两个通道共用。只跑 Telegram 或只跑 Feishu 的话，
另一侧的字段空着就行。

```dotenv
# --- Telegram (bot.py) ---
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_ALLOWED_USER_ID=123456789

# --- Feishu (feishu_bot.py) ---
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=replace_me
LARK_ALLOWED_OPEN_ID=ou_xxx

# --- Codex（两个通道共用）---
CODEX_WORKSPACE_ROOT=/srv/my-repo
CODEX_BIN=/usr/local/bin/codex
CODEX_TASK_ROOT=/srv/conveyor

# LLM 提供商 — OPENAI_API_KEY / MINIMAX_API_KEY 至少有一个
# OPENAI_API_KEY=sk-replace_me
# MINIMAX_API_KEY=sk-replace_me
# MINIMAX_BASE_URL=https://api.minimaxi.com/v1

# --- Operator 个人档（onboarding）---
# 四个都可选；config.py 有默认值。
# OPERATOR_NAME=
# OPERATOR_LANGUAGE=zh-CN
# OPERATOR_STYLE=terse
# OPERATOR_STANDING=personal-scale, single operator

# --- 可选调参 ---
# USER_TIMEZONE=Asia/Shanghai
# TELEGRAM_PROGRESS_SECONDS=3
# CODEX_RETRY_429_DELAYS_SECONDS=300,900,1800
# CODEX_MODEL=
# CODEX_TIMEOUT_SECONDS=3600
```

`CODEX_WORKSPACE_ROOT` 必须是 git 仓库的根目录。bot 会为每天创建一个
detached worktree，job 日志写在 `CODEX_TASK_ROOT` 下。

改完 `.env` 之后 `chmod 600 .env`。systemd unit 通过 `EnvironmentFile=`
读它，永远不回显值。

---

## 4. 命令表（Telegram + Feishu 同表）

- 纯文本 → 跑 Codex（和 `/run` 一样）
- `/run <prompt>` 和 `/fix <prompt>` 等价；都用 `danger-full-access` 沙箱
- `/status` / `/last` / `/jobs [n]` — 当前 / 最近的任务
- `/diff` — `git status` + 最近 worktree 的 diff 预览
- `/apply` — 把最近 worktree 合回主仓库（仅当主仓库干净）
- `/discard` — 丢掉最近 worktree
- `/cancel` — 终止正在跑的 Codex 进程
- `/clean [keep]` / `/maintain [keep]` — 清旧任务和 worktree
- `/health [full] [json] [nosecurity]` — 紧凑的健康快照
- `/doctor` / `/diag [since]` — 全套后端检查 + 一行诊断结论
- `/audit [stale-min]` / `/security [since]` / `/ratelimit [n]` — 审计与报表
- `/metrics [n]` / `/log [sel]` / `/meta [sel]` — 趋势、日志摘要、`job.json` 边车
- `/smoke` / `/editcheck` — 端到端 / 真实编辑自检
- `/memo <内容>` / `记 <内容>` — 写到当天 `MEMORY.md`（不走 Codex）
- `/memory [date] [category]` / `/journal [n]` — 读 `MEMORY.md` 和归档
- `/note <内容>` — 保存本地笔记（**WRITE_SAFE**，立即执行，审计）
- `/notes [关键词]` — 列出/搜索笔记
- `/remind <内容+时间>` — 创建本地提醒（**WRITE_SAFE**，立即执行，审计）
- `/reminders` — 列出提醒
- `/scheduler_status` — 提醒调度器状态报告
- `/scheduler_probe` — dry-run 探测（不发消息，不写 DB）
- `/scheduler_probe_live` — 实时投递测试（**WRITE**，需确认）
- `/help` — 完整命令列表

### Agent 工具层

Conveyor **不是**纯硬编码命令 bot。结构化 tool registry + 轻量 intent router 位于聊天输入与 Codex 之间：

| 路径 | 何时 | 示例 |
|---|---|---|
| **Deterministic** | 稳定主机检查 | `看看磁盘`、`/logs`、`git status` |
| **Hybrid** | 诊断 / “为什么”类问题 | `/diagnose server`、`为什么服务器这么慢` → 采集事实 + Codex 分析 |

**显式 hybrid 诊断：** `/diagnose [server|bot|logs|quick]`（默认 `server`）按模式采集工具事实，再由 Codex 用中文给出可能原因、严重程度和下一步安全操作建议。与 `/diag`（job/运行时诊断 harness）不同。

**重启别名：** `/restart telegram|feishu|maintain` 映射白名单 systemd 单元，确认流程同 `service_restart`（内联按钮或「确认执行」）。

**确认绑定：** pending 危险操作绑定 `operator_id + chat_id + channel`；跨会话确认会被拒绝。

**审计日志：** WRITE/DESTRUCTIVE 事件写入 `codex_memory_root/audit/tools.log`（JSONL）。`/audit_tools [n]` 查看最近 redact 后的记录（只读）。
| **LLM** | 开放式编码 / 调试 | `写个 quicksort`、`修这个测试` |

已注册工具（`/tools` 列出全部）：

| 工具 | 危险级别 | 行为 |
|---|---|---|
| `load` | 只读 | 主机负载/内存/CPU/top 进程 |
| `ps` | 只读 | Top 进程（默认 comm 模式） |
| `htop` | 只读 | 非交互 top 一帧 |
| `disk` | 只读 | `/ /srv /opt` 的 df |
| `logs` | 只读 | conveyor 服务 journal 尾部 |
| `service_status` | 只读 | conveyor systemd 单元状态 |
| `git_status` | 只读 | workspace git status |
| `service_restart` | **写（需确认）** | 重启 conveyor systemd 单元 |
| `scheduler_status` | 只读 | 提醒调度器状态报告 |
| `scheduler_probe` | 只读 | 调度器 dry-run 探测 |
| `scheduler_probe_live` | **写（需确认）** | 调度器实时投递测试 |

安全：**写/破坏性工具必须显式确认**（Telegram 内联按钮；飞书/文本须用明确短语如 `确认执行` / `confirm` — 随意的 `好` / `ok` / `是` **不会**被接受）。确认绑定 originating chat + channel；事件写入 `audit/tools.log`。

实现：`handlers/tools/`（registry + executors + runner）、`handlers/intent.py`（`route_intent`）。Handler 保持通道无关；Telegram callback 用 `tool:confirm:<token>` / `tool:cancel:<token>`。

### Personal Tools Hub（P3.1 + P3.2 — 本地笔记/提醒 + 投递）

为未来 Gmail / Calendar / Contacts / GitHub 集成打基础。**OAuth token 不进入 Codex prompt**，只在 VPS 服务端执行。

| 存储 | `$CODEX_MEMORY_ROOT/personal_tools.db`（SQLite） |
|---|---|
| 笔记 | `/note`、`/notes` → `notes.add/search/list_recent/delete` |
| 提醒 | `/remind`、`/reminders` → `reminders.create/list/cancel/due` |

**危险级别与 UX 选择：**

| 工具 | 级别 | 需确认？ | 理由 |
|---|---|---|---|
| `notes.add` | WRITE_SAFE | 否 | append-only，低风险，可 `notes.delete` 回退 |
| `reminders.create` | WRITE_SAFE | 否 | 同上；确认会打断 `/remind in 10m X` 的流畅性 |
| `notes.delete` | DESTRUCTIVE | 是 | 破坏性——删除数据 |
| `reminders.cancel` | WRITE | 是 | 修改状态——需意图检查 |
| `notes.search` / `list_recent` / `reminders.list` / `due` | READ | 否 | 只读 |

`WRITE_SAFE` = 立即执行，args + result preview 写入 `audit/tools.log` 并 redact。
无需交互确认，因为是个人 append/create 操作；事后可 delete/cancel。

提醒时间解析：`in 10m`、`in 2h`、`tomorrow HH:MM`、ISO 时间。
解析失败返回用法说明。`notes.delete` 和 `reminders.cancel` 复用主机工具同一套
确认 + `audit/tools.log` redaction。

**P3.2 — 提醒投递：** `/remind` 创建提醒时，bot 存储 `msg.channel`（telegram/feishu）和
`msg.chat_id`。systemd timer（`conveyor-scheduler.timer`）每 60 秒运行
`scripts/scheduler_tick.py`，查找到期提醒并通过 Telegram 发送投递消息。投递状态
按提醒追踪（`pending` → `delivered`/`failed`）；失败提醒最多重试 3 次。无 `channel`/`chat_id`
的旧提醒（P3.2 前记录）会被调度器跳过。

**P3.2.1 — 调度器可观测性：** 三个确定性工具让运维者从聊天中验证投递管线，无需 SSH：

| 工具 | 级别 | 命令 | 说明 |
|---|---|---|---|
| `scheduler_status` | READ | `/scheduler_status` | Timer/Service 状态 + journal 尾部 + 提醒统计 + 通道支持 |
| `scheduler_probe` | READ | `/scheduler_probe` | Dry-run 探测：运行 scheduler_tick --dry-run，不发消息不写 DB |
| `scheduler_probe_live` | WRITE | `/scheduler_probe_live` | 实时投递测试到 Telegram（需确认） |

`scheduler_status_report()` 在 `systemctl` 不可用时优雅降级（macOS/CI）。
`scheduler_probe_live()` 创建一条 `[probe]` 提醒，投递后验证 DB 中 `delivery_status=delivered`。
所有输出经 `redact_text()` + `truncate()` 处理，不暴露 `.env` 或 token。

代码：`personal_tools/`（`base`、`store`、`registry`、`notes`、`reminders`、`reminder_parse`）。
调度器：`scripts/scheduler_tick.py`。
探针：`scripts/scheduler_probe.py`。
Smoke：`scripts/personal_tools_smoke.py`（24 项）。
Smoke：`scripts/scheduler_probe_smoke.py`（7 项：registry、commands、无 systemctl 降级、dry-run、live 确认、/tools、/help）。

**P3.3 — Gmail App Password MVP：** 使用 IMAP + SMTP 的保守 Gmail 集成。**OAuth 是未来阶段。**

| 工具 | 级别 | 命令 | 说明 |
|---|---|---|---|
| `gmail.status` | READ | `/gmail_status` | Gmail 连接状态 |
| `gmail.recent` | READ | `/gmail_recent [n]` | 最近邮件 |
| `gmail.search` | READ | `/gmail_search <query>` | 搜索邮件 |
| `gmail.read` | READ | `/gmail_read <id>` | 读取邮件 |
| `email.send` | WRITE | `/email_send <to> \| <subject> \| <body>` | 发送邮件（需确认） |

环境变量：

| 变量 | 必需 | 默认值 |
|---|---|---|
| `GMAIL_BACKEND` | 是 | `imap_smtp` |
| `GMAIL_ADDRESS` | 是 | — |
| `GMAIL_APP_PASSWORD` | 是 | —（16 字符 App Password） |
| `GMAIL_IMAP_HOST` | 否 | `imap.gmail.com` |
| `GMAIL_IMAP_PORT` | 否 | `993` |
| `GMAIL_SMTP_HOST` | 否 | `smtp.gmail.com` |
| `GMAIL_SMTP_PORT` | 否 | `587` |

安全：App Password **绝不**暴露在聊天回复、日志、审计日志或 `repr()` 中。
发送需要 WRITE 确认。此阶段不支持删除/归档/标签操作。不下载附件。

自然语言支持：`帮我看一下收件箱` → `gmail.recent`，`邮箱状态` → `gmail.status`，
`搜索邮件 关于发票` → `gmail.search`，`发邮件` → 引导输入格式。

Smoke：`scripts/gmail_smoke.py`（9 项：config、registry、commands、缺少配置、无网络、解析错误、确认、脱敏、help/tools）。

**P3.4 — Google Calendar + Contacts：** Google OAuth broker + 只读日历/联系人工具。**Gmail 仍用 App Password，OAuth 仅用于 Calendar/Contacts。**

| 工具 | 级别 | 命令 | 说明 |
|---|---|---|---|
| `google.status` | READ | `/google_status` | OAuth token 状态 |
| `google.auth` | WRITE | `/auth_google` | 开始 OAuth 授权 |
| `google.revoke` | DESTRUCTIVE | `/google_revoke` | 撤销并删除 token |
| `calendar.status` | READ | `/calendar_status` | Calendar 连接状态 |
| `calendar.today` | READ | `/calendar_today` | 今日日程 |
| `calendar.tomorrow` | READ | `/calendar_tomorrow` | 明日日程 |
| `calendar.week` | READ | `/calendar_week` | 本周日程 |
| `calendar.search` | READ | `/calendar_search <query>` | 搜索日程 |
| `calendar.freebusy` | READ | `/calendar_freebusy <range>` | 查询忙闲 |
| `calendar.create` | WRITE | `/calendar_create <标题> \| <时间> \| <描述>` | 创建日程（需确认） |
| `contacts.search` | READ | `/contacts_search <query>` | 搜索联系人 |

环境变量：

| 变量 | 必需 | 默认值 |
|---|---|---|
| `GOOGLE_CLIENT_SECRET_PATH` | 是 | —（client_secret JSON 路径） |
| `GOOGLE_TOKEN_PATH` | 否 | `codex_memory_root/secrets/google_token.json` |
| `GOOGLE_OAUTH_SCOPES` | 否 | calendar + contacts.readonly |
| `GOOGLE_OAUTH_REDIRECT_PORT` | 否 | `8765` |

安全：OAuth token 存储在 `secrets/google_token.json`，chmod 600。token 不暴露在聊天、日志、审计或 `repr()` 中。API 错误经脱敏处理。

自然语言意图：`看看今天的日程` → `calendar.today`，`搜索日程 关于会议` → `calendar.search`，`找一下联系人 张三` → `contacts.search`。

依赖：`google-auth`、`google-auth-oauthlib`、`google-api-python-client`。

Smoke：`scripts/google_tools_smoke.py`（10 项：缺少配置、缺少授权、设置说明、确认、token 路径、registry、commands、help/tools、意图路由）。

**P3.5 — Daily Briefing：** 每日简报系统，聚合 Calendar、提醒、Gmail、笔记。

| 工具 | 级别 | 命令 | 说明 |
|---|---|---|---|
| `briefing.status` | READ | `/brief_settings` | 简报设置状态 |
| `briefing.today` | READ | `/brief_today` | 今日简报 |
| `briefing.tomorrow` | READ | `/brief_tomorrow` | 明日简报 |
| `briefing.enable` | WRITE_SAFE | `/brief_enable [HH:MM]` | 启用每日简报 |
| `briefing.disable` | WRITE | `/brief_disable` | 禁用每日简报（需确认） |
| `briefing.probe` | READ | `/brief_probe` | 简报探针（dry-run） |

简报内容包括：日历事件（需 Google OAuth）、到期提醒、最近邮件摘要（需 Gmail）、最近笔记。缺少配置时显示降级提示。

调度器集成：`scripts/scheduler_tick.py` 每分钟检查已启用的简报设置，在用户本地时间到达时发送简报，避免重复发送。

存储：`briefing_settings` 和 `briefing_runs` 表在 `personal_tools.db`。

安全：无原始邮件正文、无 OAuth token、无密码。输出经 `redact_text()` + `truncate()` 处理。

自然语言：`今日简报` → `briefing.today`，`启用每日简报` → `briefing.enable`，`禁用简报` → `briefing.disable`。

Smoke：`scripts/briefing_smoke.py`（15 项：settings CRUD、runs、graceful degradation、enable/disable、probe、registry、commands、help/tools、intent routing、dedup、redaction）。

**P3.6 — GitHub Issues/PR Tools：** 只读的 GitHub 项目工具，支持 Issues、PRs 和 CI 状态。**此阶段不支持 merge/close/delete 操作。**

|| 工具 | 级别 | 命令 | 说明 |
||---|---|---|---|
|| `github.status` | READ | `/github_status` | GitHub 连接状态 |
|| `github.issues` | READ | `/github_issues [state|query]` | 列出 Issues（open/closed/all/搜索） |
|| `github.issue` | READ | `/github_issue <number>` | 查看 Issue 详情 |
|| `github.prs` | READ | `/github_prs [state]` | 列出 Pull Requests |
|| `github.pr` | READ | `/github_pr <number>` | 查看 PR 详情 |
|| `github.ci` | READ | `/github_ci [ref]` | CI 状态（分支/commit） |
|| `github.create_issue` | WRITE_SAFE | `/github_create_issue <标题> \| <正文>` | 创建 Issue（审计） |
|| `github.comment` | WRITE | `/github_comment <编号> \| <正文>` | 评论 Issue/PR（需确认） |

环境变量：

|| 变量 | 必需 | 默认值 |
||---|---|---|
|| `GITHUB_TOKEN` | 是 | —（Personal Access Token） |
|| `GITHUB_DEFAULT_REPO` | 是 | —（如 `mammut001/Conveyor`） |
|| `GITHUB_API_BASE` | 否 | `https://api.github.com` |

安全：GitHub token **绝不**暴露在聊天回复、日志、审计日志或 `repr()` 中。
创建 Issue 是 WRITE_SAFE（审计）。评论需要 WRITE 确认。所有输出经 `redact_text()` + `truncate()` 处理。

自然语言意图：`看看 GitHub issue` → `github.issues`，`PR 状态` → `github.prs`，`CI 挂了吗` → `github.ci`，`创建 issue` → 引导输入详情。

Daily Briefing 集成：如果配置了 GitHub，简报会包含 open issue 数量、open PR 数量和默认分支的 CI 状态。

Smoke：`scripts/github_smoke.py`（11 项：缺少配置、token 脱敏、命令解析、确认、registry、commands、help/tools、意图路由、简报降级、无网络）。

**P3.7 — Natural Language Planner：** 自然语言规划器，将现有确定性工具组合成有用的个人代理工作流。**不添加新的外部集成。所有 Planner profiles 都是只读的。**

| 工具 | 级别 | 命令 | 说明 |
|---|---|---|---|
| `planner.list` | READ | `/planners` | 列出所有 Planner |
| `planner.today` | READ | `/plan_today` | 今日优先级分析 |
| `planner.dev` | READ | `/plan_dev` | 开发计划 |
| `planner.health` | READ | `/project_health` | 项目健康检查 |
| `planner.triage` | READ | `/inbox_triage` | 邮件分类整理 |
| `planner.schedule` | READ | `/schedule_review` | 日程审查 |

每个 Planner profile 从 READ 工具采集事实（日历、提醒、Gmail、GitHub、笔记、VPS 运维），
然后传递给 Codex 进行结构化分析。缺少配置时优雅降级。

安全：**不使用写工具。** 不发送邮件、不创建日历事件、不评论/创建 GitHub issue。
所有采集事实经 `redact_text()` + `truncate()` 处理。

自然语言意图：
- `我今天应该先干啥` → `daily_priority` planner
- `今天开发计划` → `dev_plan` planner
- `项目健康状态` / `Conveyor 有没有问题` → `project_health` planner
- `帮我整理邮件` → `inbox_triage` planner
- `今天日程安排` → `schedule_review` planner

Smoke：`scripts/planner_smoke.py`（9 项：registry、READ-only 验证、graceful degradation、prompt building、commands、自然语言路由、planner status）。

**P3.8 — Codex Job Queue：** 单并发 FIFO 队列，用于管理 Codex 任务。当 Codex 任务正在运行时，新任务会排队而不是被拒绝。**实际 Codex 执行仍然是单并发的。**

| 命令 | 说明 |
|---|---|
| `/queue` | 查看队列状态 |
| `/queue_cancel <id>` | 取消队列任务 |
| `/queue_clear` | 清空队列 |
| `/queue_pause` | 暂停队列自动出队 |
| `/queue_resume` | 恢复队列自动出队 |

队列行为：
- 内存 FIFO 队列（bot 重启后丢失，已文档化）。
- 最大队列长度：10 个任务。
- 当任务完成时，自动启动下一个队列任务。
- 队列仅存储 prompt 文本和路由元数据（无密钥）。
- 队列显示经过脱敏/截断处理。
- 队列变更操作会记录审计日志。
- `/cancel` 仍然取消当前正在运行的任务。

安全性：**同一时间只有一个 Codex 进程。** 队列可通过 `/queue_pause` 暂停；暂停时完成的任务不会自动启动下一个。

Smoke：`scripts/job_queue_smoke.py`（10 项：enqueue/dequeue、FIFO 顺序、最大长度、cancel、clear、pause/resume、状态显示、命令注册、help 文本、脱敏）。

**P3.9 — 通用项目管理（Project Profiles）：** 通用的项目技能层，适用于任何用户的项目。用户定义项目配置文件，并运行通用项目命令。复用现有 Gmail、Calendar、GitHub、Notes、Reminders 工具。

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

项目分析命令是 READ-only 的。它们从已配置的集成（GitHub、Notes、Gmail、Calendar、Reminders）收集事实，并使用项目类型特定的 prompt 进行 Codex 分析。如果集成未配置，会优雅降级。

每日简报集成：显示最多 3 个已启用项目的简短状态。如果没有配置项目，会优雅降级。

自然语言路由（保守）：
- "项目列表" → `/projects`
- "切换项目 X" → `projects.use`
- "项目下一步" → `project.next`
- "项目健康状态" → `project.health`
- "项目 roadmap" → `project.roadmap`
- "发布清单" → `project.release_checklist`

Smoke：`scripts/project_profiles_smoke.py`（23 项：CRUD、operator 隔离、活跃项目回退、danger 级别、确认要求、briefing 集成、命令注册、help 文本、脱敏）。

**P3.10 — 设置向导（Setup Wizard）：** 让新用户在部署后更容易配置 Conveyor。检查现有集成并引导用户完成设置。

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

安全性：所有设置命令都是 READ-only 的。永远不会打印 token 值、app password、.env 内容或原始密钥。所有输出经过 `redact_text()` + `truncate()` 处理。

Smoke：`scripts/setup_smoke.py`（13 项：缺失集成、配置状态、项目示例、gmail 警告、github 无 token 泄漏、命令注册、help 文本、工具列表、无网络调用）。

**P3.11 — 项目导入/导出（Project Import/Export）：** 使项目配置文件可移植，更容易设置。支持导入、导出和模板功能。

| 命令 | 说明 |
|------|------|
| `/project_export [id]` | 导出指定项目为 JSON |
| `/project_export_all` | 导出所有项目 |
| `/project_import <JSON>` | 从 JSON 导入项目 |
| `/project_template [type]` | 查看项目模板 |

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

安全性：导出不包含内部 DB ID、operator_id、tokens、secrets、OAuth 路径或 .env 值。导入验证 schema 和项目类型。重复项目名称不覆盖（跳过）。导入作用域为 operator_id。导入的项目仅在无活跃项目时才设为活跃。导出/模板为 READ-only，导入为 WRITE_SAFE。所有输出经过 `redact_text()` + `truncate()` 处理。

Smoke：`scripts/project_io_smoke.py`（15 项：导出单个/全部项目、无 ids/operator_id、有效 JSON 导入、跳过重复、设置活跃项目、验证 schema/类型、模板显示、命令注册、help 文本、无网络调用、输出 redacted）。

**P4.1 — Web 搜索 + 研究（Web Search + Research）：** 为 Conveyor 添加外部 Web/研究能力。三层安全架构：Web Fetch → Web Search → Research。

| 命令 | 说明 |
|------|------|
| `/web_fetch <url>` | 获取网页内容 |
| `/web_text <url>` | 获取网页文本 |
| `/web_headers <url>` | 获取 HTTP headers |
| `/web_search <查询>` | Web 搜索（多后端） |
| `/research <问题>` | Web 研究 + Codex 综合 |
| `/project_research [id] <问题>` | 项目相关研究 |

**自然语言示例：**
- `搜索 Python asyncio` → `/web_search Python asyncio`
- `研究一下 AI 编程助手` → `/research AI 编程助手`
- `获取网页 https://example.com` → `/web_fetch https://example.com`

支持的搜索后端（`WEB_SEARCH_BACKEND`）：
- `disabled`（默认）、`brave`、`tavily`、`serper`、`searxng`

安全性：所有工具都是 READ-only。URL 验证拒绝 localhost、私有 IP 和元数据端点。无文件写入、无任意 curl、无 JS 执行。所有输出经过 `redact_text()` + `truncate()` 处理。

**安全加固（P4.1.1）：**
- **API 密钥安全**：使用 urllib.request 替代 curl 子进程，避免 API 密钥暴露在进程参数中
- **重定向安全**：禁用自动重定向（`--no-location`），每跳需单独验证
- **Content-Type 验证**：仅允许 text/*、application/json、application/xml（HEAD 和 GET 响应均验证）
- **IP 拦截**：扩展拦截范围，包括 100.64.0.0/10（运营商级 NAT）、198.18.0.0/15（基准测试）、多播（224.0.0.0/4）、保留地址（240.0.0.0/4）、IPv6 链路本地（fe80::/10）
- **元数据端点**：显式拦截 169.254.169.254 和 metadata.google.internal
- **WEB_SEARCH_ENDPOINT 验证**：拒绝 localhost/私有 IP/链路本地/元数据端点
- **URL 编码**：搜索查询正确处理中文和特殊字符
- **研究工具**：使用 Codex 混合合成（`[HYBRID_PROMPT]`）
- **密钥保护**：WEB_SEARCH_API_KEY 永远不会出现在错误信息、日志或聊天输出中

Smoke：`scripts/web_tools_smoke.py`（31 项）、`scripts/research_smoke.py`（14 项）。

**P4.2 — 文件搜索 + 知识库（File Search / Knowledge Base）：** 自然语言优先的文件搜索，自动收集 READ-only 事实。斜杠命令作为后备/调试。

| 命令 | 说明 |
|------|------|
| `/files_roots` | 列出搜索根目录 |
| `/files_search <查询词>` | 搜索文件 |
| `/files_read <文件路径>` | 读取文件 |
| `/kb_index` | 索引知识库 |
| `/kb_status` | 知识库状态 |
| `/kb_search <查询词>` | 搜索知识库 |
| `/project_docs <查询词>` | 搜索项目文档 |

**自然语言示例：**
- `找一下文档里关于 deploy 的说明` → 搜索文件 "deploy"
- `README 里有没有 Gmail 配置步骤` → 搜索文件 "Gmail 配置步骤"
- `项目文档怎么说 scheduler` → 搜索文件 "scheduler"
- `根据本地文档总结安装流程` → 搜索文件 "安装流程"
- `查一下我 notes 里关于 OAuth 的内容` → 搜索文件 "OAuth"

配置（`FILE_SEARCH_*`、`KB_*`）：
- `FILE_SEARCH_ENABLED=true` — 启用文件搜索
- `FILE_SEARCH_ALLOWED_ROOTS` — 额外允许的搜索根目录（逗号分隔）
- `FILE_SEARCH_MAX_FILE_BYTES=1000000` — 最大文件大小
- `FILE_SEARCH_MAX_RESULTS=10` — 最大结果数
- `FILE_SEARCH_EXTENSIONS=.md,.txt,.py,.ts,.tsx,.js,.json,.yaml,.yml,.toml`
- `KB_ROOT` — 知识库根目录（默认：`CODEX_MEMORY_ROOT/kb`）
- `KB_INDEX_PATH` — 索引数据库路径（默认：`CODEX_MEMORY_ROOT/kb_index.sqlite`）

安全性：所有文件/KB 分析命令为 READ-only。`kb.index` 为 WRITE_SAFE（审计）。拒绝敏感文件（.env、secrets/、.ssh/、私钥、token 文件、google_token.json、client_secret.json、二进制文件、超大文件）。所有输出经过 `redact_text()` + `truncate()` 处理。无文件写入（除 KB 索引元数据/缓存）。无删除文件。无任意路径遍历。

Smoke：`scripts/file_search_smoke.py`（14 项）。

**P4.3 — 自然语言 Agent 路由器（Natural Language Agent Router）：** 自然语言优先，斜杠命令作为后备。用户可以用正常语言调用大多数注册工具。

核心特性：
- 统一工具目录：从 host + personal tool registry 构建，包含工具名、摘要、危险级别、关键词、示例、领域、NL 支持级别
- `/nl_help` 命令：按领域分组列出自然语言示例，带诚实的支持级别标记
- 扩展 NL 覆盖：笔记搜索、提醒创建、日历忙闲、队列状态、设置状态
- 确认消息使用自然语言（不建议用户使用斜杠格式）
- 安全策略：WRITE/DESTRUCTIVE 工具永远不会从 NL 自动执行
- WRITE_SAFE 工具（notes.add、reminders.create）触发时会被审计

**NL 分类（P4.3.1 更新）：**

| 分类 | 说明 | 行为 |
|------|------|------|
| READ_DETERMINISTIC | 直接读取 | 自动执行 |
| READ_HYBRID | 收集事实 + Codex 综合 | 自动收集，Codex 综合 |
| WRITE_SAFE_AUTO | 低风险审计操作 | 自动执行（有审计日志） |
| WRITE_CONFIRM_PREVIEW | WRITE/DESTRUCTIVE | 需要确认 |
| CLARIFY | 缺少参数 | 用自然语言追问 |
| CODEX_LLM | 编码/开放任务 | Codex 处理 |

**自然语言示例（按领域，带支持级别标记）：**

| 领域 | 示例 | 路由目标 | 支持级别 |
|------|------|----------|----------|
| 运维 | `看看负载`、`磁盘空间` | load / disk | 可直接执行 |
| 笔记 | `记一下 xxx`、`搜索笔记里的 deploy` | notes.add / notes.search | 自动 / 可直接执行 |
| 提醒 | `提醒我明天9点开会` | reminders.create | 自动（WRITE_SAFE） |
| 邮件 | `看看最近的邮件`、`搜索邮件关于发票` | gmail.recent / gmail.search | 可直接执行 |
| 日历 | `今天有什么安排`、`下午有空吗` | calendar.today / freebusy | 可直接执行 |
| 简报 | `今日简报`、`启用简报` | briefing.today / enable | 可直接执行 |
| GitHub | `CI 挂了吗`、`看看 issue` | github.ci / issues | 可直接执行 |
| 规划 | `今天应该先干啥`、`帮我整理邮件` | planner.today / triage | 混合 |
| 项目 | `项目列表`、`项目 roadmap` | projects.list / roadmap | 可直接执行 |
| 队列 | `队列状态`、`看看队列` | queue.status | 可直接执行 |
| Web | `搜索 Python asyncio`、`研究一下 React Native` | web.search / research | 可直接执行 |
| 文件/KB | `找一下文档里关于 deploy` | kb.collect_facts | 可直接执行 |
| 设置 | `配置状态` | setup.status | 可直接执行 |

**`/nl_help` 支持级别标记：**
- 无标记 = 可直接执行（READ）
- [自动] = WRITE_SAFE 自动执行（有审计日志）
- [需确认] = WRITE/DESTRUCTIVE 需要确认
- [会追问] = 缺少参数，会用自然语言追问
- [示例] = 仅作参考，暂无 NL 路由

安全行为：
- READ 工具可自动执行
- WRITE_SAFE 工具（如 notes.add、reminders.create）自动执行但有审计日志
- WRITE/DESTRUCTIVE 工具必须先预览再确认，不会自动执行
- 模糊的编码请求优先走 Codex LLM
- 缺少参数时用自然语言追问，不建议斜杠格式

**队列 vs 调度器区分（P4.3.1）：**
- `queue.status` — 任务队列状态（Job Queue）
- `scheduler_status` — 提醒调度器状态（Reminder Scheduler）
- 自然语言"队列状态"路由到 `queue.status`
- "调度器状态"无 NL 路由，使用 `/scheduler_status` 命令

Smoke：`scripts/nl_router_smoke.py`（35 项）。

**P4.3.2 — NL 路由器最终修复：** 修复 P4.3.1 遗留问题。
- `queue.status` 已注册到主 TOOL_REGISTRY（之前仅在 personal tools 中）
- `_build_catalog` 现在正确从 `_DOMAIN_DEFS` 传播 `nl_support` 到 `ToolCatalogEntry`
- `/nl_help` 支持级别标记现在准确反映工具能力

**Telegram slash 命令：** 新 ops/tool 命令（`/load`、`/tools`、`/disk` 等）在 `COMMAND_TABLE` 注册，并通过 `bot.py` 中的通用 `MessageHandler(filters.COMMAND, …)` fallback 到达（位于显式 `CommandHandler` 之后、纯文本 handler 之前），确保未知 slash 命令仍能进入 `dispatch()` → `COMMAND_TABLE`。

### 本机运维快路径（legacy slash 命令）

以下 slash 命令及对应自然语言仍可用，并映射到上述 tool 层：

| 命令 | 自然语言 | 行为 |
|---|---|---|
| `/load`（alias `/vps`）| `看看我的负载`、`check vps load` | 主机名、时间、uptime、CPU 数、内存、`/ /srv /opt` 磁盘、CPU/内存占用最高进程 |
| `/htop` | `跑一下 htop`、`top 看一下` | htop 是交互 TUI；返回 `top -bn1` 一帧 + 一行 TUI 解释。意图匹配保守 — 提及 htop 的编码/文档请求（如「look at htop source code」）走 LLM，不走 ops |
| `/ps` | `ps aux`、`哪些进程` | CPU/内存 top 进程。默认仅 `comm`（不含 argv → 不漏 token）。`/ps full` 显示安全提示；`/ps full confirm` 才含 args（仍 redact/truncate） |

安全：

- 用 argument array，**不**做 shell interpolation 用户文本
- 5 秒超时
- `/ps` 默认 `comm` 模式，argv 里的 token 不会泄露
- 输出过 `redact_text` 和 `truncate`
- 默认不读环境变量、`.env`、完整进程命令行

bot 跑在单台 VPS 上，所以快照就是那一台机器的状态。回复里明确
写着「这是 bot 服务当前所在机器的本地快照」，避免和 `codex exec`
sandbox 视图混淆。

Telegram 上 ops 输出是**新消息**（不流式编辑）。长跑 Codex 任务时
Telegram 是**就地编辑**原占位符；飞书目前是**新消息**（卡片/流式是 P2.2 backlog）。

任意时刻只跑一个 Codex 任务。回信刻意保持安静：开始确认、必要的重试/失败
通知、最终答案。原始 JSONL 事件留在 `logs/<job-id>/` 磁盘上，不下发。

`CODEX_RETRY_429_DELAYS_SECONDS` 控制 provider 临时 `429 Too Many Requests`
时的退避策略。

---

## 5. 安全模型

这是**单操作员私有 VPS 控制面**，不是多租户 SaaS。每个 channel 只有一个
白名单会话，合入主仓库前由同一个人审 `/diff`。

- 通道鉴权：发送者 ID 必须**精确匹配** `TELEGRAM_ALLOWED_USER_ID` 或
  `LARK_ALLOWED_OPEN_ID`，否则拒收。除了 `ALLOWED_*` 这道门没有其他
  认证 —— 这道门是这个 bot 和公网之间唯一的东西。
- prompt 只通过 Codex stdin 传，**绝不**当 shell 命令执行。
- `/run`、纯文本和 `/fix` 都在每日 worktree 里用 Codex
  `danger-full-access`（chat-first；见 `docs/architecture.md` §5）。
  个人 VPS 上这是刻意的：聊天里要能跑 shell、读主机、改 worktree。
- 每个任务用一个从 `HEAD` 创建的 detached git worktree。
- 原始 Codex JSONL 留磁盘；Telegram / Feishu 下发前会截断并脱敏常见秘钥格式。
- systemd unit 设了 `PYTHONDONTWRITEBYTECODE=1`，运行时 import 不会在部署
  目录里留 `__pycache__`。
- bot **不** commit / push / merge。`/apply` 永远是显式动作 —— 你先
  `/diff` 看过再 `/apply`。

**当前安全边界**：channel 白名单、低权 VPS 用户、按日 worktree 隔离、
输出 redaction、以及你的审查纪律（`/diff` 再 `/apply`）。**未来加固**
（例如把 sandbox 收窄回 `workspace-write`）在 backlog —— **不是当前行为**。

这仍然是远程跑代码的基础设施：bot token 保密、用专用 bot、VPS 用户保持
低权、`/diff` 看过再手动合。

---

## 6. 文件结构

```text
conveyor/
  bot.py                  # Telegram 命令处理器（薄适配层）
  feishu_bot.py           # Feishu 命令处理器（薄适配层）
  config.py               # .env 加载与校验
  runner.py               # shim → runner/ 包
  redaction.py            # 输出脱敏与截断
  requirements.txt
  .env.example
  systemd/
    conveyor-telegram-bot.service
    conveyor-feishu-bot.service
    conveyor-maintain.service
    conveyor-maintain.timer
    conveyor.env.example
  channel/
    types.py              # InboundMessage, OutboundPort
    auth.py               # 各通道 is_allowed
  handlers/
    dispatch.py           # 单一入口：auth → command/memo/codex
    commands.py           # 23 条命令 COMMAND_TABLE
    memo.py               # "记 x" / /memo 快路径
    jobs.py               # /run、/fix、纯文本 → CodexRunner
  scripts/                # CLI 工具、harness、smoke
  Makefile
  README.md               # 英文版（主）
  README.zh.md            # 中文版
  docs/
    architecture.md       # 设计（Conveyor vs Hermes、通道解耦、阶段进度）
```

`docs/` 是仓库里**唯一**自带的文档目录。除了 `README.md` 和
`docs/architecture.md` 之外的任何笔记（架构、设计日志、踩坑记录）都
只在 operator 本地留着 —— 仓库定位是一个小巧的个人工具，不要产品文档。

---

## 7. 本地开发

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
make smoke           # 无 .env 的 AST/行为 smoke，<30s，发布前门禁
make smoke-all       # 还会跑要 .env 的脚本
```

`make smoke` 是发布前门禁，红了不能合。详见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

设计笔记（运行时结构、通道类型、agent 工具层、命令表、harness 矩阵、backlog）见
[`docs/architecture.md`](docs/architecture.md)（英文版：[`docs/architecture.en.md`](docs/architecture.en.md)）。

---

## 8. 故障排查

| 症状 | 检查 |
|------|------|
| Telegram 没回 | `journalctl -u conveyor-telegram-bot`；确认 `TELEGRAM_ALLOWED_USER_ID` 等于你的 user id |

## 实时 Telegram 烟测（手动，可选）

`scripts/telegram_live_smoke.py` 以**真实 Telegram 用户**身份
（Telethon）驱动 bot，端到端验证 agent 工具层。这是唯一能真正触发
bot `MessageHandler` 的方式——bot 自己用 Bot API 发的消息不会再次
触发它自己的 handler。

**不**在 `make smoke` 里。要用时手动装 Telethon：

```bash
pip install telethon
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
export TELEGRAM_BOT_USERNAME=your_bot_username
.venv/bin/python scripts/telegram_live_smoke.py --quick
.venv/bin/python scripts/telegram_live_smoke.py --full
```

重启确认默认**只发取消**。要真正重启 conveyor 服务，必须**同时**
满足两个开关：

```bash
TELEGRAM_LIVE_ALLOW_RESTART=1 \
  .venv/bin/python scripts/telegram_live_smoke.py --full --allow-restart
```

脚本不会打印 bot token、api hash、session 路径或 `.env` 内容；
`.telegram-live-smoke*` 已被 `.gitignore` 屏蔽。
| 飞书私聊没回 | `im:message.p2p_msg:readonly` 已开且新版本已发布；`journalctl -u conveyor-feishu-bot` 找 `400` |
| 飞书：`Access denied. One of the following scopes is required: [im:message:send, im:message, im:message:send_as_bot]` | `im:message:send_as_bot` 没开，或新版本没发布，或没装到企业 |
| 飞书：每条消息都看到 `/contact/v3/users/batch ... 400` | `contact:user.id:readonly` 没开；无害但日志吵。补 scope、发版即可 |
| 飞书：WebSocket 秒断 | `.env` 值带前后空格、引号、中文标点。用 `nano` 重写 |
| 长连接保存失败 | 本地 `feishu_bot.py` 必须先跑起来 |
| Job 卡在 `running` | `/cancel` 或 `sudo systemctl restart conveyor-telegram-bot`；查日志里反复出现的 `Reconnecting... high demand` |
| Telegram 回复慢 | `TELEGRAM_PROGRESS_SECONDS`（默认 3s）控制占位编辑频率；Telegram 上限 20 edits/min |

---

## 自动 VPS 部署（GitHub Actions）

推送到 `main` 后，GitHub Actions 会自动 SSH 到 VPS，拉取最新代码，
运行 smoke 测试，重启服务 —— 一步到位。smoke 失败则不重启。

### 所需 GitHub secrets

| Secret | 必填 | 说明 |
|--------|------|------|
| `VPS_HOST` | 是 | VPS 主机名或 IP |
| `VPS_USER` | 是 | SSH 用户（建议专用 deploy 用户） |
| `VPS_SSH_KEY` | 是 | deploy 用户的私钥 |
| `VPS_PORT` | 否 | SSH 端口（默认 22） |
| `CONVEYOR_DEPLOY_PATH` | 否 | VPS 上的 repo 根目录（默认 `/opt/conveyor`） |

### VPS 一次性配置

1. 克隆仓库到 `/opt/conveyor`：
   ```bash
   sudo mkdir -p /opt/conveyor
   sudo chown $USER /opt/conveyor
   git clone https://github.com/mammut001/Conveyor.git /opt/conveyor
   ```
2. 在 VPS 上创建 `.env`（永远不要提交）。
3. 创建 `.venv` 并安装依赖：
   ```bash
   cd /opt/conveyor
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
4. 安装 systemd 服务：
   ```bash
   sudo cp systemd/conveyor-telegram-bot.service /etc/systemd/system/
   sudo cp systemd/conveyor-feishu-bot.service   /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable conveyor-telegram-bot conveyor-feishu-bot
   ```
5. 为 deploy 用户配置 sudoers（仅限这几条命令）：
   ```
   # /etc/sudoers.d/conveyor-deploy
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart conveyor-telegram-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart conveyor-feishu-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl status  conveyor-telegram-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl status  conveyor-feishu-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active conveyor-telegram-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active conveyor-feishu-bot
   ```

### 手动测试部署

```bash
ssh user@host 'bash /opt/conveyor/scripts/deploy_vps.sh'
```

### 工作流程

1. 每次推送到 `main`（或手动触发）都会触发 GitHub Actions。
2. Actions SSH 到 VPS，运行 `scripts/deploy_vps.sh`。
3. 脚本流程：
   - 获取 `flock` 锁（防止并发部署）
   - `git fetch origin main && git reset --hard origin/main`
   - 重置前备份关键文件
   - 运行 `make smoke`
   - smoke 通过：重启 `conveyor-telegram-bot` + `conveyor-feishu-bot`
   - smoke 失败：退出非零，**不重启**服务
   - 写入 `.deploy-status.json` 部署元数据
   - 重启健康检查失败：从备份回滚并重试
4. `.env` 永远不会被打印或提交。

另有 rsync 方式部署（`scripts/deploy.sh`），用于本地推送源文件后
执行同样的远程 smoke + 重启流程。

### `/deploy_status` 命令

向机器人发送 `/deploy_status` 可查看：
- 最近部署时间、来源、Git SHA
- smoke 结果和服务状态（来自 `.deploy-status.json`）
- 当前运行时 Git SHA、分支、progress mode
- 实时 `systemctl is-active` 两个服务状态

### 限制

- 实时 Telegram 烟测（`scripts/telegram_live_smoke.py`）**不会**自动运行 ——
  它需要真实 Telegram 凭据，仅限手动。
- 部署脚本假设 VPS 上已有 `.venv`。如果需要初始化新 VPS，
  请先运行 `scripts/install-remote.sh`。
- 回滚是有限的：重置前备份关键文件，如果重启后服务未启动，
  脚本会从备份恢复并重试。这不涵盖所有故障模式。

---

## 9. 许可证

MIT — 见 [`LICENSE`](LICENSE)。