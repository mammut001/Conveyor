import { expect, test, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorRepo,
  SupervisorTaskDetail,
} from '../src/components/SupervisorWorkspace'
import type { GoalDetail } from '../src/components/SupervisorGoals'
import type { SupervisorMergePreview } from '../src/components/SupervisorMerge'

// NOTE: 「合并到主分支」 card on the 项目 page. All tests mock the /api/supervisor/* contract and
// verify frontend wiring only: nothing-to-merge note, blockers without a merge button, the confirm
// gate before POST {confirm:true}, cancel posting nothing, merge_conflict wording, the entry point
// hidden while a task/goal runs and on the main checkout / unmanaged worktree, phone layout, and
// Simplified-Chinese-only visible copy.

const TOKEN = 'ux-supervisor-merge-mock-fixture-token-0001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

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
        {
          id: 'wt-plain',
          path: '/srv/plain-copy',
          branch: 'plain-branch',
          head: '0f1e2d3c4b5a',
          is_main: false,
          managed: false,
          locked: false,
        },
      ],
    },
  ]
}

/** 已经达成、改动还留在工作目录里的目标。 */
const ACHIEVED_GOAL: GoalDetail = {
  id: 'goal-mock-achieved-1',
  repo_id: 'repo-conveyor',
  worktree_id: 'wt-fix-console',
  title: '首页加载时间降到 1 秒以内',
  outcome: '首页加载时间降到 1 秒以内，并且所有测试通过',
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
      task_id: 'task-done-1',
      prompt: '复验加载时间并补上移动端验收步骤',
      decision: 'achieved',
      decision_reason: '加载时间已达标，测试全部通过',
      started_at: '2026-11-11T09:20:00+00:00',
      finished_at: '2026-11-11T09:39:00+00:00',
      task_final_state: 'completed',
      task_state: 'completed',
    },
    {
      n: 1,
      task_id: 'task-failed-1',
      prompt: '把首页加载时间降到 1 秒以内',
      decision: 'continue',
      decision_reason: '首屏时间有下降，但测试还没跑完',
      started_at: '2026-11-11T09:00:30+00:00',
      finished_at: '2026-11-11T09:09:00+00:00',
      task_final_state: 'failed',
      task_state: 'failed',
    },
  ],
}

/** 还在跑的目标：合并入口必须藏起来。 */
const RUNNING_GOAL: GoalDetail = {
  ...ACHIEVED_GOAL,
  id: 'goal-mock-running-1',
  state: 'running',
  summary: '第 1 轮已派发：正在实现并自测。',
  stop_reason: '',
  iterations_used: 1,
  iterations: [
    {
      n: 1,
      task_id: 'task-running-1',
      prompt: '把首页加载时间降到 1 秒以内，并保证所有测试通过',
      decision: '',
      decision_reason: '',
      started_at: '2026-11-11T09:00:30+00:00',
      finished_at: '',
      task_final_state: 'running',
      task_state: 'running',
    },
  ],
}

// 任务必须落在真实的工作目录路径上，否则会被 isTaskInWorktreeContext 过滤掉。
const WORKTREE_PATHS: Record<string, string> = {
  'wt-fix-console': '/srv/repos/conveyor-wt/fix-console',
  'wt-main': '/srv/repos/conveyor',
  'wt-plain': '/srv/plain-copy',
}

function makeTask(id: string, worktreeId: string, state: string, goal: string): SupervisorTaskDetail {
  return {
    id,
    goal,
    repo_path: '/srv/repos/conveyor',
    worktree_path: WORKTREE_PATHS[worktreeId] || `/srv/repos/conveyor-wt/${worktreeId}`,
    branch: 'fix/console',
    state,
    stage: 'implementation',
    repair_round: 0,
    max_repair_rounds: 2,
    created_at: '2026-11-11T09:00:30+00:00',
    updated_at: '2026-11-11T09:39:00+00:00',
    metadata: { repo_id: 'repo-conveyor', worktree_id: worktreeId },
    handoffs: [],
    executions: [],
    acceptance: null,
    summary: '',
  }
}

const DONE_TASK = makeTask('task-done-1', 'wt-fix-console', 'completed', '把首页加载时间降到 1 秒以内')
const RUNNING_TASK = makeTask('task-running-1', 'wt-fix-console', 'running', '把首页加载时间降到 1 秒以内')
const MAIN_DONE_TASK = makeTask('task-main-done', 'wt-main', 'completed', '主工作目录上的一个已完成任务')
const PLAIN_DONE_TASK = makeTask('task-plain-done', 'wt-plain', 'completed', '未托管目录上的一个已完成任务')

