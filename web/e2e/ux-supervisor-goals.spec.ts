import { expect, test, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorDeployment,
  SupervisorRepo,
  SupervisorTaskDetail,
} from '../src/components/SupervisorWorkspace'
import type { Goal, GoalDetail } from '../src/components/SupervisorGoals'
import type { SupervisorChecksCandidate } from '../src/components/SupervisorChecks'

// Server-detected 自动检查 candidates for the mocked Conveyor repository.
const CHECKS_CANDIDATES: SupervisorChecksCandidate[] = [
  {
    id: 'npm-test',
    label: '运行测试（npm test）',
    argv: ['npm', 'test', '--silent'],
    reason: 'package.json 的 scripts.test',
    available: true,
    unavailable_reason: '',
  },
  {
    id: 'npm-build',
    label: '构建项目（npm run build）',
    argv: ['npm', 'run', 'build'],
    reason: 'package.json 的 scripts.build',
    available: true,
    unavailable_reason: '',
  },
]

// NOTE: 目标（goal-driven loop）tab. All tests mock the /api/supervisor/* contract and verify
// frontend wiring only: default sub-tab rule, create/poll/reply/stop calls, read-only guard,
// phone layout, and Simplified-Chinese-only visible copy (no "goal" / "iteration" words).

const TOKEN = 'ux-supervisor-goals-mock-fixture-token-0001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

function createMockCapabilities(overrides: Partial<SupervisorCapabilities> = {}): SupervisorCapabilities {
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
    ...overrides,
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

const RUNNING_GOAL: GoalDetail = {
  id: 'goal-mock-running-1',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '首页加载时间降到 1 秒以内',
  outcome: '首页加载时间降到 1 秒以内，并且所有测试通过',
  max_iterations: 5,
  role_overrides: {},
  state: 'running',
  created_at: '2026-11-10T09:00:00+00:00',
  updated_at: '2026-11-10T09:01:00+00:00',
  question: '',
  summary: '第 1 轮已派发：正在实现并自测。',
  stop_reason: '',
  last_next_prompt: '',
  iterations_used: 1,
  iterations: [
    {
      n: 1,
      task_id: 'goal-task-1',
      prompt: '把首页加载时间降到 1 秒以内，并保证所有测试通过',
      decision: '',
      decision_reason: '',
      started_at: '2026-11-10T09:00:30+00:00',
      finished_at: '',
      task_final_state: 'running',
      task_state: 'running',
    },
  ],
}

const WAITING_GOAL: GoalDetail = {
  id: 'goal-mock-waiting-1',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '首页加载时间降到 1 秒以内',
  outcome: '首页加载时间降到 1 秒以内，并且所有测试通过',
  max_iterations: 5,
  role_overrides: {},
  state: 'waiting_user',
  created_at: '2026-11-10T09:00:00+00:00',
  updated_at: '2026-11-10T09:20:00+00:00',
  question: '这一轮的改动已经做完，但没有配置自动验证命令，请检查结果后告诉我是否达成或还要改什么',
  summary: '第 2 轮已跑完，等待你确认结果。',
  stop_reason: '',
  last_next_prompt: '',
  iterations_used: 2,
  iterations: [
    {
      n: 2,
      task_id: 'goal-task-2',
      prompt: '补充移动端的验收步骤，并复验加载时间',
      decision: 'ask_user',
      decision_reason: '改动完成，但没有自动验证命令，需要人工判断',
      started_at: '2026-11-10T09:10:00+00:00',
      finished_at: '2026-11-10T09:19:00+00:00',
      task_final_state: 'waiting_user',
      task_state: 'waiting_user',
    },
    {
      n: 1,
      task_id: 'goal-task-1',
      prompt: '把首页加载时间降到 1 秒以内，并保证所有测试通过',
      decision: 'continue',
      decision_reason: '首屏时间有下降，但测试还没跑完',
      started_at: '2026-11-10T09:00:30+00:00',
      finished_at: '2026-11-10T09:09:00+00:00',
      task_final_state: 'completed',
      task_state: 'completed',
    },
  ],
}

// 81 个中文单行 outcome：真实那次跑崩的标题就是这么来的。
const LONG_OUTCOME = '把首页加载时间降到一秒以内，并且保证所有测试都通过'.repeat(4).slice(0, 81)

// 已经跑完的目标：标题是老数据留下的 `[truncated]` 标记，要靠 outcome 现算。
const FINISHED_GOAL: GoalDetail = {
  id: 'goal-mock-finished-1',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '\n...[truncated]',
  outcome: LONG_OUTCOME,
  max_iterations: 3,
  role_overrides: {},
  state: 'achieved',
  created_at: '2026-11-11T09:00:00+00:00',
  updated_at: '2026-11-11T09:40:00+00:00',
  question: '',
  summary: '第 2 轮跑完，首页加载时间已经降到 0.8 秒，所有测试通过。',
  stop_reason: '评估器判断目标已达成',
  last_next_prompt: '',
  iterations_used: 2,
  iterations: [
    {
      n: 2,
      task_id: 'goal-task-2',
      prompt: '复验加载时间并补上移动端验收步骤',
      decision: 'achieved',
      decision_reason: '加载时间已达标，测试全部通过',
      started_at: '2026-11-09T09:20:00+00:00',
      finished_at: '2026-11-09T09:39:00+00:00',
      task_final_state: 'completed',
      task_state: 'completed',
    },
    {
      n: 1,
      task_id: 'goal-task-1',
      prompt: '把首页加载时间降到一秒以内，并保证所有测试通过',
      decision: 'continue',
      decision_reason: '首屏时间有下降，但测试还没跑完',
      started_at: '2026-11-09T09:00:30+00:00',
      finished_at: '2026-11-09T09:09:00+00:00',
      task_final_state: 'failed',
      task_state: 'failed',
    },
  ],
}

// 轮数用完：结果卡要说清楚「为什么停了」。
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
  last_next_prompt: '',
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
  ],
}

