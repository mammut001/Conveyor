import { expect, test, type Page, type Route } from '@playwright/test'
import type { SupervisorCapabilities, SupervisorRepo, SupervisorTaskDetail } from '../src/components/SupervisorWorkspace'
import type { SupervisorMergePreview } from '../src/components/SupervisorMerge'

// NOTE: 工作目录的文件夹丢了（R1）。All tests mock the /api/supervisor/* contract and verify
// frontend wiring only: the 侧栏 card for a missing worktree, 「恢复工作目录」 posting to
// POST /api/supervisor/worktrees/<id>/restore and then selecting the restored worktree, the
// 目标 / 任务与结果 forms being replaced by the missing card, and 「合并到主分支」 answering
// with the one actionable Chinese sentence instead of a git error.

const TOKEN = 'r1-worktree-restore-mock-fixture-token-0001'
const DESKTOP = { width: 1440, height: 900 }

const MISSING_WORKTREE_ID = 'wt-sup-114aa945'
const MISSING_BRANCH = 'sup/114aa945'
const MISSING_PATH = '/srv/conveyor-tasks/worktrees/supervisor/sup-114aa945'
const RESTORED_PATH = MISSING_PATH

const MISSING_NOTE = `文件夹丢了，改动还在分支 ${MISSING_BRANCH} 上`
const MERGE_MISSING_NOTE = '工作目录的文件夹丢了，请先在 项目 里点「恢复工作目录」'

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
    source_machine: 'vps',
  }
}

/** 恢复之前：主分支 + 一个文件夹丢了的受托管工作目录。 */
function missingWorktree(state: 'missing' | 'ready' = 'missing'): SupervisorRepo['worktrees'][number] {
  return {
    id: MISSING_WORKTREE_ID,
    path: RESTORED_PATH,
    branch: MISSING_BRANCH,
    head: '114aa9450aa1',
    is_main: false,
    managed: true,
    locked: false,
    chat_session_id: `web:web-console:${MISSING_WORKTREE_ID}`,
    state,
    missing: state === 'missing',
    ahead_of_base: state === 'missing' ? 3 : 0,
  }
}

function createInitialRepos(state: 'missing' | 'ready' = 'missing'): SupervisorRepo[] {
  return [
    {
      id: 'repo-conveyor',
      name: 'Conveyor',
      path: '/srv/repos/conveyor',
      source_machine: 'vps',
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
          state: 'ready',
          missing: false,
        },
        missingWorktree(state),
      ],
    },
  ]
}

const WORKTREE_PATHS: Record<string, string> = {
  'wt-main': '/srv/repos/conveyor',
  [MISSING_WORKTREE_ID]: MISSING_PATH,
}

