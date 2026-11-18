import { expect, test, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorRepo,
} from '../src/components/SupervisorWorkspace'
import type { GoalDetail } from '../src/components/SupervisorGoals'

// NOTE: N1 — 项目 must open where the owner was working, and a read-only worktree must
// not show an unusable form. Everything here mocks the /api/supervisor/* contract and
// checks frontend wiring only: the initial landing pick, the read-only card, and the
// layout of the 目标 form.

const TOKEN = 'n1-project-landing-mock-token-0001'
const DESKTOP = { width: 1440, height: 900 }

const OLD_GOAL_AT = '2026-11-01T08:00:00Z'
const NEW_GOAL_AT = '2026-11-12T09:30:00Z'

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

/** 老仓库排在前面：旧实现会无条件落到它的主分支上。 */
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
          created_at: '2026-10-01T00:00:00Z',
        },
        {
          id: 'wt-fix-console',
          path: '/srv/repos/conveyor-wt/fix-console',
          branch: 'fix/console',
          head: 'a1b2c3d4e5f6',
          is_main: false,
          managed: true,
          locked: false,
          created_at: '2026-10-02T00:00:00Z',
          chat_session_id: 'web:web-console:wt-fix-console',
        },
      ],
    },
    {
      id: 'repo-qa-todo',
      name: 'qa-todo-cli',
      path: '/srv/projects/qa-todo-cli',
      source_machine: 'dev-workstation',
      validation_source: 'repo',
      worktrees: [
        {
          id: 'wt-todo-main',
          path: '/srv/projects/qa-todo-cli',
          branch: 'main',
          head: '111111111111',
          is_main: true,
          managed: false,
          locked: false,
          created_at: '2026-10-03T00:00:00Z',
        },
        {
          id: 'wt-todo-1',
          path: '/srv/projects/qa-todo-cli/.worktrees/wt-1',
          branch: 'sup/wt-1',
          head: '222222222222',
          is_main: false,
          managed: true,
          locked: false,
          created_at: '2026-10-04T00:00:00Z',
          chat_session_id: 'web:web-console:supwt-wt-1',
        },
      ],
    },
  ]
}

function goal(
  id: string,
  repoId: string,
  worktreeId: string,
  updatedAt: string,
): GoalDetail {
  return {
    id,
    repo_id: repoId,
    worktree_id: worktreeId,
    title: '整理回归测试',
    outcome: '整理回归测试',
    max_iterations: 5,
    role_overrides: {},
    state: 'achieved',
    created_at: '2026-10-20T00:00:00Z',
    updated_at: updatedAt,
    question: '',
    summary: '这一轮已经跑完。',
    stop_reason: '',
    last_next_prompt: '',
    deadline_at: null,
    mode: 'outcome',
    resume_at: null,
    rounds_passed: 1,
    rounds_total: 1,
    remaining_seconds: null,
    iterations_used: 1,
    iterations: [
      {
        n: 1,
        task_id: `${id}-task-1`,
        prompt: '整理回归测试',
        decision: 'achieved',
        decision_reason: '测试都过了',
        started_at: '2026-10-20T00:00:00Z',
        finished_at: '2026-10-20T00:20:00Z',
        task_final_state: 'completed',
        task_state: 'completed',
        passed: true,
        round_title: '补齐回归用例',
      },
    ],
  }
}

async function installFixture(
  page: Page,
  options: {
    goals?: GoalDetail[]
    tasks?: Array<Record<string, unknown>>
    savedContext?: { repoId: string; worktreeId: string }
  } = {},
) {
  const goals: GoalDetail[] = (options.goals || []).map(g => JSON.parse(JSON.stringify(g)) as GoalDetail)
  const tasks: Array<Record<string, unknown>> = options.tasks || []
  const repos = createRepos()
  const createWorktreeUrls: string[] = []

  await page.addInitScript(([token, saved]) => {
    sessionStorage.setItem('conveyor-token', token as string)
    if (saved) localStorage.setItem('conveyor-supervisor-context-v1', JSON.stringify(saved))
    else localStorage.removeItem('conveyor-supervisor-context-v1')
  }, [TOKEN, options.savedContext || null] as [string, { repoId: string; worktreeId: string } | null])

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
      return json({ repositories: repos })
    }
    // 项目 reads the whole task list once, without a worktree filter, to pick the
    // worktree with the most recent activity.
    if (path === '/api/supervisor/tasks' && method === 'GET') {
      const worktreeId = url.searchParams.get('worktree_id') || ''
      const list = worktreeId
        ? tasks.filter(t => (t.metadata as { worktree_id?: string } | undefined)?.worktree_id === worktreeId)
        : tasks
      return json({ tasks: list, count: list.length })
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
      const list = goals.map(g => ({ ...g, iterations_used: g.iterations.length }))
      return json({ goals: list, count: list.length })
    }

    const goalDetailMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)$/)
    if (goalDetailMatch && method === 'GET') {
      const found = goals.find(g => g.id === goalDetailMatch[1])
      if (!found) return json({ error: '目标不存在' }, 404)
      return json({ ...found, iterations_used: found.iterations.length })
    }

    const createWtMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/worktrees$/)
    if (createWtMatch && method === 'POST') {
      const repoId = createWtMatch[1]
      const repo = repos.find(r => r.id === repoId)
      if (!repo) return json({ error: 'repository not found' }, 404)
      createWorktreeUrls.push(path)
      const body = request.postDataJSON() as Record<string, unknown>
      const branch = String(body.branch || 'sup/n1-new')
      const created = {
        id: `wt-created-${createWorktreeUrls.length}`,
        path: `${repo.path}-wt/${branch.replace(/\//g, '-')}`,
        branch,
        head: 'a1b2c3d4e5f6',
        is_main: false,
        managed: true,
        locked: false,
        created_at: now(),
        chat_session_id: 'web:web-console:wt-created',
      }
      repo.worktrees.push(created)
      return json(created, 201)
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return { createWorktreeUrls, repos }
}

