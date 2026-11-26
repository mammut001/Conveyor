# Supervisor 任务运行（工作区代码）

当前入口是已认证的 Web 控制台“自动任务”和 Web API。在运行 Web 的进程环境中配置：

```text
CONVEYOR_AGENT_SUPERVISOR_ENABLED=true
CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTATION_EXECUTOR=claude
```

三个执行角色默认继承此执行器；如有需要，可通过 `CONVEYOR_AGENT_SUPERVISOR_ANALYST_EXECUTOR`、`CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_EXECUTOR` 和 `CONVEYOR_AGENT_SUPERVISOR_REVIEWER_EXECUTOR` 分别覆盖。该说明针对旧分支；线上新版的 Pi 工作流以新版文档为准。

控制台选择服务器上的仓库并输入目标后即可启动、查看进度或停止。也可以先 `POST /api/supervisor/tasks`，请求体至少包含 `goal` 和 `repo_path`；然后 `POST /api/supervisor/tasks/<id>/dispatch`，例如：

```json
{
  "acceptance_criteria": "描述期望结果",
  "validation_commands": [["python3", "-m", "unittest", "discover", "-s", "tests"]]
}
```

不提供 `worktree_path` 时，运行时会在 `CODEX_TASK_ROOT/supervisor-worktrees` 下按仓库当前提交创建隔离 worktree；主工作区未提交的修改不会带入。也可以指定属于该仓库的现成、干净的独立 worktree。创建的 worktree 会保留以供审阅。

省略 `validation_commands` 时，系统尝试识别根目录 `package.json` 的 `test` 脚本、pytest 配置或 `tests` 目录中的 unittest 测试；无法确定时保留工作树和待启动任务，供补充命令后重试。自动识别的是项目的基础验证，不保证覆盖目标的所有行为；审阅仍需判断任务要求。unittest 发现零个测试不会被当成通过。

手动指定命令时，使用 1–8 条参数数组直接执行，不经过 shell；也可在创建任务时放入 `metadata.validation_commands`。请确保命令适合该项目，且不会改动受版本控制或未忽略的文件。测试所需依赖仍须存在于运行环境中。

任务详情 `GET /api/supervisor/tasks/<id>` 包含状态、交接、验证输出和有效验收。命令失败时会反馈给实现角色并按配置限制修复次数；完成前还需独立审阅。`POST /api/supervisor/tasks/<id>/cancel` 请求停止本 Web 进程管理的任务，运行时在进程组结束后释放锁。验收只覆盖 Git 快照中的源码，不证明忽略文件、外部服务或部署结果。

持久锁防止工作树被重复使用。异常退出后当前实现不能识别或重连可能仍存活的执行进程，也不会自动释放锁；状态可能仍显示运行中。操作人员应先确认相关进程已停止，再由维护人员处理记录，不能仅按超时推断进程已退出。部署记录和应用改动仍是独立步骤。
