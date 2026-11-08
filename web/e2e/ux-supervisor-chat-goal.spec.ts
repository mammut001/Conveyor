import { expect, test, type Page, type Route } from '@playwright/test'
import type { SupervisorCapabilities, SupervisorRepo } from '../src/components/SupervisorWorkspace'
import type { Goal, GoalDetail } from '../src/components/SupervisorGoals'

// NOTE: 「设为目标」 on 项目 → 讨论 replies. Every /api/* call is mocked; this verifies frontend wiring
// only: the draft (owner's last request + that reply, ≤ 4000 字, no code fences) lands in 目标 →
// 描述你想要的结果, nothing starts on its own, the action is disabled while a goal is running /
// waiting on the worktree, and the main 对话 page never shows it.

const TOKEN = 'ux-supervisor-chat-goal-mock-token-0001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }
const CHAT_SESSION = 'web:web-console:wt-fix-console'

type HistoryMessage = { role: 'user' | 'assistant'; text: string; created_at?: string; origin_channel?: 'web' }

const REQUEST_1 = '首页太慢了，帮我看看怎么提速'
const REPLY_1 = [
  '可以分两步：',
  '',
  '```bash',
  'npm run build',
  '```',
  '',
  '先压缩首页图片，再把三个接口改成并行请求。',
].join('\n')
const EXPECTED_DRAFT_1 = [
  '我的要求：',
  REQUEST_1,
  '',
  '讨论结论：',
  '可以分两步：',
  '',
  'npm run build',
  '',
  '先压缩首页图片，再把三个接口改成并行请求。',
].join('\n')

const REQUEST_2 = '那登录页的报错提示也顺便改成中文'
const REPLY_2 = '好的，登录失败时改成显示「密码不对，请再试一次」。'

const CONVERSATION: HistoryMessage[] = [
  { role: 'user', text: REQUEST_1, created_at: '2026-11-08T09:00:00+00:00', origin_channel: 'web' },
  { role: 'assistant', text: REPLY_1, created_at: '2026-11-08T09:00:10+00:00', origin_channel: 'web' },
  { role: 'user', text: REQUEST_2, created_at: '2026-11-08T09:01:00+00:00', origin_channel: 'web' },
  { role: 'assistant', text: REPLY_2, created_at: '2026-11-08T09:01:10+00:00', origin_channel: 'web' },
]

function createMockCapabilities(): SupervisorCapabilities {
  return {
    enabled: true,
    coordinator: { executor: 'stepfun', model: 'step-2-16k' },
    roles: {
      analyst: { executor: 'claude', model: 'claude-3-7-sonnet', available: true },
      implementer: { executor: 'antigravity', model: null, available: true },
      reviewer: { executor: 'claude', model: null, available: true },
    },
    executors: [
      { name: 'claude', label: 'Claude Code', available: true },
      { name: 'antigravity', label: 'Antigravity', available: true },
    ],
    deployment: { configured: false, can_start: false, reason: '尚未配置生产部署运行器' },
    source_machine: 'dev-workstation',
  }
}

function createInitialRepos(): SupervisorRepo[] {
  return [
    {
      id: 'repo-conveyor',
      name: 'Conveyor',
      path: '/srv/repos/conveyor',
      source_machine: 'dev-workstation',
      validation_source: 'none',
      worktrees: [
        {
          id: 'wt-main',
          path: '/srv/repos/conveyor',
          branch: 'main',
          head: '8352f46a9c01',
          is_main: true,
          managed: false,
          locked: false,
        },
        {
          id: 'wt-fix-console',
          path: '/srv/repos/conveyor-wt/fix-console',
          branch: 'fix/console',
          head: 'a1b2c3d4e5f6',
          is_main: false,
          managed: true,
          locked: false,
          chat_session_id: CHAT_SESSION,
        },
      ],
    },
  ]
}

function goalFixture(state: GoalDetail['state'], overrides: Partial<GoalDetail> = {}): GoalDetail {
  return {
    id: `goal-mock-${state}`,
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
    title: '首页加载时间降到 1 秒以内',
    outcome: '首页加载时间降到 1 秒以内，并且所有测试通过',
    max_iterations: 5,
    role_overrides: {},
    state,
    created_at: '2026-11-08T08:00:00+00:00',
    updated_at: '2026-11-08T08:30:00+00:00',
    question: state === 'waiting_user' ? '这一轮的改动已经做完，请检查结果后告诉我是否达成或还要改什么' : '',
    summary: '第 1 轮已跑完。',
    stop_reason: state === 'achieved' ? '评估器判断目标已达成' : '',
    last_next_prompt: '',
    iterations_used: 1,
    iterations: [
      {
        n: 1,
        task_id: 'goal-task-1',
        prompt: '把首页加载时间降到 1 秒以内',
        decision: state === 'achieved' ? 'achieved' : state === 'waiting_user' ? 'ask_user' : '',
        decision_reason: '',
        started_at: '2026-11-08T08:00:30+00:00',
        finished_at: state === 'running' ? '' : '2026-11-08T08:29:00+00:00',
        task_final_state: state === 'running' ? 'running' : 'completed',
        task_state: state === 'running' ? 'running' : 'completed',
      },
    ],
    ...overrides,
  }
}

