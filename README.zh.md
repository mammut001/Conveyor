# Conveyor

一个小巧的个人 transport 层，桥接一个白名单用户（Telegram 和/或 Feishu）
到 VPS 上运行的 [`codex`](https://github.com/openai/codex) CLI。在手机上
敲一句话，把 Codex 的回答拿回来；`记 xxx`（写进记忆）、`/status`、`/diff`、
`/run` —— 所有命令都行。单 operator、单 VPS、不是 SaaS。

> Conveyor 是 **transport 层**，不是 agent。Agent 是 Codex CLI 自己。
> 完整设计见 [`docs/architecture.md`](docs/architecture.md)。

---

## 1. 快速开始

VPS 前置：Ubuntu、安装并认证 [`codex` CLI](https://github.com/openai/codex)
（用 `codex doctor` 确认）、`CODEX_WORKSPACE_ROOT` 指向一个 git 仓库。

**首次安装**（在你的笔记本上跑，前提是能 SSH 到 VPS；VPS 地址和秘钥保留在
你本地的 shell 环境里，永远不进仓库）：

```bash
git clone https://github.com/mammut001/conveyor.git
cd conveyor
CONVEYOR_REMOTE=ubuntu@<host> bash scripts/install-remote.sh
```

安装脚本会 rsync 源码、建 `.venv`、装 3 个 systemd unit
（`conveyor-telegram-bot`、`conveyor-feishu-bot`、`conveyor-maintain`），
如果 `.env` 不存在就跑交互式 `configure_env.py`，最后启动服务。装完
打开 Telegram 发 `/start`。

**后续代码更新**（首次安装之后）：

```bash
CONVEYOR_REMOTE=ubuntu@<host> bash scripts/deploy.sh
```

可选的本地 shell 别名（写在 `~/.zshrc`）：

```bash
export CONVEYOR_REMOTE=ubuntu@<host>
export CONVEYOR_REMOTE_DIR=/opt/conveyor
alias deploy-runner='cd ~/conveyor && bash scripts/deploy.sh'
```

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
- `/run <prompt>` 和 `/fix <prompt>` 等价；都用 `workspace-write` 沙箱
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

安全：**写/破坏性工具必须显式确认**（Telegram 内联按钮；飞书/文本须用明确短语如 `确认执行` / `confirm` — 随意的 `好` / `ok` / `是` **不会**被接受）。确认绑定 originating chat + channel；事件写入 `audit/tools.log`。

实现：`handlers/tools/`（registry + executors + runner）、`handlers/intent.py`（`route_intent`）。Handler 保持通道无关；Telegram callback 用 `tool:confirm:<token>` / `tool:cancel:<token>`。

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

- 通道鉴权：发送者 ID 必须**精确匹配** `TELEGRAM_ALLOWED_USER_ID` 或
  `LARK_ALLOWED_OPEN_ID`，否则拒收。除了 `ALLOWED_*` 这道门没有其他
  认证 —— 这道门是这个 bot 和公网之间唯一的东西。
- prompt 只通过 Codex stdin 传，**绝不**当 shell 命令执行。
- `/run` 和纯文本用 Codex `workspace-write` 沙箱（chat-first；见
  `docs/architecture.md` §5）。`/fix` 是别名，沙箱相同。
- `danger-full-access` **永远不用**。
- 每个任务用一个从 `HEAD` 创建的 detached git worktree。
- 原始 Codex JSONL 留磁盘；Telegram / Feishu 下发前会截断并脱敏常见秘钥格式。
- systemd unit 设了 `PYTHONDONTWRITEBYTECODE=1`，运行时 import 不会在部署
  目录里留 `__pycache__`。
- bot **不** commit / push / merge。`/apply` 永远是显式动作 —— 你先
  `/diff` 看过再 `/apply`。

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

## 9. 许可证

MIT — 见 [`LICENSE`](LICENSE)。