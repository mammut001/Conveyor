import { expect, test, type Page, type Route } from '@playwright/test'

// NOTE: 项目 nav badge for goals (目标) waiting for the owner's reply. Every API is mocked;
// these tests check the frontend wiring only: the 15 s poll of GET /api/supervisor/goals,
// the badge + aria-label on the desktop and phone nav, the 「(N) 」 title prefix, no badge
// at 0, no poll with the supervisor feature off or a hidden tab, and no phone overflow.

const TOKEN = 'ux-goal-badge-mock-fixture-token-0001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

type MockGoal = { id: string; state: string; title: string; worktree_id: string; repo_id: string; question: string; updated_at: string }

function goal(id: string, state: string): MockGoal {
  return {
    id,
    state,
    title: `目标 ${id}`,
    worktree_id: 'wt-fix-console',
    repo_id: 'repo-conveyor',
    question: state === 'waiting_user' ? '请检查结果后告诉我是否达成' : '',
    updated_at: '2026-11-10T09:20:00+00:00',
  }
}

function waitingGoals(n: number, others = 1): MockGoal[] {
  return [
    ...Array.from({ length: n }, (_, i) => goal(`goal-waiting-${i + 1}`, 'waiting_user')),
    ...Array.from({ length: others }, (_, i) => goal(`goal-running-${i + 1}`, i % 2 ? 'achieved' : 'running')),
  ]
}

async function installBadgeFixture(page: Page, options: { goals?: MockGoal[]; supervisor?: boolean } = {}) {
  const state = { goals: options.goals || [], goalRequests: 0 }
  const supervisor = options.supervisor ?? true

  await page.addInitScript(token => {
    sessionStorage.setItem('conveyor-token', token)
  }, TOKEN)

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

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
        features: { agents: false, long_term_memory: true, routines: true, teammate: true, agent_supervisor: supervisor },
      })
    }
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [] })
    if (path === '/api/inbox') return json({ unread: 0, items: [] })
    if (path === '/api/chat/history' && method === 'GET') {
      return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    }

    if (path === '/api/supervisor/goals' && method === 'GET') {
      state.goalRequests += 1
      return json({ goals: state.goals, count: state.goals.length })
    }
    // The 项目 page itself: an empty but valid workspace.
    if (path === '/api/supervisor/capabilities') {
      return json({
        enabled: true,
        coordinator: { executor: 'chat', model: null },
        roles: {
          analyst: { executor: 'claude', model: null, available: true },
          implementer: { executor: 'claude', model: null, available: true },
          reviewer: { executor: 'claude', model: null, available: true },
        },
        executors: [{ name: 'claude', label: 'Claude Code', available: true }],
        deployment: { configured: false, can_start: false, reason: '尚未配置生产部署运行器' },
        source_machine: 'dev-workstation',
      })
    }
    if (path === '/api/supervisor/repositories') return json({ repositories: [] })
    if (path === '/api/supervisor/tasks') return json({ tasks: [], count: 0 })
    if (path === '/api/supervisor/deployments') return json({ deployments: [], count: 0 })
    if (path.startsWith('/api/supervisor/')) return json({})

    return json({})
  })

  return state
}

async function setTabVisibility(page: Page, visibility: 'visible' | 'hidden') {
  await page.evaluate(value => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => value })
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => value === 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  }, visibility)
}

test('desktop: 项目 shows how many goals wait, updates on the next poll and prefixes the title', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install()
  const fixture = await installBadgeFixture(page, { goals: waitingGoals(2) })
  await page.goto('/#tasks')

  const nav = page.getByRole('navigation', { name: '主导航' })
  const projectButton = nav.getByRole('button', { name: /^项目/ })
  await expect(projectButton.getByRole('img', { name: '有 2 个目标在等你回复' })).toBeVisible()
  await expect(projectButton.getByRole('img', { name: '有 2 个目标在等你回复' })).toHaveText('2')
  await expect(page).toHaveTitle('(2) 任务 · Conveyor 控制台')

  // A third goal starts waiting: nothing changes before the next 15 s poll, then the badge and title follow.
  fixture.goals = waitingGoals(3)
  const before = fixture.goalRequests
  await page.clock.fastForward(5_000)
  await expect(projectButton.getByRole('img', { name: '有 2 个目标在等你回复' })).toBeVisible()
  await page.clock.fastForward(10_500)
  await expect(projectButton.getByRole('img', { name: '有 3 个目标在等你回复' })).toBeVisible()
  await expect(page).toHaveTitle('(3) 任务 · Conveyor 控制台')
  expect(fixture.goalRequests).toBeGreaterThan(before)

  // Clicking 项目 opens the 项目 page as before; the prefix stays.
  await projectButton.click()
  await expect(projectButton).toHaveAttribute('aria-current', 'page')
  await expect(page).toHaveURL(/#code$/)
  await expect(page).toHaveTitle('(3) 项目 · Conveyor 控制台')
})