async function installFixture(
  page: Page,
  options: { history?: HistoryMessage[]; goals?: GoalDetail[]; mainChatSession?: string } = {},
) {
  const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
  const goals: GoalDetail[] = (options.goals || []).map(clone)
  const history = options.history ?? CONVERSATION
  const goalCreateCalls: Array<Record<string, unknown>> = []
  const chatPosts: string[] = []

  await page.addInitScript(
    ({ token, mainSession }) => {
      sessionStorage.setItem('conveyor-token', token)
      if (mainSession) localStorage.setItem('conveyor-chat-session', mainSession)
    },
    { token: TOKEN, mainSession: options.mainChatSession || '' },
  )

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    const now = () => new Date().toISOString()

    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 3600,
        load_average: [0.2, 0.2, 0.2],
        cpu_count: 4,
        memory: { total: 8_000_000_000, available: 4_000_000_000 },
        disk: { total: 100_000_000_000, used: 40_000_000_000, free: 60_000_000_000 },
        queue: { depth: 0, paused: false, states: { running: 0, completed: 3 } },
        channels: { telegram: { configured: true }, feishu: { configured: false } },
        nodes: [],
        features: { agents: true, long_term_memory: true, routines: true, teammate: true, agent_supervisor: true },
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
      return json({ session_id: url.searchParams.get('session_id'), messages: history })
    }
    if (path === '/api/chat' && method === 'POST') {
      const body = request.postDataJSON() as { message: string }
      chatPosts.push(body.message)
      const sse = [
        `event: session\ndata: ${JSON.stringify({ session_id: CHAT_SESSION })}\n\n`,
        `event: message\ndata: ${JSON.stringify({ text: `已收到关于「${body.message}」的讨论。` })}\n\n`,
        'event: done\ndata: {}\n\n',
      ].join('')
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
    }

    if (path === '/api/supervisor/capabilities' && method === 'GET') return json(createMockCapabilities())
    if (path === '/api/supervisor/repositories' && method === 'GET') {
      return json({ repositories: createInitialRepos() })
    }
    if (path === '/api/supervisor/tasks' && method === 'GET') return json({ tasks: [], count: 0 })
    if (path === '/api/supervisor/deployments' && method === 'GET') return json({ deployments: [], count: 0 })

    if (path === '/api/supervisor/goals' && method === 'GET') {
      const list: Goal[] = goals.map(g => ({ ...g, iterations_used: g.iterations.length }))
      return json({ goals: list, count: list.length })
    }
    if (path === '/api/supervisor/goals' && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      goalCreateCalls.push(body)
      const outcome = String(body.outcome || '')
      const created: GoalDetail = {
        ...goalFixture('running'),
        id: `goal-mock-created-${goalCreateCalls.length}`,
        title: outcome.length > 40 ? `${outcome.slice(0, 40)}…` : outcome,
        outcome,
        max_iterations: Number(body.max_iterations || 5),
        created_at: now(),
        updated_at: now(),
      }
      goals.unshift(created)
      return json(created, 201)
    }
    const goalDetailMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)$/)
    if (goalDetailMatch && method === 'GET') {
      const found = goals.find(g => g.id === goalDetailMatch[1])
      return found ? json({ ...found, iterations_used: found.iterations.length }) : json({ error: '目标不存在' }, 404)
    }

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

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return { goalCreateCalls, chatPosts }
}

function discussion(page: Page) {
  return page.locator('.sup-chat-pane')
}

function replyArticle(page: Page, text: string) {
  return discussion(page).locator('article.role-assistant').filter({ hasText: text })
}

async function openProject(page: Page) {
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()
}

