# Conveyor PR #104 Telegram Legacy Reply Keyboard 验收

## 最终裁决

**MANUAL CHECK REQUIRED。** 两条旧键盘文本路径和只读导航回调均通过；Telegram Web A 当前没有显示旧 Reply Keyboard。iPhone Mirroring 显示设备正在使用且需要先锁定 iPhone 才能连接，因此无法确认手机 Telegram 是否仍保留旧键盘，也未尝试解锁、绕过登录或操作设备。完整旧键盘 UI 验收不能报 PASS。

观察到的文本兼容性结论为：**LEGACY KEYBOARD TEXT COMPATIBILITY PASS; Telegram Web UI NOT PRESENT; mobile UI check pending.**

## 范围与客户端

- Repository：`mammut001/Conveyor`
- Production SHA：`2a29b11ee2041738745ccbe211287b39285051a3`
- 实际客户端：已登录的 Telegram Web A，`https://web.telegram.org/a/`，Codex In-app Browser；页面未显示可读取的客户端构建版本号。使用既有已授权会话，没有重新登录或访问其他用户账号。
- 手机客户端：无法检查。iPhone Mirroring 状态为设备正在使用，提示需先锁定 iPhone；未点击 Connect 或尝试绕过设备状态。
- 没有更改 GitHub、生产代码、部署状态或 systemd 服务；没有重启任何服务。

## 验收结果

|验收项目|裁决|观察结果|
|---|---|---|
|Telegram `/workers`|PASS|此前已在真实私聊验证；本轮旧键盘文本回复再次显示当前聊天任务统计和命名 Agent `Conveyor`。|
|Telegram `/sessions`|PASS|此前已在真实私聊验证；本轮文本与按钮导航均返回当前私聊的只读历史列表。|
|Telegram `/switch`|PASS|此前已在真实私聊验证；显示只读会话导航，不改变执行会话。|
|旧 Workers Reply Keyboard UI 点击|NOT PRESENT（Telegram Web）；手机 MANUAL CHECK REQUIRED|私聊中未显示旧 Reply Keyboard。只看到 Bot Commands 菜单入口；展开后显示 `/workers`、`/sessions`、`/switch` 等命令，不是旧 Reply Keyboard。没有把 Inline Keyboard 或同名文本当作旧按钮点击。|
|旧 Session Reply Keyboard UI 点击|NOT PRESENT（Telegram Web）；手机 MANUAL CHECK REQUIRED|同上；未见旧键盘按钮，也没有可展开旧 Reply Keyboard 的控件。未点击假定不存在的按钮。|
|旧 Workers 文字消息路由|PASS|在 Telegram Web 私聊手动发送 `👷 我的 Workers`。客户端实际发出该消息；Bot 返回 Workers 导航卡，显示最近任务运行中 0、排队中 0 和 Agent 名称 `Conveyor`。没有 Agent instructions 或 workspace 路径。只有确定性导航卡，没有额外 LLM 对话回复。|
|旧 Session 文字消息路由|PASS|在同一私聊手动发送 `🔄 切换会话`。Bot 返回当前会话及该私聊最近任务的只读列表，说明不会改变执行会话。没有额外 LLM 对话回复。|
|历史任务导航|PASS|实际点击 Inline Keyboard 的会话导航按钮、`q3 · interrupted` 详情及返回列表；详情仅显示 q3 状态，返回后历史列表恢复。此操作没有改变 q3 状态或当前执行会话。此为 Inline Keyboard 回调验收，不是旧 Reply Keyboard 点击。|
|`tgn:workers` / `tgn:sessions` 回调|PASS|在当前私聊通过实际 Inline Keyboard 导航按钮进入 Workers 卡和会话列表。|
|合法当前聊天 `tgn:job:*` 与返回按钮|PASS|点击当前列表中的 `q3 · interrupted` 并使用“返回会话列表”；未操作其他聊天或任务。|
|无新建 Codex Job|PASS|操作前/后队列均为 queued/running `0/0`。|
|无跨用户信息泄露|PASS|没有使用其他账号。目标 SHA 的现有 `tests/test_telegram_navigation.py` 以隔离 fixture 运行 6 项全部通过，覆盖 operator、物理 Telegram chat、channel、无效任务回调、命名 Agent 路径/instructions 隔离及其它 callback namespace。|
|生产状态未被改变|PASS|生产 SHA 不变；q3 仍为 `interrupted`；Worktree 条目仍为 20；活动 Human Takeover 为 0；Approval Relay pending 为 0；部署锁空闲；Telegram service active、`NRestarts=0`；Web health HTTP 200；host desktop active。测试后 10 分钟内 Telegram service error journal 无记录。|

## 生产前后核验

操作前核验记录：生产 SHA `2a29b11ee2041738745ccbe211287b39285051a3`，Telegram service active、`NRestarts=0`，Web health HTTP 200，队列 `0/0`，q3 `interrupted`，Worktree 条目 20，host desktop active。

操作后只读核验时间为 `2026-10-11T20:43:20Z`：上述值保持一致；活动 takeover 0、Approval Relay 数据库不存在（pending 记录为 0，测试时间段没有新增 Relay 数据库/请求）、部署锁 free。当前真实聊天仅新增了本次明确指定的两条验收文本及相应导航卡，不会形成 Codex Queue Job。

## 目标 SHA 的隔离安全测试

从目标 commit `2a29b11ee2041738745ccbe211287b39285051a3` 临时导出源码快照，在隔离目录中运行既有 `tests/test_telegram_navigation.py`：6 项通过，0 项失败。运行后临时目录已清理；未创建 Git Worktree，隔离测试自身没有访问生产数据库。生产数据库状态仅由上述只读检查核对。

覆盖项包括：旧标签由确定性路由处理而不进入 LLM；Workers 和 Sessions 按 operator、物理 chat 与 Telegram channel 过滤；当前聊天合法任务详情可读、跨 operator 或 chat 的同一 callback 被拒绝；返回与 callback namespace 检查；Agent 卡不泄漏 workspace 路径或 instructions；`tool:`、`relay:`、`ob:`、`deep` 等既有 callback namespace 不被导航处理器接管。

## 截图与隐私

Telegram Web 的界面状态通过当前任务的浏览器截图和无障碍树检查。截图中包含测试前已有的服务告警文字；为避免把私人历史内容写入报告，没有将未脱敏原图作为附件，也没有把其描述成已脱敏截图。本报告只记录与验收有关的脱敏摘要，不含用户 ID、聊天 ID、Token 或历史任务正文。

由于手机 Telegram 客户端当前不可连接，且旧 Reply Keyboard 在 Telegram Web 中未出现，剩余人工检查为：在现有授权的手机 Telegram 客户端恢复可用后，查看 Conveyor Bot 私聊是否保留旧 Reply Keyboard；若显示，再实际点击两个旧按钮并记录脱敏截图。
