# 002 — 飞书 Bot 接入指南

> **状态**: Active  
> **日期**: 2026-06-09  
> **关联**: `feishu_bot.py`、`docs/001-hermes-learning-and-chat-mode.md`

---

## 1. 摘要

飞书通道用 **`lark-oapi` WebSocket 长连接** 收消息，后端复用现有 **`CodexRunner`**（与 Telegram bot 相同）。

---

## 2. 开发者后台配置

应用：[飞书开放平台](https://open.feishu.cn/app)

### 2.1 凭证

| 变量 | 来源 |
|------|------|
| `LARK_APP_ID` | 凭证与基础信息 → App ID |
| `LARK_APP_SECRET` | 凭证与基础信息 → App Secret |
| `LARK_ALLOWED_OPEN_ID` | 首次私聊后从日志/bootstrap 消息获取 |

**注意：** `im.message.receive_v1` 是**事件名**，不要在 Permissions 页搜索；在 **Events & Callbacks** 里添加。

### 2.2 权限（Permissions & Scopes）

搜索并开通（Tenant token scopes）。**少了任一项** bot 要么收不到消息，要么收到但回不出来：

| 搜关键词 | Scope | 用途 | 不开的后果 |
|----------|-------|------|-----------|
| `p2p` | `im:message.p2p_msg:readonly` | 收私聊消息 | 收不到 DM |
| `send_as_bot` | `im:message:send_as_bot` | bot 身份发消息 | 收得到但 `400: Access denied` |
| `group_at` | `im:message.group_at_msg:readonly` | 收群聊 @ 机器人 | 群里 @ 没反应（DM 不受影响） |
| `message` | `im:message` | 收 + 发消息的父 scope | 部分 SDK 调用会要求 |
| `im` → 「im:message:readonly」 | `im:message:readonly` | 读取消息元数据 | `on_message` 拿不到 `sender_id` 时 fallback |
| `reaction` | `im:message.reaction:readonly` *(可选)* | 表情回应回执 | 不需要就不开 |

**经验性确认**（2026-06-09 oracle VPS 跑通时实际依赖的最小集）：

```
im:message.p2p_msg:readonly
im:message:send_as_bot
```

加上 `im:message.group_at_msg:readonly` 才能在群里用。`im:message` / `im:message:readonly` 在 1.4.x 之后部分 API 会隐式要求，开了更稳。

**怎么验**：在开发者后台「权限管理」点「开通」后，**必须回到「版本管理与发布」创建新版本 + 发布**，否则线上仍是旧 scope 集。

### 2.3 事件（Events & Callbacks）

1. **Subscription method** → **Receive events through persistent connection**（长连接）
2. **Add events** → 应用身份 → **Receive message**（`im.message.receive_v1`）
3. 保存前需 **本地先运行 `feishu_bot.py`**（长连接在线才能保存）

> 注意：长连接只支持在「**应用身份**」下订阅消息事件；「机器人身份」需要在「机器人」菜单单独配置，并启用「机器人能力」开关（默认关闭）。

### 2.3.1 机器人能力开关

- **应用能力 → 机器人 → 启用**（默认关闭，**这是消息能到达 bot 的硬性前提**）
- 启用后，事件订阅里才能看到 `im.message.receive_v1` 选项

### 2.3.2 添加事件时选错身份

`im.message.receive_v1` 在「应用身份」和「机器人身份」下都可能出现，但长连接 bot 只能挂在**应用身份**下。选错会导致保存后 event 永远不进 `feishu_bot.py` 的 `on_message`，日志看到连接正常却零回调。

### 2.4 发布

改权限/事件后：**创建版本 → 发布 → 安装到企业**。

---

## 3. 本地运行（打通长连接）

```bash
cd telegram_codex_runner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 最小 .env（与 Telegram 共用 Codex 配置）
cat >> .env <<'EOF'
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=replace_me
# LARK_ALLOWED_OPEN_ID=   # 留空 = bootstrap 模式，首条消息会回显 open_id
CODEX_WORKSPACE_ROOT=/path/to/your/git/repo
CODEX_BIN=codex
EOF

python feishu_bot.py
```

保持终端运行 → 回后台保存长连接 + 事件 → 飞书私聊 Bot 发「你好」。

首条消息若处于 bootstrap 模式，Bot 会回复你的 `open_id`；写入 `.env` 后重启。

---

## 4. VPS 部署

与 Telegram bot 并行（独立 systemd 单元）：

```bash
sudo cp systemd/codex-feishu-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-feishu-bot
sudo journalctl -u codex-feishu-bot -f
```

`.env` 需同时包含 Codex 变量（`CODEX_WORKSPACE_ROOT` 等）和 `LARK_*`。

---

## 5. 命令

| 输入 | 行为 |
|------|------|
| 纯文本 | Codex job（`JobMode.RUN`） |
| `记 xxx` / `/memo xxx` | MEMORY 快路径 |
| `/status` `/last` `/cancel` | 运维 |
| `/help` | 帮助 |

---

## 6. 与 Telegram 的差异

| 维度 | Telegram | 飞书 |
|------|----------|------|
| SDK | python-telegram-bot | lark-oapi FeishuChannel |
| 收消息 | long polling | WebSocket 长连接 |
| 白名单 | `TELEGRAM_ALLOWED_USER_ID` | `LARK_ALLOWED_OPEN_ID` |
| 流式进度 | edit_message_text | 当前版：完成后一次回复 |
| Onboarding | `/onboard` 问卷 | 暂未移植（用 .env OPERATOR_*） |

---

## 7. 故障排查

| 现象 | 检查 |
|------|------|
| 长连接保存失败 | `feishu_bot.py` 是否在跑 |
| 私聊无回复 | `im:message.p2p_msg:readonly` 是否开通并已发版 |
| Unauthorized | `LARK_ALLOWED_OPEN_ID` 是否与 sender 一致 |
| Job 卡住 | `/cancel` 或重启服务 |
| 收到消息但回不出 (`Access denied. One of the following scopes is required: [im:message:send, im:message, im:message:send_as_bot]`) | `im:message:send_as_bot` 没开通 / 没发版 / 企业未安装应用 |
| 私聊有提示但 `/status` 等命令无响应 | 消息进了 bot 但 `on_message` 抛异常 → 看 `journalctl -u codex-feishu-bot` 找 traceback |
| bot 启动了但 WebSocket 立即断 | 检查 `LARK_APP_ID` / `LARK_APP_SECRET` 是否带空格 / 引号 / 中文标点 |

---

## 8. 通道解耦（后续）

飞书与 Telegram 的 **handler / harness 统一方案** 见 [`docs/003-channel-decoupling.md`](003-channel-decoupling.md)（`channel/` + `handlers/` + `InboundMessage`）。

---

## 9. 权限/事件一次性核对表

新装飞书 bot 时按这个清单打勾，少一个会卡：

- [ ] 凭证页：`LARK_APP_ID` (App ID) 复制到 `.env`
- [ ] 凭证页：`LARK_APP_SECRET` (App Secret) 复制到 `.env`
- [ ] 应用能力 → 机器人：**已启用**
- [ ] 权限管理：`im:message.p2p_msg:readonly` **已开通**
- [ ] 权限管理：`im:message:send_as_bot` **已开通**
- [ ] （可选）`im:message.group_at_msg:readonly` **已开通**
- [ ] （可选）`im:message` / `im:message:readonly` **已开通**
- [ ] 事件订阅：方式 = **长连接**，**本地 bot 已运行**
- [ ] 事件订阅：身份 = **应用身份**，事件 = `im.message.receive_v1` **已添加**
- [ ] 版本管理与发布：**新版本已创建 + 已发布 + 已安装到企业**
- [ ] VPS：`pip install -r requirements.txt` 包含 `lark-oapi>=1.4.0`
- [ ] VPS：`.env` 写入 `LARK_APP_ID` / `LARK_APP_SECRET`（不写历史 / 仓库）
- [ ] VPS：`sudo cp systemd/codex-feishu-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload`
- [ ] VPS：`sudo systemctl enable --now codex-feishu-bot`
- [ ] VPS：`journalctl -u codex-feishu-bot -f` 看到 `connected to wss://msg-frontier.feishu.cn/...`
- [ ] 飞书私聊 bot：发「hi」得到 bootstrap 回复
- [ ] `.env` 写 `LARK_ALLOWED_OPEN_ID=<your open_id>`，重启服务
