import { expect, test, type Page, type Route } from '@playwright/test'
import type { SupervisorCapabilities, SupervisorRepo } from '../src/components/SupervisorWorkspace'

const TOKEN = 'a2-chat-intents-e2e-token'
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

async function setupMockServer(page: Page) {
  const actionCalls: Array<{ action_id?: string }> = []
  let repos: SupervisorRepo[] = []
  let createdActionCard = {
    id: 'card-create-todo',
    title: '项目已创建',
    text: '已创建项目 todo-app（空的 Git 仓库），可以开始了。',
    used: false,
    outcome: '',
    buttons: [
      {
        id: 'act-open-todo',
        label: '打开项目',
        action: 'open_project',
        style: 'primary',
        payload: { view: 'code', repo_id: 'repo-todo-app', worktree_id: 'wt-todo-1' },
      },
      {
        id: 'act-prefill-todo',
        label: '让它开始干活',
        action: 'prefill_timed_goal',
        style: 'secondary',
        payload: { repo_id: 'repo-todo-app', repo_name: 'todo-app' },
      },
    ],
  }
  let confirmActionCard = {
    id: 'card-confirm-goal',
    title: '确认开始持续改进',
    text: '准备在 todo-app 上持续改进 10 小时：把首页做好。由执行者池自动安排。时间到或者模型额度用完会自动停。',
    used: false,
    outcome: '',
    buttons: [
      {
        id: 'act-start-goal',
        label: '开始',
        action: 'confirm_start_timed_goal',
        style: 'primary',
        payload: {},
      },
      {
        id: 'act-edit-goal',
        label: '改一改',
        action: 'edit_timed_goal',
        style: 'secondary',
        payload: { view: 'code', tab: 'goals', repo_id: 'repo-todo-app', worktree_id: 'wt-todo-1', goal_outcome: '把首页做好', duration_minutes: 600 },
      },
      {
        id: 'act-cancel-goal',
        label: '取消',
        action: 'cancel_action',
        style: 'secondary',
        payload: {},
      },
    ],
  }

  await page.addInitScript(() => {
    localStorage.setItem('conveyor-token', 'a2-chat-intents-e2e-token')
  })

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
      return json({ session_id: SESSION_ID, messages: [] })
    }
    if (path === '/api/chat' && method === 'POST') {
      const body = req.postDataJSON() as { message?: string }
      const text = String(body.message || '')
      if (text.includes('todo-app')) {
        repos = [
          {
            id: 'repo-todo-app',
            name: 'todo-app',
            path: '/srv/projects/todo-app',
            source_machine: 'dev-workstation',
            validation_source: 'none',
            worktrees: [
              { id: 'wt-todo-main', path: '/srv/projects/todo-app', branch: 'main', head: '1111111', is_main: true, managed: false, locked: false },
              { id: 'wt-todo-1', path: '/srv/projects/todo-app/.worktrees/wt-1', branch: 'sup/wt-1', head: '1111111', is_main: false, managed: true, locked: false, chat_session_id: 'web:web-console:supwt-todo-1' },
            ],
          },
        ]
        const sse = [
          `event: session\ndata: ${JSON.stringify({ session_id: SESSION_ID })}\n\n`,
          `event: message\ndata: ${JSON.stringify({ text: createdActionCard.text, kind: 'action_card', action_card: createdActionCard })}\n\n`,
          'event: done\ndata: {"outcome":"answered"}\n\n',
        ].join('')
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
      }
      const sse = [
        `event: session\ndata: ${JSON.stringify({ session_id: SESSION_ID })}\n\n`,
        `event: message\ndata: ${JSON.stringify({ text: confirmActionCard.text, kind: 'action_card', action_card: confirmActionCard })}\n\n`,
        'event: done\ndata: {"outcome":"answered"}\n\n',
      ].join('')
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
    }
    if (path === '/api/chat/actions' && method === 'POST') {
      const body = req.postDataJSON() as { action_id?: string }
      actionCalls.push(body)
      if (body.action_id === 'act-prefill-todo') {
        if (createdActionCard.used) {
          return json({ error: '这个操作已经处理过了，不能重复使用' }, 409)
        }
        createdActionCard = { ...createdActionCard, used: true, outcome: '已准备在 todo-app 上设定定时任务' }
        return json({
          ok: true,
          card_id: createdActionCard.id,
          outcome: createdActionCard.outcome,
          card: createdActionCard,
          client_action: { type: 'prefill_chat', text: '在 todo-app 上持续改进 2 小时：' },
        })
      }
      if (body.action_id === 'act-edit-goal') {
        if (confirmActionCard.used) {
          return json({ error: '这个操作已经处理过了，不能重复使用' }, 409)
        }
        confirmActionCard = { ...confirmActionCard, used: true, outcome: '已转到「项目 › 目标」修改' }
        return json({
          ok: true,
          card_id: confirmActionCard.id,
          outcome: confirmActionCard.outcome,
          card: confirmActionCard,
          client_action: {
            type: 'edit_timed_goal',
            payload: {
              view: 'code',
              tab: 'goals',
              repo_id: 'repo-todo-app',
              worktree_id: 'wt-todo-1',
              goal_outcome: '把首页做好',
              duration_minutes: 600,
            },
          },
        })
      }
      if (body.action_id === 'act-start-goal') {
        if (confirmActionCard.used) {
          return json({ error: '这个操作已经处理过了，不能重复使用' }, 409)
        }
        confirmActionCard = { ...confirmActionCard, used: true, outcome: '已开始持续改进「todo-app」（10 小时）' }
        return json({
          ok: true,
          card_id: confirmActionCard.id,
          outcome: confirmActionCard.outcome,
          card: confirmActionCard,
          follow_up_message: {
            role: 'assistant',
            text: '已经开始了，预计 22:00 结束。随时在 项目 › 目标 里看进度或停止。',
            kind: 'action_card',
            created_at: new Date().toISOString(),
            action_card: {
              id: 'card-goal-started',
              title: '已开始持续改进',
              text: '已经开始了，预计 22:00 结束。随时在 项目 › 目标 里看进度或停止。',
              used: false,
              outcome: '',
              buttons: [
                {
                  id: 'act-view-progress',
                  label: '查看进度',
                  action: 'open_goal_progress',
                  style: 'primary',
                  payload: { view: 'code', tab: 'goals', repo_id: 'repo-todo-app', worktree_id: 'wt-todo-1' },
                },
              ],
            },
          },
        })
      }
      return json({ error: 'unknown action' }, 404)
    }

    if (path === '/api/supervisor/capabilities' && method === 'GET') return json(createMockCapabilities())
    if (path === '/api/supervisor/repositories' && method === 'GET') return json({ repositories: repos })
    if (path === '/api/supervisor/tasks' && method === 'GET') return json({ tasks: [], count: 0 })
    if (path === '/api/supervisor/deployments' && method === 'GET') return json({ deployments: [], count: 0 })
    if (path === '/api/supervisor/goals' && method === 'GET') return json({ goals: [], count: 0 })
    const validationMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/validation$/)
    if (validationMatch && method === 'GET') {
      return json({
        repo_id: validationMatch[1],
        candidates: [],
        selected: [],
        source: 'none',
        env_commands: [],
        effective: [],
      })
    }
    return json({})
  })

  return { actionCalls }
}