test('no badge and no title prefix when nothing waits; the badge disappears when the count drops to 0', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install()
  const fixture = await installBadgeFixture(page, { goals: waitingGoals(0, 2) })
  await page.goto('/#tasks')

  const nav = page.getByRole('navigation', { name: '主导航' })
  const projectButton = nav.getByRole('button', { name: '项目', exact: true })
  await expect(projectButton).toBeVisible()
  await expect.poll(() => fixture.goalRequests).toBeGreaterThan(0)
  await expect(projectButton.locator('.shell-nav-badge')).toHaveCount(0)
  await expect(page.getByRole('img', { name: /个目标在等你回复/ })).toHaveCount(0)
  await expect(page).toHaveTitle('任务 · Conveyor 控制台')

  fixture.goals = waitingGoals(1)
  await page.clock.fastForward(15_500)
  await expect(nav.getByRole('img', { name: '有 1 个目标在等你回复' })).toBeVisible()
  await expect(page).toHaveTitle('(1) 任务 · Conveyor 控制台')

  fixture.goals = waitingGoals(0, 3)
  await page.clock.fastForward(15_500)
  await expect(page.getByRole('img', { name: /个目标在等你回复/ })).toHaveCount(0)
  await expect(page).toHaveTitle('任务 · Conveyor 控制台')
})

test('supervisor feature off: no 项目 nav and no goals poll', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install()
  const fixture = await installBadgeFixture(page, { goals: waitingGoals(2), supervisor: false })
  await page.goto('/#tasks')

  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('button', { name: '任务' })).toBeVisible()
  await expect(nav.getByRole('button', { name: /^项目/ })).toHaveCount(0)
  await page.clock.fastForward(31_000)
  expect(fixture.goalRequests).toBe(0)
  await expect(page).toHaveTitle('任务 · Conveyor 控制台')
})

test('a hidden tab stops the poll; showing it again reads at once', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install()
  const fixture = await installBadgeFixture(page, { goals: waitingGoals(1) })
  await page.goto('/#tasks')

  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('img', { name: '有 1 个目标在等你回复' })).toBeVisible()

  await setTabVisibility(page, 'hidden')
  const whileHidden = fixture.goalRequests
  fixture.goals = waitingGoals(4)
  await page.clock.fastForward(46_000)
  expect(fixture.goalRequests).toBe(whileHidden)

  await setTabVisibility(page, 'visible')
  await expect(nav.getByRole('img', { name: '有 4 个目标在等你回复' })).toBeVisible()
  await expect(page).toHaveTitle('(4) 任务 · Conveyor 控制台')
})

test('phone: the bottom 项目 tab carries the badge without horizontal overflow', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await page.clock.install()
  await installBadgeFixture(page, { goals: waitingGoals(12) })
  await page.goto('/#tasks')

  const bottomNav = page.getByRole('navigation', { name: '底部导航' })
  const projectTab = bottomNav.getByRole('button', { name: /^项目/ })
  const badge = projectTab.getByRole('img', { name: '有 12 个目标在等你回复' })
  await expect(badge).toBeVisible()
  await expect(badge).toHaveText('12')
  await expect(page).toHaveTitle('(12) 任务 · Conveyor 控制台')

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
  const box = await badge.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(PHONE.width)

  await projectTab.click()
  await expect(projectTab).toHaveAttribute('aria-current', 'page')
  await expect(page).toHaveTitle('(12) 项目 · Conveyor 控制台')
  const overflowOnProject = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflowOnProject).toBeLessThanOrEqual(0)
})
