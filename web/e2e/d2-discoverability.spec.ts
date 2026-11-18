import { expect, test, type Page, type Route } from '@playwright/test'

const TOKEN = 'd2-discoverability-e2e-token'
const SESSION_ID = 'web:web-console:default'

type HistoryMsg = {
  role: 'user' | 'assistant'
  text: string
  created_at: string
  origin_channel?: 'web' | 'telegram' | 'feishu'
}

type MockGoal = {
  id: string
  repo_id: string
  state: 'running' | 'waiting_user' | 'waiting_quota' | 'achieved' | 'stopped' | 'exhausted' | 'failed'
  title: string
  outcome?: string
}

type MockRepo = {
  id: string
  name: string
  path: string
  source_machine: string
  archived_at?: string | null
  worktrees: Array<Record<string, unknown>>
}

async function setupMock(
  page: Page,
  options: {
    goals?: MockGoal[]
    repos?: MockRepo[]
    history?: HistoryMsg[]
  } = {},
) {
  const goals = options.goals ?? []
  const repos = options.repos ?? []
  const history = options.history ?? []
  let chatPosts = 0

  await page.addInitScript(
    ({ token, sid }) => {
      localStorage.setItem('conveyor-token', token)
      localStorage.setItem('conveyor-chat-session', sid)
      localStorage.setItem('conveyor-onboarding-dismissed', 'true')
    },
    { token: TOKEN, sid: SESSION_ID },
  )

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
    if (path === '/api/supervisor/goals' && method === 'GET') {
      return json({ goals, count: goals.length })
    }
    if (path === '/api/supervisor/repositories' && method === 'GET') {
      return json({ repositories: repos })
    }
    if (path === '/api/chat/history' && method === 'GET') {
      return json({ session_id: SESSION_ID, messages: history })
    }
    if (path === '/api/chat' && method === 'POST') {
      chatPosts += 1
      const sse = [
        `event: session\ndata: ${JSON.stringify({ session_id: SESSION_ID })}\n\n`,
        `event: message\ndata: ${JSON.stringify({ text: '好的，收到。' })}\n\n`,
        'event: done\ndata: {"outcome":"answered"}\n\n',
      ].join('')
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
    }
    return json({ items: [] })
  })

  return {
    getChatPosts: () => chatPosts,
  }
}

test('empty chat renders 3 chips without running goal, click fills composer without sending, popover lists 6 examples', async ({ page }) => {
  const tracker = await setupMock(page)
  await page.setViewportSize({ width: 1280, height: 840 })
  await page.goto('/#chat')

  await expect(page.getByRole('heading', { name: '想聊点什么？' })).toBeVisible()
  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })

  const createChip = page.getByRole('button', { name: '帮我建个新项目', exact: true })
  const iterateChip = page.getByRole('button', { name: '让它连续改几个小时', exact: true })
  const progressChip = page.getByRole('button', { name: '看看进度', exact: true })
  const stopChip = page.getByRole('button', { name: '先停下来', exact: true })

  await expect(createChip).toBeVisible()
  await expect(iterateChip).toBeVisible()
  await expect(progressChip).toBeVisible()
  await expect(stopChip).toHaveCount(0)

  await createChip.click()
  await expect(composer).toHaveValue('帮我建个项目叫 ')
  await expect(composer).toBeFocused()
  expect(await composer.evaluate(el => (el as HTMLTextAreaElement).selectionStart)).toBe('帮我建个项目叫 '.length)
  expect(tracker.getChatPosts()).toBe(0)

  await iterateChip.click()
  await expect(composer).toHaveValue('迭代 3 个小时，把 ')
  expect(tracker.getChatPosts()).toBe(0)

  await progressChip.click()
  await expect(composer).toHaveValue('进度怎么样了')
  expect(tracker.getChatPosts()).toBe(0)

  await page.getByRole('button', { name: '你可以这样说', exact: true }).click()
  const popover = page.getByRole('dialog', { name: '你可以这样说' })
  await expect(popover).toBeVisible()
  await expect(popover.locator('.chat-example-item')).toHaveCount(6)
  await expect(popover).not.toContainText('合并到主分支')

  await popover.getByRole('button', { name: '先停一下', exact: true }).click()
  await expect(popover).toBeHidden()
  await expect(composer).toHaveValue('先停一下')
  expect(tracker.getChatPosts()).toBe(0)
})

