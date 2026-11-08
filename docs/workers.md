# 执行者池 (Worker Pool)

Conveyor 执行者池（Worker Pool）允许系统将任务动态分配给多个本地或远程代码执行工具（如 Codex、Claude Code、Pi 等），支持自动状态探测、优先级排序、配额限制感知及错误轮换重试。

## 功能特性

1. **自动探测与非阻塞缓存**：检测本地环境各执行者的安装路径、版本号及登录授权状态。在异步运行路径中，检测过程通过 `asyncio.to_thread` 在后台工作线程执行，避免阻塞主事件循环；检测结果缓存 60 秒，支持强制刷新。Antigravity (`agy`) 采用 `agy models` 命令探测认证状态（退出码 0 且列出有效模型输出时视为已登录；退出码非 0 且输出包含未登录关键词时标记为 `needs_login`；超时或其他执行失败标记为 `unknown`；该检测不消耗模型配额）。
2. **优先级与启用控制**：操作员可自定义执行者顺序及是否启用，系统始终选择首个可用的执行者。
3. **配额感知与真实运行轮换重试**：
   - **启动时拦截**：若在 `runner.start()` 阶段抛出配额异常，立即冷却当前执行者并向下一个可用执行者重试一次。
   - **执行完成检测**：因大多数代理（如 `CodexRunner`）异步执行任务，任务在后台运行并在遭遇配额耗尽（如 429、`RESOURCE_EXHAUSTED` 等）时以 `FAILED` 状态结束。系统在任务结束后检测到配额失败且该任务未产生任何未提交变更时：
     1. 解析 `"Resets in XhYmZs"`（若无具体重置时间则默认 45 分钟），将当前执行者标记为冷却状态（cooling）；
     2. 检查池中是否存在下一个可用执行者；
     3. 若存在，向用户发送一行中文提示：`"当前执行者额度用完，已换用 <label> 重试"`；
     4. 使用相同 Prompt 自动向新执行者发起任务重试，并继续等待最终结果。
   - **严格安全边界**：任务最多重试一次（绝不重试第二次）；非配额错误绝不重试；已产生变更的任务不重试以避免工作区冲突；功能未开启时保持原有行为不变。
4. **状态持久化**：执行者顺序、启用状态与冷却信息原子写入持久化保存至 `<codex_memory_root>/state/workers.json`。

## 配置开关

- `CONVEYOR_WORKERS_ENABLED` (布尔值，默认 `true`): 执行者池主开关（显式 `"false"` / `"0"` / `"off"` 可禁用）。开启时任务通过执行者池动态轮换；若环境变量中已显式配置 `CONVEYOR_AGENT_BACKEND`，系统将保持原有 backend（不走池），除非同时显式指定 `CONVEYOR_WORKERS_ENABLED=true`。关闭时相关 API 返回 404，任务执行保持原有的默认 backend。
- `CONVEYOR_WORKERS_USAGE_INTERVAL_SECONDS` (整数，默认 `3600`，最小 `300`): 后台刷新各执行者实时用量的间隔。
- `CONVEYOR_WORKERS_INSTALL` (布尔值，默认 `true`): 执行者一键安装开关（显式 `"false"` / `"0"` / `"off"` 可禁用）。开启后仍需已认证所有者显式传递 `{"confirm": true}` 确认，且仅运行目录中预置的固定安装命令。未开启时禁止通过 API 安装任何工具。

系统状态接口 `/api/system/status` 中的 `features["workers"]` 反映主开关状态。

## 已知执行者目录 (Catalog)