test('mock fixture: 设为目标 fills 目标 with the request + reply (no code fences) and never starts on its own', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installFixture(page)
  await openProject(page)

  const reply = replyArticle(page, '先压缩首页图片')
  await expect(reply).toBeVisible()
  // Every finished reply in the discussion carries the action.
  await expect(discussion(page).getByRole('button', { name: '设为目标' })).toHaveCount(2)

  await reply.getByRole('button', { name: '设为目标' }).click()

  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')
  const form = page.getByRole('region', { name: '描述你想要的结果' })
  const textarea = form.getByRole('textbox', { name: '描述你想要的结果' })
  await expect(textarea).toHaveValue(EXPECTED_DRAFT_1)
  await expect(textarea).not.toHaveValue(/```/)
  await expect(textarea).toBeFocused()

  // Nothing starts by itself: no goal POST, and 开始 waits for the owner.
  await page.waitForTimeout(600)
  expect(fixture.goalCreateCalls).toHaveLength(0)
  await expect(page.getByRole('region', { name: '目标进度' })).toHaveCount(0)
  const start = form.getByRole('button', { name: '开始', exact: true })
  await expect(start).toBeEnabled()

  // The owner edits the draft, then clicks 开始 themselves.
  await textarea.fill(`${EXPECTED_DRAFT_1}\n\n另外：所有测试要通过。`)
  await start.click()
  await expect(page.getByRole('region', { name: '目标进度' })).toBeVisible()
  expect(fixture.goalCreateCalls).toHaveLength(1)
  expect(fixture.goalCreateCalls[0]).toMatchObject({
    outcome: `${EXPECTED_DRAFT_1}\n\n另外：所有测试要通过。`,
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
  })
})

test('mock fixture: the draft pairs each reply with the owner request right before it', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page)
  await openProject(page)

  await replyArticle(page, '密码不对').getByRole('button', { name: '设为目标' }).click()
  const textarea = page.getByRole('textbox', { name: '描述你想要的结果' })
  await expect(textarea).toHaveValue(`我的要求：\n${REQUEST_2}\n\n讨论结论：\n${REPLY_2}`)
  await expect(textarea).not.toHaveValue(new RegExp(REQUEST_1))
})

test('mock fixture: a long reply is trimmed to 4000 字 and code fences are removed', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const longBody = '把首页的图片换成更小的格式，并且把接口改成并行请求。'.repeat(300)
  await installFixture(page, {
    history: [
      { role: 'user', text: '首页加载太慢', created_at: '2026-11-08T09:00:00+00:00' },
      {
        role: 'assistant',
        text: `方案如下：\n\n~~~ts\nconst fast = true\n~~~\n\n\`\`\`\n${longBody}\n\`\`\`\n\n结尾说明`,
        created_at: '2026-11-08T09:00:10+00:00',
      },
    ],
  })
  await openProject(page)

  await replyArticle(page, '方案如下').getByRole('button', { name: '设为目标' }).click()
  const textarea = page.getByRole('textbox', { name: '描述你想要的结果' })
  await expect(textarea).toHaveValue(/^我的要求：\n首页加载太慢\n\n讨论结论：\n方案如下：\n\nconst fast = true\n\n把首页/)
  const value = await textarea.inputValue()
  expect(Array.from(value).length).toBeLessThanOrEqual(4000)
  expect(Array.from(value).length).toBeGreaterThan(3900)
  expect(value).not.toContain('```')
  expect(value).not.toContain('~~~')
  expect(value.endsWith('…')).toBe(true)
})

test('mock fixture: a finished goal card does not hide the draft (form opens like 开始新目标)', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, { goals: [goalFixture('achieved')] })
  await openProject(page)

  const button = replyArticle(page, '先压缩首页图片').getByRole('button', { name: '设为目标' })
  await expect(button).toBeEnabled()
  await button.click()

  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('textbox', { name: '描述你想要的结果' })).toHaveValue(EXPECTED_DRAFT_1)
  await page.waitForTimeout(400)
  // The finished goal stays in history and does not pop back over the form.
  await expect(page.getByRole('textbox', { name: '描述你想要的结果' })).toHaveValue(EXPECTED_DRAFT_1)
  await expect(page.getByRole('region', { name: '目标进度' })).toHaveCount(0)
})

for (const state of ['running', 'waiting_user'] as const) {
  test(`mock fixture: 设为目标 is disabled with a tooltip while a goal is ${state}`, async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    const fixture = await installFixture(page, { goals: [goalFixture(state)] })
    await openProject(page)

    // An active goal makes 目标 the default tab; go back to the discussion.
    await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')
    await page.getByRole('tab', { name: '任务与结果' }).click()

    const buttons = discussion(page).getByRole('button', { name: '设为目标' })
    await expect(buttons).toHaveCount(2)
    for (const button of await buttons.all()) {
      await expect(button).toBeDisabled()
      await expect(button).toHaveAttribute('title', '这个工作目录已经有进行中的目标')
    }
    await buttons.first().click({ force: true })
    await expect(page.getByRole('tab', { name: '任务与结果' })).toHaveAttribute('aria-selected', 'true')
    expect(fixture.goalCreateCalls).toHaveLength(0)
  })
}

test('mock fixture: phone layout — 设为目标 from the 讨论 tab lands on 目标 with the draft', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installFixture(page)
  await openProject(page)

  await page.getByRole('tab', { name: '讨论' }).click()
  const button = replyArticle(page, '先压缩首页图片').getByRole('button', { name: '设为目标' })
  await expect(button).toBeVisible()
  await button.click()

  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('textbox', { name: '描述你想要的结果' })).toHaveValue(EXPECTED_DRAFT_1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('mock fixture: the main 对话 page never shows 设为目标', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, { mainChatSession: 'web:web-console:main' })
  await page.goto('/#chat')

  const mainChat = page.locator('.stream-chat-column')
  await expect(mainChat.locator('article.role-assistant').filter({ hasText: '先压缩首页图片' })).toBeVisible()
  await expect(mainChat.getByRole('button', { name: '复制', exact: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '设为目标' })).toHaveCount(0)
})
