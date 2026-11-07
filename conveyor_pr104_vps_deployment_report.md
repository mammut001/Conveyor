# Conveyor PR #104 Oracle VPS 部署与生产验收报告

## 最终裁决

**PR #104 部署成功；完整生产功能验收待完成。** 生产已从 `b03f2d19658926134013b97ab32ab2b7cb82d229` 升级到 `2a29b11ee2041738745ccbe211287b39285051a3`。Atomic Deploy Fence 已释放，部署 Token 不存在，部署锁空闲；授权服务均已恢复。Telegram 四个命令已在真实聊天中验证。当前 Telegram Web 界面没有显示旧版 Reply Keyboard，因此两个旧键盘按钮仍须人工点击，不能把整项验收报告为完全 PASS。

## 授权与执行时间

PR：[#104](https://github.com/mammut001/Conveyor/pull/104)，已合并至 `main`。用户最初提供 `2026-10-11T07:00:00Z` 至 `08:00:00Z` 的窗口，随后明确改为“现在就弄”，并指定以工作完成为结束时间。因此本次按立即执行授权开始，没有等待 07:00Z；动态预检约于 `2026-10-11T06:44Z` 完成，生产部署约于 `06:45Z` 开始，`06:48Z` 完成部署后只读核验。实际期间所有重启操作均在用户授权范围内。

部署通过现有 `/opt/conveyor/scripts/deploy_vps.sh` 和 Atomic Deploy Fence 执行；未修改部署脚本，GitHub 自动部署保持 manual-only。

## 动态预检和数据保护

维护开始时的动态预检全部通过后才启动部署：目标 `origin/main` SHA 精确匹配；生产旧 SHA 与授权起点一致；tracked 工作区干净且 `git clean -nd` 没有待删除的生产文件；队列 queued/running 为 `0/0`；无活动 human takeover；无占用的部署锁或 Fence；备份可用；授权服务状态符合预期。

q3 保持 `interrupted` 且 `session_id` 为空；没有重放、修改或删除。部署后队列仍为 `0/0`、Fence 元数据为 0、Token 文件不存在、锁空闲。工作树仍有 20 个条目。部署后 `.codex` 下 10 个 SQLite 数据库的 `PRAGMA integrity_check` 均为 `ok`。

部署前已在生产仓库外创建并验证保护备份：`/var/backups/conveyor/pr104_20261011T061940Z_e_pk46wj/`。目录权限为 `0700`，51 个文件的 SHA-256 清单全部通过，10 个 SQLite 副本完整性检查通过。由于部署脚本会轮转仓库内备份，部署前还将两份旧内部备份复制到 `/var/backups/conveyor/pr104_preserved_internal_20261011T064552Z/`；2 个目录、9 个文件的树和哈希比对通过。数据库、会话、Worktrees、q3 历史和备份均予以保留。

此前预检还确认：活动接管数为 0，10 个授权重启单元在部署前均 active；`conveyor-host-desktop.service` active。备份内容及数据库记录没有输出到报告。

## 部署结果

部署脚本从旧 SHA 切换到目标 SHA，运行候选隔离 smoke 并通过生产 smoke，随后重启并确认以下 8 个服务和 2 个 Timer 全部 active：

- `conveyor-telegram-bot.service`
- `conveyor-feishu-bot.service`
- `conveyor-desktop-agent.service`
- `conveyor-web.service`
- `conveyor-vps-computer.service`
- `conveyor-desktop-chat.service`
- `conveyor-handoff.service`
- `conveyor-agent-desktops.service`
- `conveyor-maintain.timer`
- `conveyor-scheduler.timer`

`conveyor-host-desktop.service` 在部署前后保持 active，`ActiveEnterTimestamp` 未改变。未触碰 X11、Firefox Profile 或桌面会话。部署状态记录 `rollback_attempted=false`；没有回滚。部署结束后 tracked 工作区干净，线上 HEAD 为目标 SHA。

## 部署后状态检查

- **Fence 与部署锁：** Fence 已 thaw 并释放；Fence Token 不存在，`queue_metadata` 中 Fence 记录为 0；部署锁空闲。
- **队列和持久化数据：** queued/running=`0/0`；q3 仍为 `interrupted`、无 session；20 个 Worktree 条目保留；10 个 SQLite 数据库完整性均为 `ok`。
- **Web：** `/api/health` 与 Console 返回 HTTP 200；Web service active。
- **Feishu：** service active。
- **Telegram：** service active；Telegram Bot API `getMe` 可达。`getMyCommands` 已包含 `workers`、`sessions`、`switch`。
- **日志和进程：** Telegram、Feishu、Web 的 systemd `NRestarts=0`；部署后这三项服务的 error-level journal 条数为 0。TCP 5900、5800、6080 无监听。
- **自动部署：** `.github/workflows/deploy.yml` 仍只有 `workflow_dispatch` 入口；保持 manual-only。

## Telegram 实机功能验收

已在真实的 Conveyor Bot 私聊中发送并读取以下命令响应：

|项目|结果|实际观察|
|---|---|---|
|`/workers`|PASS|返回当前 Telegram 聊天最近任务状态，运行中 0、排队中 0，并显示配置的 Agent。没有创建任务。|
|`/sessions`|PASS|返回当前会话和该聊天的任务历史；q4 failed 与 q3 interrupted 显示为历史按钮。未点击任务项，未改变执行会话。|
|`/switch`|PASS|显示只读会话/历史导航；没有切换任务执行上下文。|
|`/help`|PASS|真实回复包含 `/workers`、`/sessions`、`/switch` 的说明。|
|Bot 命令菜单|PASS|`getMyCommands` 返回新命令 `workers`、`sessions`、`switch`。|
|旧 Reply Keyboard：`👷 我的 Workers`|**MANUAL CHECK REQUIRED**|当前 Telegram Web 聊天没有显示旧版 Reply Keyboard，命令菜单也不包含该旧按钮，无法真实点击。没有用手动发送同名文本冒充点击验收。|
|旧 Reply Keyboard：`🔄 切换会话`|**MANUAL CHECK REQUIRED**|当前 Telegram Web 聊天没有显示旧版 Reply Keyboard，无法真实点击。没有用手动发送同名文本冒充点击验收。|

## 阶段 A/B/C 既有验证

用户指明阶段 A/B/C 已完成，因此本次未重复完整测试套件。既有报告记录了以下通过结果：PR Backend 与 Web CI 成功；目标候选 Python compileall、Telegram navigation 定向测试（6 项）、Backend 测试（718 项运行、7 项按配置跳过）、`make smoke`、部署脚本语法、Web typecheck/lint/build 和 Playwright E2E（4 项）。隔离 fixture 使用占位凭证，修正测试用户 fixture 后 smoke 通过。目标版本 systemd unit 与生产安装版本一致，依赖清单未变化。

部署脚本自带的候选隔离 smoke 和生产 smoke 在本次部署过程中按既定流程运行并通过；未额外重复阶段 A/B/C 全套测试。

## 后续人工事项

用可显示旧版 Reply Keyboard 的 Telegram 客户端打开 Conveyor Bot 私聊，实际点击 `👷 我的 Workers` 与 `🔄 切换会话`，记录响应和截图/验收结果后，才能把完整生产功能验收改为 PASS。当前部署本身已经成功，两个待验项不应描述成自动化 PASS。
