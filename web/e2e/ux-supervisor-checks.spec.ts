import { expect, test, type Locator, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorRepo,
} from '../src/components/SupervisorWorkspace'
import type { SupervisorChecksCandidate, SupervisorChecksState } from '../src/components/SupervisorChecks'

// NOTE: 自动检查 card on the 项目 page. All tests mock the /api/supervisor/* contract and verify
// frontend wiring only: env read-only line, candidate checkboxes (unavailable disabled), save
// payload + 已保存, Chinese-only errors, no-candidates message, 目标 form warning, phone layout,
// and Simplified-Chinese-only visible copy (no "validation" / "argv" / "lint" words).

const TOKEN = 'ux-supervisor-checks-mock-fixture-token-0001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

const CANDIDATES: SupervisorChecksCandidate[] = [
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
  {
    id: 'npm-check',
    label: '运行代码检查（npm run check）',
    argv: ['npm', 'run', 'check'],
    reason: 'package.json 的 scripts.check',
    available: false,
    unavailable_reason: '未找到可执行文件：npm',
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

function createInitialRepos(validationSource: 'env' | 'repo' | 'none'): SupervisorRepo[] {
  return [
    {
      id: 'repo-conveyor',
      name: 'Conveyor',
      path: '/srv/repos/conveyor',
      source_machine: 'dev-workstation',
      validation_source: validationSource,
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

async function installChecksMockFixture(
  page: Page,
  options: {
    mode?: 'candidates' | 'env' | 'no-candidates'
    failNextSave?: 'unknown_check' | 'server'
  } = {},
) {
  const mode = options.mode ?? 'candidates'
  let state: SupervisorChecksState =
    mode === 'env'
      ? {
          repo_id: 'repo-conveyor',
          candidates: CANDIDATES,
          selected: [],
          source: 'env',
          env_commands: ['服务器统一测试', '服务器统一构建'],
          effective: [
            { name: '服务器统一测试', argv: ['make', 'test'] },
            { name: '服务器统一构建', argv: ['make', 'build'] },
          ],
        }
      : mode === 'no-candidates'
        ? {
            repo_id: 'repo-conveyor',
            candidates: [],
            selected: [],
            source: 'none',
            env_commands: [],
            effective: [],
          }
        : {
            repo_id: 'repo-conveyor',
            candidates: CANDIDATES,
            selected: [],
            source: 'none',
            env_commands: [],
            effective: [],
          }

  const repos = createInitialRepos(state.source)
  const saveCalls: Array<{ selected: string[] }> = []
  let repoListCalls = 0
  let failNextSave: 'unknown_check' | 'server' | null = options.failNextSave ?? null

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
      repoListCalls += 1
      return json({ repositories: repos })
    }
    if (path === '/api/supervisor/tasks' && method === 'GET') {
      return json({ tasks: [], count: 0 })
    }
    if (path === '/api/supervisor/deployments' && method === 'GET') {
      return json({ deployments: [], count: 0 })
    }
    if (path === '/api/supervisor/goals' && method === 'GET') {
      return json({ goals: [], count: 0 })
    }

    // ---- 自动检查 contract ----
    const validationMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/validation$/)
    if (validationMatch && method === 'GET') {
      return json(state)
    }
    if (validationMatch && method === 'POST') {
      const body = request.postDataJSON() as { selected?: unknown }
      const selected = Array.isArray(body.selected) ? body.selected.map(String) : []
      saveCalls.push({ selected })
      if (failNextSave === 'unknown_check') {
        failNextSave = null
        return json({ error: 'unknown check id: npm-test', code: 'unknown_check' }, 400)
      }
      if (failNextSave === 'server') {
        failNextSave = null
        return json({ error: 'internal error while storing checks' }, 500)
      }
      if (state.source !== 'env') {
        state = {
          ...state,
          selected,
          source: selected.length > 0 ? 'repo' : 'none',
          effective: selected.map(id => {
            const candidate = state.candidates.find(c => c.id === id)
            return { name: candidate?.label || id, argv: candidate?.argv || [] }
          }),
        }
        repos[0].validation_source = state.source
      }
      return json(state)
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return {
    saveCalls,
    getRepoListCalls: () => repoListCalls,
  }
}

async function expandChecks(page: Page) {
  const summary = page.locator('.sup-checks-summary').first()
  await expect(summary).toBeVisible()
  await summary.click()
  await expect(page.locator('.sup-checks-body').first()).toBeVisible()
}

// The checkbox for one candidate row, addressed by its Chinese label fragment.
function checkFor(card: Locator, label: string) {
  return card.locator('.sup-checks-item', { hasText: label }).locator('input[type="checkbox"]')
}

test('mock fixture: 任务与结果 tab shows the 自动检查 card collapsed above the dispatch card', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installChecksMockFixture(page)
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const summary = page.locator('.sup-checks-summary').first()
  await expect(summary).toContainText('自动检查：未设置')
  // Collapsed by default: the card body stays hidden until the owner opens it.
  await expect(page.locator('.sup-checks').first()).not.toHaveAttribute('open', '')
  await expect(page.locator('.sup-checks-body').first()).toBeHidden()

  // The card sits above the dispatch card inside the same task pane.
  const pane = page.locator('.sup-task-pane')
  const checks = pane.locator('.sup-checks')
  const dispatch = pane.locator('.sup-dispatch-card')
  const checksBox = await checks.boundingBox()
  const dispatchBox = await dispatch.boundingBox()
  expect(checksBox).not.toBeNull()
  expect(dispatchBox).not.toBeNull()
  expect(checksBox!.y + checksBox!.height).toBeLessThanOrEqual(dispatchBox!.y + 1)
})

test('mock fixture: 服务器已配置 source is read-only with no checkboxes', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installChecksMockFixture(page, { mode: 'env' })
  await page.goto('/#code')

  const summary = page.locator('.sup-checks-summary').first()
  await expect(summary).toContainText('自动检查：服务器已配置')

  await expandChecks(page)
  const card = page.locator('.sup-checks-body').first()
  await expect(card).toContainText('由服务器管理员配置：服务器统一测试、服务器统一构建')
  await expect(card.getByRole('checkbox')).toHaveCount(0)
  await expect(card.getByRole('button', { name: '保存' })).toHaveCount(0)
})

test('mock fixture: candidates render as checkboxes and unavailable ones are disabled', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installChecksMockFixture(page)
  await page.goto('/#code')
  await expandChecks(page)

  const card = page.locator('.sup-checks-body').first()
  const boxes = card.getByRole('checkbox')
  await expect(boxes).toHaveCount(3)

  // Labels plus the small grey reason line under each one.
  await expect(card).toContainText('运行测试（npm test）')
  await expect(card).toContainText('package.json 的 scripts.test')
  await expect(card).toContainText('构建项目（npm run build）')
  await expect(card).toContainText('package.json 的 scripts.build')

  const available = checkFor(card, '运行测试')
  const unavailable = checkFor(card, '运行代码检查')
  await expect(available).toBeEnabled()
  await expect(unavailable).toBeDisabled()
  await expect(card.locator('.sup-checks-item.is-unavailable')).toContainText('未找到可执行文件：npm')

  // 保存 stays disabled until the selection actually changes.
  const save = card.getByRole('button', { name: '保存' })
  await expect(save).toBeDisabled()
  await available.check()
  await expect(save).toBeEnabled()
})

test('mock fixture: 保存 posts {selected:[...]}, shows 已保存 and refreshes the repository list', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installChecksMockFixture(page)
  await page.goto('/#code')
  await expandChecks(page)

  const card = page.locator('.sup-checks-body').first()
  const before = fixture.getRepoListCalls()
  await checkFor(card, '构建项目').check()
  await card.getByRole('button', { name: '保存' }).click()

  await expect(card.locator('.sup-checks-saved')).toHaveText('已保存')
  expect(fixture.saveCalls).toHaveLength(1)
  expect(fixture.saveCalls[0]).toEqual({ selected: ['npm-build'] })
  // Saving is idempotent from here: nothing changed, so the button goes quiet again.
  await expect(card.getByRole('button', { name: '保存' })).toBeDisabled()

  // The collapsed summary now counts the selection, and the repo list was re-read.
  await expect(page.locator('.sup-checks-summary').first()).toContainText('自动检查：1 项')
  expect(fixture.getRepoListCalls()).toBeGreaterThan(before)
})

