import { expect, test, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorRepo,
} from '../src/components/SupervisorWorkspace'
import type { Goal, GoalDetail } from '../src/components/SupervisorGoals'
import type { SupervisorChecksCandidate } from '../src/components/SupervisorChecks'

const TOKEN = 'ux-supervisor-goal-extend-mock-token-0001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

const CHECKS_CANDIDATES: SupervisorChecksCandidate[] = [
  {
    id: 'npm-test',
    label: '运行测试（npm test）',
    argv: ['npm', 'test', '--silent'],
    reason: 'package.json 的 scripts.test',
    available: true,
    unavailable_reason: '',
  },
]

function createMockCapabilities(): SupervisorCapabilities {
  return {
    enabled: true,
    coordinator: { executor: 'stepfun', model: 'step-2-16k' },
    roles: {
      analyst: { executor: 'claude', model: 'claude-3-7-sonnet', available: true },
      implementer: { executor: 'antigravity', model: null, available: true },
      reviewer: { executor: 'codex', model: 'o3-mini', available: true },
    },
    executors: [
      { name: 'claude', label: 'Claude Code', available: true },
      { name: 'antigravity', label: 'Antigravity', available: true },
      { name: 'codex', label: 'Codex CLI', available: true },
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
          chat_session_id: 'web:web-console:wt-fix-console',
        },
      ],
    },
  ]
}

const EXHAUSTED_GOAL: GoalDetail = {
  id: 'goal-mock-exhausted-1',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '把构建时间压到 30 秒以内',
  outcome: '把构建时间压到 30 秒以内，并不让任何测试变慢',
  max_iterations: 3,
  role_overrides: {},
  state: 'exhausted',
  created_at: '2026-11-10T08:00:00+00:00',
  updated_at: '2026-11-10T10:30:00+00:00',
  question: '',
  summary: '第 3 轮已结束，构建时间还有 41 秒。',
  stop_reason: '迭代预算已用完（3 轮），目标尚未达成',
  last_next_prompt: '继续开启增量编译缓存并复测耗时',
  iterations_used: 3,
  iterations: [
    {
      n: 3,
      task_id: 'goal-task-3',
      prompt: '继续压缩构建时间',
      decision: 'continue',
      decision_reason: '还有一点空间',
      started_at: '2026-11-10T10:00:00+00:00',
      finished_at: '2026-11-10T10:29:00+00:00',
      task_final_state: 'completed',
      task_state: 'completed',
    },
    {
      n: 2,
      task_id: 'goal-task-2',
      prompt: '拆分大型模块打包',
      decision: 'continue',
      decision_reason: '构建从 55 秒降到 45 秒',
      started_at: '2026-11-10T09:00:00+00:00',
      finished_at: '2026-11-10T09:29:00+00:00',
      task_final_state: 'completed',
      task_state: 'completed',
    },
    {
      n: 1,
      task_id: 'goal-task-1',
      prompt: '把构建时间压到 30 秒以内，并不让任何测试变慢',
      decision: 'continue',
      decision_reason: '初步分析完成',
      started_at: '2026-11-10T08:00:00+00:00',
      finished_at: '2026-11-10T08:29:00+00:00',
      task_final_state: 'completed',
      task_state: 'completed',
    },
  ],
}

const FAILED_GOAL: GoalDetail = {
  id: 'goal-mock-failed-1',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '首页加载时间降到 1 秒以内',
  outcome: '首页加载时间降到 1 秒以内，并且所有测试通过',
  max_iterations: 5,
  role_overrides: {},
  state: 'failed',
  created_at: '2026-11-12T09:00:00+00:00',
  updated_at: '2026-11-12T09:20:00+00:00',
  question: '',
  summary: '第 1 轮没有跑成功。',
  stop_reason: '这一轮的任务失败了，目标已经停止',
  last_next_prompt: '先修复首屏懒加载路由报错',
  iterations_used: 1,
  iterations: [
    {
      n: 1,
      task_id: 'goal-task-1',
      prompt: '把首页加载时间降到 1 秒以内，并保证所有测试通过',
      decision: 'give_up',
      decision_reason: '任务失败，无法继续',
      started_at: '2026-11-12T09:00:30+00:00',
      finished_at: '2026-11-12T09:19:00+00:00',
      task_final_state: 'failed',
      task_state: 'failed',
    },
  ],
}

const ACHIEVED_GOAL: GoalDetail = {
  id: 'goal-mock-achieved-1',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '修复登录页样式对齐',
  outcome: '修复登录页样式对齐并通过快照测试',
  max_iterations: 3,
  role_overrides: {},
  state: 'achieved',
  created_at: '2026-11-13T09:00:00+00:00',
  updated_at: '2026-11-13T09:30:00+00:00',
  question: '',
  summary: '第 1 轮已达成目标。',
  stop_reason: '评估器判断目标已达成',
  last_next_prompt: '',
  iterations_used: 1,
  iterations: [
    {
      n: 1,
      task_id: 'goal-task-1',
      prompt: '修复登录页样式对齐并通过快照测试',
      decision: 'achieved',
      decision_reason: '已达成',
      started_at: '2026-11-13T09:00:30+00:00',
      finished_at: '2026-11-13T09:29:00+00:00',
      task_final_state: 'completed',
      task_state: 'completed',
    },
  ],
}

