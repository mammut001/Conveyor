import { expect, test, type Page, type Route } from '@playwright/test'

// QA r8 onboarding fixes:
//  N24  a failed model call renders as an error line with 重试 that resends the same
//       question in the same session; the composer stops looking like an error state.
//  N25  the 项目 goal (目标 tab) and the 任务与结果 dispatch text survive a reload as a
//       per-worktree draft, with a 「草稿已自动保存」 hint, cleared after 开始 / 开始执行.
//  N26  模型设置 hides 服务商代号 / 密钥名称 under a nested 技术设置 disclosure.
//  N27  filling a field clears its red border together with its message.
//  N28  checklist step ① says the built-in Pi only needs a model (no login).
//  N29  owner-facing copy is plain Chinese (no token / 常用档案 / unittest / main 分支 /
//       Chat Completions / model ids outside 技术详情).
//  N30  a session whose last turn failed is marked 「（没有完成）」 in the list.

const TOKEN = 'r8-e2e-token-not-a-secret-0001'
const createdAt = '2026-11-08T03:00:00.000Z'
const finishedAt = '2026-11-08T03:02:00.000Z'

const QUESTION = '请用一句话说明这台电脑现在是否正常。'
const FAILURE_REASON = '连不上模型服务，请检查「设置 → 模型设置」里的接口地址，或稍后再试。'
const FAILED_SESSION_ID = 'web:web-console:task-failed-0001'
const NORMAL_SESSION_ID = 'web:web-console:task-ok-0002'
const NORMAL_TITLE = '把首页加载时间降到 1 秒以内'

type Options = {
  sessions?: any[]
  jobs?: any[]
  providerConfigured?: boolean
  workerReady?: boolean
  workersEnabled?: boolean
  piInstalled?: boolean
  repositories?: any[]
  /** Captured POST /api/tasks bodies. */
  taskPosts?: any[]
  providerPosts?: any[]
}

function failedSession() {
  return {
    id: FAILED_SESSION_ID, channel: 'web', operator_id: 'web-console', source_chat_id: 'task-failed-0001',
    title: QUESTION, created_at: createdAt, last_activity: finishedAt,
    job_count: 0, message_count: 2, latest_job: null, active_refinement: null,
    last_message_kind: 'error',
  }
}

function normalSession() {
  return {
    id: NORMAL_SESSION_ID, channel: 'web', operator_id: 'web-console', source_chat_id: 'task-ok-0002',
    title: NORMAL_TITLE, created_at: createdAt, last_activity: finishedAt,
    job_count: 1, message_count: 2, latest_job: null, active_refinement: null,
    last_message_kind: 'chat',
  }
}

/** The transcript of the failed turn: the question, then the provider failure (kind="error"). */
function failedTranscript() {
  return [
    { id: 'm1', session_id: FAILED_SESSION_ID, role: 'user', kind: 'chat', content: QUESTION, created_at: createdAt },
    { id: 'm2', session_id: FAILED_SESSION_ID, role: 'assistant', kind: 'error', content: FAILURE_REASON, created_at: finishedAt },
  ]
}

function repository() {
  return {
    id: 'repo-ws', name: 'ws', path: '/tmp/ws', source_machine: 'dev-box', validation_source: 'none',
    worktrees: [
      {
        id: 'wt-managed', path: '/tmp/ws-wt/fix', branch: 'fix/console', head: 'a1b2c3d4e5f6',
        is_main: false, managed: true, locked: false, chat_session_id: 'web:web-console:wt-fix-console',
      },
    ],
  }
}

function memoryItems() {
  return {
    items: [
      { id: 7, kind: 'profile', source: 'owner', text: '我喜欢简短回答', day: '2026-11-08', created_at: createdAt, updated_at: createdAt, source_channel: 'web' },
      { id: 9, kind: 'log', source: 'owner', text: '回复时先说结论，再讲原因。', day: '2026-11-08', created_at: finishedAt, updated_at: finishedAt, source_channel: 'web' },
    ],
    counts: { profile: 1, facts: 0, log: 1 },
    shared: false,
    profile_cap: 8,
  }
}

