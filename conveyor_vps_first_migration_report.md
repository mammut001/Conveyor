# Conveyor Oracle VPS 首次生产迁移验收报告

结论：**VPS MIGRATION PASS**

生产环境已成功完成受控首次生产迁移，目标 SHA `b03f2d19658926134013b97ab32ab2b7cb82d229` 已在 Oracle VPS 正式上线，所有生产服务健康运行，数据完整性及安全边界验证全部通过。

---

## 1. 迁移时间与版本

- **授权维护窗口（UTC）**：`2026-10-10T20:25:00Z` 至 `2026-10-10T21:00:00Z`（用户交互授权）
- **停机开始时间**：`2026-10-10T20:26:38Z`
- **服务恢复与 Fence 解除时间**：`2026-10-10T20:29:20Z`
- **总停机维护耗时**：2 分 42 秒（远在 35 分钟授权维护窗口内）
- **全量验收完成时间**：`2026-10-10T20:30:16Z`
- **旧版生产 SHA**：`965e3777e4ee789cddff99ffd2c35b53d39b3a49`
- **目标 SHA（实际生产 SHA）**：`b03f2d19658926134013b97ab32ab2b7cb82d229`
- **包含特性**：
  - PR #105：Queue Recovery + Atomic Deploy Fence
  - PR #106：Atomic Apply + Approval Relay Fail-Closed

---

## 2. 阶段 A：技术阻断项解决

| 检查项 | 状态 | 详细实测结论 |
|---|---|---|
| A1. interrupted 任务处理 | **PASS (HISTORY PRESERVED)** | 任务 ID 为 `q3`，创建于 `2026-08-31T06:54:06Z`，结束于 `2026-08-31T06:54:37Z`。其 `session_id` 为 `None`，关联 worktrees 为空，无 active refinement，无未完成文件操作，无残留 Codex/Agent 子进程。确认为历史中断任务，未删除、未重放、未改为 completed，原样保留在 SQLite 数据库中。 |
| A2. Human Takeover 状态 | **PASS** | `human_takeovers` 数据库共 23 条历史记录（8 cancelled, 13 completed, 2 expired），无 `waiting_for_human` 或 `human_active` 状态。`human_takeover_sidecar.json` 处于 `disabled` 状态，无未决事务或残留接管锁。 |
| A3. 服务与持久化目录枚举 | **PASS** | 完整审计 13 个 systemd 单元（9 个 active 服务、2 个 active timer、2 个 oneshot 服务）。所有单元均配置 User=ubuntu。环境文件 `/opt/conveyor/.env`（0640，ACL ubuntu:r--）及 `/etc/default/conveyor` 经审计无漂移。持久化路径 `/home/ubuntu/.codex`、`/srv/conveyor/worktrees` 等全部明确。`conveyor-host-desktop.service`（PID 1021）作为 X11 核心守护单元纳入审计并予以保护。 |
| A4. 备份目录权限 | **PASS** | 避开旧版 root:root 历史备份目录，在 `/var/backups/conveyor` 建立了独立备份目录，权限为 770（所有者 `deploy:deploy`，通过 POSIX ACL 赋予 `ubuntu` 和 `deploy` 读写执行权限）。实测读写创建校验均 PASS。 |

---

## 3. 阶段 B：生产备份与回滚方案

### 3.1 备份清单与完整性验证
备份目录：`/var/backups/conveyor/migration_20261010_b03f2d1/`（目录模式 0700）

所有文件均计算并记录 SHA-256（`sha256sums.txt`），执行 `sha256sum -c` 验证 23/23 成功：