// 正在跑的目标：第 1 轮失败后继续，第 2 轮已经跑完、结论还没写出来。
const RUNNING_GOAL_MID_EVAL: GoalDetail = {
  id: 'goal-mock-running-mid-eval',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '把首页加载时间降到一秒以内',
  outcome: '把首页加载时间降到一秒以内，并保证所有测试通过',
  max_iterations: 3,
  role_overrides: {},
  state: 'running',
  created_at: '2026-11-10T09:00:00+00:00',
  updated_at: '2026-11-10T09:30:00+00:00',
  question: '',
  summary: '第 1 轮失败，已继续第 2 轮。',
  stop_reason: '',
  last_next_prompt: '',
  iterations_used: 2,
  iterations: [
    {
      n: 2,
      task_id: 'goal-task-2',
      prompt: '补充移动端验收步骤，并复验加载时间',
      decision: '',
      decision_reason: '',
      started_at: '2026-11-10T09:10:00+00:00',
      finished_at: '2026-11-10T09:29:00+00:00',
      task_final_state: 'completed',
      task_state: 'completed',
    },
    {
      n: 1,
      task_id: 'goal-task-1',
      prompt: '把首页加载时间降到一秒以内，并保证所有测试通过',
      decision: 'continue',
      decision_reason: '测试还没跑完，继续改',
      started_at: '2026-11-10T09:00:30+00:00',
      finished_at: '2026-11-10T09:09:00+00:00',
      task_final_state: 'failed',
      task_state: 'failed',
    },
  ],
}

const GOAL_TASK: SupervisorTaskDetail = {
  id: 'goal-task-1',
  goal: '第 1 轮：把首页加载时间降到 1 秒以内',
  repo_path: '/srv/repos/conveyor',
  worktree_path: '/srv/repos/conveyor-wt/fix-console',
  branch: 'fix/console',
  state: 'running',
  stage: 'implementation',
  repair_round: 0,
  max_repair_rounds: 2,
  created_at: '2026-11-10T09:00:30+00:00',
  updated_at: '2026-11-10T09:01:00+00:00',
  metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
  handoffs: [],
  executions: [],
  acceptance: null,
  summary: '',
}

