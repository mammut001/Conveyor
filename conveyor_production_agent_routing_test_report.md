# Conveyor 生产 Agent 路由验收报告

日期：2026-10-16  
范围：只读生产基线、Telegram 真实客户端测试、脱敏日志观察。未改代码或 `.env`，未重启服务，未使用回滚脚本。

## 总体结论

**验收未通过 / 部分阻断。** 生产 HEAD 和服务基线符合预期，七个指定开关在三个运行服务进程中均为开启。真实 Telegram 私聊的 `/agents` 回复只列出 `main — default assistant`，没有可用于测试的命名 agent。因此所有依赖命名 agent 的路由、委派、停止、kill-switch 路由和限流测试都无法执行，不能报 PASS。

已通过的范围有限：通过当前已登录的 Telegram Web 会话发送了 `/agents` 和一条不含 agent 名称的普通闲聊，bot 返回 agent 列表和默认助手回复。owner allowlist 与当前 Telegram Web 账号未能通过日志独立比对；日志没有记录这两条消息对应的身份标识。未输出任何账号/聊天 ID 或凭据。

## 环境确认

| 项目 | 结果 |
|---|---|
| SSH 身份 | 现有 SSH alias 以 `ubuntu` 建立连接；仓库 HEAD 查询通过 `sudo -u deploy` 执行 |
| `/opt/conveyor` HEAD | PASS — `ffb6b960864f335117f73be91156cd99a9bbc188` |
| `conveyor-web` | PASS — active |
| `conveyor-telegram-bot` | PASS — active |
| `conveyor-feishu-bot` | PASS — active |
| 七个指定 `CONVEYOR_*` 开关 | PASS — 在以上三个服务进程中均存在且开启；未输出开关值 |
| Telegram 客户端 | Telegram Web（`web.telegram.org/a/`）；页面未显示可核实的客户端版本 |
| 目标 bot | 通过运行服务凭据调用只读 `getMe` 确认身份；只输出公开 bot 用户名，没有打印或记录 token |

部署用户的 SSH 公钥登录不可用；只读仓库操作通过现有 SSH 连接再切换到 `deploy` 身份完成。未改变 SSH 配置或权限。

## Telegram 实测

| 步骤 | 裁决 | 观察 |
|---|---|---|
| a. `/agents` | **FAIL** | 真实私聊返回 `Available Agents: • main — default assistant`；没有命名 agent |
| b. `/agent <name>` 与隔离上下文 | **BLOCKED** | 没有命名 agent 名称可测试；未发送猜测名称 |
| c. `@<name> 帮我总结一下今天要做什么` 与去重推送 | **BLOCKED** | 无命名 agent，未触发委派或创建执行任务 |
| d. `让 <name> 查一下现在几点` | **BLOCKED** | 无命名 agent，未测试中文显式路由 |
| e. 不提 agent 的普通闲聊 | **PASS（有限范围）** | 发送普通闲聊路由测试消息，收到默认助手的普通回复；未观察到命名 agent 回复。由于系统没有命名 agent，不能据此证明命名路由隔离 |
| f. 委派后发送 stop | **BLOCKED** | 没有目标 agent；未创建委派任务 |
| g. `/agentkill on`、目标路由拒绝、再 `/agentkill off` | **NOT RUN** | 没有目标 agent；为避免无测试目标时切换生产全局开关，未启停 kill switch |
| h. 连发 10 条 `@<name>` 验证限流 | **BLOCKED** | 没有目标 agent；未发送 10 条生产消息 |
| Web Console 抽查 | **NOT RUN（可选）** | 本轮未登录 Web Console |

实际发送的消息会保留在 Telegram 私聊历史中；除此之外未主动检查或改动生产任务、数据库、会话、配置或工作区状态。

## 脱敏日志判定

从开始测试到复核服务状态期间，实时 journal 观察器只输出经过归类的服务名和事件标签，没有打印原始消息、token、密钥、账号 ID 或聊天 ID。观察到的安全化标签如下：

```text
conveyor-web.service                 AGENT/ROUTING keyword marker observed once
conveyor-telegram-bot.service        AGENT/ROUTING keyword marker observed once around ordinary-chat test
PTBUserWarning                       no match observed
TRACEBACK                            no match observed
ERROR / FATAL                        no match observed
service stop/start/restart           no match observed
```

这里的 `AGENT/ROUTING` 是日志关键词过滤器的归类标签，不足以单独证明发生了某个具体路由动作；这些行是脱敏分类结果，不是原始 journal 行。原始消息内容未导出。测试后复核三个服务仍为 active，HEAD 未改变。近 10 分钟 journal 中没有可用于比对 owner allowlist 的测试消息记录，所以 owner 身份验证仍未完成。

## 严重程度排序与复现

1. **验收阻断 / 高：运行时没有命名 agent。** 复现：在目标 bot 私聊发送 `/agents`；当前只显示 `main — default assistant`。尽管七个开关已开启，命名 agent 功能没有可见目标。原因尚未调查；本轮按要求未读写 `.env`、代码或其他配置。
2. **验证限制：owner 身份未独立确认。** 使用的是现有 Telegram Web 登录会话，bot 有回复；但日志未记录可与服务端 owner allowlist 做不泄露 ID 的匹配证据。
3. **其余路由项暂不能裁决。** 在命名 agent 出现在 `/agents` 后，使用真实客户端依次复现用户要求的 b–h 流程；不要把本轮 BLOCKED/NOT RUN 当作 PASS。

没有执行修复、部署、回滚、服务重启或生产配置变更。