/** 可以合并：分支上有 4 个文件改动，主分支是 main。 */
const READY_PREVIEW: SupervisorMergePreview = {
  worktree_id: 'wt-fix-console',
  repo_id: 'repo-conveyor',
  source_branch: 'fix/console',
  target_branch: 'main',
  can_merge: true,
  blockers: [],
  files: [
    { path: 'web/src/App.tsx', status: 'M', additions: 12, deletions: 3 },
    { path: 'web/src/ux/supervisor.css', status: 'M', additions: 40, deletions: 1 },
    { path: 'web/src/components/SupervisorMerge.tsx', status: 'A', additions: 90, deletions: 0 },
    { path: 'docs/supervisor.md', status: 'M', additions: 5, deletions: 2 },
  ],
  commits_ahead: 2,
  has_uncommitted: false,
}

/** 合并之后：这个工作目录里再也没有可合并的改动。 */
const NOTHING_PREVIEW: SupervisorMergePreview = {
  ...READY_PREVIEW,
  can_merge: true,
  blockers: [],
  files: [],
  commits_ahead: 0,
  has_uncommitted: false,
}

const BLOCKED_PREVIEW: SupervisorMergePreview = {
  ...READY_PREVIEW,
  can_merge: false,
  blockers: [
    { code: 'target_dirty', message: '主工作目录里有还没提交的改动，为了不弄乱它，先不合并' },
    { code: 'target_not_checked_out', message: '主工作目录当前不在 main 分支上，请先切回去再合并' },
  ],
}

async function installMergeMockFixture(
  page: Page,
  options: {
    initialGoalsByWt?: Record<string, GoalDetail[]>
    initialTasksByWt?: Record<string, SupervisorTaskDetail[]>
    preview?: SupervisorMergePreview
    mergeResponse?: { status: number; body: Record<string, unknown> }
  } = {},
) {
  // Deep-clone every seeded fixture: route handlers mutate state, and tests share them.
  const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
  const goalsByWt: Record<string, GoalDetail[]> = {}
  for (const [wtId, list] of Object.entries({
    'wt-fix-console': [],
    'wt-main': [],
    'wt-plain': [],
    ...(options.initialGoalsByWt || {}),
  })) {
    goalsByWt[wtId] = (list || []).map(clone)
  }
  const tasksByWt: Record<string, SupervisorTaskDetail[]> = {
    'wt-fix-console': [],
    'wt-main': [],
    'wt-plain': [],
    ...(options.initialTasksByWt || {}),
  }
  const previewRequests: string[] = []
  const mergeCalls: Array<{ id: string; body: Record<string, unknown> }> = []
  let preview = options.preview ? clone(options.preview) : clone(READY_PREVIEW)
  const mergeResponse = options.mergeResponse ?? {
    status: 200,
    body: { merged: true, target_branch: 'main', merge_commit: '9f8e7d6c5b4a', files_changed: 4 },
  }

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
      const sse = [
        `event: session\ndata: ${JSON.stringify({ session_id: 'web:web-console:wt-fix-console' })}\n\n`,
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
      return json({ deployments: [], count: 0 })
    }
    if (path === '/api/supervisor/goals' && method === 'GET') {
      const all = Object.values(goalsByWt).flat()
      return json({ goals: all, count: all.length })
    }
    const goalDetailMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)$/)
    if (goalDetailMatch && method === 'GET') {
      const found = Object.values(goalsByWt).flat().find(g => g.id === goalDetailMatch[1])
      return found ? json(found) : json({ error: '目标不存在' }, 404)
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
    const taskDiffMatch = path.match(/^\/api\/supervisor\/tasks\/([^/]+)\/diff$/)
    if (taskDiffMatch && method === 'GET') {
      return json({ task_id: taskDiffMatch[1], files: [], diff: '', truncated: false, untracked: [] })
    }

    // ---- 「合并到主分支」 contract ----
    const previewMatch = path.match(/^\/api\/supervisor\/worktrees\/([^/]+)\/merge-preview$/)
    if (previewMatch && method === 'GET') {
      previewRequests.push(previewMatch[1])
      return json({ ...preview, worktree_id: previewMatch[1] })
    }
    const mergeMatch = path.match(/^\/api\/supervisor\/worktrees\/([^/]+)\/merge$/)
    if (mergeMatch && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      mergeCalls.push({ id: mergeMatch[1], body })
      if (mergeResponse.status !== 200) return json(mergeResponse.body, mergeResponse.status)
      // 合并成功后这个工作目录就没什么可合并的了：预览再读一次要说这句话。
      preview = NOTHING_PREVIEW
      return json(mergeResponse.body, 200)
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return { previewRequests, mergeCalls }
}

test('mock fixture: nothing to merge says so in one Chinese line and offers no merge button', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [ACHIEVED_GOAL] },
    preview: NOTHING_PREVIEW,
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await expect(card).toContainText('已达成')
  const toggle = card.getByRole('button', { name: '合并到主分支…' })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  // 还没点开：预览一次都没读过。
  expect(fixture.previewRequests).toHaveLength(0)
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')

  const mergeCard = page.getByRole('region', { name: '合并到主分支' })
  await expect(mergeCard).toContainText('这个工作目录还没有可以合并的改动。')
  await expect(mergeCard.getByRole('button', { name: '重新检查' })).toHaveCount(0)
  await expect(mergeCard.getByRole('button', { name: '合并到 main' })).toHaveCount(0)
  expect(fixture.previewRequests).toEqual(['wt-fix-console'])
  expect(fixture.mergeCalls).toHaveLength(0)
})