async function installGoalsMockFixture(
  page: Page,
  options: {
    initialGoalsByWt?: Record<string, GoalDetail[]>
    initialTasksByWt?: Record<string, SupervisorTaskDetail[]>
    failNextGoalCreate?: boolean
  } = {},
) {
  // Deep-clone every seeded goal: route handlers mutate state, and tests share the fixtures.
  const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
  const goalsByWt: Record<string, GoalDetail[]> = {}
  for (const [wtId, list] of Object.entries({ 'wt-fix-console': [], ...(options.initialGoalsByWt || {}) })) {
    goalsByWt[wtId] = (list || []).map(clone)
  }
  const tasksByWt: Record<string, SupervisorTaskDetail[]> = {
    'wt-fix-console': [],
    ...(options.initialTasksByWt || {}),
  }
  const deployments: SupervisorDeployment[] = []
  const goalCreateCalls: Array<Record<string, unknown>> = []
  const goalStopCalls: Array<{ id: string; body: Record<string, unknown> }> = []
  const goalReplyCalls: Array<{ id: string; body: { message: string; achieved: boolean } }> = []

  let failNextGoalCreate = options.failNextGoalCreate ?? false
  let growGoalIterations = false

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
    if (path === '/api/chat' && method === 'POST') {
      const body = request.postDataJSON() as { message: string }
      const sse = [
        `event: session\ndata: ${JSON.stringify({ session_id: 'web:web-console:wt-fix-console' })}\n\n`,
        `event: message\ndata: ${JSON.stringify({ text: `已收到关于「${body.message}」的讨论。` })}\n\n`,
        'event: done\ndata: {}\n\n',
      ].join('')
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
    }

    // Supervisor contract endpoints
    if (path === '/api/supervisor/capabilities' && method === 'GET') {
      return json(createMockCapabilities())
    }
    if (path === '/api/supervisor/repositories' && method === 'GET') {
      return json({ repositories: createInitialRepos() })
    }
    if (path === '/api/supervisor/tasks' && method === 'GET') {
      const wtId = url.searchParams.get('worktree_id') || ''
      const list = wtId ? (tasksByWt[wtId] || []) : Object.values(tasksByWt).flat()
      return json({ tasks: list, count: list.length })
    }
    const taskDetailMatch = path.match(/^\/api\/supervisor\/tasks\/([^/]+)$/)
    if (taskDetailMatch && method === 'GET') {
      const found = Object.values(tasksByWt).flat().find(t => t.id === taskDetailMatch[1])
      return found ? json(found) : json({ error: 'task not found' }, 404)
    }
    if (path === '/api/supervisor/deployments' && method === 'GET') {
      const taskId = url.searchParams.get('task_id')
      const filtered = taskId ? deployments.filter(d => !d.task_id || d.task_id === taskId) : deployments
      return json({ deployments: filtered, count: filtered.length })
    }

    // ---- 目标（goals） contract ----
    if (path === '/api/supervisor/goals' && method === 'GET') {
      const all = Object.values(goalsByWt).flat()
      const list: Goal[] = all.map(g => ({ ...g, iterations_used: g.iterations.length }))
      return json({ goals: list, count: list.length })
    }

    if (path === '/api/supervisor/goals' && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      goalCreateCalls.push(body)
      if (failNextGoalCreate) {
        failNextGoalCreate = false
        return json({ error: 'executor unavailable: claude binary not found', code: 'executor_unavailable' }, 503)
      }
      const wtId = String(body.worktree_id || 'wt-fix-console')
      const outcome = String(body.outcome || '')
      const created: GoalDetail = {
        id: `goal-mock-created-${goalCreateCalls.length}`,
        repo_id: String(body.repo_id || 'repo-conveyor'),
        worktree_id: wtId,
        title: outcome.length > 40 ? `${outcome.slice(0, 40)}…` : outcome || '新目标',
        outcome,
        max_iterations: Number(body.max_iterations || 5),
        role_overrides: (body.role_overrides as Record<string, unknown>) || {},
        state: 'running',
        created_at: now(),
        updated_at: now(),
        question: '',
        summary: '第 1 轮已派发：正在实现并自测。',
        stop_reason: '',
        last_next_prompt: '',
        iterations_used: 1,
        iterations: [
          {
            n: 1,
            task_id: 'goal-task-1',
            prompt: outcome,
            decision: '',
            decision_reason: '',
            started_at: now(),
            finished_at: '',
            task_final_state: 'running',
            task_state: 'running',
          },
        ],
      }
      goalsByWt[wtId] = [created, ...(goalsByWt[wtId] || [])]
      return json(created, 201)
    }

    const goalDetailMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)$/)
    if (goalDetailMatch && method === 'GET') {
      const found = Object.values(goalsByWt).flat().find(g => g.id === goalDetailMatch[1])
      if (!found) return json({ error: '目标不存在' }, 404)
      let iterations = found.iterations
      if (growGoalIterations && found.state === 'running' && iterations.length < 2) {
        iterations = [
          ...iterations,
          {
            n: 2,
            task_id: 'goal-task-2',
            prompt: '继续第 2 轮：补测试并复验首页加载时间',
            decision: 'continue',
            decision_reason: '上一轮改动有效，但测试还没跑完',
            started_at: now(),
            finished_at: '',
            task_final_state: 'running',
            task_state: 'running',
          },
        ]
      }
      return json({ ...found, iterations, iterations_used: iterations.length }, 200)
    }

    const goalStopMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)\/stop$/)
    if (goalStopMatch && method === 'POST') {
      const id = goalStopMatch[1]
      const body = request.postDataJSON() as Record<string, unknown>
      goalStopCalls.push({ id, body })
      const found = Object.values(goalsByWt).flat().find(g => g.id === id)
      if (!found) return json({ error: '目标不存在' }, 404)
      found.state = 'stopped'
      found.question = ''
      found.stop_reason = '用户手动停止了目标'
      found.updated_at = now()
      return json({ ...found, iterations_used: found.iterations.length }, 200)
    }

    const goalReplyMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)\/reply$/)
    if (goalReplyMatch && method === 'POST') {
      const id = goalReplyMatch[1]
      const body = request.postDataJSON() as { message?: string; achieved?: boolean }
      goalReplyCalls.push({ id, body: { message: String(body.message || ''), achieved: Boolean(body.achieved) } })
      const found = Object.values(goalsByWt).flat().find(g => g.id === id)
      if (!found) return json({ error: '目标不存在' }, 404)
      if (found.state !== 'waiting_user') {
        return json({ error: '目标当前没有在等待用户回复', code: 'goal_not_waiting_user' }, 409)
      }
      if (body.achieved) {
        found.state = 'achieved'
        found.question = ''
        found.stop_reason = '用户确认已达成'
      } else {
        found.state = 'running'
        found.question = ''
        const nextN = found.iterations.length + 1
        found.iterations = [
          ...found.iterations,
          {
            n: nextN,
            task_id: `goal-task-${nextN}`,
            prompt: String(body.message || ''),
            decision: '',
            decision_reason: '用户补充说明后继续',
            started_at: now(),
            finished_at: '',
            task_final_state: '',
            task_state: '',
          },
        ]
      }
      found.updated_at = now()
      return json({ ...found, iterations_used: found.iterations.length }, 200)
    }

    // ---- 自动检查 contract ----
    const validationMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/validation$/)
    if (validationMatch && method === 'GET') {
      const repoId = validationMatch[1]
      return json({
        repo_id: repoId,
        candidates: CHECKS_CANDIDATES,
        selected: [],
        source: 'none',
        env_commands: [],
        effective: [],
      })
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return {
    goalCreateCalls,
    goalStopCalls,
    goalReplyCalls,
    setGrowGoalIterations(v: boolean) {
      growGoalIterations = v
    },
  }
}

