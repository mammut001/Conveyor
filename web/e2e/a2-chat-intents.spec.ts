import { expect, test, type Page, type Route } from '@playwright/test'
import type { SupervisorCapabilities, SupervisorRepo } from '../src/components/SupervisorWorkspace'

const TOKEN = 'a2-chat-intents-e2e-token'
const SESSION_ID = 'web:web-console:default'
const GOAL_DEADLINE_ISO = '2026-11-15T18:30:00+00:00'

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

async function setupMockServer(page: Page, options: { noChecksConfirmCard?: boolean; createdArchived?: boolean } = {}) {
  const actionCalls: Array<{ action_id?: string }> = []
  const unarchiveCalls: string[] = []
  let repos: SupervisorRepo[] = options.noChecksConfirmCard
    ? [
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
    : []
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
  const makeConfirmCard = (seq: number) => {
    const baseButtons = [
      {
        id: `act-start-goal${seq === 0 ? '' : `-${seq}`}`,
        label: '开始',
        action: 'confirm_start_timed_goal',
        style: 'primary',
        payload: {},
      },
      ...(options.noChecksConfirmCard
        ? [
            {
              id: `act-manual-goal${seq === 0 ? '' : `-${seq}`}`,
              label: '每轮等我确认',
              action: 'confirm_start_timed_goal_manual',
              style: 'secondary',
              payload: {},
            },
          ]
        : []),
      {
        id: `act-edit-goal${seq === 0 ? '' : `-${seq}`}`,
        label: '改一改',
        action: 'edit_timed_goal',
        style: 'secondary',
        payload: { view: 'code', tab: 'goals', repo_id: 'repo-todo-app', worktree_id: 'wt-todo-1', goal_outcome: '把首页做好', duration_minutes: 600 },
      },
      {
        id: `act-cancel-goal${seq === 0 ? '' : `-${seq}`}`,
        label: '取消',
        action: 'cancel_action',
        style: 'secondary',
        payload: {},
      },
    ]
    return {
      id: `card-confirm-goal${seq === 0 ? '' : `-${seq}`}`,
      title: '确认开始持续改进',
      text: options.noChecksConfirmCard
        ? '准备在 todo-app 上持续改进 10 小时：把首页做好。由执行者池自动安排。时间到或者模型额度用完会自动停。这个项目还没有自动检查：会让执行者先写一个 scripts/check.sh 跑真正的测试，之后每一轮都用它来验证。'
        : '准备在 todo-app 上持续改进 10 小时：把首页做好。由执行者池自动安排。时间到或者模型额度用完会自动停。',
      used: false,
      outcome: '',
      buttons: baseButtons,
    }
  }
  let confirmSeq = 0
  const confirmCards = new Map<string, ReturnType<typeof makeConfirmCard>>()

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
            archived_at: options.createdArchived ? '2026-11-18T12:00:00+00:00' : null,
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
      const confirmCard = makeConfirmCard(confirmSeq++)
      confirmCards.set(confirmCard.id, confirmCard)
      const sse = [
        `event: session\ndata: ${JSON.stringify({ session_id: SESSION_ID })}\n\n`,
        `event: message\ndata: ${JSON.stringify({ text: confirmCard.text, kind: 'action_card', action_card: confirmCard })}\n\n`,
        'event: done\ndata: {"outcome":"answered"}\n\n',
      ].join('')
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
    }
    if (path === '/api/chat/actions' && method === 'POST') {
      const body = req.postDataJSON() as { action_id?: string }
      actionCalls.push(body)
      const actionId = String(body.action_id || '')
      if (actionId === 'act-prefill-todo') {
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
      const matchedConfirm = Array.from(confirmCards.values()).find(c =>
        c.buttons.some(b => b.id === actionId),
      )
      if (matchedConfirm) {
        if (matchedConfirm.used) {
          return json({ error: '这个操作已经处理过了，不能重复使用' }, 409)
        }
        const clicked = matchedConfirm.buttons.find(b => b.id === actionId)!
        if (clicked.action === 'edit_timed_goal') {
          const updated = { ...matchedConfirm, used: true, outcome: '已转到「项目 › 目标」修改' }
          confirmCards.set(matchedConfirm.id, updated)
          return json({
            ok: true,
            card_id: updated.id,
            outcome: updated.outcome,
            card: updated,
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
        if (clicked.action === 'cancel_action') {
          const updated = { ...matchedConfirm, used: true, outcome: '已取消这次定时任务' }
          confirmCards.set(matchedConfirm.id, updated)
          return json({
            ok: true,
            card_id: updated.id,
            outcome: updated.outcome,
            card: updated,
          })
        }
        if (clicked.action === 'confirm_start_timed_goal' || clicked.action === 'confirm_start_timed_goal_manual') {
          const updated = { ...matchedConfirm, used: true, outcome: '已开始持续改进「todo-app」（10 小时）' }
          confirmCards.set(matchedConfirm.id, updated)
          return json({
            ok: true,
            card_id: updated.id,
            outcome: updated.outcome,
            card: updated,
            follow_up_message: {
              role: 'assistant',
              text: '已经开始了，大约 10 小时后结束。随时在 项目 › 目标 里看进度或停止。',
              kind: 'action_card',
              created_at: new Date().toISOString(),
              action_card: {
                id: `card-goal-started-${updated.id}`,
                title: '已开始持续改进',
                text: '已经开始了，大约 10 小时后结束。随时在 项目 › 目标 里看进度或停止。',
                deadline_at: GOAL_DEADLINE_ISO,
                used: false,
                outcome: '',
                buttons: [
                  {
                    id: `act-view-progress-${updated.id}`,
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
      }
      return json({ error: 'unknown action' }, 404)
    }

    if (path === '/api/supervisor/capabilities' && method === 'GET') return json(createMockCapabilities())
    if (path === '/api/supervisor/repositories' && method === 'GET') return json({ repositories: repos })
    const unarchiveMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/unarchive$/)
    if (unarchiveMatch && method === 'POST') {
      const repoId = decodeURIComponent(unarchiveMatch[1])
      unarchiveCalls.push(repoId)
      repos = repos.map(r => (r.id === repoId ? { ...r, archived_at: null } : r))
      return json(repos.find(r => r.id === repoId) || {})
    }
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

  return { actionCalls, unarchiveCalls }
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
  await expect(page.getByText('已经开始了，大约 10 小时后结束。随时在 项目 › 目标 里看进度或停止。')).toBeVisible()
  const expectedLocalTime = new Date(GOAL_DEADLINE_ISO).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  const startedCard = page.getByRole('region', { name: '已开始持续改进', exact: true })
  await expect(startedCard).toContainText(`预计 ${expectedLocalTime} 结束`)
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

test('no-checks repo timed-goal card renders 每轮等我确认 and each button posts its action', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  const { actionCalls } = await setupMockServer(page, { noChecksConfirmCard: true })
  await page.goto('/')
  await page.getByRole('button', { name: /对话/ }).first().click()

  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await expect(composer).toBeVisible()

  // 1. First card: click 开始
  await composer.fill('你来迭代十个小时，把首页做好')
  await composer.press('Enter')

  const cards = page.locator('.chat-action-card', { hasText: '确认开始持续改进' })
  const firstCard = cards.nth(0)
  await expect(firstCard).toBeVisible()
  const firstMessage = page.locator('.transcript-message.role-assistant', { has: cards }).nth(0)
  await expect(firstMessage).toContainText('这个项目还没有自动检查：会让执行者先写一个 scripts/check.sh 跑真正的测试，之后每一轮都用它来验证。')
  await expect(firstCard.getByRole('button', { name: '开始', exact: true })).toBeVisible()
  await expect(firstCard.getByRole('button', { name: '每轮等我确认', exact: true })).toBeVisible()
  await expect(firstCard.getByRole('button', { name: '改一改', exact: true })).toBeVisible()
  await expect(firstCard.getByRole('button', { name: '取消', exact: true })).toBeVisible()

  await firstCard.getByRole('button', { name: '开始', exact: true }).click()
  await expect(firstCard.getByRole('button', { name: '每轮等我确认', exact: true })).toBeDisabled()

  // 2. Second card: click 每轮等我确认
  await composer.fill('你来迭代十个小时，把首页做好')
  await composer.press('Enter')
  const secondCard = cards.nth(1)
  await expect(secondCard).toBeVisible()
  await secondCard.getByRole('button', { name: '每轮等我确认', exact: true }).click()
  await expect(secondCard.getByRole('button', { name: '开始', exact: true })).toBeDisabled()

  // 3. Third card: click 取消
  await composer.fill('你来迭代十个小时，把首页做好')
  await composer.press('Enter')
  const thirdCard = cards.nth(2)
  await expect(thirdCard).toBeVisible()
  await thirdCard.getByRole('button', { name: '取消', exact: true }).click()
  await expect(thirdCard.getByRole('status')).toContainText('已取消这次定时任务')

  // 4. Fourth card: click 改一改
  await composer.fill('你来迭代十个小时，把首页做好')
  await composer.press('Enter')
  const fourthCard = cards.nth(3)
  await expect(fourthCard).toBeVisible()
  await fourthCard.getByRole('button', { name: '改一改', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'todo-app / sup/wt-1' })).toBeVisible()

  await expect.poll(() => actionCalls).toEqual([
    { action_id: 'act-start-goal' },
    { action_id: 'act-manual-goal-1' },
    { action_id: 'act-cancel-goal-2' },
    { action_id: 'act-edit-goal-3' },
  ])
})

test('R23: 打开项目 on a card for a project archived since shows an archived note with 恢复项目', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  const { unarchiveCalls } = await setupMockServer(page, { createdArchived: true })
  await page.goto('/')
  await page.getByRole('button', { name: /对话/ }).first().click()
  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await composer.fill('帮我创建一个 repo 叫 todo-app')
  await composer.press('Enter')
  await page.getByRole('button', { name: '打开项目', exact: true }).click()

  await expect(page.getByRole('heading', { name: 'todo-app / sup/wt-1' })).toBeVisible()
  const note = page.locator('.sup-context-archived')
  await expect(note).toBeVisible()
  await expect(note).toHaveAttribute('role', 'status')
  await expect(note).toContainText('这个项目已归档，不在项目列表里。要继续使用，请先恢复。')

  await note.getByRole('button', { name: '恢复项目', exact: true }).click()
  await expect.poll(() => unarchiveCalls).toEqual(['repo-todo-app'])
  await expect(page.locator('.sup-context-archived')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'todo-app / sup/wt-1' })).toBeVisible()
})

test('R23: an active project shows no archived note', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  await setupMockServer(page)
  await page.goto('/')
  await page.getByRole('button', { name: /对话/ }).first().click()
  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await composer.fill('帮我创建一个 repo 叫 todo-app')
  await composer.press('Enter')
  await page.getByRole('button', { name: '打开项目', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'todo-app / sup/wt-1' })).toBeVisible()
  await expect(page.locator('.sup-context-archived')).toHaveCount(0)
})