const STOPPED_GOAL: GoalDetail = {
  id: 'goal-mock-stopped-1',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '清理旧日志归档脚本',
  outcome: '清理旧日志归档脚本并保留最近 7 天日志',
  max_iterations: 3,
  role_overrides: {},
  state: 'stopped',
  created_at: '2026-11-14T08:00:00+00:00',
  updated_at: '2026-11-14T08:20:00+00:00',
  question: '',
  summary: '第 1 轮已取消。',
  stop_reason: '用户手动停止了目标',
  last_next_prompt: '',
  iterations_used: 1,
  iterations: [
    {
      n: 1,
      task_id: 'goal-task-1',
      prompt: '清理旧日志归档脚本',
      decision: '',
      decision_reason: '',
      started_at: '2026-11-14T08:00:30+00:00',
      finished_at: '2026-11-14T08:19:00+00:00',
      task_final_state: 'cancelled',
      task_state: 'cancelled',
    },
  ],
}

async function installExtendMockFixture(
  page: Page,
  options: {
    initialGoalsByWt?: Record<string, GoalDetail[]>
    conflictOnExtendMessage?: string
  } = {},
) {
  const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
  const goalsByWt: Record<string, GoalDetail[]> = {}
  for (const [wtId, list] of Object.entries({ 'wt-fix-console': [], ...(options.initialGoalsByWt || {}) })) {
    goalsByWt[wtId] = (list || []).map(clone)
  }
  const goalExtendCalls: Array<{ id: string; body: { rounds?: number; message?: string } }> = []
  let conflictOnExtendMessage = options.conflictOnExtendMessage || ''

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
    if (path === '/api/chat/history' && method === 'GET') {
      return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    }

    if (path === '/api/supervisor/capabilities' && method === 'GET') {
      return json(createMockCapabilities())
    }
    if (path === '/api/supervisor/repositories' && method === 'GET') {
      return json({ repositories: createInitialRepos() })
    }
    if (path === '/api/supervisor/tasks' && method === 'GET') {
      return json({ tasks: [], count: 0 })
    }
    if (path === '/api/supervisor/deployments' && method === 'GET') {
      return json({ deployments: [], count: 0 })
    }
    const validationMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/validation$/)
    if (validationMatch && method === 'GET') {
      return json({
        repo_id: validationMatch[1],
        candidates: CHECKS_CANDIDATES,
        selected: [],
        source: 'none',
        env_commands: [],
        effective: [],
      })
    }

    if (path === '/api/supervisor/goals' && method === 'GET') {
      const all = Object.values(goalsByWt).flat()
      const list: Goal[] = all.map(g => ({ ...g, iterations_used: g.iterations.length }))
      return json({ goals: list, count: list.length })
    }

    const goalDetailMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)$/)
    if (goalDetailMatch && method === 'GET') {
      const found = Object.values(goalsByWt).flat().find(g => g.id === goalDetailMatch[1])
      if (!found) return json({ error: '目标不存在' }, 404)
      return json({ ...found, iterations_used: found.iterations.length }, 200)
    }

    const goalExtendMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)\/extend$/)
    if (goalExtendMatch && method === 'POST') {
      const id = goalExtendMatch[1]
      const body = request.postDataJSON() as { rounds?: number; message?: string }
      goalExtendCalls.push({ id, body })
      if (conflictOnExtendMessage) {
        return json({ error: conflictOnExtendMessage, code: 'worktree_busy' }, 409)
      }
      const found = Object.values(goalsByWt).flat().find(g => g.id === id)
      if (!found) return json({ error: '目标不存在', code: 'goal_not_found' }, 404)
      if (found.state !== 'exhausted' && found.state !== 'failed') {
        return json({ error: '只有已用完轮数或失败的目标才能再试几轮', code: 'goal_not_extendable' }, 409)
      }
      const rounds = Number(body.rounds || 3)
      const message = (body.message || '').trim()
      const nextN = found.iterations.length + 1
      const header = `这是目标「${found.title}」的第 ${nextN} 轮。目标：${found.outcome}\n\n`
      const base = (found.last_next_prompt || '').trim()
      let prompt = `${header}本轮要做：${base || '请基于前面的进展继续推进，完成目标。'}`
      if (message) {
        prompt = base
          ? `${header}本轮要做：${base}\n\n用户补充：${message}`
          : `${header}本轮要做：请按用户下面的补充继续。\n\n用户补充：${message}`
      }
      found.max_iterations += rounds
      found.state = 'running'
      found.stop_reason = ''
      found.question = ''
      found.updated_at = now()
      found.iterations = [
        {
          n: nextN,
          task_id: `goal-task-${nextN}`,
          prompt,
          decision: '',
          decision_reason: '',
          started_at: now(),
          finished_at: '',
          task_final_state: 'running',
          task_state: 'running',
        },
        ...found.iterations,
      ]
      return json({ ...found, iterations_used: found.iterations.length }, 200)
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return {
    goalExtendCalls,
    setConflictOnExtend(msg: string) {
      conflictOnExtendMessage = msg
    },
  }
}