function teammateStatus() {
  return {
    enabled: true, is_paused: false, paused_until: null, muted_sources: [],
    interval_seconds: 300, cooldown_seconds: 7200, last_patrol_at: finishedAt,
    total_alerts_count: 0, recent_alerts: [], monitored_services: [],
    thresholds: { disk_pct: 90, disk_gb: 3, load_ratio: 2, error_burst: 5 },
    status_text: 'ok',
  }
}

async function install(page: Page, options: Options = {}) {
  const sessions = options.sessions ?? []
  const jobs = options.jobs ?? []
  const providerConfigured = options.providerConfigured ?? false
  const workerReady = options.workerReady ?? false
  const workersEnabled = options.workersEnabled ?? true
  const piInstalled = options.piInstalled ?? false
  const repositories = options.repositories ?? []
  await page.addInitScript(([token]) => {
    sessionStorage.setItem('conveyor-token', token as string)
    localStorage.setItem('conveyor-token', token as string)
    localStorage.removeItem('conveyor-chat-session')
  }, [TOKEN] as const)

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/health') return json({ ok: true, service: 'conveyor-web', schema_version: 1 })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (path === '/api/sessions' && request.method() === 'GET') return json({ sessions })
    if (path.startsWith('/api/sessions/') && request.method() === 'GET') {
      const id = path.slice('/api/sessions/'.length)
      const found = sessions.find((s: any) => s.id === id)
      if (!found) return json({ error: 'not found' }, 404)
      const messages = found.id === FAILED_SESSION_ID ? failedTranscript() : []
      return json({ ...found, messages, runs: [], jobs: [] })
    }
    if (path === '/api/chat/history') return json({ session_id: '', messages: [] })
    if (path === '/api/chat' && request.method() === 'POST') {
      return route.fulfill({
        status: 200, headers: { 'content-type': 'text/event-stream' },
        body: 'event: session\ndata: {"session_id":"web:web-console:webchat-new0001"}\n\nevent: done\ndata: {"outcome":"answered"}\n\n',
      })
    }
    if (path === '/api/tasks' && request.method() === 'POST') {
      options.taskPosts?.push(request.postDataJSON())
      return json({ job_id: 'q-new', session_id: FAILED_SESSION_ID }, 202)
    }
    if (path === '/api/jobs' && request.method() === 'GET') return json({ jobs })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/system/status') return json({
      uptime_seconds: 900, load_average: [0.1, 0.1, 0.1], cpu_count: 4,
      memory: { total: null, available: null }, disk: { total: 1024, used: 128, free: 896 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: false }, feishu: { configured: false } },
      nodes: [], features: { workers: workersEnabled, agent_supervisor: true, long_term_memory: true, teammate: true, mobile_ui: false },
    })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json({ events: [] })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) return json({ job_id: 'q1', diff: '' })
    if (path === '/api/config/provider' && request.method() === 'GET') return json({
      provider_id: providerConfigured ? 'pi-local' : '',
      provider_name: providerConfigured ? 'Pi Local' : '',
      model: providerConfigured ? 'fake-model' : '',
      reasoning_effort: 'minimal',
      base_url: providerConfigured ? 'http://127.0.0.1:18795/v1' : '',
      wire_api: 'chat', env_key: 'PROVIDER_PI_LOCAL_API_KEY',
      api_key_configured: providerConfigured, api_key_hint: providerConfigured ? '••••1234' : '',
      config_path: '/tmp/config.toml', chat_model_configured: providerConfigured,
    })
    if (path === '/api/config/provider' && request.method() === 'POST') {
      options.providerPosts?.push(request.postDataJSON())
      return json({ config: { ...request.postDataJSON(), api_key_configured: true, api_key_hint: '••••1234' } })
    }
    if (path === '/api/workers' && request.method() === 'GET') {
      if (!workersEnabled) return json({ error: 'workers disabled' }, 404)
      return json({
        enabled: true, install_enabled: true, active: workerReady ? 'pi' : null,
        workers: [{
          id: 'pi', label: 'Pi（内置）', installed: piInstalled, version: piInstalled ? '0.5.0' : null, path: null,
          auth: workerReady ? 'ok' : 'needs_login', login_command: '', install_command: null,
          install_supported: false, runnable: true, enabled: true, priority: 0,
          quota: { state: 'available', reset_at: null, reason: null },
        }],
      })
    }
    // 记忆
    if (path === '/api/memory' && request.method() === 'GET') return json(memoryItems())
    if (path === '/api/memory' && request.method() === 'POST') return json({ ok: true })
    if (path.startsWith('/api/memory/notes')) return json({ notes: [], used_chars: 0, cap_chars: 4000 })
    if (path.startsWith('/api/memory/sessions/search')) return json({ results: [] })
    // 值守
    if (path === '/api/teammate/status') return json(teammateStatus())
    // 项目 (Supervisor)
    if (path === '/api/supervisor/capabilities' && request.method() === 'GET') {
      return json({
        enabled: true,
        coordinator: { executor: 'chat', model: 'fake-model', available: true },
        roles: {
          analyst: { executor: 'pool', model: null, available: true },
          implementer: { executor: 'pool', model: null, available: true },
          reviewer: { executor: 'pool', model: null, available: true },
        },
        executors: [{ name: 'pool', label: '执行者池（自动轮换）', available: true }],
        deployment: { configured: false, can_start: false },
      })
    }
    if (path === '/api/supervisor/repositories' && request.method() === 'GET') return json({ repositories })
    if (path === '/api/supervisor/goals' && request.method() === 'GET') return json({ goals: [] })
    if (path === '/api/supervisor/goals' && request.method() === 'POST') {
      return json({ id: 'goal-new', repo_id: 'repo-ws', worktree_id: 'wt-managed', title: '新目标', outcome: 'x', state: 'running' }, 201)
    }
    if (path === '/api/supervisor/tasks' && request.method() === 'GET') return json({ tasks: [], count: 0 })
    if (path === '/api/supervisor/tasks' && request.method() === 'POST') {
      return json({ id: 'task-new', state: 'running', goal: '新任务', repo_id: 'repo-ws', worktree_id: 'wt-managed' }, 201)
    }
    if (/^\/api\/supervisor\/tasks\/[^/]+\/dispatch$/.test(path)) {
      return json({ id: 'task-new', state: 'running', goal: '新任务', repo_id: 'repo-ws', worktree_id: 'wt-managed' })
    }
    if (/^\/api\/supervisor\/repositories\/[^/]+\/validation$/.test(path)) {
      return json({ repo_id: 'repo-ws', candidates: [], selected: [], source: 'none', env_commands: [], effective: [] })
    }
    if (/^\/api\/supervisor\/repositories\/[^/]+\/worktrees$/.test(path) && request.method() === 'POST') {
      return json({
        id: 'wt-new', path: '/tmp/ws-wt/new', branch: 'feat/new', head: 'a1b2c3d4e5f6',
        is_main: false, managed: true, locked: false, chat_session_id: 'web:web-console:wt-new',
      }, 201)
    }
    if (path === '/api/supervisor/deployments') return json({ deployments: [], count: 0 })
    return json({ error: `unhandled fixture request: ${request.method()} ${path}` }, 404)
  })
}

