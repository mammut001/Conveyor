# Conveyor PR #107 — Local + Oracle VPS 端到端验收报告

验收日期：2026-10-12（UTC）  
仓库：`mammut001/Conveyor`  
PR：#107（验收时为 open、draft）  
验收目标：`9030349b2a55513e92ff204dcd7f4adb6a6b6db5`  
目标基线 / main：`2a29b11ee2041738745ccbe211287b39285051a3`

## 裁决

- **LOCAL ACCEPTANCE PASS**：本地后端、Smoke、Web 类型检查、Lint、构建和 E2E 均通过；完整 Python 测试套件有 7 项因平台能力缺失而跳过，详见下文。
- **VPS ISOLATED ACCEPTANCE PASS**：目标 SHA 在隔离 VPS 环境中完成真实浏览器端到端验收，包括工作流创建、步骤推进、失败/重试、人类确认门、并发冲突、幂等操作和进程重启后的持久化检查。
- **PRODUCTION UNCHANGED PASS**：生产环境验收前后快照一致；没有部署、重启服务或写入生产队列/数据库。
- **TEST ACCEPTANCE PASS; REVIEW FOLLOW-UP REQUIRED**：本报告证明目标 SHA 的本地与隔离 VPS 验收结果，不代表独立代码审查项已解决。PR 保持 open draft；未改动 GitHub main、未创建 PR、未合并或部署。

## 本地验收

在固定目标 SHA 的独立临时 Git worktree 中执行，未使用原工作目录的本地 `.env` 或生产凭据。验收结束后已删除该 worktree。

| 检查 | 结果 |
|---|---|
| Python 编译检查 `python3.12 -m compileall -q .` | PASS；仅 `scripts/icon_smoke.py:187` 有非阻断的无效转义 `SyntaxWarning` |
| 定向 Durable Workflow / Web API 单测 | PASS，14/14 |
| 完整 Python 单测 `unittest discover -s tests -v` | PASS，732 项中 725 通过、7 项跳过，进程退出码 0 |
| `make smoke`（隔离测试环境） | PASS |
| Web `npm ci`、typecheck、lint、build | PASS |
| Web Playwright E2E | PASS，4/4 |

7 项跳过不是通过项：2 项要求本机未安装的 Xvfb/xdotool；4 项要求 Linux `/proc` 进程所有者身份；1 项要求 Linux boot identity。完整套件末尾另有一条 asyncio ResourceWarning，没有导致测试失败。

本地环境：macOS 27.0、Python 3.12.13、Node.js 24.18.0、npm 11.16.0。可用磁盘空间约 409 GiB。

## Oracle VPS 隔离验收

生产机为 Ubuntu 22.04.1 LTS / aarch64，Python 3.10.12、Node.js 22.22.3、npm 10.9.8。通过 Git 验证并检出目标 SHA，在独立的 `/home/ubuntu/conveyor-pr107-test` 目录中构建；没有复制生产 `.env`。隔离测试目录已在验收后移除。

预览服务仅绑定 VPS loopback 的 `127.0.0.1:18887`；关闭功能的对照服务使用独立根目录和 `127.0.0.1:18888`。浏览器通过 SSH 本地转发访问。服务进程在 bubblewrap 隔离中运行，根文件系统只读，生产 `/opt/conveyor` 与 `/home/ubuntu/.codex` 对进程不可见。进程仅获得测试所需的最小环境和临时 Web token；Codex 可执行程序设为 `/usr/bin/false`，Agents、Agent Desktops、Live Screen、Takeover、Routines、Webhooks、Approval Relay、Approval Inbox、MCP、Web Fetch、Search、File Search 等真实集成能力均关闭。主验收保留 PR 的 Workflows 功能；另启动 Workflows 关闭的独立实例进行功能门控检查。

使用 Chromium `156.0.8078.4` 实际操作 Web Console，而非仅调用内部函数或单测。浏览器网络审计只观察到测试 origin `http://127.0.0.1:18887`，没有外部 origin 请求。具体结果：

- 登录、工作流列表、桌面 Workflows 导航及移动端 `More → Durable Workflows` 导航均可用。
- 内置 `Create safe visa demo` 生成 `Australia Visitor 600 — simulated workflow`；文档准备、暂停/恢复、开始/完成、失败/重试以及人类确认门均可从 UI 推进。此工作流最终为 `completed`，revision/event count 为 13/13。
- 按指定名称创建 `Australia Visitor 600 — Mock Application` 并通过 UI 推进。进程重启前它在 revision 4 等待人工确认；重启后 ID、步骤状态、历史和 revision 均保留，经显式人工确认后到达提交检查点。自动开始/完成提交步骤均返回 HTTP 409，只有显式人工确认才能完成；最终 revision/event count 为 8/8。整个流程没有打开签证网站，也没有进行申请提交、付款或 MFA。
- 两个浏览器客户端验证 revision CAS：客户端 B 先推进后，客户端 A 的过期操作得到 409 并刷新状态。重复点击完成操作只产生一次 revision 增量。CAS 工作流最终 revision/event count 为 2/2，第一步完成、下一步就绪。
- 一个正在运行的步骤在隔离服务进程停止并以同一测试数据库重启后仍保持 `running`；随后通过 UI 明确完成，最终 revision/event count 为 3/3。
- 重启后确认等待人工处理的工作流仍阻止自动提交；测试中不存在自动外部副作用。
- 功能关闭实例中，桌面与移动导航均不显示 Workflows；`/api/workflows` 的 GET/POST 在有效测试 token 下返回 409，缺少或错误 token 返回 401。没有创建 workflow 数据库，测试队列为 0。路径穿越探测返回 400/404，未读出文件内容。