test('mock fixture: 目标 sub-tab is first and stays non-default when the worktree has no goal', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page)
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const tabs = page.locator('.sup-subnav [role="tab"]')
  await expect(tabs).toHaveCount(3)
  await expect(tabs.first()).toHaveText('目标')
  await expect(page.getByRole('tab', { name: '任务与结果' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'false')
})

test('mock fixture: a running 目标 becomes the default sub-tab and shows 第 1 / 5 轮', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page, { initialGoalsByWt: { 'wt-fix-console': [RUNNING_GOAL] } })
  await page.goto('/#code')

  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')
  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toBeVisible()
  await expect(card).toContainText('进行中')
  await expect(card).toContainText('第 1 / 5 轮')
  await expect(card).toContainText('首页加载时间降到 1 秒以内')
  await expect(card).toContainText('第 1 轮已派发：正在实现并自测。')
  await expect(card).toContainText('把首页加载时间降到 1 秒以内，并保证所有测试通过')
  await expect(card).toContainText('执行中')
})

test('mock fixture: 开始 posts outcome / max_iterations / repo_id / worktree_id and opens the goal view', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installGoalsMockFixture(page)
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const form = page.getByRole('region', { name: '描述你想要的结果' })
  await expect(form).toContainText('会一轮一轮地改、验证，直到达成；需要你判断时会停下来问你。')
  await form.getByRole('textbox', { name: '描述你想要的结果' }).fill('首页加载时间降到 1 秒以内，并且所有测试通过')
  await form.getByRole('spinbutton', { name: '最多尝试几轮' }).fill('7')
  await form.getByRole('button', { name: '开始', exact: true }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('第 1 / 7 轮')
  expect(fixture.goalCreateCalls).toHaveLength(1)
  expect(fixture.goalCreateCalls[0]).toMatchObject({
    outcome: '首页加载时间降到 1 秒以内，并且所有测试通过',
    max_iterations: 7,
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
  })
  // The create form is replaced by the running goal view (no second 开始 button).
  await expect(page.getByRole('region', { name: '描述你想要的结果' })).toHaveCount(0)
})