test('running goal shows stop chip and uses recent project name', async ({ page }) => {
  const tracker = await setupMock(page, {
    repos: [
      {
        id: 'repo-todo',
        name: 'todo-app',
        path: '/srv/projects/todo-app',
        source_machine: 'dev-workstation',
        worktrees: [],
      },
    ],
    goals: [
      {
        id: 'goal-1',
        repo_id: 'repo-todo',
        state: 'running',
        title: '把首页做好',
      },
    ],
  })

  await page.setViewportSize({ width: 1280, height: 840 })
  await page.goto('/#chat')

  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  const stopChip = page.getByRole('button', { name: '先停下来', exact: true })
  await expect(stopChip).toBeVisible()

  await page.getByRole('button', { name: '让它连续改几个小时', exact: true }).click()
  await expect(composer).toHaveValue('迭代 3 个小时，把 todo-app 做好')

  await page.getByRole('button', { name: '看看进度', exact: true }).click()
  await expect(composer).toHaveValue('todo-app 进度怎么样了')

  await stopChip.click()
  await expect(composer).toHaveValue('先停一下')
  expect(tracker.getChatPosts()).toBe(0)
})

test('chips show above composer under 3 messages and disappear after 3+ messages', async ({ page }) => {
  await setupMock(page, {
    repos: [
      {
        id: 'repo-todo',
        name: 'todo-app',
        path: '/srv/projects/todo-app',
        source_machine: 'dev-workstation',
        worktrees: [],
      },
    ],
    goals: [
      {
        id: 'goal-finished',
        repo_id: 'repo-todo',
        state: 'achieved',
        title: '完成首页',
      },
    ],
    history: [
      { role: 'user', text: '帮我看看项目状态', created_at: '2026-11-17T10:00:00Z' },
      { role: 'assistant', text: '一切正常。', created_at: '2026-11-17T10:00:05Z' },
    ],
  })

  await page.setViewportSize({ width: 1280, height: 840 })
  await page.goto('/#chat')

  await expect(page.locator('.chat-discover-area.is-composer')).toBeVisible()
  await expect(page.getByRole('button', { name: '帮我建个新项目', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '先停下来', exact: true })).toHaveCount(0)

  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await composer.fill('再检查一遍')
  await composer.press('Enter')

  await expect(page.getByText('好的，收到。')).toBeVisible()
  await expect(page.locator('.chat-discover-area')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '帮我建个新项目', exact: true })).toHaveCount(0)
})

test('phone width (390x844) wraps chips cleanly without horizontal scroll', async ({ page }) => {
  await setupMock(page, {
    repos: [
      {
        id: 'repo-todo',
        name: 'todo-app',
        path: '/srv/projects/todo-app',
        source_machine: 'dev-workstation',
        worktrees: [],
      },
    ],
    goals: [
      {
        id: 'goal-1',
        repo_id: 'repo-todo',
        state: 'running',
        title: '持续改进',
      },
    ],
  })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#chat')

  const chips = page.locator('.chat-suggestion-chip')
  await expect(chips).toHaveCount(4)

  const boxTops = await chips.evaluateAll(nodes =>
    nodes.map(node => Math.round(node.getBoundingClientRect().top)),
  )
  expect(Math.max(...boxTops)).toBeGreaterThan(Math.min(...boxTops))

  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect(await page.locator('.chat-panel-container').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)

  await page.getByRole('button', { name: '你可以这样说', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '你可以这样说' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
