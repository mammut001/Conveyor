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

- `CONVEYOR_WORKERS_ENABLED` (布尔值，默认 `false`): 执行者池主开关。关闭时相关 API 返回 404，任务执行保持原有的默认 backend。
- `CONVEYOR_WORKERS_INSTALL` (布尔值，默认 `false`): 执行者一键安装开关。未开启时禁止通过 API 安装任何工具。

系统状态接口 `/api/system/status` 中的 `features["workers"]` 反映主开关状态。

## 已知执行者目录 (Catalog)

| ID | 名称 | 命令 | 安装命令 | 登录命令 | 认证检查 | 可选模型 | 可运行 |
|---|---|---|---|---|---|---|---|
| `codex` | Codex | `codex` | `npm install -g @openai/codex` | `codex login` | `codex login status` (rc 0) | 暂不支持 | 是 |
| `claude-code` | Claude Code | `claude` | `npm install -g @anthropic-ai/claude-code` | `claude auth login` | `claude auth status` (rc 0) | 固定列表 (3款) | 是 |
| `pi` | Pi | `pi` | 无 (手动安装) | 无 | 无 (unknown) | 暂不支持 | 是 |
| `agy` | Antigravity | `agy` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | `agy` (浏览器交互式桥接) | `agy models` (rc 0 且列出模型) | `agy models` | 暂不可运行 |
| `cmd` | Command Code | `cmd` | `npm install -g command-code` | `cmd login` | `cmd whoami` (rc 0 且无 401/未登录) | `cmd --list-models` | 暂不可运行 |

## HTTP API

所有接口均需 Bearer Token 鉴权。功能未开启时均返回 404 `{"error": "not found"}`。

### 1. 获取执行者状态
`GET /api/workers`
- 返回格式：
  ```json
  {
    "enabled": true,
    "install_enabled": false,
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
        "login_supported": true
      }
    ],
    "active": "codex"
  }
  ```

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
  - 若未知 session 返回 404 `{"code": "session_not_found"}`。
- **提交验证码**：`POST /api/workers/<id>/login/<session>/code`
  - 请求体：`{"code": "<验证码>"}`。
  - 验证码必须为单行 1..200 可打印字符，否则返回 400 `{"code": "bad_code"}`。
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
- 安全性：
  - 仅支持 HTTPS 协议（测试环境允许本地 `http://127.0.0.1` 或 `localhost`）。
  - 返回载荷仅包含模型 ID `{"models": [{"id": "..."}]}`，绝不泄露 API Key。
- 失败时返回 502 `{"code": "models_fetch_failed", "error": "<简要错误说明>"}`。

## 安全性

1. **固定命令**：一键安装端点严格限制为执行者目录预定义的固定命令，绝不将任何外部用户输入传递给子进程或 shell。
2. **多重防护**：安装功能默认受 `CONVEYOR_WORKERS_INSTALL` 独立环境变量开关保护，并要求调用方显式传递 `{"confirm": true}` 确认。
3. **PTY 隔离与敏感信息保护**：登录桥接使用标准 pseudo-terminal（pty）隔离运行，过滤环境变量（仅保留基础系统变量），密钥与敏感凭据绝不通过 API 载荷暴露。
