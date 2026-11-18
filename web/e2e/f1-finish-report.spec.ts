import { expect, test, type Page, type Route } from '@playwright/test'
import type { SupervisorCapabilities, SupervisorRepo } from '../src/components/SupervisorWorkspace'

const TOKEN = 'f1-finish-report-e2e-token'
const SESSION_ID = 'web:web-console:default'

function createMockCapabilities(): SupervisorCapabilities {
  return {
    enabled: true,
    coordinator: { executor: 'pi', model: null },
    roles: {
      analyst: { executor: 'pi', model: null, available: true },
      implementer: { executor: 'pi', model: null, available: true },
      reviewer: { executor: 'pi', model: null, available: true },
    },
    executors: [{ name: 'pi', label: 'Pi（内置）', available: true }],
    deployment: { configured: false, can_start: false, reason: '尚未配置生产部署运行器' },
    source_machine: 'dev-workstation',
  }
}

const REPORT_TEXT = [
  'qa-todo-cli 的目标结束了：时间到了。',
  '跑了 6 轮，通过 4 轮，用时 1 小时。',
  '- 初始化项目与测试框架',
  '- 实现 add 与 list 命令',
  '- 实现 done 命令',
  '- 实现delete命令并全部测试通过',
  '自动检查：scripts/check.sh（最后一轮 通过）',
  '改动在工作目录 sup/wt-1，还没有合并到主分支。',
].join('\n')

const F1_DONE_GOAL = {
  id: 'goal-f1-done', repo_id: 'repo-qa-todo', worktree_id: 'wt-1',
  title: '实现 delete 命令', outcome: '实现 delete 命令并全部测试通过',
  max_iterations: 20, role_overrides: {}, state: 'exhausted', mode: 'continuous',
  created_at: '2026-11-18T10:00:00+00:00', updated_at: '2026-11-18T10:30:00+00:00',
  question: '', summary: '时间到了：一共跑了 1 轮，其中 1 轮通过检查。', stop_reason: 'deadline',
  last_next_prompt: '', iterations_used: 1, rounds_total: 1, rounds_passed: 1,
  iterations: [{ n: 1, task_id: 'task-f1', prompt: '实现 delete 命令', decision: 'achieved', decision_reason: '完成',
    started_at: '2026-11-18T10:00:00+00:00', finished_at: '2026-11-18T10:20:00+00:00', task_final_state: 'completed', task_state: 'completed', passed: true, round_title: '实现delete命令并全部测试通过' }],
}
const diffCalls: string[] = []