| ID | 名称 | 命令 | 安装命令 | 登录命令 | 认证检查 | 可选模型 | 可运行 |
|---|---|---|---|---|---|---|---|
| `codex` | Codex | `codex` | `npm install -g @openai/codex` | `codex login` | `codex login status` (rc 0) | 暂不支持 | 是 |
| `claude-code` | Claude Code | `claude` | `npm install -g @anthropic-ai/claude-code` | `claude auth login` | `claude auth status` (rc 0) | 固定列表 (3款) | 是 |
| `pi` | Pi | `pi` | 无 (手动安装) | 无 | 无 (unknown) | 暂不支持 | 是 |
| `agy` | Antigravity | `agy` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | `agy` (浏览器交互式桥接) | `agy models` (rc 0 且列出模型) | `agy models` | 是 |
| `cmd` | Command Code | `cmd` | `npm install -g command-code` | `cmd login` | `cmd whoami` (rc 0 且无 401/未登录) | `cmd --list-models` | 是 |
| `grok` | Super Grok | `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash` | `grok login` | `~/.grok/auth.json` 存在且 `grok models` rc 0 | `grok models` | 是 |

`agy`、`cmd`、`grok` 的实际运行由 CLI backend 负责（另行实现），本模块只提供目录、检测、用量与轮换选择。

## HTTP API

所有接口均需 Bearer Token 鉴权。功能未开启时均返回 404 `{"error": "not found"}`。

### 1. 获取执行者状态
`GET /api/workers`
- 返回格式：
  ```json
  {
    "enabled": true,
    "install_enabled": true,
    "workers": [
      {
        "id": "codex",
        "label": "Codex",
        "installed": true,
        "version": "0.1.0",
        "path": "/usr/local/bin/codex",
        "auth": "ok",
        "login_command": "codex login",
        "install_command": "npm install -g @openai/codex",
        "install_supported": true,
        "runnable": true,
        "enabled": true,
        "priority": 0,
        "quota": {
          "state": "available",
          "reset_at": null,
          "reason": null
        },
        "models_supported": false,
        "model": null,
        "login_supported": true,
        "usage": {
          "windows": [
            {"name": "5h", "used_percent": 61.0, "resets_at": 1731060000},
            {"name": "weekly", "used_percent": 23.4, "resets_at": 1731400000}
          ],
          "plan": "plus",
          "fetched_at": 1731056400,
          "available": true
        },
        "threshold_percent": 95,
        "over_threshold": false
      }
    ],
    "active": "codex",
    "switch_log": []
  }
  ```
- 每个执行者另含 `usage`（见“按用量自动切换”）、`threshold_percent`（1-100，默认 95）、`over_threshold`（任一未过期窗口用量 >= 阈值）。
- `active`：当前会被选中的执行者（只计算，不写切换记录）。`switch_log`：最近 20 条切换记录（按时间正序）。

### 2. 重新探测执行者
`POST /api/workers/detect`
- 刷新 60 秒检测缓存，重新探测后返回与 GET 相同的载荷。

### 3. 更新排序与启用状态
`POST /api/workers/order`
- 请求体：`{"order": ["codex", "claude-code", ...], "enabled": ["codex", ...]}`
- 若包含未知 ID，返回 400 `{"error": "...", "code": "unknown_worker"}`。
- 成功返回更新后的执行者列表。

### 4. 获取执行者可用模型
`GET /api/workers/<id>/models`
- 返回格式：
  ```json
  {
    "models": [
      { "id": "claude-haiku-5-5", "label": "claude-haiku-5-5" },
      { "id": "claude-sonnet-5-5", "label": "claude-sonnet-5-5" }
    ],
    "cached_at": 1731056400
  }
  ```
- 结果在服务端缓存 10 分钟。
- 若执行者不支持模型选择（如 `codex`, `pi`），返回 400 `{"code": "models_unsupported"}`。

### 5. 设置执行者所选模型
`POST /api/workers/<id>/model`
- 请求体：`{"model": "claude-sonnet-5-5"}` 或 `{"model": null}`（清除选择）。
- 所选模型必须在可用模型列表中，否则返回 400 `{"code": "unknown_model"}`。
- 所选模型持久化保存至 `<codex_memory_root>/state/workers.json`。
- 成功返回完整的执行者 GET 载荷。

### 6. 浏览器登录桥接 (Login Bridge)
适用于远端或无图形界面终端环境下的交互式登录（如 Antigravity CLI）：
- **启动登录**：`POST /api/workers/<id>/login/start`
  - 服务端在虚拟终端（pty，无 shell）中以隔离的纯净环境启动登录命令，启动输出解析线程。
  - 每个执行者同时只维护一个活跃登录会话；启动新会话会自动终止旧会话。
  - 会话超时时间为 10 分钟，超时后进程自动终止。
  - 返回格式：`{"session": "<uuid>", "state": "waiting_url", "url": null, "message": "正在启动登录..."}`