const sidebarRow = (page: Page, title: string) => page.locator('button.session-item', { hasText: title })

// ---------------------------------------------------------------------------
// N24 — a failed turn reads as a failure, and can be retried in one tap
// ---------------------------------------------------------------------------

test('N24: a failed turn renders as an error line with 重试 that resends the same question', async ({ page }) => {
  const taskPosts: any[] = []
  await install(page, { sessions: [failedSession(), normalSession()], taskPosts })
  await page.goto('/#tasks')

  // The question stays, and the reason is an error card — not an assistant bubble.
  await expect(page.locator('.transcript-message.role-user')).toContainText(QUESTION)
  const card = page.locator('.transcript-error-card')
  await expect(card).toBeVisible()
  await expect(card).toContainText(FAILURE_REASON)
  await expect(card.locator('.error-title')).toHaveText('发送没有成功')
  await expect(page.locator('.transcript-message.role-assistant')).toHaveCount(0)

  // 重试 resends the same question into the same session.
  await card.getByRole('button', { name: '重试' }).click()
  await expect.poll(() => taskPosts.length).toBe(1)
  expect(taskPosts[0]).toMatchObject({ prompt: QUESTION, session_id: FAILED_SESSION_ID })

  // The composer is not in an error state: the failure is explained beside it.
  await expect(page.locator('form.task-composer')).toHaveClass(/task-composer--settled/)
  await expect(page.getByLabel('任务内容')).not.toHaveAttribute('aria-invalid', 'true')
})

