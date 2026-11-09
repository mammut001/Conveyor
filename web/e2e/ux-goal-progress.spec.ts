import { expect, test, type Page, type Route } from '@playwright/test'
import type { SupervisorExecution, SupervisorRepo } from '../src/components/SupervisorWorkspace'
import type { GoalDetail, GoalRoundTask } from '../src/components/SupervisorGoals'

// NOTE: 目标 tab — live progress of the CURRENT round of a running goal. Every API is mocked;
// these tests check the frontend wiring only: the step label follows the task's state / stage
// across polls, the executor that is working, the elapsed time ticking locally every second
// without extra requests, task-detail reads at most every 5 s and only while the page is
// visible, and no horizontal overflow on a 390px phone.

const TOKEN = 'ux-goal-progress-mock-fixture-token-0001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

// The fake clock starts here; the current round started 3 分 12 秒 earlier.
const T0 = new Date('2026-11-10T09:10:00+00:00')
const ROUND_STARTED_AT = new Date(T0.getTime() - 192_000).toISOString()
const TASK_ID = 'goal-task-live-2'

function createRepos(): SupervisorRepo[] {
  return [
    {
      id: 'repo-conveyor',
      name: 'Conveyor',
      path: '/srv/repos/conveyor',
      source_machine: 'dev-workstation',
      validation_source: 'repo',
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
          chat_session_id: 'web:web-console:wt-fix-console',
        },
      ],
    },
  ]
}

type Step = {
  state: string
  stage: string
  repair_round?: number
  executions?: SupervisorExecution[]
}

function running(role: string, executor: string): SupervisorExecution[] {
  return [
    {
      id: 'exec-done-1',
      role: 'analyst',
      executor: 'claude',
      status: 'succeeded',
      started_at: ROUND_STARTED_AT,
      finished_at: new Date(T0.getTime() - 120_000).toISOString(),
    },
    { id: `exec-${role}`, role, executor, status: 'running', started_at: T0.toISOString(), finished_at: '' },
  ]
}

const STEP_ANALYZING: Step = {
  state: 'analyzing',
  stage: 'analysis',
  executions: [{ id: 'exec-a', role: 'analyst', executor: 'claude', status: 'running', started_at: ROUND_STARTED_AT }],
}
const STEP_IMPLEMENTING: Step = { state: 'implementing', stage: 'implementation', executions: running('implementer', 'antigravity') }
const STEP_VALIDATING: Step = { state: 'reviewing', stage: 'validation', executions: running('validation', 'operator-validation') }
const STEP_REVIEWING: Step = { state: 'reviewing', stage: 'review', executions: running('reviewer', 'command-code') }
const STEP_REPAIRING: Step = {
  state: 'implementing',
  stage: 'implementation',
  repair_round: 1,
  executions: running('implementer', 'claude'),
}
const STEP_COMPLETED: Step = { state: 'completed', stage: 'completed', executions: [] }