- **查询登录状态**：`GET /api/workers/<id>/login/<session>`
  - 状态流转：`waiting_url` -> `open_url`（已捕获授权链接）-> `needs_code`（检测到验证码提示行）-> `done`（进程正常退出且凭据复检通过）或 `failed`（失败/取消/超时）。
  - 若未知或已过期的 session 返回 404 `{"error": "登录已超时或已结束，请重新开始登录", "code": "session_not_found"}`；超时的会话消息为“登录已超时，请重新开始登录”。
- **提交验证码**：`POST /api/workers/<id>/login/<session>/code`
  - 请求体：`{"code": "<验证码>"}`。
  - 验证码必须为单行 1..200 可打印字符（复制时夹带的零宽等不可见字符会被自动去掉），否则返回 400 `{"error": "验证码格式不正确，请检查后再提交", "code": "bad_code"}`。**格式错误不会终止登录会话**，可以直接改正后重新提交；响应里附带当前 `session` 与 `state`。
  - 服务端将验证码与换行写入 pty 伪终端输入流。
- **取消登录**：`POST /api/workers/<id>/login/<session>/cancel`
  - 终止登录进程并关闭终端，会话状态转为 `failed`，消息为 `"已取消"`。

### 7. 安装执行者
`POST /api/workers/<id>/install`
- 请求体：`{"confirm": true}`
- 安全机制：
  - 未开启 `CONVEYOR_WORKERS_INSTALL` 时，返回 403 `{"error": "install disabled", "code": "install_disabled"}`。
  - 请求体缺少 `confirm: true` 时，返回 400 `{"code": "confirm_required"}`。
  - 工具不支持一键安装（如 `pi`）时，返回 400 `{"code": "install_unsupported"}`。
  - 仅执行目录中预置的安全安装命令（绝不接收用户输入参数）。`agy` 执行固定的官方安装脚本 `curl -fsSL https://antigravity.google/cli/install.sh | bash`（由 `["bash", "-c", <fixed_script>]` 运行），无 `shell=True`，单次运行超时时间为 300 秒。
- 成功返回 200 `{"ok": true, "output": "<最后4000字符日志>"}`。

### 8. 清除冷却状态
`POST /api/workers/<id>/reset-quota`
- 清除指定执行者的 cooling 冷却标记，返回与 GET 相同的载荷。

### 9. 供应商模型列表查询
`GET /api/config/provider/models`
- 读取服务端持久化存储的当前 Provider 配置及 API Key，向 `{base_url}/models` 发起请求（超时 10 秒）。

`POST /api/config/provider/models`
- 使用表单里的值，无需先保存。请求体：`{"base_url": "...", "env_key": "DEEPSEEK_API_KEY", "api_key": "..."}`；`api_key` 留空时，仅当表单的 `base_url`（规范化后：协议+主机+端口+路径，去掉末尾斜杠，主机不区分大小写）与已保存的供应商地址一致，且 `env_key` 与已保存的一致，才会使用已保存的密钥；否则不发出任何请求，返回 400 `{"code": "api_key_required", "error": "换了接口地址后，需要先填写 API 密钥才能读取模型列表"}`。已保存的密钥绝不会发往其他地址。
- 同样仅允许 HTTPS（本机 `http://127.0.0.1`/`localhost` 除外），缺少 `base_url` 返回 502 `models_fetch_failed` 与中文提示。
- 安全性：
  - 仅支持 HTTPS 协议（测试环境允许本地 `http://127.0.0.1` 或 `localhost`）。
  - 返回载荷仅包含模型 ID `{"models": [{"id": "..."}]}`，绝不泄露 API Key。
