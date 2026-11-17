import { expect, test, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorRepo,
} from '../src/components/SupervisorWorkspace'
import type { Goal, GoalDetail, GoalUsage } from '../src/components/SupervisorGoals'

const TOKEN = 'a3-goal-usage-mock-token-0001'
const DESKTOP = { width: 1440, height: 900 }

// 后端 usage 字典的原样拷贝：cached_input_tokens 已经算在 input_tokens 里面，
// 总数 = input + output（30000 + 4000 = 34000 → 「约 3.4 万 token」）。
const USAGE_WITH_NOTE: GoalUsage = {
  input_tokens: 30000,
  output_tokens: 4000,
  cached_input_tokens: 9000,
  runs_with_usage: 3,
  runs_without_usage: 1,
}

const USAGE_COMPLETE: GoalUsage = {
  input_tokens: 120000,
  output_tokens: 3000,
  cached_input_tokens: 0,
  runs_with_usage: 4,
  runs_without_usage: 0,
}

const USAGE_EMPTY: GoalUsage = {
  input_tokens: 0,
  output_tokens: 0,
  cached_input_tokens: 0,
  runs_with_usage: 0,
  runs_without_usage: 2,
}

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

function createGoal(
  id: string,
  title: string,
  usage: GoalUsage | undefined,
): GoalDetail {
  return {
    id,
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
    title,
    outcome: title,
    max_iterations: 5,
    role_overrides: {},
    state: 'achieved',
    created_at: '2026-11-15T09:00:00Z',
    updated_at: '2026-11-15T10:00:00Z',
    question: '',
    summary: '目标已经达成，改动都留在这个工作目录里。',
    stop_reason: '',
    last_next_prompt: '',
    resume_at: null,
    rounds_passed: 3,
    rounds_total: 3,
    remaining_seconds: 0,
    iterations_used: 3,
    ...(usage ? { usage } : {}),
    iterations: [],
  }
}

// Mocking pattern copied from web/e2e/a1-timeboxed-goals.spec.ts: one
// **/api/** route that answers every supervisor endpoint the goal card reads.
async function installGoalsFixture(page: Page, initialGoals: GoalDetail[]) {
  const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
  const goalsList: GoalDetail[] = initialGoals.map(clone)

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
        candidates: [],
        selected: [],
        source: 'repo',
        env_commands: [],
        effective: [],
      })
    }

    if (path === '/api/supervisor/goals' && method === 'GET') {
      const list: Goal[] = goalsList.map(g => ({ ...g, iterations_used: g.iterations.length }))
      return json({ goals: list, count: list.length })
    }

    const goalDetailMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)$/)
    if (goalDetailMatch && method === 'GET') {
      const found = goalsList.find(g => g.id === goalDetailMatch[1])
      if (!found) return json({ error: '目标不存在' }, 404)
      return json({ ...found, iterations_used: found.iterations.length })
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })
}

test('goal card shows 已用约 X token plus the partial-reporting note', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsFixture(page, [createGoal('goal-usage-1', '十小时迭代优化', USAGE_WITH_NOTE)])
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  // 30000 + 4000 = 34000 → 万 with one decimal; 9000 cached is already inside input.
  await expect(card).toContainText('已用约 3.4 万 token')
  await expect(card).toContainText('（部分执行者没有报告用量）')
  // 10 万以上 only whole 万, and never raw backend codes.
  const cardText = await card.innerText()
  expect(cardText.toLowerCase()).not.toContain('input_tokens')
  expect(cardText.toLowerCase()).not.toContain('runs_without_usage')
})

test('goal card omits the note when every run reported usage', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsFixture(page, [createGoal('goal-usage-2', '另一项十小时优化', USAGE_COMPLETE)])
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('已用约 12 万 token')
  await expect(card).not.toContainText('（部分执行者没有报告用量）')
})

test('goal card shows no usage line at all when nothing reported usage', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsFixture(page, [createGoal('goal-usage-3', '没有任何用量的目标', USAGE_EMPTY)])
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('没有任何用量的目标')
  await expect(card).not.toContainText('已用约')
  await expect(card).not.toContainText('0 token')
})

test('goal card stays clean against an older server that sends no usage field', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installGoalsFixture(page, [createGoal('goal-usage-4', '老服务器上的目标', undefined)])
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('老服务器上的目标')
  await expect(card).not.toContainText('已用约')
})
