# 003 — 通道解耦（Channel Decoupling）

> **文档编号**: 003  
> **状态**: Draft → Active（设计稿，待分阶段落地）  
> **日期**: 2026-06-09  
> **受众**: 维护 telegram_codex_runner 的开发者  
> **关联**: `docs/001`、`docs/002-feishu-bot-setup.md`、`project.md` §13、`runner/`、`bot.py`、`feishu_bot.py`

---

## 1. 摘要

项目已有 **两个 IM 入口**（Telegram `bot.py`、飞书 `feishu_bot.py`），但 **Brain 只有一个**（`CodexRunner`）。当前问题是：命令路由、memo 快路径、job 进度回显、鉴权白名单都 **绑在 Telegram 的 `Update` 类型上**；飞书侧是 **复制粘贴**，harness/smoke 也 **只覆盖 Telegram**。

本文定义 **通道适配层（Channel Adapter）** 的目标形状，把「怎么收发包」与「怎么处理消息」拆开，使 harness 测 **handler 层** 而非某个 SDK 的 fake 对象。

**原则**：`runner/` 继续只管 Codex；新包 `handlers/`（或 `channel/`）管 IM 语义；`bot.py` / `feishu_bot.py` 缩成 **薄入口**（wire-up + 平台 SDK）。

---

## 2. 现状（As-Is）

```text
                    ┌─────────────────────────────────────┐
                    │           CodexRunner               │
                    │  worktree · prefetch · streaming    │
                    │  memo · lifecycle · metadata        │
                    └──────────────▲──────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         │                         │                         │
  bot.py │ Telegram Update         │ feishu_bot.py           │
         │ python-telegram-bot     │ lark-oapi FeishuChannel │
         │ · 30+ command handlers  │ · 复制 memo/命令/鉴权    │
         │ · _start_job + edit_msg │ · 完成后一次 reply      │
         │ · onboarding 问卷       │ · 无 onboarding         │
         └─────────────────────────┴─────────────────────────┘

  harness / smoke
  ├── runner 层（共用）     memo_smoke, classify_memo, progress(streaming) …
  ├── Telegram 专用       command_harness (FakeUpdate), progress_smoke (e2e /start)
  └── 飞书                  （无）
```

### 2.1 已共用（无需 decouple）

| 模块 | 说明 |
|------|------|
| `runner/` | Job 队列、git worktree、`codex exec --json`、MEMORY 归档 |
| `config._load_codex_fields()` | workspace、operator、timeout、retry |
| `redaction.py` | 输出截断与脱敏 |
| `scripts/submit_job.py` 等 | 绕过 IM，直接调 `CodexRunner` |
| `make smoke` 大部分 | 测 runner 行为，与通道无关 |

### 2.2 重复 / 耦合（decouple 目标）

| 逻辑 | Telegram | 飞书 | 问题 |
|------|----------|------|------|
| 白名单鉴权 | `_guard(update)` | `_allowed(sender_id)` | 平台 ID 类型不同 |
| memo 快路径 | `_handle_memo_fast_path` | `_handle_memo` | 正则 + classify 重复 |
| 命令表 | `/status` `/diff` … 20+ | `/status` `/last` 子集 | 无单一来源 |
| Codex job + 进度 | `_start_job` + `edit_message_text` | `_start_job` + 单次 `send` | 进度策略分裂 |
| onboarding | `/onboard` ConversationHandler | 无 | 可做成可选 handler |
| harness | `FakeUpdate` / `FakeBot` | 无 | 无法共享 e2e |

---

## 3. 目标架构（To-Be）

```text
  Telegram SDK          Feishu SDK
  bot.py                feishu_bot.py
      │                      │
      ▼                      ▼
  TelegramAdapter       FeishuAdapter        ← 各 ~100 行：SDK → 统一类型
      │                      │
      └──────────┬───────────┘
                 ▼
         InboundMessage                    ← 通道无关入站模型
                 │
                 ▼
         MessageDispatcher                  ← handlers/dispatch.py
           · auth (operator allowlist)
           · route: command | memo | codex job | onboarding
                 │
       ┌─────────┼─────────┐
       ▼         ▼         ▼
   handlers/  handlers/  handlers/
   commands   memo       jobs
       │         │         │
       └─────────┴─────────┘
                 │
                 ▼
         OutboundPort                       ← 抽象「怎么回用户」
         · send_text / edit_progress / reply_thread
                 ▲
                 │
         CodexRunner (unchanged)
```

### 3.1 核心类型（建议）