const readOnlyCard = (page: Page) => page.getByRole('region', { name: '只读工作目录' })

test('fresh browser lands on the repo/worktree with the most recent goal, not the first repo', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, {
    goals: [
      goal('goal-old', 'repo-conveyor', 'wt-fix-console', OLD_GOAL_AT),
      goal('goal-new', 'repo-qa-todo', 'wt-todo-1', NEW_GOAL_AT),
    ],
  })
  await page.goto('/#code')

  // The second repository has the newest goal -> it is selected on its managed worktree.
  await expect(page.getByRole('heading', { name: 'qa-todo-cli / sup/wt-1' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Conveyor / main' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toHaveCount(0)
})

test('a valid saved context still wins over the most recent activity', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, {
    goals: [
      goal('goal-old', 'repo-conveyor', 'wt-fix-console', OLD_GOAL_AT),
      goal('goal-new', 'repo-qa-todo', 'wt-todo-1', NEW_GOAL_AT),
    ],
    savedContext: { repoId: 'repo-conveyor', worktreeId: 'wt-fix-console' },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()
})

test('a saved context that no longer exists falls back to the most recent activity', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, {
    goals: [goal('goal-new', 'repo-qa-todo', 'wt-todo-1', NEW_GOAL_AT)],
    savedContext: { repoId: 'repo-gone', worktreeId: 'wt-gone' },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'qa-todo-cli / sup/wt-1' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Conveyor / main' })).toHaveCount(0)
})

test('the most recent supervisor task also decides the landing worktree', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, {
    tasks: [
      {
        id: 'task-old',
        goal: '老任务',
        state: 'completed',
        created_at: '2026-10-05T00:00:00Z',
        updated_at: '2026-10-05T01:00:00Z',
        metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
      },
      {
        id: 'task-new',
        goal: '刚跑完的任务',
        state: 'completed',
        created_at: '2026-11-12T08:00:00Z',
        updated_at: NEW_GOAL_AT,
        metadata: { repo_id: 'repo-qa-todo', worktree_id: 'wt-todo-1' },
      },
    ],
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'qa-todo-cli / sup/wt-1' })).toBeVisible()
})

test('with no activity at all the most recently created managed worktree is used', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installFixture(page)
  fixture.repos[1].worktrees.push({
    id: 'wt-todo-2',
    path: '/srv/projects/qa-todo-cli/.worktrees/wt-2',
    branch: 'sup/wt-2',
    head: '333333333333',
    is_main: false,
    managed: true,
    locked: false,
    created_at: '2026-11-20T00:00:00Z',
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'qa-todo-cli / sup/wt-2' })).toBeVisible()
})

test('read-only main shows one card instead of the unusable dispatch form', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page)
  await page.goto('/#code')

  await expect(page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first()).toBeVisible()
  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()
  await expect(page.getByRole('heading', { name: 'Conveyor / main' })).toBeVisible()

  await expect(readOnlyCard(page)).toBeVisible()
  await expect(readOnlyCard(page)).toContainText('这是只读的主分支，不能直接在这里改。')
  await expect(readOnlyCard(page).getByRole('button', { name: '新建工作目录并开始' })).toBeVisible()

  // No disabled form is rendered on a read-only worktree.
  await expect(page.getByRole('textbox', { name: '任务目标' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开始执行' })).toHaveCount(0)
  await expect(page.getByText('当前为只读工作目录')).toHaveCount(0)
})

test('the read-only card creates a worktree through the existing action and switches to it', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installFixture(page)
  await page.goto('/#code')

  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()
  await expect(readOnlyCard(page)).toBeVisible()

  await readOnlyCard(page).getByRole('button', { name: '新建工作目录并开始' }).click()

  expect(fixture.createWorktreeUrls).toEqual(['/api/supervisor/repositories/repo-conveyor/worktrees'])
  // The existing create-worktree path selects the new worktree for us.
  await expect(page.getByRole('heading', { name: 'Conveyor / sup/n1-new' })).toBeVisible()
  await expect(readOnlyCard(page)).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: '任务目标' })).toBeVisible()
})