test('mock fixture: a rejected save shows one Chinese line, never the raw backend text', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installChecksMockFixture(page, { failNextSave: 'unknown_check' })
  await page.goto('/#code')
  await expandChecks(page)

  const card = page.locator('.sup-checks-body').first()
  await checkFor(card, '运行测试').check()
  await card.getByRole('button', { name: '保存' }).click()

  const error = card.locator('.sup-checks-save-error')
  await expect(error).toHaveText('这个检查项目已经不在了，请刷新页面后再保存一次。')
  await expect(error).not.toContainText('unknown')
  await expect(card.locator('.sup-checks-saved')).toHaveCount(0)
  expect(fixture.saveCalls).toHaveLength(1)
})

test('mock fixture: an English backend failure still becomes one Chinese line', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installChecksMockFixture(page, { failNextSave: 'server' })
  await page.goto('/#code')
  await expandChecks(page)

  const card = page.locator('.sup-checks-body').first()
  await checkFor(card, '运行测试').check()
  await card.getByRole('button', { name: '保存' }).click()

  await expect(card.locator('.sup-checks-save-error')).toHaveText('保存没有成功，请稍后再试。')
  await expect(card).not.toContainText('internal error')
})

test('mock fixture: a repo without any check command explains what happens next', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installChecksMockFixture(page, { mode: 'no-candidates' })
  await page.goto('/#code')

  await expect(page.locator('.sup-checks-summary').first()).toContainText('自动检查：未设置')
  await expandChecks(page)

  const card = page.locator('.sup-checks-body').first()
  await expect(card).toContainText('没有在这个仓库里找到测试命令。没有自动检查时，每一轮做完都会停下来请你检查。')
  await expect(card.getByRole('checkbox')).toHaveCount(0)
  await expect(card.getByRole('button', { name: '保存' })).toHaveCount(0)
})

