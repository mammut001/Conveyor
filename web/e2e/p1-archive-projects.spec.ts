import { expect, test, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorRepo,
} from '../src/components/SupervisorWorkspace'
import type { GoalDetail } from '../src/components/SupervisorGoals'

const TOKEN = 'p1-archive-projects-mock-token-0001'
const DESKTOP = { width: 1440, height: 900 }

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

function createRepos(): SupervisorRepo[] {
  return [
    {
      id: 'repo-real',
      name: 'Conveyor',
      path: '/srv/repos/conveyor',
      source_machine: 'dev-workstation',
      validation_source: 'repo',
      archived_at: null,
      worktrees: [
        {
          id: 'wt-real-main',
          path: '/srv/repos/conveyor',
          branch: 'main',
          head: '8352f46a9c01',
          is_main: true,
          managed: false,
          locked: false,
          created_at: '2026-10-01T00:00:00Z',
        },
        {
          id: 'wt-real-work',
          path: '/srv/repos/conveyor-wt/feat-clean',
          branch: 'feat/clean',
          head: 'a1b2c3d4e5f6',
          is_main: false,
          managed: true,
          locked: false,
          created_at: '2026-10-02T00:00:00Z',
          chat_session_id: 'web:web-console:wt-real-work',
        },
      ],
    },
    {
      id: 'repo-probe',
      name: 'Handoff探针1114',
      path: '/srv/projects/Handoff-1114',
      source_machine: 'dev-workstation',
      validation_source: 'repo',
      archived_at: null,
      worktrees: [
        {
          id: 'wt-probe-main',
          path: '/srv/projects/Handoff-1114',
          branch: 'main',
          head: '111111111111',
          is_main: true,
          managed: false,
          locked: false,
          created_at: '2026-10-03T00:00:00Z',
        },
        {
          id: 'wt-probe-1',
          path: '/srv/projects/Handoff-1114/.worktrees/wt-1',
          branch: 'sup/probe-1',
          head: '222222222222',
          is_main: false,
          managed: true,
          locked: false,
          created_at: '2026-11-14T00:00:00Z',
          chat_session_id: 'web:web-console:wt-probe-1',
        },
      ],
    },
  ]
}

async function installFixture(
  page: Page,
  options: {
    initialArchivedIds?: string[]
    archiveBusyError?: string
    goals?: GoalDetail[]
    savedContext?: { repoId: string; worktreeId: string }
  } = {},
) {
  const repos = createRepos()
  for (const id of options.initialArchivedIds || []) {
    const target = repos.find(r => r.id === id)
    if (target) target.archived_at = '2026-11-15T10:00:00Z'
  }
  const goals = options.goals || []
  const archiveCalls: Array<{ id: string; xRequestedWith: string | null }> = []
  const unarchiveCalls: Array<{ id: string; xRequestedWith: string | null }> = []
  let nextArchiveError = options.archiveBusyError || ''

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
      const includeArchived = url.searchParams.get('include_archived') === '1'
      const list = includeArchived ? repos : repos.filter(r => !r.archived_at)
      return json({ repositories: list })
    }
    if (path === '/api/supervisor/tasks' && method === 'GET') {
      return json({ tasks: [], count: 0 })
    }
    if (path === '/api/supervisor/deployments' && method === 'GET') {
      return json({ deployments: [], count: 0 })
    }
    if (path === '/api/supervisor/goals' && method === 'GET') {
      return json({ goals, count: goals.length })
    }
    if (/^\/api\/supervisor\/repositories\/[^/]+\/validation$/.test(path) && method === 'GET') {
      return json({
        repo_id: path.split('/')[4],
        candidates: [],
        selected: [],
        source: 'repo',
        env_commands: [],
        effective: [],
      })
    }

    const archiveMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/archive$/)
    if (archiveMatch && method === 'POST') {
      const repoId = archiveMatch[1]
      archiveCalls.push({ id: repoId, xRequestedWith: request.headers()['x-requested-with'] || null })
      if (nextArchiveError) {
        const msg = nextArchiveError
        nextArchiveError = ''
        return json({ error: msg, code: 'repository_busy' }, 409)
      }
      const repo = repos.find(r => r.id === repoId)
      if (!repo) return json({ error: '找不到这个仓库', code: 'repository_not_found' }, 404)
      repo.archived_at = '2026-11-17T12:00:00Z'
      return json(repo)
    }

    const unarchiveMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/unarchive$/)
    if (unarchiveMatch && method === 'POST') {
      const repoId = unarchiveMatch[1]
      unarchiveCalls.push({ id: repoId, xRequestedWith: request.headers()['x-requested-with'] || null })
      const repo = repos.find(r => r.id === repoId)
      if (!repo) return json({ error: '找不到这个仓库', code: 'repository_not_found' }, 404)
      repo.archived_at = null
      return json(repo)
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return { archiveCalls, unarchiveCalls }
}