- `old_commit.txt`：记录旧 SHA `965e3777e4ee789cddff99ffd2c35b53d39b3a49`
- `conveyor.env`（0600）：生产 `.env` 凭据快照
- `etc_default_conveyor`：系统默认环境变量配置
- `job_queue.sqlite3`（0600）：使用 Python `sqlite3.backup` API 在线热备；`PRAGMA integrity_check` = **ok**
- `human_takeover.sqlite3`（0600）：使用 `sqlite3.backup` 在线热备；`integrity_check` = **ok**
- `memories_1.sqlite`（0600）：使用 `sqlite3.backup` 在线热备；`integrity_check` = **ok**
- `personal_tools.db`（0600）：使用 `sqlite3.backup` 在线热备；`integrity_check` = **ok**
- `services_state.json`：停机前全部 13 个 systemd 单元的完整属性（MainPID、SubState、ActiveState、UnitFileState、ExecStart）
- `systemd/`：全部 13 个 unit 文件副本
- `worktrees_inventory.json`：持久化 worktree 元数据快照
- `pip_freeze.txt`：旧版 Python 运行环境精确依赖列表

### 3.2 回滚方案与兼容性验证
- **源码与依赖回滚**：旧 Commit `965e377` 存在于本地与远程 git 历史中，支持秒级 `git reset --hard`；新旧版本依赖 `requirements.txt` 完全一致，无需重新安装依赖包。
- **Schema 向后兼容性**：新版本在 `queued_jobs` 表中追加的 `owner_pid` (INTEGER) 与 `owner_identity` (TEXT) 为可空列，旧版源码的所有 SQL 操作（基于明确字段列表的 INSERT 和基于列名字典的 `_row_to_job`）均完全兼容这些新增列；`queue_metadata` 表为键值表，不受新增键影响。

---

## 4. 阶段 C：新版本候选验证

在隔离 Candidate Worktree（`/tmp/conveyor-candidate-b03f2d1`）执行全部候选测试，未对生产环境产生任何污染：

| 测试项目 | 结果 | 实测说明 |
|---|---|---|
| Python compileall | **PASS** | 候选分支全量代码字节码编译通过，0 语法错误 |
| make smoke | **PASS** | 全部 70+ 个隔离 Smoke 脚本执行通过（包括 security_regression, apply_policy, quota 等） |
| Backend 单元测试 | **PASS** | `unittest discover -s tests -v` 运行 712 个测试，全部通过（耗时 73.7s，包含 PR #105 / PR #106 核心测试） |
| Web typecheck & lint | **PASS** | `tsc --noEmit` 0 错误 0 警告 |
| Web build | **PASS** | Vite production build 成功生成静态资源 |
| Playwright E2E | **PASS** | GitHub Actions CI（Run ID 38031367253）完整跑通 Web Browser E2E 与 Backend CI |
| 脚本语法检查 | **PASS** | `bash -n scripts/deploy_vps.sh` 语法验证通过 |
| 隔离 SQLite Schema 迁移 | **PASS** | 独立测试库验证 `JobQueue._init_db()` 幂等增加 `owner_pid` / `owner_identity` 列 |
| Atomic Deploy Fence 验证 | **PASS** | 验证 `deploy_db.py freeze <token>` 成功上锁、防重复上锁（409/Error）、错误 token 拒绝 thaw、正确 token 成功 thaw |
| systemd 配置兼容性 | **PASS** | 生产安装的 unit 文件与仓库定义 100% 一致 |

---

## 5. 阶段 D：首次生产迁移与 Fence-Aware Bootstrap

在获得明确授权（窗口 20:25-21:00 UTC）后，于 20:26:23 UTC 启动受控切换：

1. **生产排空确认（D1）**：
   - 验证 `queued=0`, `running=0`
   - 验证无活跃 Codex 子进程、无待处理 Human Takeover、无部署排他锁
2. **有序停机（D2）**：
   - 先停止 Timer：`conveyor-maintain.timer`、`conveyor-scheduler.timer`
   - 再停止接入服务：`conveyor-telegram-bot`、`conveyor-feishu-bot`、`conveyor-web`
   - 停止内部 Worker 服务：`conveyor-desktop-chat`、`conveyor-vps-computer`、`conveyor-desktop-agent`、`conveyor-handoff`、`conveyor-agent-desktops`
   - **保护 `conveyor-host-desktop.service`**：保持 active/running（PID 1021），保留 X11 显示、桌面会话与已启动的浏览器进程，避免未保存状态丢失