async function setupMockServer(page: Page) {
  const actionCalls: string[] = []
  const repos: SupervisorRepo[] = [
    {
      id: 'repo-qa-todo',
      name: 'qa-todo-cli',
      path: '/srv/projects/qa-todo-cli',
      source_machine: 'dev-workstation',
      validation_source: 'repo',
      worktrees: [
        { id: 'wt-main', path: '/srv/projects/qa-todo-cli', branch: 'main', head: '1111111', is_main: true, managed: false, locked: false },
        { id: 'wt-1', path: '/srv/projects/qa-todo-cli/.worktrees/wt-1', branch: 'sup/wt-1', head: '2222222', is_main: false, managed: true, locked: false, chat_session_id: 'web:web-console:supwt-wt-1' },
      ],
    },
  ]

  let reportCard = {
    id: 'card-finish-report-1',
    title: '目标已结束',
    text: REPORT_TEXT,
    used: false,
    outcome: '',
    buttons: [
      {
        id: 'act-view-changes',
        label: '查看改动',
        action: 'open_project',
        style: 'secondary',
        payload: { view: 'code', tab: 'goals', repo_id: 'repo-qa-todo', worktree_id: 'wt-1', goal_id: 'goal-f1-done', show_changes: true },
      },
      {
        id: 'act-merge-branch',
        label: '合并到主分支',
        action: 'request_merge_worktree',
        style: 'primary',
        payload: {},
      },
      {
        id: 'act-rerun-1h',
        label: '再跑 1 小时',
        action: 'rerun_goal_one_hour',
        style: 'secondary',
        payload: {},
      },
    ],
  }

  let confirmCard = {
    id: 'card-merge-confirm-1',
    title: '确认合并到主分支',
    text: '把 sup/wt-1 合并到 main？合并后不能自动撤销，确认要合并吗？',
    used: false,
    outcome: '',
    buttons: [
      {
        id: 'act-confirm-merge',
        label: '确认合并',
        action: 'confirm_merge_worktree',
        style: 'primary',
        payload: {},
      },
      {
        id: 'act-cancel-merge',
        label: '取消',
        action: 'cancel_action',
        style: 'secondary',
        payload: {},
      },
    ],
  }

  await page.addInitScript((tok: string) => {
    localStorage.setItem('conveyor-token', tok)
    localStorage.setItem('conveyor-chat-session', 'web:web-console:default')
  }, TOKEN)

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (value: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })

    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 120,
        load_average: [0.1, 0.1, 0.1],
        cpu_count: 4,
        memory: { total: 8_000_000_000, available: 4_000_000_000 },
        disk: { total: 100_000_000_000, used: 20_000_000_000, free: 80_000_000_000 },
        queue: { depth: 0, paused: false, states: {} },
        channels: { telegram: { configured: false }, feishu: { configured: false } },
        nodes: [],
        features: { agents: false, long_term_memory: true, routines: true, teammate: true, agent_supervisor: true },
      })
    }
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [] })
    if (path === '/api/inbox') return json({ unread: 0, items: [] })
    if (path === '/api/takeover/status') return json({ available: false })
    if (path === '/api/screen/status') return json({ enabled: false, available: false })
    if (path === '/api/events/stream') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    }
    if (path === '/api/chat/history' && method === 'GET') {
      return json({
        session_id: SESSION_ID,
        messages: [
          {
            role: 'assistant',
            text: REPORT_TEXT,
            kind: 'action_card',
            created_at: '2026-11-16T01:00:00Z',
            action_card: reportCard,
          },
        ],
      })
    }
    if (path === '/api/chat/actions' && method === 'POST') {
      const body = req.postDataJSON() as { action_id?: string }
      const actionId = String(body.action_id || '')
      actionCalls.push(actionId)

      if (actionId === 'act-merge-branch') {
        if (reportCard.used) return json({ error: '这个操作已经处理过了，不能重复使用' }, 409)
        reportCard = { ...reportCard, used: true, outcome: '准备把 sup/wt-1 合并到 main' }
        return json({
          ok: true,
          card_id: reportCard.id,
          outcome: reportCard.outcome,
          card: reportCard,
          follow_up_message: {
            role: 'assistant',
            text: confirmCard.text,
            kind: 'action_card',
            created_at: '2026-11-16T01:01:00Z',
            action_card: confirmCard,
          },
        })
      }

      if (actionId === 'act-confirm-merge') {
        if (confirmCard.used) return json({ error: '这个操作已经处理过了，不能重复使用' }, 409)
        confirmCard = { ...confirmCard, used: true, outcome: '已把 sup/wt-1 合并到 main' }
        return json({
          ok: true,
          card_id: confirmCard.id,
          outcome: confirmCard.outcome,
          card: confirmCard,
          follow_up_message: {
            role: 'assistant',
            text: '已经把 sup/wt-1 合并到 main（改了 3 个文件）。',
            kind: 'chat',
            created_at: '2026-11-16T01:01:10Z',
          },
        })
      }

      return json({ error: 'unknown action' }, 404)
    }

    if (path === '/api/supervisor/capabilities' && method === 'GET') return json(createMockCapabilities())
    if (path === '/api/supervisor/repositories' && method === 'GET') return json({ repositories: repos })
    if (path === '/api/supervisor/tasks' && method === 'GET') return json({ tasks: [], count: 0 })
    if (path === '/api/supervisor/deployments' && method === 'GET') return json({ deployments: [], count: 0 })
    if (path === '/api/supervisor/goals' && method === 'GET') return json({ goals: [F1_DONE_GOAL], count: 1 })
    if (path === '/api/supervisor/goals/goal-f1-done' && method === 'GET') return json(F1_DONE_GOAL)
    if (path === '/api/supervisor/goals/goal-f1-done/diff' && method === 'GET') {
      diffCalls.push(path)
      return json({
        goal_id: 'goal-f1-done', base_sha: '1111111', head_sha: '2222222',
        files: [{ path: 'todo.py', status: 'M', additions: 30, deletions: 2 }, { path: 'tests/test_todo.py', status: 'M', additions: 55, deletions: 0 }],
        diff: ['diff --git a/todo.py b/todo.py', '--- a/todo.py', '+++ b/todo.py', '@@ -1,1 +1,2 @@', ' import sys', '+import json'].join('\n'),
        truncated: false,
      })
    }
    const validationMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/validation$/)
    if (validationMatch && method === 'GET') {
      return json({
        repo_id: validationMatch[1],
        candidates: [
          {
            id: 'check-script',
            label: 'scripts/check.sh',
            argv: ['sh', 'scripts/check.sh'],
            reason: '项目自带检查脚本',
            available: true,
            unavailable_reason: '',
          },
        ],
        selected: ['check-script'],
        source: 'repo',
        env_commands: [],
        effective: [{ name: 'check-script', argv: ['sh', 'scripts/check.sh'] }],
      })
    }
    return json({})
  })

  return { actionCalls }
}