- 失败时返回 502 `{"code": "models_fetch_failed", "error": "<简短中文说明>"}`。**绝不透传上游的错误正文、异常文本或命令的 stderr**：401/403 提示“服务商拒绝了这个密钥（HTTP 401），请检查 API 密钥”，其他状态码提示 HTTP 状态码，超时或网络错误提示“请求超时或网络连接失败”。
- `GET /api/workers/<id>/models` 同理：失败只返回固定中文（如“获取模型列表失败，请确认执行者已安装并已登录”）。
- `cmd --list-models` 解析只保留真正的 `<厂商>/<模型>` 行（id 不含空格），帮助文字、`cmd --model ...` 示例、`Docs:` 和 “Decision models” 区段不会进入模型列表。

### 10. 按用量自动切换 (Usage-based rotation)

所有者按拖拽顺序排列执行者，Conveyor 使用第一个可用的执行者，并在它**用完之前**切换：某个执行者任一窗口（5h / weekly）的已用百分比达到其阈值（默认 95%）时，在该窗口重置前跳过它；全部执行者都超过阈值时，退回到旧行为（配额错误后重试一次），并选用“用量最低”的那个。

**用量来源**（`worker_usage.py`，改写自 Kanjo (MIT)，Linux + macOS）：

| 执行者 | 来源 | 窗口 |
|---|---|---|
| `grok` | `~/.grok/auth.json` 的令牌 → `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` | weekly |
| `codex` | `~/.codex/auth.json` → `https://chatgpt.com/backend-api/wham/usage` | 5h + weekly |
| `claude-code` | macOS 钥匙串；Linux 读 `~/.claude/.credentials.json` 或 `~/.claude.json`；再回退到 `~/.claude.json` 的缓存用量 → `https://api.anthropic.com/api/oauth/usage` | 5h + weekly |
| `agy` | 本机运行的 Antigravity language server（`127.0.0.1`）。进程发现：Linux 用 `ps -eo pid=,args=`，端口用 `ss -ltnp`（失败再读 `/proc/<pid>`）；macOS 用 `ps -ax` 与 `lsof`。找不到进程返回空 | 5h + weekly |
| `cmd`、`pi` | 暂无（未知） | - |

- 每个读取器返回 `{"windows": [{"name": "5h"|"weekly", "used_percent": float, "resets_at": epoch}], "plan": str|null, "fetched_at": epoch}` 或 `None`（不可用）。Worker 的 `usage` 为 `{windows, plan, fetched_at, available}`；不可用时 `available=false`，界面显示“实时用量不可用，额度用完时自动切换”。
- 安全：令牌只存在内存，只发往各服务商自己的 https 主机，重定向到其他主机会被拒绝；超时 10 秒、响应大小上限 256 KB；令牌不写日志。Antigravity 只访问 `127.0.0.1`。
- 刷新：后台每 60 分钟刷新一次（`CONVEYOR_WORKERS_USAGE_INTERVAL_SECONDS`，默认 3600，最小 300）；选择执行者前若缓存超过 10 分钟会先按需刷新；失败时保留上一次的值。已过重置时间的窗口视为未知，不再阻挡该执行者。
- 切换记录：被选中的执行者与上一次不同时写入 `switch_log`（最近 20 条）：`{"at": epoch, "from": id|null, "to": id, "reason": "threshold"|"quota_error"|"unavailable", "detail": "中文说明"}`。持久化在 `workers.json` 的 `thresholds`、`switch_log`、`last_picked`。

`POST /api/workers/<id>/threshold`
- 请求体：`{"threshold_percent": 80}`（1..100 的整数，布尔值与小数无效）。无效返回 400 `{"code": "bad_threshold"}`；未知执行者返回 400 `unknown_worker`。成功返回完整 GET 载荷。

`POST /api/workers/usage/refresh`
- 立即刷新全部执行者的用量，返回完整 GET 载荷。

`POST /api/workers/order` 不变（拖拽排序使用它）。以上接口在功能未开启时同样返回 404。

## 可运行的 CLI 执行者（agy / grok / cmd）

`runner/cli_backend.py` 提供通用 `CliBackend`，在任务 worktree 内以非交互模式运行外部 CLI，沿用现有 diff/apply/discard 生命周期：