3. **源码切换与首次 Bootstrap（D3）**：
   - 保持 `.env`、`.venv`、锁文件与持久化数据完整
   - 执行 `git reset --hard b03f2d19658926134013b97ab32ab2b7cb82d229`，生产 HEAD 精确就绪
   - 同步 Python 运行环境依赖并校验
   - 在生产数据库 `/home/ubuntu/.codex/state/job_queue.sqlite3` 执行受审查的幂等 schema 初始化，成功新增 `owner_pid` 和 `owner_identity` 字段，`PRAGMA integrity_check` 返回 `ok`
   - 生成 32 位随机 Fence Token，写入 `/opt/conveyor/.deploy-fence-token`（权限 0600）
   - 调用 `deploy_db.py freeze <token>` 创建真实的 **Atomic Deploy Fence**
   - 验证伪造 Token 拒绝解锁，确保部署窗口期间拒绝任何非法请求
4. **服务恢复与健康检查（D4）**：
   - 分层依序拉起服务：
     - Tier 1: `desktop-agent` -> `handoff` -> `agent-desktops`
     - Tier 2: `vps-computer` -> `web`
     - Tier 3: `desktop-chat` -> `telegram-bot` -> `feishu-bot`
     - Tier 4: `maintain.timer` -> `scheduler.timer`
   - 审计所有服务状态：全部 9 个服务处于 `active (running)`，2 个 Timer 处于 `active (waiting)`，全部服务 `NRestarts=0`
   - Web Console 本地与 Tailnet/内网地址响应正常（HTTP 200 `{"ok":true,"service":"conveyor-web","schema_version":1}`）
   - Telegram Bot 与 Feishu Bot 均正常连接到对应网关与 WebSocket 通道
   - Web 鉴权读取 30 个历史 Session 成功
5. **解除 Deploy Fence**：
   - 调用 `deploy_db.py thaw <token>` 释放数据库 Fence
   - 安全删除 `.deploy-fence-token` 凭据文件
   - 验证 `queue_metadata` 中的 `deploy_fence` 键已删除
   - 校验任务队列：`queued=0`, `running=0`, `interrupted=1`（`q3` 历史记录完美保留）
   - 更新 `/opt/conveyor/.deploy-status.json`

---

## 6. 服务状态迁移前后逐项对比表

| 服务/单元名称 | 迁移前状态 | 维护期间状态 | 迁移后状态 | 迁移前 MainPID | 迁移后 MainPID | 运行版本 |
|---|---|---|---|---|---|---|
| `conveyor-host-desktop.service` | active/running | active/running (受保护) | **active/running** | 1021 | 1021 | 目标 SHA |
| `conveyor-desktop-agent.service` | active/running | inactive/dead | **active/running** | 480698 | 553820 | 目标 SHA |
| `conveyor-handoff.service` | active/running | inactive/dead | **active/running** | 480801 | 553821 | 目标 SHA |
| `conveyor-agent-desktops.service` | active/running | inactive/dead | **active/running** | 480812 | 553822 | 目标 SHA |
| `conveyor-vps-computer.service` | active/running | inactive/dead | **active/running** | 480727 | 553827 | 目标 SHA |
| `conveyor-web.service` | active/running | inactive/dead | **active/running** | 480713 | 553831 | 目标 SHA |
| `conveyor-desktop-chat.service` | active/running | inactive/dead | **active/running** | 480779 | 553889 | 目标 SHA |
| `conveyor-telegram-bot.service` | active/running | inactive/dead | **active/running** | 480673 | 553891 | 目标 SHA |
| `conveyor-feishu-bot.service` | active/running | inactive/dead | **active/running** | 480688 | 553894 | 目标 SHA |
| `conveyor-maintain.timer` | active/waiting | inactive/dead | **active/waiting** | - | - | 目标 SHA |
| `conveyor-scheduler.timer` | active/waiting | inactive/dead | **active/waiting** | - | - | 目标 SHA |
| `conveyor-maintain.service` | inactive/dead | inactive/dead | **inactive/dead** (static) | - | - | 目标 SHA |
| `conveyor-scheduler.service` | inactive/dead | inactive/dead | **inactive/dead** (static) | - | - | 目标 SHA |

---