test('mock fixture: blockers are listed with no merge button and 重新检查 re-reads the preview', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [ACHIEVED_GOAL] },
    preview: BLOCKED_PREVIEW,
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await card.getByRole('button', { name: '合并到主分支…' }).click()

  const mergeCard = page.getByRole('region', { name: '合并到主分支' })
  await expect(mergeCard).toContainText('主工作目录里有还没提交的改动，为了不弄乱它，先不合并')
  await expect(mergeCard).toContainText('主工作目录当前不在 main 分支上，请先切回去再合并')
  await expect(mergeCard.getByRole('button', { name: '合并到 main' })).toHaveCount(0)
  expect(fixture.mergeCalls).toHaveLength(0)

  await mergeCard.getByRole('button', { name: '重新检查' }).click()
  await expect(mergeCard).toContainText('主工作目录里有还没提交的改动，为了不弄乱它，先不合并')
  expect(fixture.previewRequests).toHaveLength(2)
})

test('mock fixture: the happy path posts {confirm:true} only after 确定合并 and shows the short sha', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [ACHIEVED_GOAL] },
    initialTasksByWt: { 'wt-fix-console': [DONE_TASK] },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  await card.getByRole('button', { name: '合并到主分支…' }).click()

  const mergeCard = page.getByRole('region', { name: '合并到主分支' })
  await expect(mergeCard).toContainText('把 fix/console 里的 4 个文件改动合并到 main')
  await expect(mergeCard.locator('.changed-file')).toHaveCount(4)
  await expect(mergeCard.locator('.changed-file-name').first()).toHaveText('App.tsx')

  // 先问清楚，再动手：点「合并到 main」只是弹出确认，还没有发请求。
  await mergeCard.getByRole('button', { name: '合并到 main' }).click()
  await expect(mergeCard).toContainText('合并后主分支 main 会包含这些改动。不会推送到远程。确定合并吗？')
  expect(fixture.mergeCalls).toHaveLength(0)

  // 取消：什么都不发，按钮回来。
  await mergeCard.getByRole('button', { name: '取消' }).click()
  await expect(mergeCard).not.toContainText('不会推送到远程。确定合并吗？')
  await expect(mergeCard.getByRole('button', { name: '合并到 main' })).toBeVisible()
  expect(fixture.mergeCalls).toHaveLength(0)

  await mergeCard.getByRole('button', { name: '合并到 main' }).click()
  await mergeCard.getByRole('button', { name: '确定合并' }).click()

  await expect(mergeCard).toContainText('已合并到 main（提交 9f8e7d6）。')
  expect(fixture.mergeCalls).toHaveLength(1)
  expect(fixture.mergeCalls[0].id).toBe('wt-fix-console')
  // 只带 confirm:true，不多带一个字段。
  expect(fixture.mergeCalls[0].body).toEqual({ confirm: true })
  // 合并后预览重新读过一次：这回没有什么可合并的了。
  await expect(mergeCard).toContainText('这个工作目录还没有可以合并的改动。')
  expect(fixture.previewRequests).toEqual(['wt-fix-console', 'wt-fix-console'])
})