```python
# channel/types.py — 通道无关，不 import telegram / lark_oapi

@dataclass(frozen=True)
class InboundMessage:
    channel: Literal["telegram", "feishu"]
    operator_id: str          # Telegram user id 或 Feishu open_id（字符串统一）
    chat_id: str              # 会话 ID（Telegram int → str）
    message_id: str | None    # 用于 reply/thread
    text: str
    chat_type: Literal["p2p", "group", "unknown"]
    mentioned_bot: bool = False

@dataclass
class OutboundPort:
    """Handler 依赖的出站能力；各 Adapter 实现。"""
    async def reply(self, msg: InboundMessage, text: str) -> str | None: ...
    async def edit_progress(self, placeholder_id: str, text: str) -> None: ...
    async def send_new(self, msg: InboundMessage, text: str) -> None: ...
```

**设计要点**

- `operator_id` 统一为 `str`，adapter 负责 `str(telegram_user_id)` / feishu `open_id`。
- Handler **禁止** import `Update`、`FeishuChannel`。
- `OutboundPort.edit_progress`：Telegram 实现为 `edit_message_text`；飞书首版可 **degrade 为 throttle 后的新消息**，后续再接卡片流式。

### 3.2 配置

```python
# config.py — 扩展，不破坏现有 load_settings()

@dataclass(frozen=True)
class ChannelAuth:
    telegram_allowed_user_id: int | None = None
    lark_allowed_open_id: str | None = None

def is_allowed(msg: InboundMessage, auth: ChannelAuth) -> bool:
    if msg.channel == "telegram":
        return msg.operator_id == str(auth.telegram_allowed_user_id)
    if msg.channel == "feishu":
        if not auth.lark_allowed_open_id:
            return True  # bootstrap：首条回显 open_id（仅 feishu adapter 特殊处理）
        return msg.operator_id == auth.lark_allowed_open_id
    return False
```

Telegram-only / Feishu-only 部署仍用同一份 `.env`；未用的通道字段可省略。

### 3.3 MessageDispatcher（单一路由）

```python
# handlers/dispatch.py

async def dispatch(msg: InboundMessage, port: OutboundPort, runner: CodexRunner) -> None:
    if not is_allowed(msg, auth):
        await port.reply(msg, "Unauthorized.")
        return
    if is_command(msg.text):
        await handle_command(msg, port, runner)
        return
    if detect_memory_intent(msg.text):
        await handle_memo(msg, port, runner)
        return
    await handle_codex_job(msg, port, runner, mode=JobMode.RUN)
```

命令表集中在一处（`handlers/commands.py`），Telegram `/diff` 与飞书 `/diff` 行为一致；平台不支持的 UI（如 inline onboarding 按钮）由 **adapter 可选能力** 暴露：

```python
class OutboundPort(Protocol):
    supports_inline_buttons: bool = False
    async def reply_with_buttons(self, msg, text, buttons): ...
```

---

## 4. 文件布局（建议）

```text
telegram_codex_runner/
  channel/
    types.py           InboundMessage, OutboundPort (Protocol)
    auth.py            is_allowed, bootstrap hint
    telegram.py        TelegramAdapter: Update → InboundMessage
    feishu.py          FeishuAdapter: InboundMessage ↔ FeishuChannel
  handlers/
    dispatch.py        MessageDispatcher
    commands.py        /status /diff /apply … 纯函数，依赖 runner + port
    memo.py            memo 快路径（从 bot.py 抽出）
    jobs.py            start_job + progress 策略（从 _start_job 抽出）
    onboarding.py      可选；Telegram 专用，由 dispatch 按 channel 分支
  bot.py               Application + handler 注册 → TelegramAdapter
  feishu_bot.py        FeishuChannel.connect → FeishuAdapter
  runner/              不变
```

**边界规则**