test('mock fixture: a running 目标 polls every 3s and paints the next 轮', async ({ page }) => {
  await page.clock.install()
  const fixture = await installGoalsMockFixture(page, { initialGoalsByWt: { 'wt-fix-console': [RUNNING_GOAL] } })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('第 1 / 5 轮')

  fixture.setGrowGoalIterations(true)
  await page.clock.fastForward(3_200)
  await expect(card).toContainText('第 2 / 5 轮')
  await expect(card).toContainText('继续下一轮')
  await expect(card).toContainText('上一轮改动有效，但测试还没跑完')
})

test('mock fixture: waiting_user shows the question and 继续 posts {message, achieved:false}', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installGoalsMockFixture(page, { initialGoalsByWt: { 'wt-fix-console': [WAITING_GOAL] } })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('等你回复')
  await expect(card).toContainText('这一轮的改动已经做完，但没有配置自动验证命令，请检查结果后告诉我是否达成或还要改什么')
  await expect(card).toContainText('需要你决定')
  await expect(card).toContainText('第 2 轮')

  await card.getByRole('textbox', { name: '补充说明或回答' }).fill('再把移动端的按钮调大一点')
  await card.getByRole('button', { name: '继续', exact: true }).click()

  await expect(card).toContainText('进行中')
  await expect(card).toContainText('第 3 / 5 轮')
  await expect(fixture.goalReplyCalls).toHaveLength(1)
  expect(fixture.goalReplyCalls[0]).toEqual({
    id: 'goal-mock-waiting-1',
    body: { message: '再把移动端的按钮调大一点', achieved: false },
  })
})

test('mock fixture: 已经达成了 posts achieved:true and the chip becomes 已达成', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installGoalsMockFixture(page, { initialGoalsByWt: { 'wt-fix-console': [WAITING_GOAL] } })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await card.getByRole('button', { name: '已经达成了' }).click()

  await expect(card).toContainText('已达成')
  await expect(card).not.toContainText('等你回复')
  expect(fixture.goalReplyCalls).toHaveLength(1)
  expect(fixture.goalReplyCalls[0].body.achieved).toBe(true)

  // 跑完了：结果卡留在最上面（带摘要），表单藏起来，点「开始新目标」才露出来。
  await expect(card).toContainText('第 2 轮已跑完，等待你确认结果。')
  await expect(card).toContainText('开始新目标')
  await expect(page.getByRole('region', { name: '描述你想要的结果' })).toHaveCount(0)
  await card.getByRole('button', { name: '开始新目标' }).click()
  await expect(page.getByRole('region', { name: '描述你想要的结果' })).toBeVisible()
  await expect(page.getByRole('region', { name: '目标进度' })).toHaveCount(0)

  const history = page.locator('.sup-goal-history')
  await expect(history).toContainText('以前的目标 · 1 个')
})