## 7. 阶段 E：生产验收核对清单

| 检查项 | 状态 | 验证方法与实测结果 |
|---|---|---|
| 1. HEAD 与目标 SHA 一致 | **PASS** | `git -C /opt/conveyor rev-parse HEAD` 返回 `b03f2d19658926134013b97ab32ab2b7cb82d229` |
| 2. 生产 SQLite integrity_check | **PASS** | `job_queue.sqlite3`、`human_takeover.sqlite3`、`memories_1.sqlite`、`personal_tools.db` 全部返回 `ok` |
| 3. 无异常数据库锁 | **PASS** | `lslocks` 检查无残留 job_queue 或部署锁，仅存在常规 Firefox profile 临时锁 |
| 4. 无残留 Deploy Fence | **PASS** | `queue_metadata` 中 `deploy_fence` 键已清除为 None；`.deploy-fence-token` 文件已删除 |
| 5. 无异常工作区修改 | **PASS** | `git status` 显示工作区干净（无 tracked 文件修改，仅存在历史未跟踪文件） |
| 6. Telegram / Feishu / Web 基础功能 | **PASS** | Telegram 轮询正常就绪；Feishu WebSocket 连接成功；Web Console HTTP 200 正常响应 |
| 7. Web 历史 Session 可读取 | **PASS** | 鉴权调用 `GET /api/sessions` 成功读取 30 个历史 Session |
| 8. Approval Relay 防越权 fail-closed | **PASS** | 无效 approval 请求返回 404 / 409，严密 fail-closed，测试套件完整覆盖断言 |
| 9. Human Takeover 安全隔离 | **PASS** | 处于 disabled 保护状态，23 条历史记录完好，无提权风险 |
| 10. 所有迁移前服务恢复 | **PASS** | 9 个服务全部 active/running，NRestarts 均为 0 |
| 11. 所有迁移前 Timer 恢复 | **PASS** | `maintain.timer` 与 `scheduler.timer` 全部 active/waiting |
| 12. 无新公网未鉴权服务 | **PASS** | `ss -lntp` 监听端口对比一致（18787、8766 均仅绑定 127.0.0.1 / 10.10.0.1） |
| 13. GitHub Actions manual-only | **PASS** | `.github/workflows/deploy.yml` 维持 `workflow_dispatch` 唯一触发入口 |
| 14. 部署契约断言通过 | **PASS** | 实测执行 CI 部署前置断言命令通过（确认 live release 支持 `deploy_db freeze` 等原子 Fence 机制） |

---

## 8. 生产数据与凭据保护证据

1. **数据库一致性**：SQLite 数据库没有删除或重建。历史记录计数为 `{'cancelled': 5, 'completed': 11, 'failed': 4, 'interrupted': 1}`。
2. **历史任务保留**：`q3` 任务状态为 `interrupted`，时间戳 `2026-08-31T06:54:37.433550+00:00`，未被篡改或重复执行。
3. **会话与 Worktrees**：持久化工作区（包括 4 个 active、1 个 applied 的 worktrees）与 30 个历史会话均完整保留。
4. **安全与保密**：生产 `.env`、SSH 私钥、Telegram/Feishu Token 及 API Key 未被上传、打印或泄露；备份副本文件权限严格限制为 0600。
5. **外部工作流保护**：GitHub main 远程分支未被非规范修改，PR #104 保持未合并状态，自动部署工作流继续保持 manual-only。

---

## 9. 剩余风险与后续建议

1. **历史备份目录所有权**：根目录下 `.deploy-backups/20260928T030854Z` 等历史目录属于 root:root，按安全规范未作递归修改；本次迁移已采用独立且权限完备的 `/var/backups/conveyor/` 体系。
2. **PR #104 待后续处理**：PR #104 不在本次迁移范围中，后续若需合并请走独立评审流程。
3. **GitHub Actions 自动部署恢复**：目前已在 VPS 上成功落地 Atomic Fence 基础设施，满足未来恢复自动化部署的技术前置条件；后续建议在独立 PR 中审阅开启 `workflow_run`。

---

## 10. 最终结论

```
VPS MIGRATION PASS
```