async function installProgressFixture(page: Page, initial: Step) {
  const state = {
    step: initial,
    goalState: 'running' as string,
    taskRequests: 0,
    goalDetailRequests: 0,
  }

  const goalDetail = (): GoalDetail => ({
    id: 'goal-live-1',
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
    title: '首页加载时间降到 1 秒以内',
    outcome: '首页加载时间降到 1 秒以内，并且所有测试通过',
    max_iterations: 5,
    role_overrides: {},
    state: state.goalState as GoalDetail['state'],
    created_at: '2026-11-10T09:00:00+00:00',
    updated_at: '2026-11-10T09:06:48+00:00',
    question: '',
    summary: '',
    stop_reason: state.goalState === 'achieved' ? '评估器判断目标已达成' : '',
    last_next_prompt: '',
    iterations_used: 2,
    iterations: [
      {
        n: 2,
        task_id: TASK_ID,
        prompt: '补测试并复验首页加载时间',
        decision: state.goalState === 'achieved' ? 'achieved' : '',
        decision_reason: '',
        started_at: ROUND_STARTED_AT,
        finished_at: '',
        task_final_state: state.step.state,
        // Like the backend: the goal poll carries the live task state.
        task_state: state.step.state,
      },
      {
        n: 1,
        task_id: 'goal-task-live-1',
        prompt: '把首页加载时间降到 1 秒以内',
        decision: 'continue',
        decision_reason: '首屏时间有下降，但测试还没跑完',
        started_at: '2026-11-10T09:00:30+00:00',
        finished_at: '2026-11-10T09:06:40+00:00',
        task_final_state: 'completed',
        task_state: 'completed',
      },
    ],
  })

  const taskDetail = (): GoalRoundTask => ({
    id: TASK_ID,
    goal: '补测试并复验首页加载时间\n\n这是目标的第 2 轮。',
    branch: 'fix/console',
    state: state.step.state,
    stage: state.step.stage,
    started_at: ROUND_STARTED_AT,
    repair_round: state.step.repair_round ?? 0,
    max_repair_rounds: 2,
    metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
    handoffs: [],
    executions: state.step.executions || [],
    acceptance: null,
    summary: '',
  })

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
    if (path === '/api/chat/history' && method === 'GET') {
      return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    }

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
    if (path === '/api/supervisor/repositories') return json({ repositories: createRepos() })
    // The task list stays empty so the workspace never reads task details on its own:
    // every task-detail request counted below comes from the 目标 progress line.
    if (path === '/api/supervisor/tasks') return json({ tasks: [], count: 0 })
    if (path === `/api/supervisor/tasks/${TASK_ID}` && method === 'GET') {
      state.taskRequests += 1
      return json(taskDetail())
    }
    if (path === '/api/supervisor/deployments') return json({ deployments: [], count: 0 })
    if (path === '/api/supervisor/goals' && method === 'GET') {
      return json({ goals: [goalDetail()], count: 1 })
    }
    if (path === '/api/supervisor/goals/goal-live-1' && method === 'GET') {
      state.goalDetailRequests += 1
      return json(goalDetail())
    }
    if (path.startsWith('/api/supervisor/')) return json({ error: `unhandled ${method} ${path}` }, 404)
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

/** 「已经 3 分 12 秒」→ 192 */
function elapsedSeconds(text: string): number {
  const h = /(\d+) 小时/.exec(text)
  const m = /(\d+) 分/.exec(text)
  const s = /(\d+) 秒/.exec(text)
  return (h ? Number(h[1]) * 3600 : 0) + (m ? Number(m[1]) * 60 : 0) + (s ? Number(s[1]) : 0)
}

async function openGoals(page: Page) {
  await page.goto('/#code')
  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')
  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('第 2 / 5 轮')
  return card
}

test('the current round shows its step in plain Chinese and follows the task across polls', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install({ time: T0 })
  const fixture = await installProgressFixture(page, STEP_ANALYZING)
  const card = await openGoals(page)

  const live = card.locator('.sup-goal-live')
  await expect(live).toHaveCount(1)
  const step = live.locator('.sup-goal-live-step')
  const executor = live.locator('.sup-goal-live-executor')
  await expect(step).toHaveText('分析中')
  await expect(executor).toHaveText('Claude Code 在做')

  // The line sits right under the header that says 第 2 / 5 轮.
  const headerBox = await card.locator('.sup-goal-header').boundingBox()
  const liveBox = await live.boundingBox()
  expect(headerBox && liveBox).toBeTruthy()
  expect(liveBox!.y).toBeGreaterThanOrEqual(headerBox!.y + headerBox!.height - 1)

  const expectations: Array<[Step, string, string | null]> = [
    [STEP_IMPLEMENTING, '动手改', 'Antigravity 在做'],
    [STEP_VALIDATING, '运行自动检查', null],
    [STEP_REVIEWING, '检查中', 'Command Code 在做'],
    [STEP_REPAIRING, '自动修复第 1 轮', 'Claude Code 在做'],
  ]
  for (const [next, label, who] of expectations) {
    fixture.step = next
    await page.clock.fastForward(5_100)
    await expect(step).toHaveText(label)
    if (who) await expect(executor).toHaveText(who)
    else await expect(executor).toHaveCount(0)
  }

  // The task finished but the evaluator has not decided yet.
  fixture.step = STEP_COMPLETED
  await page.clock.fastForward(5_100)
  await expect(step).toHaveText('正在判断下一步…')
  await expect(executor).toHaveCount(0)

  // Only the current round gets the line; the older round never shows it.
  await expect(card.locator('.sup-goal-iteration').nth(1).locator('.sup-goal-live')).toHaveCount(0)
  // No raw backend words anywhere on the line.
  const text = await live.innerText()
  expect(text).not.toMatch(/analy|implement|review|validation|repair|completed/i)
})