test('archive confirm moves repo to collapsed 已归档 section and 恢复 brings it back', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installFixture(page)
  await page.goto('/#code')

  const sidebar = page.locator('.sup-repo-sidebar')
  await expect(sidebar.getByRole('button', { name: /^Conveyor(?:\s|\/)/ })).toBeVisible()
  await expect(sidebar.getByRole('button', { name: /^Handoff探针1114(?:\s|\/)/ })).toBeVisible()
  // No delete button anywhere.
  await expect(sidebar.getByRole('button', { name: /删除/ })).toHaveCount(0)
  await expect(sidebar.locator('.sup-archived-section')).toHaveCount(0)

  // Click 归档 on Handoff探针1114 -> confirmation appears.
  const probeNode = sidebar.locator('.sup-repo-node').filter({ hasText: 'Handoff探针1114' })
  await probeNode.getByRole('button', { name: '归档', exact: true }).click()

  const confirmRegion = probeNode.getByRole('region', { name: '确认归档 Handoff探针1114' })
  await expect(confirmRegion).toContainText(
    '归档后这个项目会从列表里隐藏，代码、分支和目标记录都不会删除，随时可以恢复。',
  )

  // Cancel keeps it in active list.
  await confirmRegion.getByRole('button', { name: '取消', exact: true }).click()
  await expect(confirmRegion).toHaveCount(0)
  await expect(sidebar.locator('.sup-repo-node').filter({ hasText: 'Handoff探针1114' })).toBeVisible()

  // Confirm archive -> moves to collapsed 已归档（1）.
  await probeNode.getByRole('button', { name: '归档', exact: true }).click()
  await probeNode.getByRole('button', { name: '确认归档', exact: true }).click()

  expect(fixture.archiveCalls).toEqual([{ id: 'repo-probe', xRequestedWith: 'XMLHttpRequest' }])
  await expect(sidebar.locator('.sup-repo-node').filter({ hasText: 'Handoff探针1114' })).toHaveCount(0)

  const archivedDetails = sidebar.locator('.sup-archived-section')
  await expect(archivedDetails).toBeVisible()
  await expect(archivedDetails).not.toHaveAttribute('open', '')
  await expect(archivedDetails.locator('summary')).toHaveText('已归档（1）')

  // Expand 已归档（1） and click 恢复 -> brings it back to active list.
  await archivedDetails.locator('summary').click()
  const archivedItem = archivedDetails.locator('.sup-archived-item').filter({ hasText: 'Handoff探针1114' })
  await expect(archivedItem).toBeVisible()
  await archivedItem.getByRole('button', { name: '恢复', exact: true }).click()

  expect(fixture.unarchiveCalls).toEqual([{ id: 'repo-probe', xRequestedWith: 'XMLHttpRequest' }])
  await expect(sidebar.locator('.sup-repo-node').filter({ hasText: 'Handoff探针1114' })).toBeVisible()
  await expect(sidebar.locator('.sup-archived-section')).toHaveCount(0)
})

test('initial landing ignores archived repos even with saved context or newer goal', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, {
    initialArchivedIds: ['repo-probe'],
    savedContext: { repoId: 'repo-probe', worktreeId: 'wt-probe-1' },
  })
  await page.goto('/#code')

  // Saved context points to archived repo-probe, so landing falls back to active Conveyor.
  await expect(page.getByRole('heading', { name: 'Conveyor / feat/clean' })).toBeVisible()
  await expect(page.locator('.sup-archived-section summary')).toHaveText('已归档（1）')
})

test('archive refusal shows Chinese 409 error inline and keeps repo in active list', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installFixture(page, {
    archiveBusyError: '这个项目还有正在跑的目标或任务，先停下再归档。',
  })
  await page.goto('/#code')

  const probeNode = page.locator('.sup-repo-node').filter({ hasText: 'Handoff探针1114' })
  await probeNode.getByRole('button', { name: '归档', exact: true }).click()
  await probeNode.getByRole('button', { name: '确认归档', exact: true }).click()

  await expect(probeNode.getByRole('alert')).toHaveText('这个项目还有正在跑的目标或任务，先停下再归档。')
  await expect(probeNode).toBeVisible()
  await expect(page.locator('.sup-archived-section')).toHaveCount(0)
})