| 层 | 允许 import | 禁止 |
|----|-------------|------|
| `runner/` | config, redaction, scripts/* | telegram, lark_oapi, handlers |
| `handlers/` | runner, channel.types, redaction | telegram, lark_oapi |
| `channel/*.py` | 对应 SDK + handlers + channel.types | 业务命令逻辑 |
| `bot.py` / `feishu_bot.py` | channel adapter + handlers.dispatch | 直接调 runner（除 wire-up） |

---

## 5. Harness / Smoke 迁移

### 5.1 现状

| 脚本 | 测什么 | 通道 |
|------|--------|------|
| `memo_smoke.py` 等 | runner / memo | 无 |
| `progress_smoke.py` | streaming + **Telegram e2e** | Telegram |
| `command_harness.py` | bot 命令 + **FakeUpdate** | Telegram |

### 5.2 目标

```text
make smoke
  ├── runner smokes（不变）
  ├── handlers_smoke.py      FakeOutboundPort + InboundMessage → dispatch
  └── channel/telegram_smoke.py   可选：薄层 adapter 转换
  └── channel/feishu_smoke.py     bootstrap · allowlist · command routing
```

**`handlers_smoke.py` 契约示例**

1. `InboundMessage(channel="feishu", operator_id="ou_x", text="hi")` → 触发 `handle_codex_job`
2. `text="/status"` → 调用 `runner.status_text()`，port 收到一条 reply
3. `text="记 foo"` → `append_memo`，不经 codex
4. `operator_id` 不在白名单 → `"Unauthorized."`

**`command_harness.py` 迁移**

- 短期：保留，内部改为构造 `InboundMessage` + `FakeOutboundPort`，再调 `dispatch()`。
- 长期：重命名为 `handlers_harness.py`，删除 `FakeUpdate`。

**`progress_smoke.py`**

- streaming 相关用例 **留在 runner 层**（已如此）。
- Telegram placeholder/edit 用例移到 `channel/telegram_smoke.py` 或 adapter 单测。

---

## 6. 分阶段落地（推荐顺序）

### P0 — 抽 handler，零行为变化（Telegram 先行）

| 步骤 | 内容 | 验收 |
|------|------|------|
| P0.1 | 新增 `channel/types.py`、`handlers/memo.py`、`handlers/jobs.py` | `make smoke` 全绿 |
| P0.2 | `bot.py` 的 `_handle_memo_fast_path` / `_start_job` 改为调用 handlers | Telegram 手动回归 |
| P0.3 | `feishu_bot.py` 删除重复逻辑，改 import handlers | 飞书私聊 hi + Codex 一条链路 |
| P0.4 | `handlers_smoke.py` + 加入 `Makefile SMOKE_FREE` | CI 覆盖 dispatch |

**不做的**：不大改 onboarding；不强制飞书进度 UI 对齐 Telegram。

### P1 — 命令表统一 + harness 迁移

| 步骤 | 内容 |
|------|------|
| P1.1 | `handlers/commands.py` 收纳全部 `/diff` `/apply` … |
| P1.2 | `feishu_bot.py` 通过同一 `dispatch` 获得完整命令集（按平台裁剪） |
| P1.3 | `command_harness.py` → 基于 `InboundMessage` |

### P2 — Adapter 完整化 + 可选能力

| 步骤 | 内容 |
|------|------|
| P2.1 | `TelegramAdapter` / `FeishuAdapter` 独立文件 |
| P2.2 | 飞书 progress：卡片流式或 throttle reply |
| P2.3 | onboarding 作为 `handlers/onboarding.py`，仅 `channel=="telegram"` 注册 |
| P2.4 | 单进程双通道（可选）：一个 event loop 跑 Telegram polling + Feishu WS |

---

## 7. 与现有文档的关系

| 文档 | 关系 |
|------|------|
| `001` | Brain = CodexRunner 不变；对话模式（chat-first）在 `handlers/jobs.py` 实现 |
| `002` | 飞书接入退化为 **FeishuAdapter + dispatch**；长连接/ws 仍在 adapter |
| `project.md` §13 | 「Hermes tool-dispatch」与「Channel decouple」正交；本文只管 IM 层 |

---

## 8. 非目标（Out of Scope）

- 不把 `lark-cli` 并入 dispatch（CLI 仍是 Codex tool-registry 可选能力，见 001 §7）。
- 不在 decouple 阶段重写 `runner/` 或改 Codex sandbox 语义。
- 不做多 tenant / 多 operator 白名单（仍单 operator，双通道各一个 ID）。
- 不替换 `submit_job.py` CLI 路径。

---

## 9. 风险与决策记录

| 风险 | 缓解 |
|------|------|
| 大 bang refactor 弄断 Telegram | P0 只动 memo + job 两条热路径；smoke 先行 |
| 飞书进度体验差 | P0 接受「完成后一次回复」；P2 再做卡片 |
| onboarding 强依赖 Telegram UI | 保留 channel 分支，不强行抽象按钮 |
| Settings /dataclass 膨胀 | `ChannelAuth` 子结构；`load_feishu_settings` 保留 |

---

## 10. 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| 1.0 | 2026-06-09 | 初稿：As-Is / To-Be、类型、目录、harness 迁移、P0–P2 |