test('N24: a normal assistant reply is never rendered as a failure', async ({ page }) => {
  await install(page, { sessions: [normalSession()] })
  await page.goto('/#tasks')
  await expect(page.locator('.transcript-error-card')).toHaveCount(0)
  await expect(page.locator('form.task-composer')).not.toHaveClass(/task-composer--settled/)
})

// ---------------------------------------------------------------------------
// N25 — drafts survive a reload, per worktree
// ---------------------------------------------------------------------------

test('N25: the 任务与结果 dispatch goal survives a reload and shows the draft hint', async ({ page }) => {
  await install(page, { repositories: [repository()], providerConfigured: true, workerReady: true })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'ws / fix/console' })).toBeVisible()

  const goal = page.locator('#sup-goal-textarea')
  await goal.fill('把设置页的说明改成大白话')
  await expect(page.locator('.sup-draft-hint').first()).toContainText('草稿已自动保存')

  await page.reload()
  await expect(page.getByRole('heading', { name: 'ws / fix/console' })).toBeVisible()
  await expect(page.locator('#sup-goal-textarea')).toHaveValue('把设置页的说明改成大白话')
  await expect(page.locator('.sup-draft-hint').first()).toContainText('草稿已自动保存')
})

test('N25: the 目标 tab goal survives a reload as its own draft', async ({ page }) => {
  await install(page, { repositories: [repository()], providerConfigured: true, workerReady: true })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'ws / fix/console' })).toBeVisible()
  await page.getByRole('tab', { name: '目标' }).click()

  const outcome = page.locator('#sup-goal-outcome')
  await outcome.fill('首页加载时间降到 1 秒以内')
  await expect(page.locator('.sup-goal-form-card .sup-draft-hint')).toContainText('草稿已自动保存')

  // Switching tabs and back keeps it: the 目标 form unmounts, the draft does not.
  await page.getByRole('tab', { name: '任务与结果' }).click()
  await page.getByRole('tab', { name: '目标' }).click()
  await expect(page.locator('#sup-goal-outcome')).toHaveValue('首页加载时间降到 1 秒以内')

  await page.reload()
  await page.getByRole('tab', { name: '目标' }).click()
  await expect(page.locator('#sup-goal-outcome')).toHaveValue('首页加载时间降到 1 秒以内')
  await expect(page.locator('.sup-goal-form-card .sup-draft-hint')).toContainText('草稿已自动保存')

  // 开始 clears the stored draft together with the box.
  await page.getByRole('button', { name: '开始', exact: true }).click()
  await expect(page.locator('#sup-goal-outcome')).toHaveValue('')
  await expect(page.locator('.sup-goal-form-card .sup-draft-hint')).toHaveCount(0)
  const afterStart = await page.evaluate(() => Object.keys(localStorage).filter(key => key.includes('goal-draft-')))
  expect(afterStart).toEqual([])
})