test('exhausted goal result card offers 再试几轮 (default 3 rounds + optional 补充说明) and continues the goal', async ({
  page,
}) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installExtendMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [EXHAUSTED_GOAL] },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('已用完轮数')
  await expect(card).toContainText('第 3 / 3 轮')

  const extendBtn = card.getByRole('button', { name: '再试几轮', exact: true })
  await expect(extendBtn).toBeVisible()
  await expect(extendBtn).toHaveAttribute('aria-expanded', 'false')

  await extendBtn.click()
  await expect(extendBtn).toHaveAttribute('aria-expanded', 'true')

  const extendForm = card.getByRole('form', { name: '再试几轮' })
  await expect(extendForm).toBeVisible()

  const roundsInput = extendForm.getByRole('spinbutton', { name: '再试几轮' })
  await expect(roundsInput).toHaveValue('3')
  await expect(roundsInput).toHaveAttribute('min', '1')
  await expect(roundsInput).toHaveAttribute('max', '10')

  const messageInput = extendForm.getByRole('textbox', { name: '补充说明' })
  await expect(messageInput).toHaveValue('')
  // 补充说明可选：不填时「继续」按钮也可以点。
  const continueBtn = extendForm.getByRole('button', { name: '继续', exact: true })
  await expect(continueBtn).toBeEnabled()

  await messageInput.fill('把打包拆成两个并行子任务试试')
  await continueBtn.click()

  expect(fixture.goalExtendCalls).toHaveLength(1)
  expect(fixture.goalExtendCalls[0]).toEqual({
    id: 'goal-mock-exhausted-1',
    body: {
      rounds: 3,
      message: '把打包拆成两个并行子任务试试',
    },
  })

  await expect(card).toContainText('进行中')
  await expect(card).toContainText('第 4 / 6 轮')
  await expect(card).toContainText('继续开启增量编译缓存并复测耗时')
  await expect(card).toContainText('用户补充：把打包拆成两个并行子任务试试')
})

test('failed goal result card offers 再试几轮 and can submit with custom rounds and no 补充说明', async ({
  page,
}) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installExtendMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [FAILED_GOAL] },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('这个目标没有跑成功')
  await expect(card).toContainText('第 1 / 5 轮')

  await card.getByRole('button', { name: '再试几轮', exact: true }).click()
  const extendForm = card.getByRole('form', { name: '再试几轮' })
  await extendForm.getByRole('spinbutton', { name: '再试几轮' }).fill('2')
  await extendForm.getByRole('button', { name: '继续', exact: true }).click()

  expect(fixture.goalExtendCalls).toHaveLength(1)
  expect(fixture.goalExtendCalls[0]).toEqual({
    id: 'goal-mock-failed-1',
    body: { rounds: 2 },
  })

  await expect(card).toContainText('进行中')
  await expect(card).toContainText('第 2 / 7 轮')
  await expect(card).toContainText('先修复首屏懒加载路由报错')
})

test('achieved and stopped goal result cards do not offer 再试几轮', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installExtendMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [ACHIEVED_GOAL, STOPPED_GOAL] },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('已达成')
  await expect(card.getByRole('button', { name: '再试几轮' })).toHaveCount(0)

  const history = page.locator('.sup-goal-history')
  await history.locator('.sup-run-history-bar').click()
  await history.locator('.sup-goal-history-row').filter({ hasText: '已停止' }).click()
  await expect(card).toContainText('已经停止')
  await expect(card.getByRole('button', { name: '再试几轮' })).toHaveCount(0)
})

test('409 conflict on extend shows plain Chinese error inside the extend form', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installExtendMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [EXHAUSTED_GOAL] },
    conflictOnExtendMessage: '当前工作目录还有正在执行的任务，等它结束后再试',
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await card.getByRole('button', { name: '再试几轮', exact: true }).click()
  const extendForm = card.getByRole('form', { name: '再试几轮' })
  await extendForm.getByRole('button', { name: '继续', exact: true }).click()

  await expect(extendForm.getByRole('alert')).toContainText('没有继续成功')
  await expect(extendForm.getByRole('alert')).toContainText('当前工作目录还有正在执行的任务，等它结束后再试')
  await expect(card).toContainText('已用完轮数')
})

test('phone 390px keeps 再试几轮 form inside viewport with >=40px buttons', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installExtendMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [EXHAUSTED_GOAL] },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  const extendBtn = card.getByRole('button', { name: '再试几轮', exact: true })
  const extendBox = await extendBtn.boundingBox()
  expect(extendBox).not.toBeNull()
  expect(extendBox!.height).toBeGreaterThanOrEqual(40)

  await extendBtn.click()
  const extendForm = card.getByRole('form', { name: '再试几轮' })
  const continueBtn = extendForm.getByRole('button', { name: '继续', exact: true })
  const continueBox = await continueBtn.boundingBox()
  expect(continueBox).not.toBeNull()
  expect(continueBox!.height).toBeGreaterThanOrEqual(40)

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  expect(overflow.scroll).toBeLessThanOrEqual(390)
})
