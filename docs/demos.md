# Demo cases

English first, 中文在后. Real media is the Telegram set from the 2026-10-08 deployment. Recipes you can run yourself are marked pending until a recording is added. Illustrative cases have no sample output. Feishu live cards are not a demo in this guide.

Asset notes: [docs/assets/demos/README.md](assets/demos/README.md).

## Shown now

| Frame | What it shows |
| :--- | :--- |
| [Workers keyboard](assets/demos/telegram-workers-menu.png) | Persistent menu open from the keyboard icon next to the input: 我的 Workers, 继续对话, 切换会话, 查看任务. Same week as the deployment shots; the image does not print a calendar date. |
| [Workers list](assets/demos/telegram-workers-live.png) | `/workers`, then the Conveyor worker: continue, tasks, switch session, back. |
| [Screenshot routing](assets/demos/telegram-screenshot-routing.png) | 2026-10-08. Selected session is `当前：Conveyor › 主会话`. Target is the shared VPS desktop, node `vps-desktop`. |

### How selection and screens work

- `/start` installs the persistent keyboard and the onboarding prompt. The keyboard is a separate message when the welcome also has the inline onboarding button.
- Open it again from the keyboard icon beside the message field.
- Workers → select a worker → continue or switch session binds this private chat, this topic, and this operator. Browsing the list does not bind.
- A private Telegram or Feishu chat with nothing selected uses the canonical Conveyor main session (`web:web-console:agent-default`, shown as 主会话). Group and topic chats do not get that default.
- A screenshot is of the Agent selected now. The default main session and a default secondary session share the VPS desktop. An independent Agent uses its own X display. If that Agent's Mac is offline, the request fails. It does not move to another screen.

## Weather (runnable recipe, recording pending)

A supervisor is running this Computer Use query. This page does not report a result, a temperature, or a video.

Use this only after an administrator has already enabled Computer Use (`CONVEYOR_COMPUTER_USE_ENABLED`) and Direct mode (`CONVEYOR_COMPUTER_DIRECT_ENABLED`). Then arm a time limit:

```text
/computer_arm 30
/computer_task Use the current desktop browser to check today's weather in Montreal. Read temperature and rain conditions from a weather webpage and keep it open for a screenshot. Use only browser UI, no shell/API.
```

`/computer_arm [minutes]` does nothing useful unless those two switches are already on. This guide does not ask anyone to turn on always-direct or to relax the action policy.

While it runs:

```text
/computer_status
/computer_screenshot
/computer_stop
```

`/computer_stop` is the kill switch. When a recording exists it will be added under `docs/assets/demos/` and linked here. Until then there is no weather media link.

## Local web app check (recipe, no sample output)

Same gates as the weather recipe. Browser UI only:

```text
/computer_task Open the desktop browser to the local web app already running on this machine. Confirm the page loaded and leave that tab open for a screenshot. Use only browser UI, no shell/API.
```

Check with `/computer_status` and `/computer_screenshot`. Stop with `/computer_stop`. No captured page is attached to this doc.

## Debugging, server, and worktree (illustrative)

These are shapes of work, not transcripts from a run. Do not treat any line below as something the bot returned.

- A CI or test failure goes to a Codex job in an isolated git worktree. Review with `/diff` before `/apply`. Drop the worktree with `/discard`. Stop the job with `/cancel`.
- Host questions such as load or process list stay on the fast path (`/load`, `/ps`) and do not start a desktop task.
- Desktop clicks stay on `/computer_task` after the USE and DIRECT gates, and stop with `/computer_stop`.

---

# 演示案例

上面是英文。这里是同一套说明。目前的实拍只有 2026-10-08 部署上的 Telegram 三张图。可自己跑的配方在录屏附上之前标为待录。示意案例没有示例输出。飞书实况卡片不在本指南的演示范围内。

## 已经放上的画面

| 画面 | 内容 |
| :--- | :--- |
| [Workers 键盘](assets/demos/telegram-workers-menu.png) | 从输入框旁的键盘图标打开常驻菜单：我的 Workers、继续对话、切换会话、查看任务。与部署截图同一周；图上没有日历日期。 |
| [Workers 列表](assets/demos/telegram-workers-live.png) | `/workers` 后进入 Conveyor：继续对话、查看任务、切换会话、返回列表。 |
| [截图路由](assets/demos/telegram-screenshot-routing.png) | 2026-10-08。当前为 `当前：Conveyor › 主会话`，目标是 VPS 共享桌面，节点 `vps-desktop`。 |

### 选择和屏幕

- `/start` 装上常驻键盘和引导。欢迎语若自带内联引导按钮，键盘是另一条消息。
- 之后从输入框旁的键盘图标再打开。
- Workers → 选中 Worker → 继续对话或切换会话，绑定这个私聊、这个话题和这位操作者。只浏览列表不会绑定。
- 私聊里什么都没选时，用 Conveyor 的规范主会话（`web:web-console:agent-default`，显示为「主会话」）。群和话题不会套这个默认。
- 截图对准当前选中的 Agent。默认主会话和默认次会话共用 VPS 桌面。独立 Agent 用自己的 X 显示。该 Agent 的 Mac 离线时请求失败，不会改截别的屏幕。

## 天气（可运行配方，录屏待补）

主管正在跑这条 Computer Use。本页不写结果、温度或视频。

仅在管理员已经打开 Computer Use（`CONVEYOR_COMPUTER_USE_ENABLED`）和 Direct（`CONVEYOR_COMPUTER_DIRECT_ENABLED`）之后，再限时解锁：

```text
/computer_arm 30
/computer_task Use the current desktop browser to check today's weather in Montreal. Read temperature and rain conditions from a weather webpage and keep it open for a screenshot. Use only browser UI, no shell/API.
```

这两个开关没开时，`/computer_arm [分钟]` 不会让任务直接动桌面。本指南不要求打开 always-direct，也不要求放宽动作策略。

进行中：

```text
/computer_status
/computer_screenshot
/computer_stop
```

`/computer_stop` 是急停。有录屏后会放进 `docs/assets/demos/` 并在这里链接。在那之前没有天气素材链接。

## 本地 Web 应用检查（配方，无示例输出）

门闩与天气相同，只用浏览器界面：

```text
/computer_task Open the desktop browser to the local web app already running on this machine. Confirm the page loaded and leave that tab open for a screenshot. Use only browser UI, no shell/API.
```

用 `/computer_status` 和 `/computer_screenshot` 查看，用 `/computer_stop` 停止。本文不附页面截图。

## 排障、服务器与 worktree（示意）

下面是工作形态，不是某次运行的回执。

- CI 或测试失败进入隔离 git worktree 里的 Codex 任务。先 `/diff`，再决定 `/apply`。不要这次改动就 `/discard`。停任务用 `/cancel`。
- 负载、进程这类主机问题走快路径（`/load`、`/ps`），不发起桌面任务。
- 桌面点击在 USE 与 DIRECT 已开启后走 `/computer_task`，用 `/computer_stop` 停下。