测试数据库共 4 个工作流、26 个事件；每个工作流的事件数与 revision 一致，最新事件的结果 revision 与当前 revision 一致。测试队列始终为 0；测试 workspaces/tasks 目录为空；Approval Relay 数据库不存在。数据库文件权限为 `0600`，隔离状态目录权限为 `0700`，未发现符号链接。

## 生产状态前后对比

只读生产基线及隔离测试后的快照匹配：

| 项目 | 验收前 | 验收后 | 结果 |
|---|---|---|---|
| `/opt/conveyor` Git SHA | `2a29b11ee2041738745ccbe211287b39285051a3` | 相同 | PASS |
| `http://127.0.0.1:18787/api/health` | HTTP 200 | HTTP 200 | PASS |
| Conveyor 服务 | 9 个均 active/running | 相同 | PASS |
| maintain / scheduler one-shot service | inactive/dead | 相同 | PASS |
| maintain / scheduler timer | active/waiting | 相同 | PASS |
| 生产队列数据库 | queued=0、running=0、total=21 | 相同 | PASS |
| Deploy Fence | token 不存在、metadata false、共享锁可取、无部署进程 | 相同 | PASS |
| PR #107 测试 systemd unit | 不存在 | 仍不存在 | PASS |

隔离预览进程、SSH 转发、VPS 测试目录及本地临时 worktree 均已停止或清理。没有重启生产 Telegram Bot、Web 或其他 systemd 服务，也没有写入生产数据库、任务、会话、Worktree 或备份。仓库原工作目录中的既有未跟踪文件保持原样。

## 证据

以下截图由隔离预览环境的真实浏览器 UI 操作生成。截图仅包含合成验收工作流和测试控制台状态；没有用户身份、聊天 ID、生产任务内容或凭据，因此未做图像脱敏，也没有修改原始截图。截图文件权限为 `0600`，目录权限为 `0700`。

| 证据 | 文件 |
|---|---|
| 空列表与创建后的列表 | [`workflow-list-empty.png`](conveyor_pr107_evidence/workflow-list-empty.png)、[`workflow-list.png`](conveyor_pr107_evidence/workflow-list.png) |
| 步骤视图、运行中、失败及重试 | [`visitor600-steps.png`](conveyor_pr107_evidence/visitor600-steps.png)、[`workflow-running.png`](conveyor_pr107_evidence/workflow-running.png)、[`workflow-failed.png`](conveyor_pr107_evidence/workflow-failed.png)、[`workflow-retry.png`](conveyor_pr107_evidence/workflow-retry.png) |
| 等待人工确认、提交检查点与完成 | [`workflow-waiting-human.png`](conveyor_pr107_evidence/workflow-waiting-human.png)、[`workflow-submission-human-checkpoint.png`](conveyor_pr107_evidence/workflow-submission-human-checkpoint.png)、[`workflow-completed.png`](conveyor_pr107_evidence/workflow-completed.png) |
| 移动端导航与重启后恢复 | [`workflow-mobile-more.png`](conveyor_pr107_evidence/workflow-mobile-more.png)、[`workflow-after-restart-waiting-human.png`](conveyor_pr107_evidence/workflow-after-restart-waiting-human.png)、[`workflow-after-restart-submission-checkpoint.png`](conveyor_pr107_evidence/workflow-after-restart-submission-checkpoint.png)、[`workflow-after-restart-completed.png`](conveyor_pr107_evidence/workflow-after-restart-completed.png) |

## 产品能力边界

本次验证的 Durable Workflow 是由操作员维护的检查点与事件记录流程。它不自动浏览或提交签证申请，不处理文件上传、MFA、付款或真实提交。人工确认只记录操作者在控制台的显式确认。安全 Human Takeover 集成仍是独立能力。Web Console 使用单个共享 bearer token；本验收不构成多租户隔离验证。

## PR 评论中的独立审查事项

在完成验收报告后核对了 [PR #107 的独立并行审查评论](https://github.com/mammut001/Conveyor/pull/107#issuecomment-6117727743)。评论提出以下事项；本次端到端验收没有覆盖或解决它们，因此测试通过不应被解释为这些语义已通过审查：

1. **P1：中途接管恢复可能跳过未完成步骤。** 评论指出，对正在执行的 `browser` / `work` 步骤请求人工接管后，确认操作可能把整个步骤标成完成，而不是恢复原步骤的游标并要求重新验证。
2. **P1：创建请求的歧义失败重试可能重复创建。** 评论指出，前端每次点击都生成新的 `request_id`；若服务端已提交而响应丢失，用户重试可能创建第二个工作流。需要保留待处理操作 ID 并在重试时复用。
3. **P1，未来执行器边界：** `confirm_human` 是 Web bearer-token 保护的状态变更，不构成 Secure Human Takeover 的审批证明，不应授权外部执行、提交或付款。当前报告验证的只是人工检查点账本行为。
4. **P2，隐私：** 工作流标签以明文存储。文件权限和已知敏感模式过滤不能阻止任意地址、护照号码等个人信息输入；真实材料仍需安全文档库及针对性验证。
5. **P2，负载：** 每次 `/api/workflows` 请求都会实例化 `WorkflowStore` 并运行 schema 初始化；高频轮询并发时可能带来 SQLite 写锁竞争。

评论建议补充中途 MFA/接管恢复与丢响应重试测试，并在真实执行器接入前明确审批审计、个人材料存储和并发初始化设计。本次没有生成代码改动或提交；PR 仍为 draft，后续应先处理或明确裁决这些审查事项，再按项目流程推进。