test('mock fixture: the 目标 form warns about missing checks and clears the warning after saving one', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installChecksMockFixture(page)
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const pane = page.locator('.sup-goals-scroll')
  const warning = pane.locator('.sup-checks-warning')
  await expect(warning).toHaveText('还没有设置自动检查，每一轮做完都会停下来请你检查。')
  // The warning never blocks 开始.
  await pane.getByRole('textbox', { name: '描述你想要的结果' }).fill('把首页加载时间降到 1 秒以内')
  await expect(pane.getByRole('button', { name: '开始', exact: true })).toBeEnabled()

  // Same compact summary + expandable card lives in the new-goal form.
  await expect(pane.locator('.sup-checks-summary').first()).toContainText('自动检查：未设置')
  await pane.locator('.sup-checks-summary').first().click()
  const card = pane.locator('.sup-checks-body').first()
  await expect(card).toContainText('每一轮改完后会在工作目录里运行勾选的命令，全部通过才算这一轮完成。')

  await checkFor(card, '运行测试').check()
  await card.getByRole('button', { name: '保存' }).click()
  await expect(card.locator('.sup-checks-saved')).toHaveText('已保存')

  await expect(warning).toHaveCount(0)
  await expect(pane.locator('.sup-checks-summary').first()).toContainText('自动检查：1 项')
})

test('mock fixture: the 目标 form shows no warning when the server already configured the checks', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installChecksMockFixture(page, { mode: 'env' })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const pane = page.locator('.sup-goals-scroll')
  await expect(pane.locator('.sup-checks-warning')).toHaveCount(0)
  await expect(pane.locator('.sup-checks-summary').first()).toContainText('自动检查：服务器已配置')
})

test('mock fixture: phone 390px keeps the expanded 自动检查 card inside the viewport', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installChecksMockFixture(page)
  await page.goto('/#code')
  await expandChecks(page)

  const save = page.locator('.sup-checks-body').first().getByRole('button', { name: '保存' })
  const box = await save.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.height).toBeGreaterThanOrEqual(40)

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  expect(overflow.scroll).toBeLessThanOrEqual(390)
})

test('mock fixture: the 自动检查 card shows no English validation/argv/lint words', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installChecksMockFixture(page, { failNextSave: 'unknown_check' })
  await page.goto('/#code')
  await expandChecks(page)

  const card = page.locator('.sup-checks-body').first()
  await checkFor(card, '运行测试').check()
  await card.getByRole('button', { name: '保存' }).click()
  await expect(card.locator('.sup-checks-save-error')).toBeVisible()

  const summaryText = await page.locator('.sup-checks-summary').first().innerText()
  const bodyText = await card.innerText()
  for (const text of [summaryText, bodyText]) {
    expect(text).not.toMatch(/validation/i)
    expect(text).not.toMatch(/argv/i)
    expect(text).not.toMatch(/lint/i)
  }
  expect(bodyText).toContain('自动检查')

  // The 目标 tab copy stays Chinese-only too.
  await page.getByRole('tab', { name: '目标' }).click()
  const goalsText = await page.locator('.sup-goals-scroll').innerText()
  expect(goalsText).not.toMatch(/validation/i)
  expect(goalsText).not.toMatch(/argv/i)
  expect(goalsText).not.toMatch(/lint/i)
})