test('create project action card navigates to 项目 and timed goal 开始 disables card', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  const { actionCalls } = await setupMockServer(page)
  await page.goto('/')
  await page.getByRole('button', { name: /对话/ }).first().click()

  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await expect(composer).toBeVisible()

  await composer.fill('帮我创建一个 repo 叫 todo-app')
  await composer.press('Enter')

  await expect(page.getByText('已创建项目 todo-app（空的 Git 仓库），可以开始了。')).toBeVisible()
  const openProjectBtn = page.getByRole('button', { name: '打开项目', exact: true })
  await expect(openProjectBtn).toBeVisible()
  await openProjectBtn.click()

  // Navigated to 项目 view with todo-app and its managed worktree selected.
  await expect(page.getByRole('heading', { name: 'todo-app / sup/wt-1' })).toBeVisible()

  // Go back to 对话 and trigger timed goal confirmation card.
  await page.getByRole('button', { name: /对话/ }).first().click()
  await expect(composer).toBeVisible()
  await composer.fill('你来迭代十个小时，把首页做好')
  await composer.press('Enter')

  const confirmCard = page.locator('.chat-action-card', { hasText: '确认开始持续改进' })
  await expect(confirmCard).toBeVisible()
  const startBtn = confirmCard.getByRole('button', { name: '开始', exact: true })
  const editBtn = confirmCard.getByRole('button', { name: '改一改', exact: true })
  const cancelBtn = confirmCard.getByRole('button', { name: '取消', exact: true })
  await startBtn.click()

  expect(actionCalls).toEqual([{ action_id: 'act-start-goal' }])
  await expect(startBtn).toBeDisabled()
  await expect(editBtn).toBeDisabled()
  await expect(cancelBtn).toBeDisabled()
  await expect(confirmCard.getByRole('status')).toContainText('已开始持续改进「todo-app」')
  await expect(page.getByText('已经开始了，预计 22:00 结束。随时在 项目 › 目标 里看进度或停止。')).toBeVisible()
  await expect(page.getByRole('button', { name: '查看进度', exact: true })).toBeEnabled()
})

test('改一改 disables confirmation card and 打开项目 stays enabled after 让它开始干活', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  const { actionCalls } = await setupMockServer(page)
  await page.goto('/')
  await page.getByRole('button', { name: /对话/ }).first().click()

  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await composer.fill('帮我创建一个 repo 叫 todo-app')
  await composer.press('Enter')

  const createCard = page.locator('.chat-action-card', { hasText: '项目已创建' })
  await expect(createCard).toBeVisible()
  const openProjectBtn = createCard.getByRole('button', { name: '打开项目', exact: true })
  const prefillBtn = createCard.getByRole('button', { name: '让它开始干活', exact: true })
  await prefillBtn.click()

  await expect(prefillBtn).toBeDisabled()
  await expect(openProjectBtn).toBeEnabled()
  await expect(composer).toHaveValue('在 todo-app 上持续改进 2 小时：')

  await composer.fill('那你来迭代十个小时吧，把它做成一个好用的待办应用')
  await composer.press('Enter')

  const confirmCard = page.locator('.chat-action-card', { hasText: '确认开始持续改进' })
  await expect(confirmCard).toBeVisible()
  const startBtn = confirmCard.getByRole('button', { name: '开始', exact: true })
  const editBtn = confirmCard.getByRole('button', { name: '改一改', exact: true })
  const cancelBtn = confirmCard.getByRole('button', { name: '取消', exact: true })
  await editBtn.click()

  expect(actionCalls).toEqual([{ action_id: 'act-prefill-todo' }, { action_id: 'act-edit-goal' }])
  await expect(page.getByRole('heading', { name: 'todo-app / sup/wt-1' })).toBeVisible()

  await page.getByRole('button', { name: /对话/ }).first().click()
  await expect(startBtn).toBeDisabled()
  await expect(editBtn).toBeDisabled()
  await expect(cancelBtn).toBeDisabled()
})