test('mock fixture: 任务与结果 offers the same collapsed 合并到主分支… entry for a completed task', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialTasksByWt: { 'wt-fix-console': [DONE_TASK] },
  })
  await page.goto('/#code')

  const taskCard = page.getByRole('region', { name: '当前任务进度与结果' })
  await expect(taskCard).toContainText('已完成')

  const toggle = page.getByRole('button', { name: '合并到主分支…' })
  await expect(toggle).toBeVisible()
  expect(fixture.previewRequests).toHaveLength(0)
  await toggle.click()

  const mergeCard = page.getByRole('region', { name: '合并到主分支' })
  await expect(mergeCard).toContainText('把 fix/console 里的 4 个文件改动合并到 main')
  expect(fixture.previewRequests).toEqual(['wt-fix-console'])

  await mergeCard.getByRole('button', { name: '合并到 main' }).click()
  await mergeCard.getByRole('button', { name: '确定合并' }).click()
  await expect(mergeCard).toContainText('已合并到 main（提交 9f8e7d6）。')
  expect(fixture.mergeCalls).toHaveLength(1)
})

test('mock fixture: merge_conflict shows the Chinese error plus 主分支没有被改动', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialTasksByWt: { 'wt-fix-console': [DONE_TASK] },
    mergeResponse: {
      status: 409,
      body: {
        error: '合并有冲突，已经撤销，主工作目录没有变化。可以让执行者在工作目录里先合并主分支再试。',
        code: 'merge_conflict',
        conflicts: ['web/src/App.tsx'],
      },
    },
  })
  await page.goto('/#code')

  await page.getByRole('button', { name: '合并到主分支…' }).click()
  const mergeCard = page.getByRole('region', { name: '合并到主分支' })
  await mergeCard.getByRole('button', { name: '合并到 main' }).click()
  await mergeCard.getByRole('button', { name: '确定合并' }).click()

  await expect(mergeCard).toContainText('没有合并成功')
  await expect(mergeCard).toContainText('合并有冲突，已经撤销，主工作目录没有变化。可以让执行者在工作目录里先合并主分支再试。')
  await expect(mergeCard).toContainText('主分支没有被改动。')
  expect(fixture.mergeCalls).toHaveLength(1)
  expect(fixture.mergeCalls[0].body).toEqual({ confirm: true })
  // 冲突之后按钮还在，可以改了再合。
  await expect(mergeCard.getByRole('button', { name: '合并到 main' })).toBeVisible()
})

test('mock fixture: a 400 without confirm becomes one Chinese error line', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialTasksByWt: { 'wt-fix-console': [DONE_TASK] },
    mergeResponse: { status: 400, body: { error: '请确认后再合并（confirm: true）', code: 'confirm_required' } },
  })
  await page.goto('/#code')

  await page.getByRole('button', { name: '合并到主分支…' }).click()
  const mergeCard = page.getByRole('region', { name: '合并到主分支' })
  await mergeCard.getByRole('button', { name: '合并到 main' }).click()
  await mergeCard.getByRole('button', { name: '确定合并' }).click()

  await expect(mergeCard).toContainText('请确认后再合并（confirm: true）')
  await expect(mergeCard).not.toContainText('主分支没有被改动。')
  expect(fixture.mergeCalls).toHaveLength(1)
})

test('mock fixture: the entry stays hidden while a task is running', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialTasksByWt: { 'wt-fix-console': [RUNNING_TASK] },
  })
  await page.goto('/#code')

  // 任务还在跑：工作目录锁定中，没有合并入口。
  await expect(page.getByRole('region', { name: '当前任务进度与结果' })).toContainText('执行中')
  await expect(page.getByText('当前工作目录已锁定（任务执行中）')).toBeVisible()
  await expect(page.getByRole('button', { name: '合并到主分支…' })).toHaveCount(0)
  expect(fixture.previewRequests).toHaveLength(0)
  expect(fixture.mergeCalls).toHaveLength(0)
})

test('mock fixture: the entry stays hidden while a 目标 is running', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [RUNNING_GOAL] },
    initialTasksByWt: { 'wt-fix-console': [DONE_TASK] },
  })
  await page.goto('/#code')

  // 目标还在跑：自动切到「目标」页，结果卡还没出现，没有合并入口。
  await expect(page.getByRole('tab', { name: '目标' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('region', { name: '目标进度' })).toContainText('进行中')
  await expect(page.getByRole('button', { name: '合并到主分支…' })).toHaveCount(0)

  // 手动翻到「任务与结果」：这一轮虽然做完了，目标还在跑，依旧不给合并入口。
  await page.getByRole('tab', { name: '任务与结果' }).click()
  await expect(page.getByRole('region', { name: '当前任务进度与结果' })).toContainText('已完成')
  await expect(page.getByRole('button', { name: '合并到主分支…' })).toHaveCount(0)
  expect(fixture.previewRequests).toHaveLength(0)
  expect(fixture.mergeCalls).toHaveLength(0)
})

