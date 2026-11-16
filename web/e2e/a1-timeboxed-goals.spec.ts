import { expect, test, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorRepo,
} from '../src/components/SupervisorWorkspace'
import type { Goal, GoalDetail } from '../src/components/SupervisorGoals'
import type { SupervisorChecksCandidate } from '../src/components/SupervisorChecks'

const TOKEN = 'a1-timeboxed-goals-mock-token-0001'
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
      implementer: { executor: 'codex', model: null, available: true },
      reviewer: { executor: 'codex', model: 'o3-mini', available: true },
    },
    executors: [
      { name: 'claude', label: 'Claude Code', available: true },
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
      validation_source: 'repo',
      worktrees: [
        {
          id: 'wt-fix-console',
          path: '/srv/repos/conveyor-wt/fix-console',
          branch: 'feat/timebox',
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

async function installTimeboxedGoalsFixture(
  page: Page,
  initialGoals: GoalDetail[] = [],
) {
  const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
  const goalsList: GoalDetail[] = initialGoals.map(clone)
  const goalCreateCalls: Array<Record<string, unknown>> = []
  const goalStopCalls: string[] = []
  let goalListPollCount = 0

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
        queue: { depth: 0, paused: false, states: { running: 0, completed: 0 } },
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
        selected: ['npm-test'],
        source: 'repo',
        env_commands: [],
        effective: [['npm', 'test', '--silent']],
      })
    }

    if (path === '/api/supervisor/goals' && method === 'GET') {
      goalListPollCount += 1
      const list: Goal[] = goalsList.map(g => ({ ...g, iterations_used: g.iterations.length }))
      return json({ goals: list, count: list.length })
    }

    if (path === '/api/supervisor/goals' && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      goalCreateCalls.push(body)
      const durationMin = Number(body.duration_minutes || 0)
      const remainingSeconds = durationMin > 0 ? durationMin * 60 : null
      const deadlineAt = durationMin > 0
        ? new Date(Date.now() + durationMin * 60 * 1000).toISOString()
        : null
      const outcome = String(body.outcome || '')
      const created: GoalDetail = {
        id: `goal-tb-${goalCreateCalls.length}`,
        repo_id: String(body.repo_id || 'repo-conveyor'),
        worktree_id: String(body.worktree_id || 'wt-fix-console'),
        title: outcome.slice(0, 40) || '新目标',
        outcome,
        max_iterations: durationMin > 0 ? 200 : Number(body.max_iterations || 5),
        role_overrides: {},
        state: 'running',
        created_at: now(),
        updated_at: now(),
        question: '',
        summary: '第 1 轮已开始执行。',
        stop_reason: '',
        last_next_prompt: '',
        deadline_at: deadlineAt,
        mode: body.mode === 'continuous' ? 'continuous' : 'outcome',
        resume_at: null,
        rounds_passed: 0,
        rounds_total: 1,
        remaining_seconds: remainingSeconds,
        iterations_used: 1,
        iterations: [
          {
            n: 1,
            task_id: 'tb-task-1',
            prompt: outcome,
            decision: '',
            decision_reason: '',
            started_at: now(),
            finished_at: '',
            task_final_state: 'running',
            task_state: 'running',
            passed: false,
            round_title: '',
          },
        ],
      }
      goalsList.unshift(created)
      return json(created, 201)
    }

    const goalDetailMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)$/)
    if (goalDetailMatch && method === 'GET') {
      const found = goalsList.find(g => g.id === goalDetailMatch[1])
      if (!found) return json({ error: '目标不存在' }, 404)
      return json({ ...found, iterations_used: found.iterations.length })
    }

    const goalStopMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)\/stop$/)
    if (goalStopMatch && method === 'POST') {
      const id = goalStopMatch[1]
      goalStopCalls.push(id)
      const found = goalsList.find(g => g.id === id)
      if (!found) return json({ error: '目标不存在' }, 404)
      found.state = 'stopped'
      found.stop_reason = '用户手动停止了目标'
      found.resume_at = null
      found.updated_at = now()
      return json({ ...found, iterations_used: found.iterations.length })
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return {
    goalCreateCalls,
    goalStopCalls,
    getGoalListPollCount: () => goalListPollCount,
  }
}