- agy：`agy -p <prompt> --model <模型，默认 gemini-3.8-flash-high> --mode accept-edits --sandbox --output-format text`
- grok：`grok -p <prompt> --always-approve --cwd <worktree> --output-format plain`（选择了模型时追加 `-m <模型>`）
- cmd：`cmd -p <prompt> --yolo -t --skip-onboarding -m <模型，默认 deepseek/deepseek-v4-flash> --max-turns 300`

**只读/改代码模式**：提问（RUN）任务必须只读——agy 使用 `--mode plan`，cmd 使用 `--permission-mode plan`，Super Grok 暂无已验证的只读选项，提问时直接拒绝（"Super Grok 暂不支持只读提问"，池会跳过）。改代码（FIX）任务：agy 使用 `--mode accept-edits --sandbox`；cmd（`--yolo`）与 grok（`--always-approve`）可在服务器上执行任意 shell，仅当显式设置 `CONVEYOR_WORKERS_UNSANDBOXED=true`（默认 false）时才参与，否则提示“需要在服务器上开启 CONVEYOR_WORKERS_UNSANDBOXED 才能让它改代码”。`pick_worker(..., mode=)` 按模式跳过不合格的执行者。

模型取自执行者设置中选择的模型。环境变量使用与其他后端相同的受限环境，并显式剔除 `CONVEYOR_WEB_TOKEN`、`TELEGRAM_BOT_TOKEN`。stdout 逐行（节流）作为进度，最终 stdout 作为回答；超时沿用 `codex_timeout_seconds`；退出码非 0 时 `job.error` 为输出末尾约 2000 字符，以便额度分类器识别 "Resets in …" / 429 文本。

## 安全性

1. **固定命令**：一键安装端点严格限制为执行者目录预定义的固定命令，绝不将任何外部用户输入传递给子进程或 shell。
2. **多重防护**：安装功能受 `CONVEYOR_WORKERS_INSTALL` 独立环境变量开关保护（默认开启，可显式设为 `false`/`0`/`off` 禁用），并始终要求调用方显式传递 `{"confirm": true}` 确认。
3. **PTY 隔离与敏感信息保护**：登录桥接使用标准 pseudo-terminal（pty）隔离运行，过滤环境变量（仅保留基础系统变量），密钥与敏感凭据绝不通过 API 载荷暴露。

## 内置 Pi

Pi 随 Conveyor 附带（`vendor/pi`，固定版本 1.5.0），是一个通用执行者：只要在「设置 → 模型设置」里填了任意 OpenAI 兼容的服务商（DeepSeek、StepFun、Moonshot、OpenAI 等），Pi 就能用它干活，不需要手动安装或另外配置密钥。

- **安装**：在「执行者」页面点 Pi 的安装（固定命令 `bash scripts/install_pi.sh`，在仓库根目录运行，需要 Node.js >= 22.19）。它在 `vendor/pi` 内执行 `npm ci --omit=dev --ignore-scripts`，不装全局，可重复运行。`scripts/deploy_vps.sh` 部署时也会自动运行一次，失败只记日志，不影响部署。
- **可执行文件查找顺序**：`CONVEYOR_PI_BIN` > 设置里的 `pi_bin` > `vendor/pi/node_modules/.bin/pi` > PATH 上的 `pi`。
- **服务商桥接**：每次运行前，如果没有显式设置 `CONVEYOR_PI_PROVIDER` / `CONVEYOR_PI_MODEL`，Conveyor 会按「模型设置」里保存的服务商生成 `<CODEX_TASK_ROOT>/pi-agent/models.json`（权限 0600，`wire_api` 为 `responses` 时用 `openai-responses`，否则 `openai-completions`）。文件里的 `apiKey` 只写 `$环境变量名`，真正的密钥只通过子进程环境变量传给 Pi，不落盘、不进日志。设置了显式环境变量时保持原来的 DeepSeek 路径。
- **状态**：已保存服务商（或设置了 `CONVEYOR_PI_*`）显示可用；否则显示「未配置服务商」，页面上有「去模型设置」按钮。Pi 没有自己的登录命令。