test('mock fixture: the entry never shows on the main checkout or an unmanaged worktree', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installMergeMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [DONE_TASK],
      'wt-main': [MAIN_DONE_TASK],
      'wt-plain': [PLAIN_DONE_TASK],
    },
  })
  await page.goto('/#code')

  // 受托管的工作目录：有入口。
  await expect(page.getByRole('button', { name: '合并到主分支…' })).toBeVisible()

  // 主分支（主工作目录）：只读，没有入口。
  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()
  await expect(page.getByRole('heading', { name: 'Conveyor / main' })).toBeVisible()
  await expect(page.getByText('当前为只读工作目录（主分支）')).toBeVisible()
  await expect(page.getByRole('button', { name: '合并到主分支…' })).toHaveCount(0)

  // 未托管目录：同样没有入口。
  await page.locator('.sup-worktree-item').filter({ hasText: 'plain-branch' }).first().click()
  await expect(page.getByRole('heading', { name: 'Conveyor / plain-branch' })).toBeVisible()
  await expect(page.getByText('当前为只读工作目录（未托管目录）')).toBeVisible()
  await expect(page.getByRole('button', { name: '合并到主分支…' })).toHaveCount(0)
  expect(fixture.previewRequests).toHaveLength(0)
})

test('mock fixture: phone 390px shows the merge preview without horizontal overflow', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installMergeMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [ACHIEVED_GOAL] },
    initialTasksByWt: { 'wt-fix-console': [DONE_TASK] },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '目标进度' })
  const toggle = card.getByRole('button', { name: '合并到主分支…' })
  await expect(toggle).toBeVisible()
  const toggleBox = await toggle.boundingBox()
  expect(toggleBox).not.toBeNull()
  expect(toggleBox!.height).toBeGreaterThanOrEqual(40)
  await toggle.click()

  const mergeCard = page.getByRole('region', { name: '合并到主分支' })
  await expect(mergeCard).toContainText('把 fix/console 里的 4 个文件改动合并到 main')
  await mergeCard.scrollIntoViewIfNeeded()
  const mergeButton = mergeCard.getByRole('button', { name: '合并到 main' })
  await expect(mergeButton).toBeVisible()
  const mergeBox = await mergeButton.boundingBox()
  expect(mergeBox).not.toBeNull()
  expect(mergeBox!.height).toBeGreaterThanOrEqual(40)

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  expect(overflow.scroll).toBeLessThanOrEqual(390)
  await page.screenshot({ path: test.info().outputPath('sup-i2-merge-390.png') })
})

test('mock fixture: the 目标 tab shows no English merge/confirm words', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installMergeMockFixture(page, {
    initialGoalsByWt: { 'wt-fix-console': [ACHIEVED_GOAL] },
    initialTasksByWt: { 'wt-fix-console': [DONE_TASK] },
  })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()
  await page.getByRole('button', { name: '合并到主分支…' }).click()

  const pane = page.locator('.sup-goals-scroll')
  await expect(pane).toBeVisible()
  // 文件路径是数据不是界面用词（mock 里有 SupervisorMerge.tsx 这种名字）：先把改动文件
  // 列表的行和逐行差异整块摘掉，再收剩下的可见文字，保证查的是界面文案本身。
  const visibleText = await pane.evaluate(paneEl => {
    const clone = paneEl.cloneNode(true) as HTMLElement
    clone
      .querySelectorAll(
        [
          // ChangedFileList（TaskParts.tsx）：改动文件列表的行与路径
          '.changed-files', '.changed-file', '.changed-file-path', '.changed-file-name', '.changed-file-dir',
          // DiffBlocks（SupervisorDiff.tsx）：折叠的逐文件差异行与路径
          '.sup-changes-files', '.sup-changes-file', '.sup-changes-file-path',
          // DiffBody / DiffStat（TaskParts.tsx）：逐行差异与 +/- 统计
          '.diff-body', '.diff-lines', '.diff-line', '.diff-truncated', '.diff-stat',
        ].join(', '),
      )
      .forEach(node => node.remove())
    return clone.innerText
  })
  expect(visibleText).not.toMatch(/merge/i)
  expect(visibleText).not.toMatch(/confirm/i)
  expect(visibleText).not.toMatch(/worktree/i)
  expect(visibleText).toContain('合并到主分支')
})
