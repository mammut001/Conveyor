# Conveyor

**在自己的 VPS 上，通过 Telegram / 飞书远程运行 Codex。**

Conveyor 把一个私有的 Telegram / 飞书会话变成一台
[Codex CLI](https://github.com/openai/codex) 远程控制面板 —— 一个自托管、
单 operator 的 **AI 编程助手**，你可以从手机上直接调用。Codex 跑在你自己的
服务器上。用手机发一条消息，Codex 去仓库里查问题、改代码、看日志、记提醒、
搜个人工具，再把结果原路发回来。没有 SaaS 控制台，没有共享 workspace，没有
多用户概念。它就是给那些已经信 Codex 的人准备的远程 coding agent —— 一个
"手机 → agent" 的入口，连着你自己的私有开发者工具。

> **只服务单一受信 operator。** Conveyor 只为一个人、一台 VPS、每个 channel
> 一个白名单会话设计。它**不是**多租户 bot，以后也不会是。如果你要的是
> 公开的 agent，请另寻他处。

[English](README.md) · [架构文档](docs/architecture.md) · [License](LICENSE)

> [!IMPORTANT]
> **v0.1.1 安全加固版本**：Conveyor v0.1.1 是一次安全加固更新，解决了最近安全审计中发现的问题。升级后，系统管理员/Operators 应运行 `make smoke`。由于 systemd unit 配置文件收窄了 `ReadWritePaths` 配置，您**必须重新安装并重新加载** systemd unit 服务。详见部署部分的更新指令。

---

## 为什么需要它

你人在外面，手边没电脑。你的 VPS / 开发机上装着仓库、Codex CLI、跑着的服务、
挂掉的测试、漏配的环境变量。

Conveyor 就是中间那条桥：

- **你** 在 Telegram 或飞书里用日常语言说话。
- **Codex** 在你的 VPS 上跑，detached git worktree，全工作区权限。
- **Conveyor** 把消息送过去，把答案带回来，并给你 `/diff`、`/apply`、
  `/cancel`，让你控制哪些改动真正进主分支。

不是公开 agent。不是云端共享 workspace。不是"邀请同事一起用"的工作流。
就只是你的聊天、你的服务器、你的 coding agent。

---

## 一个示例流程（示意）

你：

> `/run 修一下 tests/test_parser.py 里挂掉的测试，把 diff 给我看`

Conveyor:

> ⏳ Codex 任务在 VPS 上启动了… worktree `day-2026-07-01`

一分钟后，Codex 给你一句简短的总结。然后你：

> `/status`  →  当前任务 + worktree 路径
>
> `/diff`    →  worktree 的 `git status` + 截断的 diff
>
> `/apply`   →  把 worktree 合回主分支（仅当主分支干净时）
>
> `/cancel`  →  杀掉正在跑的 Codex 进程

轻量的运维查询，直接说人话就行：

> `看看磁盘`  →  `/ /srv /opt` 的磁盘用量
>
> `为什么服务器这么慢`  →  先采 load / ps / disk 事实，再交给 Codex 分析
>
> `提醒我 10 分钟后看 build`  →  调度器提醒，到点发回这个会话

这是**示意**流程。Codex 返回的原文会因模型而异；Conveyor 保证的是它外面
那一圈——开始确认、状态、diff、apply、cancel。

---

## Conveyor 能做什么

同一套表面，Telegram 和飞书完全一致，背后是一层可审计的 agent 工具层
（"Agent tool layer" —— 见 [`docs/architecture.md`](docs/architecture.md)）。

### 远程 Codex 控制

- 纯文本，或 `/run <prompt>` / `/fix <prompt>` — 在 detached worktree 里
  启动一个 Codex 任务（沙箱按设计就是 `danger-full-access`；见安全模型）。
- `/status` · `/last` · `/jobs [n]` — 当前 / 最近的任务。
- `/diff` — 最近 worktree 的 `git status` + 截断的 diff。
- `/apply` — 把 worktree 合回主分支（仅当主分支干净时）。
- `/discard` · `/cancel` — 丢掉 worktree 或杀掉正在跑的进程。
- `/queue` · `/queue_cancel` · `/queue_clear` · `/queue_pause` · `/queue_resume`
  — 基于 SQLite 的持久化单并发 FIFO Codex 任务队列（bot 重启/部署/VPS 重启后依然保留；运行中的任务在启动时自动标为中断，排队中的任务自动恢复，且暂停状态持久化）。

### 个人记忆

- `/memo <内容>` 或 `记 <内容>` — 写入当天的 `MEMORY.md`（不走 Codex、
  不开 worktree）。
- `/memory [date] [category]` · `/journal [n]` — 读 MEMORY 和归档的
  journal。

### VPS 上的个人工具

- 笔记：`/note <内容>` · `/notes [关键词]`
- 提醒：`/remind <内容+时间>` · `/reminders`
- 调度器自检：`/scheduler_status` · `/scheduler_probe` · `/scheduler_probe_live`
- 每日简报：`/brief_today` · `/brief_tomorrow` · `/brief_settings` ·
  `/brief_enable [HH:MM]` · `/brief_disable` · `/brief_probe`

> **飞书用户：** Codex 任务开始 / 完成 / 失败、`/diff` 预览、危险操作
> 确认、节点状态都会渲染成交互式消息卡片，带可点击的按钮
>（Status / Diff / Apply / Discard / Cancel / Confirm / Refresh）。
> 按钮只是对同一批命令的便捷封装——详见[飞书接入](#飞书接入)。

### 诊断与运维

- `/health [full] [json] [nosecurity]` · `/doctor` · `/diag [since]`
- `/audit [stale-min]` · `/security [since]` · `/ratelimit [n]` ·
  `/audit_tools [n]`
- `/metrics [n]` · `/log [sel]` · `/meta [sel]`
- `/tools` · `/diagnose [server|bot|logs|quick]` · `/restart telegram|feishu|maintain`
- `/maintain [keep]` · `/clean [keep]` · `/smoke` · `/editcheck`
- VPS 主机快路径：`/load` · `/vps` · `/htop` · `/ps` · `/disk` · `/logs` ·
  `/service_status` · `/git_status`

### 可选集成（按配置启用）

- **Gmail**（IMAP / SMTP App Password）—— `/gmail_status` · `/gmail_recent` ·
  `/gmail_search` · `/gmail_read` · `/email_send`
- **Google 日历 / 联系人**（OAuth）—— `/auth_google` · `/google_status` ·
  `/calendar_today|week|search|freebusy|create` · `/contacts_search`
- **GitHub** Issues / PRs / CI（只读为主）—— `/github_status` · `/github_issues` ·
  `/github_prs` · `/github_ci` · `/github_create_issue` · `/github_comment`
- **本地文件搜索 + 知识库** —— `/files_roots` · `/files_search` · `/files_read` ·
  `/kb_index` · `/kb_status` · `/kb_search` · `/project_docs`
- **网页抓取 / 搜索 / 研究** —— `/web_fetch` · `/web_text` · `/web_search` ·
  `/research` · `/project_research`
- **项目档案与规划** —— `/projects` · `/project_add` · `/project_use` ·
  `/project_status` · `/project_health` · `/project_roadmap` · `/project_next` ·
  `/project_release_checklist` · `/plan_today` · `/plan_dev` · `/inbox_triage`
- **自然语言路由** —— 上面大多数都能用人话说：`看看负载`、
  `为什么服务器慢`、`搜一下 GitHub issue`…… 完整列表跑一下 `/nl_help`。

### 执行节点（VPS + desktop stub）

Conveyor 正在变成 VPS + 以后本地桌面的私有控制面。控制面永远跑
在 VPS 上。desktop 节点在这一阶段还是 **stub**：只在 `.env` 里
显式开启时才会出现在 `/nodes` 里，心跳到达前为 `offline`。**P5.2**
增加了通过 `capture-screen-helper` 的本地只读截屏 observe；鼠标、
键盘、浏览器控制、Computer Use 默认**仍未实现**——**P5.6** 增加了
受控的「直连 Computer Use 模式」（cua 后端，仅运行在 Mac 本地），
**默认关闭**。

- `/nodes` · `/node_status` —— 列出已知执行节点、能力以及动态的 online/offline 状态。
- `/computer_status` —— 显示 Computer Use 状态（启用开关、direct 模式来源、Cua 探测、运行中任务）。
- 自然语言：`我的节点`、`机器状态`、`主机状态`、
  `MacBook 在线吗`、`desktop node`、`nodes status`、
  `computer use status`。带桌面控制目标的人话（`帮我在 Mac 上打开
  Xcode`、`操作电脑…`、`帮我点…`、`打开 Chrome`、`在电脑上…`）只有在
  direct 模式已授权时才会走 `computer.task`（直连 Cua 循环），否则走确定性 stub。
  截屏请求类短语（`take a screenshot
  on my desktop`）走 `desktop.observe.request`（P5.3 远程 observe，仅元数据）。
  状态类短语（`截图状态`）走 `desktop.observe.status`。

### P5.6 直连 Computer Use 模式（cua 后端）

一种免干预的直连 Computer Use 模式：`Telegram/飞书 自然语言 → Codex → Conveyor
computer-use 工具 → Mac desktop_agent → 本地 cua-driver → 真实桌面操作`。后端用
`trycua/cua`，**只运行在 Mac 桌面 agent 端**——VPS 从不与 Cua 协议对话。

只在 Mac agent 侧安装 Cua。官方 `cua-driver mcp` 是 MCP stdio server，
Conveyor 的本地 wrapper 不把它暴露到网络，而是用同一个 binary 的
`cua-driver call <tool> <json>` 子命令执行本地工具。macOS 上真实 observe/click/type
前需要先给 driver 授权：

```bash
cua-driver permissions grant
cua-driver permissions status --json
```

`/computer_status` 会显示 driver 路径/版本，以及 metadata-only 的权限状态。权限未授予时，
Cua 可以启动，但截屏或输入动作会失败。

授权完成后，在 Mac 上跑只读本地验证：

```bash
python3 scripts/cua_driver_real_smoke.py --cmd "cua-driver mcp"
```

**所有开关默认 `false`/关闭**，该模式需显式开启：

- **授权（TTL）**：`/computer_arm [分钟]` 在有限时间内开启 direct 模式；过期后任务被拦截。
- **Direct 门闩**：`CONVEYOR_COMPUTER_DIRECT_ENABLED=true` 才允许
  `/computer_arm`、`/computer_task`、`/computer_action` 以及
  `is_direct_mode_active`。仅 `USE_ENABLED` 只开放 status / 观察就绪。
- **Always-direct**：`CONVEYOR_COMPUTER_ALWAYS_DIRECT=true` 仅在
  `USE_ENABLED` 与 `DIRECT_ENABLED` 均为 true 时跳过 arm。
- **紧急停止**：`/computer_stop` 立即取消当前运行中的任务。

**命令**

- `/computer_status` —— 启用开关、direct 模式来源、Cua 探测、运行中任务。
- `/computer_arm [分钟]` —— 授权 direct 模式一段时间（如 `/computer_arm 30`）。
- `/computer_task <目标>` —— 免干预运行 Codex→Cua 循环（如
  `/computer_task 打开 Chrome 并访问 conveyor.dev`）。direct 模式未开启时直接失败。
- `/computer_stop` —— 立即取消当前任务。
- `/computer_log [task_id]` —— 查看任务的脱敏轨迹。
- `/computer_screenshot` —— 在 direct 模式下截一次桌面观察（元数据/截图 id）。
- `/computer_observe` —— 触发一次桌面观察。
- `/computer_action <json>` —— 执行单个允许清单内的动作，
  如 `{"action":"click","x":100,"y":100}`。Cua 后端还支持可选的
  `pid`/`window_id`/`element_index`/`element_token`/`delivery_mode`，
  当 planner 拿到窗口状态时可用于更可靠的应用内点击。

**关键环境变量**（默认均安全）

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `CONVEYOR_COMPUTER_USE_ENABLED` | `false` | 总开关（整个功能）。 |
| `CONVEYOR_COMPUTER_DIRECT_ENABLED` | `false` | 开启 direct（免干预）模式。 |
| `CONVEYOR_COMPUTER_ALWAYS_DIRECT` | `false` | 为 true 时跳过授权（TTL）。 |
| `CONVEYOR_COMPUTER_MAX_STEPS` | `20` | 单任务最大步数。 |
| `CONVEYOR_COMPUTER_MAX_SECONDS` | `600` | 单任务最大墙钟秒数。 |
| `CONVEYOR_CUA_DRIVER_CMD` | `cua-driver mcp` | Mac 本地 cua driver 命令。Conveyor 使用第一个 token 定位本地 `cua-driver` binary，并通过 `call`/status 子命令执行。 |
| `CONVEYOR_COMPUTER_ALLOWED_ACTIONS` | `observe,click,type,hotkey,scroll,wait` | 动作允许清单。 |
| `CONVEYOR_COMPUTER_BLOCKED_KEYWORDS` | `password,passcode,bank,payment,crypto,keychain,system settings,delete account` | 命中即停的拦截词。 |
| `CONVEYOR_COMPUTER_BACKEND` | `http` | `http`（真实 Mac agent）或 `fake`（进程内，用于测试）。 |

**安全边界（即便在 direct 模式下也始终生效）**：动作允许清单、拦截词守卫、
不注入密钥、输入文本/快捷键在所有日志中脱敏、driver 返回结果白名单、
`MAX_STEPS`/`MAX_SECONDS` 上限，以及 `/computer_stop` 紧急停止。Cua 永不跨网络。
详见 `docs/desktop_security.md §7`。

### P5.1 Desktop Agent Heartbeat 心跳机制

在 P5.1 中，实现了本地 MacBook 桌面 agent 的注册与心跳：
* **VPS**: 默认绑定 `127.0.0.1:8766`。启动服务端：
  ```bash
  export CONVEYOR_DESKTOP_NODE_ENABLED=true
  export CONVEYOR_DESKTOP_AGENT_TOKEN=...
  .venv/bin/python desktop_agent_server.py
  ```
* **MacBook 节点**: 主动向 VPS 注册并发送心跳。启动 agent：
  ```bash
  export CONVEYOR_CONTROL_PLANE_URL=https://your-control-plane.example.com
  export CONVEYOR_DESKTOP_AGENT_TOKEN=...
  export CONVEYOR_DESKTOP_NODE_ID=macbook-payton
  export CONVEYOR_DESKTOP_NODE_NAME="Payton MacBook"
  .venv/bin/python desktop_agent.py
  ```

* **跨进程状态共享**: 桌面 agent 服务端与 bot 监听器进程间通过 `CODEX_MEMORY_ROOT/state/desktop_nodes.json` 共享心跳状态。此文件仅存储基本连接元数据，绝对不包含 token、秘钥或屏幕截图等敏感内容。
* **节点 ID 校验**: 本地 MacBook 桌面 agent 的 `CONVEYOR_DESKTOP_NODE_ID` 必须与 VPS 控制面配置的 `CONVEYOR_DESKTOP_NODE_ID`（默认为 `macbook-payton`）一致，否则请求将被服务端以 HTTP 400 拒绝。


### P5.2 Desktop Screenshot Observe（只读）

* **Helper**：在 `capture-your-screen` 仓库构建 `capture-screen-helper`，`CONVEYOR_DESKTOP_SCREENSHOT_HELPER` 必须是绝对路径。
* **本地截屏（Mac）**：`python desktop_agent.py --observe-once`
* **状态/元数据**：`/desktop_screenshot_status`、`/screenshot_status`，或 `截图状态` / `最近的截图`。这些命令**不会**截屏。
* **部署检查**：`/deploy_verify` 或 `scripts/deploy_verify_p5_2.py`（不截屏）。
* **飞书**：只读状态卡片（Refresh / Nodes；无截屏、上传、预览按钮）。
* 截图默认保存在 `CODEX_MEMORY_ROOT/desktop/screenshots/`，P5.2 不上传。

**P5.2.1 已支持：** Mac 本地单次截屏、元数据状态命令、飞书状态卡片。

**P5.2.1 不支持：** 上传、缩略图预览、视觉分析、鼠标/键盘/浏览器控制、Computer Use。

### P5.3 远程 Observe 请求（仅元数据）

* **聊天创建请求**：`/observe_request`、`/screenshot_request`，或 `截图看看我电脑现在是什么`
* **Mac 轮询**：`python desktop_agent.py --poll-observe --poll-computer`（注册 + 心跳 + observe / Computer Use 轮询）
* **状态查询**：`/observe_status`、`/screenshot_status`，或 `截图状态`
* **取消**：`/observe_cancel <request_id>`（仅 pending/claimed）
* VPS 在 `CODEX_MEMORY_ROOT/state/desktop_observe_requests.json` 存储请求
* P5.3.1 使用跨进程文件锁对 observe 请求存储进行硬化。这可以防止 Telegram、Feishu 和 `desktop_agent_server.py` 并发读写 `CODEX_MEMORY_ROOT/state/desktop_observe_requests.json` 时发生丢失更新。
* Mac 本地截屏，仅元数据回传 VPS — **不上传图片**


**P5.3 不支持：** 图片上传、缩略图预览、视觉分析、OCR、鼠标/键盘/浏览器控制、Computer Use。

**Mac 轮询部署：**

```bash
export CONVEYOR_CONTROL_PLANE_URL=https://your-control-plane.example.com
export CONVEYOR_DESKTOP_AGENT_TOKEN=...
export CONVEYOR_DESKTOP_SCREENSHOT_HELPER=/usr/local/bin/capture-screen-helper
python desktop_agent.py --poll-observe --poll-computer
```

**VPS 部署：**

```bash
cd /opt/conveyor
git fetch origin && git reset --hard origin/main
git rev-parse HEAD
.venv/bin/python scripts/deploy_verify_p5_2.py
sudo systemctl restart conveyor-telegram-bot conveyor-feishu-bot
```

**Mac 部署：** 构建 helper、配置绝对路径 helper、运行 `python desktop_agent.py --observe-once`。Screen Recording 权限需手动在 macOS 授予。

> **Computer Use 控制受控且默认关闭（P5.6）。** direct 模式通过 `/computer_arm` 或
> `CONVEYOR_COMPUTER_ALWAYS_DIRECT=true` 显式开启，并带硬性安全边界（动作允许清单、
> 拦截词守卫、脱敏、上限、紧急停止）。Cua 仅运行在 Mac agent。详见 `docs/desktop_security.md §7`。


上面列出的每一条都是仓库里已经实现的功能。没列在表里的 = 暂时还没有。

---

## 安全模型

Conveyor 是**单 operator 私有 VPS 控制面**，不是多租户 SaaS。模型设计简单
而且老实：

- **单 operator。** 一个 Telegram user id，一个飞书 open id，一台 VPS。
- **发送者白名单。** 不在 `TELEGRAM_ALLOWED_USER_ID` 或 `LARK_ALLOWED_OPEN_ID`
  里的消息一律拒收。这个白名单就是 bot 和公网之间唯一的门。
- **秘钥只活在 `.env` 里。** Token、App Password、OAuth refresh token、
  API key 都只被 bot 读取，绝不打印在聊天、日志、审计日志或 `repr()`。
  改完 `.env` 记得 `chmod 600`。
- **没有 SaaS 控制台。** 没有 Web UI、没有多用户 server、没有托管控制面。
  界面就是你的聊天软件。
- **危险操作需要确认。** 服务重启、调度器实时探针、发邮件、建日程、
  评论 GitHub —— 都必须显式 `确认执行` / `confirm`，或者点内联按钮。
  随口一句 `好` / `ok` / `是` 故意**不算**确认。
- **确认绑定到发起会话与 channel。** 在别的聊天或 channel 里确认同一个
  pending 操作，会被拒绝。
- **写和破坏性操作都进审计日志。** 所有 `WRITE_SAFE`、`WRITE`、
  `DESTRUCTIVE` 工具调用都会往 `audit/tools.log` 追加一条 redact 过的
  JSONL。用 `/audit_tools [n]` 查。
- **`/run` 和 `/fix` 会以很强的工作区权限跑 Codex。** 这是设计 —— 让
  Codex 真正能帮你改你服务器上的代码，前提是你不会把 bot 暴露给不受信
  的人。
- **不 commit、不 push、不 merge。** Apply 永远是显式的 `/apply`，在你
  看完 `/diff` 之后。bot 永远不会自己改 `main`。
- **安全合并与隔离通道（Safe Apply & Isolation Pass）。** 强制使用单任务（Per-job）独立 worktree、严苛的变更路径白名单/黑名单校验、对话历史 Prompt 注入防御以及飞书 strict startup mode。详情参见 [Apply Safety Policy (英文)](docs/apply_safety.md)。
- **Computer Use 默认关闭（P5.6）。** 直连桌面操作（鼠标/键盘/应用控制）需显式授权开启：通过 `/computer_arm` 或 `CONVEYOR_COMPUTER_ALWAYS_DIRECT=true` 进入 direct 模式，后端为 `trycua/cua`，仅运行在 Mac agent 本地，并带动作允许清单、拦截词、脱敏与紧急停止等安全边界。默认不会执行任何桌面动作。

这是个人基础设施，不是公开 chatbot。请按这个心智模型来用。

---

## 快速开始（10 分钟）

**前置：** Ubuntu VPS、SSH 访问、已安装 [`codex` CLI](https://github.com/openai/codex)、
一个 Telegram 账号。

### 1. 安装（在你笔记本上跑）

```bash
git clone https://github.com/mammut001/conveyor.git && cd conveyor
sudo bash scripts/install.sh
```

脚本会：

1. 装系统依赖；
2. 把代码同步到 `/opt/conveyor`；
3. 创建一个 Python `.venv`；
4. 引导你填 `.env`；
5. 装并启动 systemd 服务。

### 2. 配 `.env`

```bash
sudo nano /opt/conveyor/.env
```

最低配置：

```dotenv
TELEGRAM_BOT_TOKEN=123456789:从BotFather获取
TELEGRAM_ALLOWED_USER_ID=你的用户ID
CODEX_WORKSPACE_ROOT=/path/to/your/repo
```

改完 `chmod 600 .env`。systemd unit 通过 `EnvironmentFile=` 读它，绝不回显值。

### 3. 重启并测试

```bash
sudo systemctl restart conveyor-telegram-bot
sudo systemctl status conveyor-telegram-bot
```

打开 Telegram，给机器人发 `/start`，再试一下 `/run hello`。搞定。

### 4. 更新

```bash
cd conveyor && git pull
sudo bash scripts/install.sh --update
```

### 5. 可选：加飞书

见下方的[飞书接入](#飞书接入)。

---

## 飞书接入

Conveyor 装好之后，再把飞书那一侧配一下就行。

### 拿凭证

打开[飞书开放平台](https://open.feishu.cn/app) → 创建应用 →

- **凭证与基础信息** → 抄下 `App ID` 和 `App Secret`

这两个值写到 `.env` 的 `LARK_APP_ID` 和 `LARK_APP_SECRET`。

### 启用机器人能力

**应用能力 → 机器人 → 启用**（默认是关的——不打开的话事件列表里不会有
`im.message.receive_v1`）。

### 开权限（按需）

**权限管理** → 搜索并添加：

| 搜索关键字 | Scope | 用途 |
|------------|-------|------|
| `p2p` | `im:message.p2p_msg:readonly` | 收私聊消息 |
| `send_as_bot` | `im:message:send_as_bot` | 以机器人身份发消息 |
| `group_at` | `im:message.group_at_msg:readonly` | 群 @ 机器人（可选） |
| `user.id` | `contact:user.id:readonly` | 解析发送者（推荐） |

### 订阅事件

- **事件订阅** → 订阅方式 = **长连接 / persistent connection**
- **保存订阅前本地 bot 必须先跑起来**
- 添加事件：`im.message.receive_v1`（接收消息），订阅身份选 **应用身份**
  （不是机器人身份 —— 那是另一种长连接）

### 配 `.env`

```dotenv
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=replace_me
# LARK_ALLOWED_OPEN_ID=ou_xxx
```

`LARK_ALLOWED_OPEN_ID` 不写的时候，飞书 bot 处于 **bootstrap 模式**：任意
发送者都能收到回信，回信里带发送者的 `open_id`，让你填进 `.env` 然后
重启。一次性握手，省去你从日志里捞 ID 的麻烦。

写好 open_id 之后：

```bash
sudo systemctl restart conveyor-feishu-bot
sudo journalctl -u conveyor-feishu-bot -f
```

看到 `connected to wss://msg-frontier.feishu.cn/ws/v2...` 就连上了。

### 发版并安装到企业

**版本管理与发布** → 新建版本 → 审核（内部应用一般秒过）→ **申请发布**
→ **安装到企业**。**加了权限 scope 必须重新发版**才能在现网生效。

### 一次性核对表

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
- [ ] VPS systemd 单元装好且 active
- [ ] `journalctl -u conveyor-feishu-bot -f` 显示 `wss://msg-frontier.feishu.cn/...` 已连
- [ ] 给 bot 发私聊 → 收到 bootstrap 回信 → 把 `LARK_ALLOWED_OPEN_ID` 填进 `.env` → 重启

### 可选：卡片回调事件

Conveyor 在飞书用交互式消息卡片承载关键时刻：任务开始 / 完成 /
失败、`/diff` 预览、危险操作确认。卡片按钮是同一批斜杠命令（`/status`、
`/diff`、`/apply`、`/discard`、`/cancel`）和现有 token 确认机制的便捷
封装。

要启用按钮点击，**再多订阅一个事件**：

- **应用身份**，事件：`card.action.trigger`（**卡片回调**）

不开也能用：卡片正常渲染、聊天一切照旧，只是按钮点了 bot 收不到
（降级到手敲斜杠命令或 `确认执行` / `取消`）。开了之后，点按钮和
打字走同一套白名单和确认绑定——不受信的发送人点按钮也触发不了
任何动作。

如果飞书开发控制台在这个事件上报"回调地址无效"，那是它在要求
HTTP webhook URL——那是老通道。Conveyor 走长连接，URL 留空就行，
事件照常工作。

---

## 谁适合用

- **独立开发者**，平时就泡在聊天软件里，希望 Codex 随叫随到。
- **在 VPS 上跑 Codex CLI**、只是为了跑个小修改或看条日志就得 SSH 回去的人。
- **想要"手机到 agent"工作流**的人 —— 不在工位也能修测试、重启服务、
  整理收件箱。
- **想要私有个人自动化**的人 —— 笔记、提醒、简报、Codex，全在一个白名单
  聊天后面。
- **偏好自托管工具**的人 —— `.env` 是你的，VPS 是你的，worktree 是你的，
  审计日志也是你的。
- **想要 Telegram / 飞书当 coding / 运维界面**、不想再多一个 dashboard 的人。

## 不适合

- 公开 bot 或共享 workspace。
- 多用户团队 —— Conveyor 没有团队、角色、租户这些概念。
- 不受信的用户 —— `danger-full-access` 是真的，白名单是唯一的门，
  bot 真的会改你的仓库。
- 不了解 Codex 权限、worktree、"先 `/diff` 再 `/apply`" 这套纪律的人。
- 想要 SaaS 产品的人 —— 没有托管版 Conveyor。
- 想要 bot 自己 commit / push / merge 的人 —— 它不会。

---

## 喜欢这个项目？

如果 Conveyor 正合你的工作流，欢迎点个 Star。它能帮助其他自托管 agent
用户找到这个项目，也让我更有动力做下一轮改进。

---

## 参考：完整命令表

Telegram 和飞书共用同一份命令表。Channel-specific 行为见
[`docs/architecture.md`](docs/architecture.md)。

| 分组 | 命令 |
|------|------|
| Codex 任务 | `/run`、`/fix`、纯文本 → 在 detached worktree 里跑 Codex |
| 任务状态 | `/status`、`/last`、`/jobs [n]` |
| Worktree 控制 | `/diff`、`/apply`、`/discard`、`/cancel` |
| 任务队列 | `/queue`、`/queue_cancel <id>`、`/queue_clear`、`/queue_pause`、`/queue_resume` |
| 记忆 | `/memo`、`记 <内容>`、`/memory [date] [category]`、`/journal [n]` |
| 个人工具 | `/note`、`/notes [query]`、`/remind`、`/reminders` |
| 调度器 | `/scheduler_status`、`/scheduler_probe`、`/scheduler_probe_live` |
| 简报 | `/brief_today`、`/brief_tomorrow`、`/brief_settings`、`/brief_enable`、`/brief_disable`、`/brief_probe` |
| Gmail | `/gmail_status`、`/gmail_recent [n]`、`/gmail_search <q>`、`/gmail_read <id>`、`/email_send` |
| Google OAuth | `/auth_google`、`/google_status`、`/google_revoke` |
| 日历 | `/calendar_today`、`/calendar_tomorrow`、`/calendar_week`、`/calendar_search`、`/calendar_freebusy`、`/calendar_create` |
| 联系人 | `/contacts_search <q>` |
| GitHub | `/github_status`、`/github_issues`、`/github_issue <n>`、`/github_prs`、`/github_pr <n>`、`/github_ci`、`/github_create_issue`、`/github_comment` |
| 文件 / KB | `/files_roots`、`/files_search`、`/files_read`、`/kb_index`、`/kb_status`、`/kb_search`、`/project_docs` |
| Web | `/web_fetch`、`/web_text`、`/web_headers`、`/web_search`、`/research`、`/project_research` |
| 项目 | `/projects`、`/project_add`、`/project_use`、`/project_show`、`/project_remove`、`/project_status`、`/project_health`、`/project_roadmap`、`/project_next`、`/project_release_checklist`、`/project_brief`、`/project_export`、`/project_export_all`、`/project_import`、`/project_template` |
| 规划 | `/plan_today`、`/plan_dev`、`/planner_health`（alias `/project_health`）、`/inbox_triage`、`/schedule_review`、`/planners` |
| 设置 | `/setup`、`/setup_status`、`/setup_check`、`/setup_project`、`/setup_gmail`、`/setup_google`、`/setup_github` |
| 诊断 | `/health [full] [json] [nosecurity]`、`/doctor`、`/diag [since]`、`/diagnose [server\|bot\|logs\|quick]`、`/audit [stale-min]`、`/security [since]`、`/ratelimit [n]`、`/audit_tools [n]` |
| 报表 | `/metrics [n]`、`/log [sel]`、`/meta [sel]`、`/deploy_status` |
| 主机运维 | `/load`、`/vps`、`/htop`、`/ps`、`/disk`、`/logs`、`/service_status`、`/git_status` |
| 工具 | `/tools`、`/nl_help` |
| 自检 | `/smoke`、`/editcheck` |
| 维护 | `/maintain [keep]`、`/clean [keep]`、`/restart telegram\|feishu\|maintain` |
| 会话 | `/context`、`/forget` |
| **执行节点（P5.0 phase 0）** | `/nodes`、`/node_status`、`/computer_status` |
| **直连 Computer Use（P5.6，默认关闭）** | `/computer_status`、`/computer_arm [分钟]`、`/computer_task <目标>`、`/computer_stop`、`/computer_log [task_id]`、`/computer_screenshot`、`/computer_observe`、`/computer_action <json>` |
| 帮助 | `/help` |

危险等级（READ / WRITE_SAFE / WRITE / DESTRUCTIVE）和工具实现细节见
[`docs/architecture.md`](docs/architecture.md) 中的 `Agent tool layer` 一节。

---

## 参考：`.env` 全字段

同一份 `.env` 两个 channel 共用。只跑 Telegram 或只跑飞书的话，另一侧字段
空着就行。

```dotenv
# --- Telegram (bot.py) ---
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_ALLOWED_USER_ID=123456789

# --- Feishu (feishu_bot.py) ---
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=replace_me
LARK_ALLOWED_OPEN_ID=ou_xxx

# --- Codex（两个 channel 共用）---
CODEX_WORKSPACE_ROOT=/srv/my-repo
CODEX_BIN=/usr/local/bin/codex
CODEX_TASK_ROOT=/srv/conveyor

# LLM 提供商 —— OPENAI_API_KEY / MINIMAX_API_KEY 至少有一个
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

# --- 可选：执行节点（P5.0 phase 0）---
# 全部可选。desktop 节点是 stub，无论怎么配都是 offline。
# 协议草案见 docs/desktop_agent_protocol.md，安全契约见 docs/desktop_security.md。
# CONVEYOR_DESKTOP_NODE_ENABLED=false
# CONVEYOR_DESKTOP_NODE_ID=macbook-payton
# CONVEYOR_DESKTOP_NODE_NAME=Payton MacBook
# CONVEYOR_DESKTOP_AGENT_TOKEN=replace_me_with_long_random_string
# CONVEYOR_COMPUTER_USE_DEFAULT_MODE=observe_only
```

`CODEX_WORKSPACE_ROOT` 必须是 git 仓库的根目录。bot 会为每天创建一个
detached worktree，job 日志写在 `CODEX_TASK_ROOT` 下。Gmail、Google OAuth、
GitHub、Briefing、Scheduler、Web 抓取 / 搜索 / 研究、文件搜索 / KB 这些可选
字段在 [`.env.example`](.env.example) 里。

---

## 文件结构

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
    conveyor-scheduler.service
    conveyor-scheduler.timer
  channel/
    types.py              # InboundMessage, OutboundPort
    auth.py               # 各 channel is_allowed
    feishu_cards.py       # 飞书交互式消息卡片
  handlers/
    dispatch.py           # 单一入口：auth → command/memo/codex
    commands.py           # COMMAND_TABLE
    memo.py               # "记 x" / /memo 快路径
    jobs.py               # /run、/fix、纯文本 → CodexRunner
    intent.py             # route_intent (deterministic | hybrid | llm)
    nl_router.py          # 自然语言目录与路由
    tools/                # agent 工具层：registry、executors、audit
  nodes/                  # 执行节点（VPS + desktop stub, P5.0）
  personal_tools/         # 笔记、提醒、gmail、google、github、…
  scripts/                # CLI 工具、harness、smoke
  Makefile
  README.md               # 英文版（主）
  README.zh.md            # 中文版
  docs/
    architecture.md       # 架构与设计（中文）
    architecture.en.md    # Architecture & design (English)
    desktop_agent_protocol.md  # 未来本地 desktop agent 协议（P5.x）
    desktop_security.md   # desktop / Computer Use 安全契约
```

`docs/` 是仓库里**唯一**自带的文档目录。除了 `README.md` 和
`docs/architecture.md` 之外的笔记都在 operator 本地留着 —— 仓库定位是一个
小巧的个人工具，不要产品文档。

---

## 本地开发

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
make smoke           # 无 .env 的 AST/行为 smoke，<30s，发布前门禁
make smoke-all       # 还会跑要 .env 的脚本
```

`make smoke` 是发布前门禁，红了不能合。详见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。
仓库自带的 `scripts/docs_consistency_smoke.py` 会校验 README、架构文档、
运行时沙箱模式和当前服务命名是否一致。

设计笔记（运行时结构、channel 类型、agent 工具层、命令表、harness 矩阵、
backlog）见 [`docs/architecture.md`](docs/architecture.md)（英文版：
[`docs/architecture.en.md`](docs/architecture.en.md)）。

---

## 故障排查

| 症状 | 检查 |
|------|------|
| Telegram 没回 | `journalctl -u conveyor-telegram-bot`；确认 `TELEGRAM_ALLOWED_USER_ID` 等于你的 user id |
| 飞书私聊没回 | `im:message.p2p_msg:readonly` 已开且新版本已发布；`journalctl -u conveyor-feishu-bot` 找 `400` |
| 飞书：`Access denied. One of the following scopes is required: [im:message:send, im:message, im:message:send_as_bot]` | `im:message:send_as_bot` 没开，或新版本没发布，或没装到企业 |
| 飞书：每条消息都看到 `/contact/v3/users/batch ... 400` | `contact:user.id:readonly` 没开；无害但日志吵。补 scope、发版即可 |
| 飞书：WebSocket 秒断 | `.env` 值带前后空格、引号、中文标点。用 `nano` 重写 |
| 长连接保存失败 | 本地 `feishu_bot.py` 必须先跑起来 |
| Job 卡在 `running` | `/cancel` 或 `sudo systemctl restart conveyor-telegram-bot`；查日志里反复出现的 `Reconnecting... high demand` |
| Telegram 回复慢 | `TELEGRAM_PROGRESS_SECONDS`（默认 3s）控制占位编辑频率；Telegram 上限 20 edits/min |

### 实时 Telegram 烟测（手动，可选）

`scripts/telegram_live_smoke.py` 以**真实 Telegram 用户**身份（Telethon）
驱动 bot，端到端验证 agent 工具层。这是唯一能真正触发 bot `MessageHandler`
的方式 —— bot 用 Bot API 自己发的消息不会触发自己的 handler。

**不**在 `make smoke` 里。要用时手动装 Telethon：

```bash
pip install telethon
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
export TELEGRAM_BOT_USERNAME=your_bot_username
.venv/bin/python scripts/telegram_live_smoke.py --quick
.venv/bin/python scripts/telegram_live_smoke.py --full
```

重启确认默认**只发取消**。要真正重启 conveyor 服务，必须**同时**满足
两个开关：

```bash
TELEGRAM_LIVE_ALLOW_RESTART=1 \
  .venv/bin/python scripts/telegram_live_smoke.py --full --allow-restart
```

脚本不会打印 bot token、api hash、session 路径或 `.env` 内容；
`.telegram-live-smoke*` 已被 `.gitignore` 屏蔽。

---

## 自动 VPS 部署（GitHub Actions）

推送到 `main` 后，GitHub Actions 会自动 SSH 到 VPS，拉取最新代码，运行
smoke 测试，重启服务 —— 一步到位。smoke 失败则不重启。

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
3. 创建 `.venv` 并安装初始依赖：

   ```bash
   cd /opt/conveyor
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

   每次部署都会在 smoke 前从 `requirements.txt` 重新同步 `.venv` 依赖。
4. 安装 systemd 服务：

   ```bash
   sudo cp systemd/conveyor-telegram-bot.service /etc/systemd/system/
   sudo cp systemd/conveyor-feishu-bot.service   /etc/systemd/system/
   sudo cp systemd/conveyor-maintain.service     /etc/systemd/system/
   sudo cp systemd/conveyor-maintain.timer       /etc/systemd/system/
   sudo cp systemd/conveyor-scheduler.service    /etc/systemd/system/
   sudo cp systemd/conveyor-scheduler.timer      /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now conveyor-telegram-bot conveyor-feishu-bot
   sudo systemctl enable --now conveyor-maintain.timer conveyor-scheduler.timer
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

### 手动 VPS 更新 (v0.1.1 升级)

若要手动升级您的 VPS 并应用 v0.1.1 的安全加固更新，请执行以下命令：

```bash
cd /opt/conveyor
git pull
make smoke
sudo cp systemd/conveyor-telegram-bot.service /etc/systemd/system/
sudo cp systemd/conveyor-maintain.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart conveyor-telegram-bot
sudo systemctl restart conveyor-maintain.timer
python scripts/security_audit.py --env /opt/conveyor/.env --service conveyor-telegram-bot --since "24 hours ago"
```

### 工作流程

1. 每次推送到 `main`（或手动触发）都会触发 GitHub Actions。
2. Actions SSH 到 VPS，运行 `scripts/deploy_vps.sh`。
3. 脚本流程：
   - 获取 `flock` 锁（防止并发部署）
   - `git fetch origin main && git reset --hard origin/main`
   - 重置前备份关键文件
   - 从 `requirements.txt` 同步 `.venv` 依赖
   - 运行 `make smoke`
   - smoke 通过：重启 `conveyor-telegram-bot` + `conveyor-feishu-bot`
   - smoke 失败：退出非零，**不重启**服务
   - 写入 `.deploy-status.json` 部署元数据
   - 重启健康检查失败：从备份回滚并重试
4. `.env` 永远不会被打印或提交。

另有 rsync 方式部署（`scripts/deploy.sh`），用于本地推送源文件后执行同样
的远程 smoke + 重启流程。

### `/deploy_status` 命令

向机器人发送 `/deploy_status` 可查看：最近部署时间、来源、Git SHA、smoke
结果和服务状态、当前运行时 Git SHA / 分支 / progress mode，以及两个服务的
实时 `systemctl is-active`。

### 限制

- 实时 Telegram 烟测（`scripts/telegram_live_smoke.py`）**不会**自动运行
  —— 它需要真实 Telegram 凭据，仅限手动。
- 部署脚本假设 VPS 上已有 `.venv`，并会在每次部署时按 `requirements.txt`
  同步依赖。如果需要初始化新 VPS，请先运行 `scripts/install-remote.sh`。
- 回滚是有限的：重置前备份关键文件，如果重启后服务未启动，脚本会从备份
  恢复并重试。这不涵盖所有故障模式。

---

## 许可证

MIT —— 见 [`LICENSE`](LICENSE)。
