# 001 — 架构定位、Hermes 对照与对话模式

> **文档编号**: 001  
> **状态**: Active  
> **日期**: 2026-06-09  
> **受众**: 维护 telegram_codex_runner 的 operator / 开发者  
> **关联**: `project.md` §13、`CHANGELOG.md` Honest gaps  

---

## 1. 摘要

本项目是 **Telegram 遥控器 + Codex CLI 执行壳**：Brain 与 Hands 都在 VPS 上的 `codex exec --json`，Python 层负责鉴权、prompt 注入、worktree、日志与 MEMORY 归档。

**2026-06-09 产品转向**：默认对话（纯文本、`/run`、`/fix`）统一为 **workspace-write**，不再区分 read-only 与 fix sandbox。Operator 像日常聊天一样发消息即可，无需记得 `/fix` 才能跑 shell 或查 IP。

目标体验接近 OpenClaw / Hermes 类 personal agent，实现哲学是 **Codex 即 Agent**（路线 A），而非 Hermes 的自研 90 轮 tool loop（路线 B）。

---

## 2. 对话模式（Chat-first）

### 2.1 行为

| 触发方式 | JobMode（日志） | Codex `--sandbox` | 能力 |
|----------|-----------------|-------------------|------|
| 纯文本 | `run` | `workspace-write` | shell、web、读写 worktree、runner CLI |
| `/run` | `run` | 同上 | 同上 |
| `/fix` | `fix` | 同上 | 同上（与纯文本等价，保留命令兼容） |
| `记 xxx` / `/memo` | — | — | **不经过 Codex**，直接写 MEMORY.md |

### 2.2 设计理由

- 个人 bot、单 operator：read-only 边界带来的「查 IP 必须 /fix」不符合对话直觉。
- 安全仍靠：Telegram 白名单、worktree 隔离、`/diff` + `/apply` 才合入主仓库、输出 redaction。
- `/run` 与 `/fix` 保留仅为兼容旧习惯与 job 日志区分，**sandbox 已统一**。

### 2.3 Prompt 注入（每次 Codex 调用前）

顺序见 `runner/prefetch.py`：

1. `<operator-profile>` — 身份、语言、风格  
2. `<day-brief>` — 每天首个 job 的冷启动摘要  
3. `<memory-context>` — 当日 `MEMORY.md`  
4. `<tool-registry sandbox="workspace-write">` — shell、memorize、recall 等  
5. 用户消息  

---

## 3. 系统架构

```text
Telegram → bot.py → CodexRunner → codex exec --json
                      ├── git worktree (day-YYYY-MM-DD)
                      ├── logs/<job-id>/
                      └── ~/.codex/ (JOURNAL, operator.json)

maintain.timer → auto_maintain.py（与 bot 独立）
```

### VPS 路径（示例）

| 用途 | 路径 |
|------|------|
| Bot 代码 | `/opt/codex-telegram-runner/` |
| 用户仓库 | `/srv/codex-telegram-test-repo/` |
| 任务根 | `/srv/codex-telegram-runner/` |
| 当日 worktree | `.../worktrees/day-YYYY-MM-DD/` |

---

## 4. 与 Hermes 的对照

| 维度 | Hermes | 本项目 |
|------|--------|--------|
| Agent 内核 | Python `AIAgent` 循环 | **Codex CLI** |
| Tool 调用 | JSON Schema + dispatch | Prompt `<tool-registry>` + Codex shell |
| 多轮 | SQLite SessionDB | 每消息一 job（**待改进**：session 摘要） |
| 通道 | 多 platform | Telegram 单用户 |
| 记忆 | 可插拔 + Skills | MEMORY.md → JOURNAL |

**已从 Hermes 借鉴**：onboarding、day-brief、streaming 聊天感、MEMORY 归档。  
**建议继续学**：gateway interrupt、session 摘要、审批按钮、reconnect fail-fast（已实现基础版）。

详见 §7 Backlog。

---

## 5. 运维：Stuck Job（2026-06-09）

**现象**：Job `20260609-015905-9d077acb` 长期 `running`；Codex JSONL 连续 `Reconnecting... high demand`；新消息报 `already running`。

**恢复**：Telegram `/cancel` 或 `sudo systemctl restart codex-telegram-bot.service`。

**代码**：`runner/streaming.py` 在连续 reconnect 错误达阈值时终止子进程，触发 429 重试或失败释放锁。

---

## 6. 部署

```bash
export CODEX_TELEGRAM_REMOTE=ubuntu@<host>
bash scripts/deploy.sh
```

`deploy.sh` 同步 `runner/` 包与根目录 shim `runner.py`。勿对 VPS 根目录 `rsync --delete`（会删 `.env`）。

---

## 7. Backlog（优先级）

1. **P0** Session 摘要（最近 K 轮注入 prefetch，多轮「接着聊」）  
2. **P1** Telegram 审批按钮（`/apply` 确认）  
3. **P1** 用户 cron（`submit_job.py` + timer）  
4. **P2** Hermes-style tool dispatcher（仅当 per-tool 指标成痛点）  

---

## 8. Operator 速查

| 目的 | 做法 |
|------|------|
| 正常对话 | 直接发文字 |
| 记住 | 「记 xxx」或 `/memo` |
| 看记忆 | `/memory` |
| 看改动 | `/diff` → `/apply` / `/discard` |
| 卡住 | `/cancel` |
| 体检 | `/health` |

---

## 9. 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| 1.0 | 2026-06-09 | 初稿 + 对话模式统一 workspace-write |
| 0.1 | 2026-06-09 | 架构与 Hermes 对照草稿 |