test('N25: a successful 开始执行 clears the stored draft', async ({ page }) => {
  await install(page, { repositories: [repository()], providerConfigured: true, workerReady: true })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'ws / fix/console' })).toBeVisible()

  await page.locator('#sup-goal-textarea').fill('补充仓库搜索')
  await expect(page.locator('.sup-draft-hint').first()).toBeVisible()
  const stored = await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('conveyor-project-') && key.includes('-draft-')))
  expect(stored.length).toBeGreaterThan(0)

  await page.getByRole('button', { name: '开始执行' }).click()
  await expect(page.locator('#sup-goal-textarea')).toHaveValue('')
  await expect(page.locator('.sup-draft-hint')).toHaveCount(0)
  const after = await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('conveyor-project-') && key.includes('-draft-')))
  expect(after).toEqual([])
})

// ---------------------------------------------------------------------------
// N26 / N27 — 模型设置
// ---------------------------------------------------------------------------

async function openProviderSettings(page: Page) {
  await page.goto('/#tasks')
  await page.locator('header.topbar').getByRole('button', { name: '设置' }).click()
  const dialog = page.locator('.settings-sheet.provider-settings')
  await dialog.getByRole('button', { name: '自定义', exact: true }).click()
  return dialog
}

test('N26: 服务商代号 and 密钥名称 hide under 技术设置 with one Chinese line', async ({ page }) => {
  await install(page)
  const dialog = await openProviderSettings(page)
  await dialog.locator('.provider-advanced > summary').click()

  // 推理强度 stays directly under 高级; the two machine names are one level deeper.
  await expect(dialog.getByLabel('推理强度', { exact: true })).toBeVisible()
  await expect(dialog.getByLabel('服务商代号', { exact: true })).toBeHidden()
  const tech = dialog.locator('details.provider-tech')
  await expect(tech.locator('summary')).toHaveText('技术设置')
  await expect(tech).toContainText('自动填好')

  await tech.locator('summary').click()
  await expect(dialog.getByLabel('服务商代号', { exact: true })).toBeVisible()
  await expect(dialog.getByLabel('密钥名称', { exact: true })).toBeVisible()
  // No raw code names are visible before the disclosure is opened.
  await expect(dialog.getByText('CUSTOM_API_KEY')).toHaveCount(0)
})

test('N27: filling a field clears its red border together with its message', async ({ page }) => {
  await install(page)
  const dialog = await openProviderSettings(page)
  await dialog.getByRole('button', { name: '保存', exact: true }).click()

  // An empty save marks the four visible fields.
  await expect(dialog.locator('.dialog-field.has-error')).toHaveCount(4)
  await expect(dialog.locator('.field-error')).toHaveCount(4)
  await expect(dialog.getByText('请填写显示名称')).toBeVisible()

  // Filling them clears both the message and the red border, field by field.
  await dialog.locator('#provider-provider_name').fill('我的模型')
  await expect(dialog.getByText('请填写显示名称')).toHaveCount(0)
  await expect(dialog.locator('.dialog-field.has-error')).toHaveCount(3)
  await dialog.locator('#provider-base_url').fill('http://127.0.0.1:18795/v1')
  await dialog.locator('#provider-model').fill('fake-model')
  await dialog.locator('#provider-api_key').fill('sk-r8-local-test-1234')
  await expect(dialog.locator('.dialog-field.has-error')).toHaveCount(0)
  await expect(dialog.locator('.field-error')).toHaveCount(0)
  for (const id of ['#provider-provider_name', '#provider-base_url', '#provider-model', '#provider-api_key']) {
    await expect(dialog.locator(id)).not.toHaveAttribute('aria-invalid', 'true')
  }
})

// ---------------------------------------------------------------------------
// N28 — checklist step ①
// ---------------------------------------------------------------------------