test('mock fixture: a finished 目标 stays on top with a readable title and a collapsed timeline', async ({
  page,
}) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [FINISHED_GOAL, EXHAUSTED_GOAL] },
  })
  await page.goto('/#code')
  // 只有已跑完的目标时，目标页不是默认子页，需要手动点开。
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toBeVisible()
  // 老数据里的 `[truncated]` 标题绝不出现，标题从 outcome 现算：40 个字 + 一个省略号。
  const titleLocator = card.locator('.sup-goal-header-main h3')
  const titleText = (await titleLocator.innerText()).trim()
  expect(titleText).toHaveLength(41)
  expect(titleText.startsWith(LONG_OUTCOME.slice(0, 40))).toBe(true)
  expect(titleText.slice(40)).toMatch(/^[….]$/)
  await expect(card).not.toContainText('[truncated]')
  await expect(card).toContainText('已达成')
  await expect(card).toContainText('第 2 / 3 轮')
  await expect(card).toContainText('第 2 轮跑完，首页加载时间已经降到 0.8 秒，所有测试通过。')

  // 每一轮默认收起，点「查看每一轮」才展开。
  await expect(card.getByRole('button', { name: '查看每一轮' })).toBeVisible()
  await expect(card).not.toContainText('继续下一轮')
  await card.getByRole('button', { name: '查看每一轮' }).click()
  await expect(card).toContainText('继续下一轮')
  await expect(card).toContainText('第 1 轮')
  await expect(card).toContainText('第 2 轮')

  // 目标跑完之后表单先藏起来。
  await expect(page.getByRole('region', { name: '描述你想要的结果' })).toHaveCount(0)
})

test('mock fixture: an exhausted 目标 explains itself in plain Chinese and offers 开始新目标', async ({
  page,
}) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page, { initialGoalsByWt: { 'wt-fix-console': [EXHAUSTED_GOAL] } })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('轮数用完了，还没有达成')
  await expect(card).toContainText('迭代预算已用完（3 轮），目标尚未达成')
  await expect(card).toContainText('开始新目标')
  await expect(page.getByRole('region', { name: '描述你想要的结果' })).toHaveCount(0)

  await card.getByRole('button', { name: '开始新目标' }).click()
  await expect(page.getByRole('region', { name: '描述你想要的结果' })).toBeVisible()
  await expect(page.getByRole('region', { name: '目标进度' })).toHaveCount(0)
})

test('mock fixture: 以前的目标 rows are clickable and show that goal\'s timeline', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [FINISHED_GOAL, EXHAUSTED_GOAL] },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  // 最新一个目标的结果卡在最上面。
  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('已达成')

  const history = page.locator('.sup-goal-history')
  await expect(history).toContainText('以前的目标 · 2 个')
  await history.locator('.sup-run-history-bar').click()

  const olderRow = history.locator('.sup-goal-history-row').filter({ hasText: '已用完轮数' })
  await olderRow.click()

  await expect(card).toContainText('轮数用完了，还没有达成')
  await expect(card).toContainText('迭代预算已用完（3 轮），目标尚未达成')
  await expect(card).not.toContainText('已达成')
  // 点开的老目标也带着收起来的每一轮。
  await expect(card.getByRole('button', { name: '查看每一轮' })).toBeVisible()
  await card.getByRole('button', { name: '查看每一轮' }).click()
  await expect(card).toContainText('继续压缩构建时间')
})

test('mock fixture: a round row shows the task state, and 正在判断下一步… only while deciding', async ({
  page,
}) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [RUNNING_GOAL_MID_EVAL] },
  })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('第 2 / 3 轮')
  // 「未开始」和「进行中」绝不会同时出现在一轮里。
  await expect(card).not.toContainText('未开始')

  const secondRound = card.locator('.sup-goal-iteration').first()
  await expect(secondRound).toContainText('第 2 轮')
  await expect(secondRound.locator('.sup-goal-iteration-task-state')).toHaveText('已完成')
  await expect(secondRound).toContainText('正在判断下一步…')
  await expect(secondRound).not.toContainText('继续下一轮')
  await expect(secondRound).not.toContainText('已达成')

  const firstRound = card.locator('.sup-goal-iteration').nth(1)
  await expect(firstRound).toContainText('第 1 轮')
  await expect(firstRound.locator('.sup-goal-iteration-task-state')).toHaveText('失败')
  await expect(firstRound).toContainText('继续下一轮')
  await expect(firstRound).not.toContainText('正在判断下一步…')
})