function makeTask(id: string, worktreeId: string, state: string, goal: string): SupervisorTaskDetail {
  return {
    id,
    goal,
    repo_path: '/srv/repos/conveyor',
    worktree_path: WORKTREE_PATHS[worktreeId] || MISSING_PATH,
    branch: MISSING_BRANCH,
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

const DONE_TASK = makeTask('task-done-1', MISSING_WORKTREE_ID, 'completed', '把首页加载时间降到 1 秒以内')

/** 文件夹丢了：预览只说要先恢复，别的什么都不敢说。 */
const MISSING_PREVIEW: SupervisorMergePreview = {
  worktree_id: MISSING_WORKTREE_ID,
  repo_id: 'repo-conveyor',
  source_branch: MISSING_BRANCH,
  target_branch: 'main',
  can_merge: false,
  blockers: [{ code: 'worktree_folder_missing', message: MERGE_MISSING_NOTE }],
  files: [],
  commits_ahead: 3,
  has_uncommitted: false,
}

async function installRestoreMockFixture(
  page: Page,
  options: {
    initialTasksByWt?: Record<string, SupervisorTaskDetail[]>
    restoreResponse?: { status: number; body: Record<string, unknown> }
  } = {},
) {
  const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
  const tasksByWt: Record<string, SupervisorTaskDetail[]> = {
    'wt-main': [],
    [MISSING_WORKTREE_ID]: [],
    ...(options.initialTasksByWt || {}),
  }
  const restoreCalls: Array<{ id: string; headers: Record<string, string> }> = []
  const previewRequests: string[] = []
  let worktreeState: 'missing' | 'ready' = 'missing'
  const restoreResponse = options.restoreResponse ?? {
    status: 200,
    body: missingWorktree('ready'),
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
        `event: session\ndata: ${JSON.stringify({ session_id: `web:web-console:${MISSING_WORKTREE_ID}` })}\n\n`,
        'event: done\ndata: {}\n\n',
      ].join('')
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
    }

    // Supervisor contract endpoints
    if (path === '/api/supervisor/capabilities' && method === 'GET') {
      return json(createMockCapabilities())
    }
    if (path === '/api/supervisor/repositories' && method === 'GET') {
      return json({ repositories: createInitialRepos(worktreeState) })
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
      return json({ goals: [], count: 0 })
    }
    const validationMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/validation$/)
    if (validationMatch && method === 'GET') {
      return json({ repo_id: validationMatch[1], candidates: [], selected: [], source: 'none', env_commands: [], effective: [] })
    }
    const taskDiffMatch = path.match(/^\/api\/supervisor\/tasks\/([^/]+)\/diff$/)
    if (taskDiffMatch && method === 'GET') {
      return json({ task_id: taskDiffMatch[1], files: [], diff: '', truncated: false, untracked: [] })
    }

    // ---- 「恢复工作目录」 contract ----
    const restoreMatch = path.match(/^\/api\/supervisor\/worktrees\/([^/]+)\/restore$/)
    if (restoreMatch && method === 'POST') {
      restoreCalls.push({
        id: restoreMatch[1],
        headers: {
          xRequestedWith: request.headers()['x-requested-with'] || '',
          authorization: request.headers()['authorization'] || '',
        },
      })
      if (restoreResponse.status !== 200) return json(restoreResponse.body, restoreResponse.status)
      // 恢复成功：服务器回到原来的路径，工作目录重新可用。
      worktreeState = 'ready'
      return json(restoreResponse.body, 200)
    }
    const previewMatch = path.match(/^\/api\/supervisor\/worktrees\/([^/]+)\/merge-preview$/)
    if (previewMatch && method === 'GET') {
      previewRequests.push(previewMatch[1])
      return json(MISSING_PREVIEW)
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return { restoreCalls, previewRequests }
}

test('mock fixture: a missing worktree says the branch still holds the work and offers 恢复工作目录', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installRestoreMockFixture(page)
  await page.goto('/#code')

  const item = page.locator('.sup-worktree-item--missing')
  await expect(item).toBeVisible()
  await expect(item).toContainText(MISSING_NOTE)
  await expect(item).toContainText('文件夹丢了')
  // 恢复之前什么都不能在这里开始。
  await expect(item.getByRole('button', { name: '恢复工作目录' })).toBeVisible()
  expect(fixture.restoreCalls).toHaveLength(0)
})

test('mock fixture: 恢复工作目录 posts to the restore route and then selects the worktree', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installRestoreMockFixture(page, {
    initialTasksByWt: { [MISSING_WORKTREE_ID]: [DONE_TASK] },
  })
  await page.goto('/#code')

  await page.locator('.sup-worktree-item--missing').getByRole('button', { name: '恢复工作目录' }).click()

  // 真的发了请求，而且带着页面的同一站点头。
  await expect.poll(() => fixture.restoreCalls.length).toBe(1)
  expect(fixture.restoreCalls[0].id).toBe(MISSING_WORKTREE_ID)
  expect(fixture.restoreCalls[0].headers.xRequestedWith).toBe('XMLHttpRequest')
  expect(fixture.restoreCalls[0].headers.authorization).toBe(`Bearer ${TOKEN}`)

  // 恢复完就选中它：标题回到这个工作目录，文件夹丢了的说法消失。
  await expect(page.getByRole('heading', { name: `Conveyor / ${MISSING_BRANCH}` })).toBeVisible()
  await expect(page.locator('.sup-worktree-item--missing')).toHaveCount(0)
  await expect(page.locator('.sup-context-missing')).toHaveCount(0)
  await expect(page.getByRole('region', { name: '工作目录文件夹丢了' })).toHaveCount(0)
  await expect(page.locator('.sup-worktree-item').filter({ hasText: MISSING_BRANCH }).first()).toContainText('可以开始')
})