test('finish report renders with three buttons, 查看改动 navigates, and 合并到主分支 confirms first', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  const { actionCalls } = await setupMockServer(page)

  await page.goto('/')
  await page.getByRole('button', { name: /对话/ }).first().click()

  // Report text and all 3 action buttons render
  await expect(page.getByText('qa-todo-cli 的目标结束了：时间到了。')).toBeVisible()
  await expect(page.getByText('实现delete命令并全部测试通过')).toBeVisible()
  const viewBtn = page.getByRole('button', { name: '查看改动', exact: true })
  const mergeBtn = page.getByRole('button', { name: '合并到主分支', exact: true })
  const rerunBtn = page.getByRole('button', { name: '再跑 1 小时', exact: true })
  await expect(viewBtn).toBeVisible()
  await expect(mergeBtn).toBeVisible()
  await expect(rerunBtn).toBeVisible()

  // Clicking 查看改动 opens 项目 › 目标 on wt-1 with this goal's changes already expanded
  // (no /api/chat/actions call, and not the dispatch form).
  await viewBtn.click()
  expect(actionCalls).toEqual([])
  await expect(page.getByRole('heading', { name: 'qa-todo-cli / sup/wt-1' })).toBeVisible()
  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('.sup-goal-changes-panel')).toBeVisible()
  await expect(page.locator('.sup-goal-changes-panel')).toContainText('test_todo.py')
  expect(diffCalls.length).toBeGreaterThan(0)

  // Return to 对话 and click 合并到主分支 -> confirmation card opens first
  await page.getByRole('button', { name: /对话/ }).first().click()
  await mergeBtn.click()
  expect(actionCalls).toEqual(['act-merge-branch'])
  await expect(page.getByText('把 sup/wt-1 合并到 main？合并后不能自动撤销，确认要合并吗？')).toBeVisible()
  const confirmBtn = page.getByRole('button', { name: '确认合并', exact: true })
  await expect(confirmBtn).toBeVisible()
  await expect(page.getByRole('button', { name: '取消', exact: true })).toBeVisible()

  // Navigation button 查看改动 stays clickable even after card was used
  await expect(viewBtn).toBeEnabled()

  // Confirm merge -> calls confirm action and shows Chinese result
  await confirmBtn.click()
  expect(actionCalls).toEqual(['act-merge-branch', 'act-confirm-merge'])
  await expect(page.getByText('已经把 sup/wt-1 合并到 main（改了 3 个文件）。')).toBeVisible()
})