test('read-only main lists at most 3 managed worktrees as 去 <branch> shortcuts', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installFixture(page)
  const branches = ['sup/a', 'sup/b', 'sup/c', 'sup/d']
  branches.forEach((branch, index) => {
    fixture.repos[0].worktrees.push({
      id: `wt-extra-${index}`,
      path: `/srv/repos/conveyor-wt/extra-${index}`,
      branch,
      head: '444444444444',
      is_main: false,
      managed: true,
      locked: false,
    })
  })
  await page.goto('/#code')

  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()
  const card = readOnlyCard(page)
  await expect(card.getByRole('button', { name: '去 fix/console' })).toBeVisible()
  await expect(card.getByRole('button', { name: '去 sup/a' })).toBeVisible()
  await expect(card.getByRole('button', { name: '去 sup/b' })).toBeVisible()
  await expect(card.getByRole('button', { name: '去 sup/c' })).toHaveCount(0)
  await expect(card.getByRole('button', { name: '去 sup/d' })).toHaveCount(0)

  await card.getByRole('button', { name: '去 sup/b' }).click()
  await expect(page.getByRole('heading', { name: 'Conveyor / sup/b' })).toBeVisible()
  await expect(card).toHaveCount(0)
})

test('the 目标 tab on read-only main shows the card and no goal form at all', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page)
  await page.goto('/#code')

  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()
  await page.getByRole('tab', { name: '目标' }).click()

  await expect(readOnlyCard(page)).toContainText('这是只读的主分支，不能直接在这里改。')
  await expect(page.getByRole('region', { name: '描述你想要的结果' })).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: '描述你想要的结果' })).toHaveCount(0)
  await expect(page.getByRole('radio', { name: '按轮数' })).toHaveCount(0)
  await expect(page.getByRole('spinbutton', { name: '最多尝试几轮' })).toHaveCount(0)
  await expect(page.getByRole('checkbox', { name: /达成后继续/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开始', exact: true })).toHaveCount(0)
})

test('达成后继续 follows 最多尝试几轮 directly, with no reserved empty gap', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page)
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const rounds = page.getByRole('spinbutton', { name: '最多尝试几轮' })
  await expect(rounds).toBeVisible()
  const roundsBox = await rounds.boundingBox()
  const checkBox = await page.getByRole('checkbox', { name: /达成后继续/ }).boundingBox()
  expect(roundsBox).not.toBeNull()
  expect(checkBox).not.toBeNull()

  // 最多尝试几轮 keeps a normal field height: the old 180px flex-basis reserved a big gap.
  expect(roundsBox!.height).toBeLessThan(60)
  // The checkbox starts right under the field (label + 4px gap + input + 10px form gap).
  expect(checkBox!.y - (roundsBox!.y + roundsBox!.height)).toBeLessThan(40)
})

test('an unlocked managed worktree with a stopped goal shows no 已锁定 banner', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, {
    goals: [goal('goal-stopped', 'repo-conveyor', 'wt-fix-console', OLD_GOAL_AT)],
    savedContext: { repoId: 'repo-conveyor', worktreeId: 'wt-fix-console' },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()
  // The worktree is unlocked (locked: false, no active task), so the lock banner must stay hidden.
  await expect(page.getByText('当前工作目录已锁定')).toHaveCount(0)
})

test('a locked worktree with a running task and no active goal shows the 已锁定 banner', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installFixture(page, {
    savedContext: { repoId: 'repo-conveyor', worktreeId: 'wt-fix-console' },
  })
  const worktree = fixture.repos[0].worktrees[1]
  worktree.locked = true
  worktree.active_task_id = 'task-running-1'
  worktree.active_task_state = 'running'
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()
  await expect(page.getByText('当前工作目录已锁定（有任务正在执行）')).toBeVisible()
})

test('race condition: explicit selection is not overridden by bootstrap', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page)

  // Delay the response for /api/supervisor/goals to simulate slow load
  await page.route('**/api/supervisor/goals', async (route) => {
    await new Promise(r => setTimeout(r, 800))
    await route.continue()
  })

  await page.goto('/#code')

  // Click the Conveyor main worktree before it resolves
  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()

  // Wait for the response to resolve (800ms) plus buffer
  await page.waitForTimeout(1500)

  // Assert 'Conveyor / main' stays visible
  await expect(page.getByRole('heading', { name: 'Conveyor / main' })).toBeVisible()
})