test('mock fixture: while the folder is missing the 任务与结果 form is replaced by the restore card', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installRestoreMockFixture(page, {
    initialTasksByWt: { [MISSING_WORKTREE_ID]: [DONE_TASK] },
  })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '工作目录文件夹丢了' })
  await expect(card).toBeVisible()
  await expect(card).toContainText(MISSING_NOTE)
  // 表单不在了：不能往一个不存在的文件夹里派发。
  await expect(page.getByRole('region', { name: '任务目标与派发' })).toHaveCount(0)
  await expect(page.getByLabel('任务目标')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开始执行' })).toHaveCount(0)
  await expect(card.getByRole('button', { name: '恢复工作目录' })).toBeVisible()
})

test('mock fixture: the 目标 tab shows the same message instead of the goal form', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installRestoreMockFixture(page)
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const card = page.getByRole('region', { name: '工作目录文件夹丢了' })
  await expect(card).toBeVisible()
  await expect(card).toContainText(MISSING_NOTE)
  await expect(page.locator('#sup-goal-outcome')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开始' })).toHaveCount(0)
})

test('mock fixture: 合并到主分支 answers with the restore sentence while the folder is missing', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installRestoreMockFixture(page, {
    initialTasksByWt: { [MISSING_WORKTREE_ID]: [DONE_TASK] },
  })
  await page.goto('/#code')

  await page.getByRole('button', { name: '合并到主分支…' }).click()
  const mergeCard = page.getByRole('region', { name: '合并到主分支' })
  await expect(mergeCard).toContainText(MERGE_MISSING_NOTE)
  // 只有一个按钮能点，就是「重新检查」；合并不可能出现。
  await expect(mergeCard.getByRole('button', { name: '合并到 main' })).toHaveCount(0)
  await expect(mergeCard.getByRole('button', { name: '重新检查' })).toBeVisible()
  expect(fixture.previewRequests).toEqual([MISSING_WORKTREE_ID])
})

test('mock fixture: a failed restore keeps the card and says so in one Chinese line', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installRestoreMockFixture(page, {
    restoreResponse: { status: 409, body: { error: '这个工作目录的分支已经不在了，没有办法恢复；仓库里的提交没有被动过', code: 'branch_missing' } },
  })
  await page.goto('/#code')

  await page.locator('.sup-worktree-item--missing').getByRole('button', { name: '恢复工作目录' }).click()

  const card = page.getByRole('region', { name: '工作目录文件夹丢了' })
  await expect(card).toContainText('没有恢复成功')
  await expect(card).toContainText('这个工作目录的分支已经不在了，没有办法恢复；仓库里的提交没有被动过')
  // 失败之后还是丢了的状态，还能再点一次。
  await expect(page.locator('.sup-worktree-item--missing')).toBeVisible()
  await expect(card.getByRole('button', { name: '恢复工作目录' })).toBeEnabled()
})

test('mock fixture: the missing worktree shows no English worktree/restore words', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installRestoreMockFixture(page)
  await page.goto('/#code')

  // 老板看得见的三处：侧栏那一行、顶部标题栏、代替表单的那张卡。
  for (const selector of ['.sup-worktree-item--missing', '.sup-context-bar', '.sup-missing-card']) {
    const locator = page.locator(selector)
    await expect(locator).toBeVisible()
    const visibleText = await locator.evaluate(el => (el as HTMLElement).innerText)
    expect(visibleText, selector).not.toMatch(/worktree/i)
    expect(visibleText, selector).not.toMatch(/restore/i)
    expect(visibleText, selector).not.toMatch(/missing/i)
  }
  await expect(page.getByRole('region', { name: '工作目录文件夹丢了' })).toContainText('恢复工作目录')
  await expect(page.getByRole('region', { name: '工作目录文件夹丢了' })).toContainText(MISSING_NOTE)
})