test('N28: step ① says the built-in Pi only needs a model when Pi is installed', async ({ page }) => {
  await install(page, { sessions: [], piInstalled: true, workerReady: false })
  await page.goto('/#tasks')
  const step1 = page.locator('.setup-checklist-card li[data-step="1"]')
  await expect(step1.locator('.setup-checklist-link')).toHaveText('选一个执行者（内置 Pi 只要配好模型就能用）')
  await expect(step1.locator('.setup-checklist-link')).not.toContainText('并登录')
})

test('N28: without the built-in Pi step ① still asks for a login', async ({ page }) => {
  await install(page, { sessions: [], piInstalled: false, workerReady: false })
  await page.goto('/#tasks')
  await expect(page.locator('.setup-checklist-card li[data-step="1"] .setup-checklist-link')).toHaveText('选一个执行者并登录')
})

// ---------------------------------------------------------------------------
// N29 — plain Chinese in owner-facing copy
// ---------------------------------------------------------------------------

test('N29: owner-facing copy stays plain Chinese on 设置 / 记忆 / 值守 / 项目', async ({ page }) => {
  await install(page, {
    sessions: [failedSession()],
    repositories: [repository()],
    providerConfigured: true,
    workerReady: true,
    piInstalled: true,
  })

  const banned = [
    'token', 'Token', 'TOKEN',
    '常用档案', '记忆日志',
    'unittest', 'test_*.py',
    'main 分支', 'Git 与 GitHub', 'GitHub CI',
    'Chat Completions', 'Responses',
    'chat · fake-model', 'chat ·',
  ]

  // 模型设置 (自定义): the raw names live behind 技术设置.
  const dialog = await openProviderSettings(page)
  await expect(dialog.getByText('Chat Completions')).toHaveCount(0)
  await expect(dialog.getByText('Responses')).toHaveCount(0)
  await expect(dialog.getByLabel('接口类型', { exact: true }).locator('option:checked')).toHaveText('通用聊天接口')
  await dialog.locator('.close-button').click()
  await expect(dialog).toHaveCount(0)

  // 记忆
  await page.goto('/#memory')
  const memory = page.locator('.kn-memory')
  await expect(memory).toBeVisible()
  await expect(memory.getByRole('button', { name: /^常用信息/ })).toBeVisible()
  await expect(memory.getByRole('button', { name: /^历史记录/ })).toBeVisible()
  await expect(memory.locator('.kn-memory-id').first()).toContainText('编号')
  let text = await memory.innerText()
  for (const word of banned) expect(text).not.toContain(word)

  // 值守
  await page.goto('/#teammate')
  const teammate = page.locator('.guard-teammate')
  await expect(teammate).toBeVisible()
  await expect(teammate.getByRole('heading', { name: '代码仓库与自动构建' })).toBeVisible()
  text = await teammate.innerText()
  for (const word of banned) expect(text).not.toContain(word)

  // 项目: the coordinator card, the dispatch card and the 讨论 header.
  await page.goto('/#code')
  const workspace = page.locator('.sup-workspace')
  await expect(workspace).toBeVisible()
  await expect(workspace.locator('.sup-chat-pane-header')).toContainText('用来聊方案的模型：内置对话模型')
  await expect(workspace.locator('.sup-role-card--coordinator')).toContainText('内置对话模型')
  text = await workspace.innerText()
  for (const word of banned) expect(text).not.toContain(word)
})

// ---------------------------------------------------------------------------
// N30 — a failed session is marked in the list
// ---------------------------------------------------------------------------

test('N30: a session whose last turn failed is marked （没有完成） in the list', async ({ page }) => {
  await install(page, { sessions: [failedSession(), normalSession()] })
  await page.goto('/#tasks')

  await expect(sidebarRow(page, QUESTION)).toContainText('（没有完成）')
  await expect(sidebarRow(page, NORMAL_TITLE)).not.toContainText('（没有完成）')
  // The mark is on the list title, not on the stored session title.
  await expect(sidebarRow(page, QUESTION)).toContainText('2 条消息 · 0 次运行')
})