test('create with 10 小时 + 持续改进 sends duration_minutes: 600 and mode: continuous', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installTimeboxedGoalsFixture(page)
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const form = page.getByRole('region', { name: '描述你想要的结果' })
  // Default stays 按轮数.
  await expect(form.getByRole('radio', { name: '按轮数' })).toHaveAttribute('aria-checked', 'true')
  await expect(form.getByRole('spinbutton', { name: '最多尝试几轮' })).toBeVisible()

  // Switch to 按时间 -> verify presets and custom box toggle -> select 10 小时 + 持续改进.
  await form.getByRole('radio', { name: '按时间' }).click()
  await expect(form.getByRole('button', { name: '自定义（分钟）' })).toBeVisible()
  await form.getByRole('button', { name: '自定义（分钟）' }).click()
  await expect(form.getByRole('spinbutton', { name: '自定义时长（分钟）' })).toBeVisible()
  await form.getByRole('button', { name: '10 小时' }).click()

  await form.getByRole('textbox', { name: '描述你想要的结果' }).fill('持续优化代码质量与测试覆盖率')
  await form.getByRole('checkbox', { name: /持续改进/ }).check()
  await form.getByRole('button', { name: '开始', exact: true }).click()

  expect(fixture.goalCreateCalls).toHaveLength(1)
  expect(fixture.goalCreateCalls[0]).toMatchObject({
    outcome: '持续优化代码质量与测试覆盖率',
    duration_minutes: 600,
    mode: 'continuous',
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
  })

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('还剩 10 小时 0 分')
  await expect(card).toContainText('已跑 1 轮 · 通过 0 轮')
})

test('running time-boxed goal card shows countdown, passed rounds list, and ticks locally every 30s', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install({ time: new Date('2026-11-15T10:00:00Z') })
  const timeboxedRunning: GoalDetail = {
    id: 'goal-tb-running',
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
    title: '迭代十个小时提升稳定性',
    outcome: '迭代十个小时提升稳定性',
    max_iterations: 200,
    role_overrides: {},
    state: 'running',
    created_at: '2026-11-15T09:12:00Z',
    updated_at: '2026-11-15T10:00:00Z',
    question: '',
    summary: '正在第 3 轮：优化缓存过期逻辑。',
    stop_reason: '',
    last_next_prompt: '优化缓存过期逻辑',
    deadline_at: '2026-11-15T19:12:00Z',
    mode: 'continuous',
    resume_at: null,
    rounds_passed: 2,
    rounds_total: 3,
    remaining_seconds: 9 * 3600 + 12 * 60, // 9 小时 12 分
    iterations_used: 3,
    iterations: [
      {
        n: 1,
        task_id: 'tb-t1',
        prompt: '第 1 轮',
        decision: 'achieved',
        decision_reason: '第一轮完成',
        started_at: '2026-11-15T09:12:00Z',
        finished_at: '2026-11-15T09:30:00Z',
        task_final_state: 'completed',
        task_state: 'completed',
        passed: true,
        round_title: '补全健康检查单元测试',
      },
      {
        n: 2,
        task_id: 'tb-t2',
        prompt: '第 2 轮',
        decision: 'continue',
        decision_reason: '第二轮完成',
        started_at: '2026-11-15T09:31:00Z',
        finished_at: '2026-11-15T09:50:00Z',
        task_final_state: 'completed',
        task_state: 'completed',
        passed: true,
        round_title: '优化首页首屏渲染',
      },
      {
        n: 3,
        task_id: 'tb-t3',
        prompt: '优化缓存过期逻辑',
        decision: '',
        decision_reason: '',
        started_at: '2026-11-15T09:51:00Z',
        finished_at: '',
        task_final_state: '',
        task_state: 'implementing',
        passed: false,
        round_title: '',
      },
    ],
  }

  await installTimeboxedGoalsFixture(page, [timeboxedRunning])
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('还剩 9 小时 12 分')
  await expect(card).toContainText('已跑 3 轮 · 通过 2 轮')
  await expect(card).toContainText('已完成的改进（2）')
  await expect(card).toContainText('补全健康检查单元测试')
  await expect(card).toContainText('优化首页首屏渲染')

  // Advance local clock 60s (two 30s local ticks) without relying on API change: countdown moves to 9 小时 11 分.
  await page.clock.runFor(60_000)
  await expect(card).toContainText('还剩 9 小时 11 分')

  // Verify no English goal jargon leaks into visible card copy.
  const cardText = await card.innerText()
  expect(cardText.toLowerCase()).not.toContain('iteration')
  expect(cardText.toLowerCase()).not.toContain('waiting_quota')
})