test('mock fixture: 停止 asks for confirmation before posting stop', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installGoalsMockFixture(page, { initialGoalsByWt: { 'wt-fix-console': [RUNNING_GOAL] } })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await card.getByRole('button', { name: '停止', exact: true }).click()
  await expect(card).toContainText('停止后当前这一轮会被取消，已做的改动保留在工作目录里。')
  expect(fixture.goalStopCalls).toHaveLength(0)

  await card.getByRole('button', { name: '确认停止' }).click()
  await expect(card).toContainText('已停止')
  expect(fixture.goalStopCalls).toHaveLength(1)
  expect(fixture.goalStopCalls[0].id).toBe('goal-mock-running-1')
})

test('mock fixture: 查看这一轮的任务 switches to 任务与结果 with that task selected', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [RUNNING_GOAL] },
    initialTasksByWt: { 'wt-fix-console': [GOAL_TASK] },
  })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await card.getByRole('button', { name: '查看这一轮的任务' }).click()

  await expect(page.getByRole('tab', { name: '任务与结果' })).toHaveAttribute('aria-selected', 'true')
  const taskCard = page.getByRole('region', { name: '当前任务进度与结果' })
  await expect(taskCard).toContainText('第 1 轮：把首页加载时间降到 1 秒以内')
  await expect(taskCard).toContainText('执行中')
})

test('mock fixture: an English backend error becomes one Chinese line, never the raw message', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page, { failNextGoalCreate: true })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const form = page.getByRole('region', { name: '描述你想要的结果' })
  await form.getByRole('textbox', { name: '描述你想要的结果' }).fill('把构建时间压到 30 秒以内')
  await form.getByRole('button', { name: '开始', exact: true }).click()

  await expect(form).toContainText('操作没有成功，请稍后再试')
  await expect(form).not.toContainText('executor unavailable')
  await expect(form).not.toContainText('claude binary not found')
})

test('mock fixture: read-only worktree shows the same read-only banner and disables 开始', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page)
  await page.goto('/#code')

  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()
  await expect(page.getByRole('heading', { name: 'Conveyor / main' })).toBeVisible()
  await page.getByRole('tab', { name: '目标' }).click()

  const pane = page.locator('.sup-goals-scroll')
  await expect(pane.getByText('当前为只读工作目录（主分支）')).toBeVisible()
  await expect(pane.getByRole('button', { name: '开始', exact: true })).toBeDisabled()
})

test('mock fixture: phone 390px keeps the 目标 tab inside the viewport with 40px buttons', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installGoalsMockFixture(page)
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const pane = page.locator('.sup-goals-scroll')
  await expect(pane).toBeVisible()
  const startButton = pane.getByRole('button', { name: '开始', exact: true })
  const box = await startButton.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.height).toBeGreaterThanOrEqual(40)

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  expect(overflow.scroll).toBeLessThanOrEqual(390)
})

test('mock fixture: waiting_user on phone has no horizontal overflow and keeps reply buttons tappable', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installGoalsMockFixture(page, { initialGoalsByWt: { 'wt-fix-console': [WAITING_GOAL] } })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('需要你决定')
  for (const name of ['继续', '已经达成了']) {
    const box = await card.getByRole('button', { name, exact: true }).boundingBox()
    expect(box).not.toBeNull()
    expect(box!.height).toBeGreaterThanOrEqual(40)
  }
  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  expect(overflow.scroll).toBeLessThanOrEqual(390)
})

test('mock fixture: the 目标 tab shows no English goal/iteration words', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [WAITING_GOAL] },
    initialTasksByWt: { 'wt-fix-console': [GOAL_TASK] },
  })
  await page.goto('/#code')

  const pane = page.locator('.sup-goals-scroll')
  await expect(pane).toBeVisible()
  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')

  const visibleText = await pane.innerText()
  expect(visibleText).not.toMatch(/goal/i)
  expect(visibleText).not.toMatch(/iteration/i)
  expect(visibleText).toContain('第 2 轮')
  expect(visibleText).toContain('需要你决定')
})