test('elapsed time ticks every second without extra requests; task detail at most every 5 s', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install({ time: T0 })
  const fixture = await installProgressFixture(page, STEP_IMPLEMENTING)
  const card = await openGoals(page)

  const elapsed = card.locator('.sup-goal-live-elapsed')
  await expect(elapsed).toHaveText(/^已经 3 分 1\d 秒$/)
  await expect.poll(() => fixture.taskRequests).toBeGreaterThan(0)

  const firstSeconds = elapsedSeconds(await elapsed.innerText())
  const tasksBefore = fixture.taskRequests
  const goalsBefore = fixture.goalDetailRequests
  let previous = firstSeconds
  // Ten one-second ticks: the clock moves every time, requests only on their own schedule.
  for (let i = 0; i < 10; i += 1) {
    await page.clock.fastForward(1_000)
    await expect.poll(async () => elapsedSeconds(await elapsed.innerText())).toBeGreaterThan(previous)
    previous = elapsedSeconds(await elapsed.innerText())
  }
  expect(previous - firstSeconds).toBeGreaterThanOrEqual(10)
  expect(previous - firstSeconds).toBeLessThanOrEqual(14)
  // ~10 s passed: the task detail is read at most every 5 s (≤ 2–3 reads), never once per tick.
  expect(fixture.taskRequests - tasksBefore).toBeLessThanOrEqual(3)
  // The goal poll keeps its 3 s rhythm (≤ 4 reads in ~10 s), unaffected by the ticking.
  expect(fixture.goalDetailRequests - goalsBefore).toBeLessThanOrEqual(4)

  // A minute later the line reads 4 分多.
  await page.clock.fastForward(60_000)
  await expect(elapsed).toHaveText(/^已经 4 分 \d+ 秒$/)
})

test('a hidden page stops reading the task detail; showing it again reads at once', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install({ time: T0 })
  const fixture = await installProgressFixture(page, STEP_ANALYZING)
  const card = await openGoals(page)
  await expect(card.locator('.sup-goal-live-step')).toHaveText('分析中')
  await expect.poll(() => fixture.taskRequests).toBeGreaterThan(0)

  await setTabVisibility(page, 'hidden')
  const whileHidden = fixture.taskRequests
  fixture.step = STEP_REVIEWING
  for (let i = 0; i < 6; i += 1) {
    await page.clock.fastForward(5_100)
  }
  expect(fixture.taskRequests).toBe(whileHidden)

  await setTabVisibility(page, 'visible')
  await expect.poll(() => fixture.taskRequests).toBeGreaterThan(whileHidden)
  await page.clock.fastForward(3_100)
  await expect(card.locator('.sup-goal-live-step')).toHaveText('检查中')
  await expect(card.locator('.sup-goal-live-executor')).toHaveText('Command Code 在做')
})

test('a finished goal shows no live line and never reads the task detail', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install({ time: T0 })
  const fixture = await installProgressFixture(page, STEP_COMPLETED)
  fixture.goalState = 'achieved'
  await page.goto('/#code')
  // No running goal: 目标 is not the default sub-tab, open it by hand.
  await page.getByRole('tab', { name: '目标' }).click()
  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('已达成')
  await page.clock.fastForward(10_000)
  await expect(card.locator('.sup-goal-live')).toHaveCount(0)
  expect(fixture.taskRequests).toBe(0)
})

test('phone 390px: the live line stays inside the viewport without horizontal overflow', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await page.clock.install({ time: T0 })
  await installProgressFixture(page, STEP_REPAIRING)
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()
  const card = page.getByRole('region', { name: '目标进度' })
  const live = card.locator('.sup-goal-live')
  await expect(live.locator('.sup-goal-live-step')).toHaveText('自动修复第 1 轮')
  await expect(live.locator('.sup-goal-live-executor')).toHaveText('Claude Code 在做')
  await expect(live.locator('.sup-goal-live-elapsed')).toHaveText(/^已经 3 分 \d+ 秒$/)

  const box = await live.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(PHONE.width)
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
})