test('waiting_quota state shows HH:MM auto-resume copy and keeps 停止 button working', async ({ page }) => {
  await page.setViewportSize(PHONE)
  const expectedDate = new Date('2026-11-15T14:35:00Z')
  const expectedHHMM = expectedDate.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })

  const quotaGoal: GoalDetail = {
    id: 'goal-tb-quota',
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
    title: '通宵整理回归测试',
    outcome: '通宵整理回归测试',
    max_iterations: 200,
    role_overrides: {},
    state: 'waiting_quota',
    created_at: '2026-11-15T10:00:00Z',
    updated_at: '2026-11-15T14:05:00Z',
    question: '',
    summary: '模型额度暂时用完，大约 30 分钟后自动继续。',
    stop_reason: '',
    last_next_prompt: '补充边界测试',
    deadline_at: '2026-11-15T20:00:00Z',
    mode: 'continuous',
    resume_at: '2026-11-15T14:35:00Z',
    rounds_passed: 1,
    rounds_total: 1,
    remaining_seconds: 21300,
    iterations_used: 2,
    iterations: [
      {
        n: 1,
        task_id: 'tb-q1',
        prompt: '第 1 轮',
        decision: 'continue',
        decision_reason: '第 1 轮通过',
        started_at: '2026-11-15T10:00:00Z',
        finished_at: '2026-11-15T10:20:00Z',
        task_final_state: 'completed',
        task_state: 'completed',
        passed: true,
        round_title: '补齐核心回归用例',
      },
    ],
  }

  const fixture = await installTimeboxedGoalsFixture(page, [quotaGoal])
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('等待额度恢复')
  await expect(card).toContainText(`模型额度暂时用完，会在 ${expectedHHMM} 自动继续`)
  await expect(card.getByRole('button', { name: '停止' })).toBeVisible()

  await card.getByRole('button', { name: '停止' }).click()
  await card.getByRole('button', { name: '确认停止' }).click()
  expect(fixture.goalStopCalls).toEqual(['goal-tb-quota'])
  await expect(card).toContainText('你停止了')
})

test('finished cards show plain Chinese stop reasons for 时间到 and 额度用完', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const deadlineDone: GoalDetail = {
    id: 'goal-tb-deadline',
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
    title: '十小时迭代优化',
    outcome: '十小时迭代优化',
    max_iterations: 200,
    role_overrides: {},
    state: 'exhausted',
    created_at: '2026-11-15T00:00:00Z',
    updated_at: '2026-11-15T10:00:00Z',
    question: '',
    summary: '时间到了：一共跑了 5 轮，其中 4 轮通过检查。',
    stop_reason: 'deadline',
    last_next_prompt: '',
    deadline_at: '2026-11-15T10:00:00Z',
    mode: 'continuous',
    resume_at: null,
    rounds_passed: 4,
    rounds_total: 5,
    remaining_seconds: 0,
    iterations_used: 5,
    iterations: [],
  }
  const quotaDone: GoalDetail = {
    ...deadlineDone,
    id: 'goal-tb-quota-done',
    title: '另一项十小时优化',
    outcome: '另一项十小时优化',
    created_at: '2026-11-14T00:00:00Z',
    updated_at: '2026-11-14T10:00:00Z',
    deadline_at: '2026-11-14T10:00:00Z',
    state: 'exhausted',
    stop_reason: 'quota',
    summary: '模型额度用完了，下次可用时间已经超过截止时间，先停在这里。',
  }

  await installTimeboxedGoalsFixture(page, [deadlineDone, quotaDone])
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  // The most recent finished goal is shown on the top result card (and excluded from 以前的目标).
  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('十小时迭代优化')
  await expect(card).toContainText('时间到')
  await expect(card).toContainText('一共跑了 5 轮，其中 4 轮通过检查')

  // Older finished goals are opened from 以前的目标.
  const history = page.getByRole('region', { name: '以前的目标' })
  await history.getByRole('button', { name: /以前的目标/ }).click()
  await history.getByRole('button', { name: /另一项十小时优化/ }).click()
  await expect(card).toContainText('另一项十小时优化')
  await expect(card).toContainText('额度用完')
  await expect(card).toContainText('模型额度用完了，下次可用时间已经超过截止时间，先停在这里')
})
