import { expect, test, type Page, type Route } from '@playwright/test'

// QA r7 onboarding fixes (frontend):
//  N17  任务 never lands on a supervisor worktree session; if one is opened anyway the composer
//        says where to go instead, and any non-2xx from POST /api/tasks shows its reason.
//  N21  supervisor worktree sessions (supwt-*) stay out of the 任务 / 对话 session lists.
//  N20  项目 speaks plain Chinese and explains 仓库 / 工作目录 / 执行者 before anything exists.
//  N2   the checklist shows its completed state (all three ticked) once, then hides.
//  N14  自定义 preset placeholders are neutral examples, not DeepSeek's.

const TOKEN = 'r7-e2e-token-not-a-secret-0001'
const createdAt = '2026-11-08T03:00:00.000Z'
const finishedAt = '2026-11-08T03:02:00.000Z'

const SUPWT_SESSION_ID = 'web:web-console:supwt-wt-7f3a'
const SUPWT_TITLE = 'Code · ws (sup/aae81f0a)'
const NORMAL_SESSION_ID = 'web:web-console:normal-task'
const NORMAL_TITLE = '把首页加载时间降到 1 秒以内'

const READ_ONLY_LINE = '这是「项目」里的工作目录对话，请到「项目」页讨论或派发。'
const CHINESE_SERVER_ERROR = '这个工作目录只能读，请到「项目」页派发任务'
const RAW_409 = 'supervisor worktree sessions are read-only in chat; dispatch tasks via /api/supervisor/tasks'

type Options = {
  sessions?: any[]
  jobs?: any[]
  providerConfigured?: boolean
  workerReady?: boolean
  workersEnabled?: boolean
  /** What POST /api/tasks answers: 202 with a job, or a failure status + body. */
  taskStatus?: number
  taskError?: string
  repositories?: any[]
  chatSessionId?: string
}

function runningJob() {
  return {
    id: 'q1', state: 'running', mode: 'fix', channel: 'web', chat_id: 'normal-task', operator_id: 'web-console',
    created_at: createdAt, started_at: createdAt, updated_at: finishedAt,
    prompt_preview: NORMAL_TITLE, refinement_intent: false, metadata: {}, changed_files: [],
  }
}

function normalSession() {
  return {
    id: NORMAL_SESSION_ID, channel: 'web', operator_id: 'web-console', source_chat_id: 'normal-task',
    title: NORMAL_TITLE, created_at: createdAt, last_activity: finishedAt,
    job_count: 1, message_count: 2, latest_job: runningJob(), active_refinement: null,
  }
}

function supwtSession() {
  return {
    id: SUPWT_SESSION_ID, channel: 'web', operator_id: 'web-console', source_chat_id: 'supwt-wt-7f3a',
    title: SUPWT_TITLE, created_at: createdAt, last_activity: finishedAt,
    job_count: 0, message_count: 0, latest_job: null, active_refinement: null,
  }
}

function repository() {
  return {
    id: 'repo-ws', name: 'ws', path: '/tmp/ws', source_machine: 'dev-box', validation_source: 'none',
    worktrees: [
      { id: 'wt-main', path: '/tmp/ws', branch: 'main', head: '8352f46a9c01', is_main: true, managed: false, locked: false },
      {
        id: 'wt-managed', path: '/tmp/ws-wt/fix', branch: 'fix/console', head: 'a1b2c3d4e5f6',
        is_main: false, managed: true, locked: false, chat_session_id: 'web:web-console:wt-fix-console',
      },
    ],
  }
}

async function install(page: Page, options: Options = {}) {
  const sessions = options.sessions ?? []
  const jobs = options.jobs ?? []
  const providerConfigured = options.providerConfigured ?? false
  const workerReady = options.workerReady ?? false
  const workersEnabled = options.workersEnabled ?? true
  const taskStatus = options.taskStatus ?? 202
  const taskError = options.taskError ?? ''
  const repositories = options.repositories ?? []
  await page.addInitScript(([token, chatSession]) => {
    sessionStorage.setItem('conveyor-token', token as string)
    localStorage.setItem('conveyor-token', token as string)
    // A 对话 conversation only when the test asks for one; the N2 checklist test starts empty.
    if (chatSession) localStorage.setItem('conveyor-chat-session', chatSession as string)
    else localStorage.removeItem('conveyor-chat-session')
  }, [TOKEN, options.chatSessionId ?? ''] as const)

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
      return found ? json({ ...found, messages: [], runs: [], jobs: [] }) : json({ error: 'not found' }, 404)
    }
    if (path === '/api/chat/history') return json({ session_id: '', messages: [] })
    if (path === '/api/chat' && request.method() === 'POST') {
      // The server keeps the turn: the conversation shows up in the next /api/sessions read,
      // which is what makes checklist step ③ survive a reload.
      sessions.push({
        id: 'web:web-console:webchat-new0001', channel: 'web', operator_id: 'web-console', source_chat_id: 'webchat-new0001',
        title: '你好', created_at: createdAt, last_activity: finishedAt,
        job_count: 0, message_count: 2, latest_job: null, active_refinement: null,
      })
      return route.fulfill({
        status: 200, headers: { 'content-type': 'text/event-stream' },
        body: 'event: session\ndata: {"session_id":"web:web-console:webchat-new0001"}\n\nevent: message\ndata: {"text":"你好，我是 Conveyor。"}\n\nevent: done\ndata: {"outcome":"answered"}\n\n',
      })
    }
    if (path === '/api/tasks' && request.method() === 'POST') {
      if (taskStatus !== 202) return json({ error: taskError, code: 'supervisor_session_read_only' }, taskStatus)
      return json({ job_id: 'q-new', session_id: NORMAL_SESSION_ID }, 202)
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
      nodes: [], features: { workers: workersEnabled, agent_supervisor: true, mobile_ui: false },
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
      return json({ config: { ...request.postDataJSON(), api_key_configured: true, api_key_hint: '••••1234' } })
    }
    if (path === '/api/workers' && request.method() === 'GET') {
      if (!workersEnabled) return json({ error: 'workers disabled' }, 404)
      return json({
        enabled: true, install_enabled: true, active: workerReady ? 'pi' : null,
        workers: [{
          id: 'pi', label: 'Pi（内置）', installed: workerReady, version: workerReady ? '0.5.0' : null, path: null,
          auth: workerReady ? 'ok' : 'needs_login', login_command: '', install_command: null,
          install_supported: false, runnable: true, enabled: true, priority: 0,
          quota: { state: 'available', reset_at: null, reason: null },
        }],
      })
    }
    // 项目 (Supervisor) contract: only what the workspace needs to render.
    if (path === '/api/supervisor/capabilities' && request.method() === 'GET') {
      return json({
        enabled: true,
        coordinator: { executor: 'Pi（内置）', model: 'fake-model' },
        roles: {
          analyst: { executor: 'claude', model: null, available: true },
          implementer: { executor: 'claude', model: null, available: true },
          reviewer: { executor: 'claude', model: null, available: true },
        },
        executors: [{ name: 'claude', label: 'Claude Code', available: true }],
        deployment: { configured: false, can_start: false },
      })
    }
    if (path === '/api/supervisor/repositories' && request.method() === 'GET') return json({ repositories })
    if (path === '/api/supervisor/repositories' && request.method() === 'POST') {
      const created = repository()
      repositories.push(created)
      return json(created, 201)
    }
    if (path === '/api/supervisor/goals' && request.method() === 'GET') return json({ goals: [] })
    if (path === '/api/supervisor/tasks' && request.method() === 'GET') return json({ tasks: [], count: 0 })
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

test('N21/N17a: a supervisor worktree session never joins the 任务 list and is never auto-selected', async ({ page }) => {
  // The worktree session is the newest one; the normal task session must win.
  await install(page, { sessions: [supwtSession(), normalSession()] })
  await page.goto('/#tasks')

  await expect(sidebarRow(page, SUPWT_TITLE)).toHaveCount(0)
  await expect(sidebarRow(page, NORMAL_TITLE)).toBeVisible()
  // The newest normal session is the selected one, not the worktree conversation.
  await expect(page.locator('.shell-heading h2')).toHaveText(NORMAL_TITLE)
  await expect(page.getByLabel('任务内容')).toBeVisible()
})

test('N21: a supervisor worktree session never joins the 对话 list either', async ({ page }) => {
  await install(page, { sessions: [supwtSession(), normalSession()], chatSessionId: 'web:web-console:webchat-1' })
  await page.goto('/#chat')
  await expect(sidebarRow(page, SUPWT_TITLE)).toHaveCount(0)
})

test('N17b: opening a worktree session on 任务 swaps the composer for where to go instead', async ({ page }) => {
  await install(page, { sessions: [supwtSession(), normalSession()] })
  await page.addInitScript(id => { sessionStorage.setItem('conveyor-selected-session', id as string) }, SUPWT_SESSION_ID)
  await page.goto('/#tasks')

  // No composer at all: the owner cannot send into a read-only conversation.
  await expect(page.getByLabel('任务内容')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '发送' })).toHaveCount(0)
  await expect(page.getByText(READ_ONLY_LINE)).toBeVisible()

  // 去项目 opens the 项目 page.
  await page.getByRole('button', { name: '去项目' }).click()
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '项目' })).toHaveAttribute('aria-current', 'page')

  // 新建会话 gives a fresh composer back.
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '任务' }).click()
  await expect(page.getByText(READ_ONLY_LINE)).toBeVisible()
  await page.locator('.task-composer--notice').getByRole('button', { name: '新建会话' }).click()
  await expect(page.getByLabel('任务内容')).toBeVisible()
  await expect(page.locator('.shell-heading h2')).toHaveText('新会话')
})

test('N17c: a 409 from POST /api/tasks shows its reason next to the composer', async ({ page }) => {
  await install(page, { sessions: [normalSession()], taskStatus: 409, taskError: CHINESE_SERVER_ERROR })
  await page.goto('/#tasks')
  await page.getByLabel('任务内容').fill('请用一句话说明这台电脑现在是否正常。')
  await page.getByRole('button', { name: '发送' }).click()

  const notice = page.locator('.task-composer-error-text')
  await expect(notice).toBeVisible()
  await expect(notice).toContainText(CHINESE_SERVER_ERROR)
  // The words stay in the box, and the notice can be closed.
  await expect(page.getByLabel('任务内容')).toHaveValue('请用一句话说明这台电脑现在是否正常。')
  await page.getByRole('button', { name: '知道了' }).click()
  await expect(notice).toHaveCount(0)
})

test('N17c: an English 409 falls back to one Chinese line, with the raw text as detail', async ({ page }) => {
  await install(page, { sessions: [normalSession()], taskStatus: 409, taskError: RAW_409 })
  await page.goto('/#tasks')
  await page.getByLabel('任务内容').fill('你好')
  await page.getByRole('button', { name: '发送' }).click()

  const notice = page.locator('.task-composer-error-text')
  await expect(notice).toBeVisible()
  await expect(notice).toContainText('操作没有完成，请稍后重试')
  await expect(notice.locator('small')).toContainText('supervisor worktree sessions are read-only in chat')
})

test('N2: the checklist shows all three steps ticked once, then hides on the next load', async ({ page }) => {
  await install(page, { sessions: [], providerConfigured: true, workerReady: true })
  await page.goto('/#chat')
  const checklist = page.locator('.setup-checklist-card')
  await expect(checklist).toBeVisible()
  await expect(checklist.locator('li[data-step="1"]')).toHaveClass(/is-done/)
  await expect(checklist.locator('li[data-step="2"]')).toHaveClass(/is-done/)
  await expect(checklist.locator('li[data-step="3"]')).not.toHaveClass(/is-done/)

  // The first message ticks ③ — and the card stays, all three ticked, saying so.
  await page.getByRole('textbox', { name: '给 Conveyor 发消息' }).fill('你好')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(checklist).toBeVisible()
  await expect(checklist.locator('li[data-step="3"]')).toHaveClass(/is-done/)
  await expect(checklist.locator('li[data-step="3"] .setup-checklist-badge')).toHaveText('✓')
  await expect(checklist.getByText('都准备好了，可以开始用。')).toBeVisible()

  // The next load does not celebrate again.
  await page.reload()
  await expect(page.locator('.setup-checklist-card')).toHaveCount(0)
})

test('N2: closing the checklist hides it, completed state and all', async ({ page }) => {
  await install(page, { sessions: [], providerConfigured: true, workerReady: true })
  await page.goto('/#chat')
  const checklist = page.locator('.setup-checklist-card')
  await page.getByRole('textbox', { name: '给 Conveyor 发消息' }).fill('你好')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(checklist.getByText('都准备好了，可以开始用。')).toBeVisible()
  await checklist.getByRole('button', { name: '关闭' }).click()
  await expect(page.locator('.setup-checklist-card')).toHaveCount(0)
})

test('N20: 项目 explains 仓库 / 工作目录 / 执行者 while there is no repository yet', async ({ page }) => {
  await install(page, { repositories: [], providerConfigured: true, workerReady: true })
  await page.goto('/#code')

  const intro = page.getByRole('region', { name: '项目是做什么的' })
  await expect(intro).toBeVisible()
  await expect(intro).toContainText('仓库')
  await expect(intro).toContainText('工作目录')
  await expect(intro).toContainText('每个工作目录是项目的一份独立副本，改动不会碰到主分支，确认后再合并。')
  await expect(intro).toContainText('可用的执行者')
  await expect(intro.getByRole('button', { name: '去执行者页' })).toBeVisible()
  await expect(page.locator('.sup-empty-tree')).toContainText('还没有登记仓库')

  // Dismissible, and it stays away for the rest of this page.
  await intro.getByRole('button', { name: '关闭说明' }).click()
  await expect(page.getByRole('region', { name: '项目是做什么的' })).toHaveCount(0)
})

test('N20: once a repository exists the intro is gone', async ({ page }) => {
  await install(page, { repositories: [repository()], providerConfigured: true, workerReady: true })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'ws / fix/console' })).toBeVisible()
  await expect(page.getByRole('region', { name: '项目是做什么的' })).toHaveCount(0)
})

test('N20: 项目 says 工作目录, 可以开始, 在这里改, 讨论, 分析/动手改/检查 and 高级设置', async ({ page }) => {
  await install(page, { repositories: [repository()], providerConfigured: true, workerReady: true })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'ws / fix/console' })).toBeVisible()

  // One name for the worktree conversation on desktop.
  await expect(page.locator('.sup-chat-pane-header strong')).toHaveText('讨论')

  // Worktree badges read as plain Chinese.
  await expect(page.locator('.sup-worktree-item', { hasText: 'main' }).first()).toContainText('主分支（只看）')
  await expect(page.locator('.sup-worktree-item', { hasText: 'fix/console' }).first()).toContainText('可以开始')

  // The read-only banner offers 新建工作目录, not a Worktree.
  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()
  await expect(page.locator('.sup-banner--readonly').getByRole('button', { name: '新建工作目录' })).toBeVisible()

  // Dispatch scope and the role names.
  await page.locator('.sup-worktree-item').filter({ hasText: 'fix/console' }).first().click()
  await expect(page.locator('.sup-dispatch-context-tag')).toContainText('在这里改')
  await expect(page.locator('.sup-role-card--coordinator .sup-role-title')).toHaveText('讨论')
  for (const [label, hint] of [['分析', '先想清楚要改成什么样'], ['动手改', '在工作目录里改代码并自测'], ['检查', '照验收标准逐条检查']]) {
    await expect(page.locator('.sup-role-card', { hasText: label }).first()).toContainText(hint)
  }
  await expect(page.getByLabel('分析执行器')).toBeVisible()
  await expect(page.getByLabel('动手改执行器')).toBeVisible()
  await expect(page.getByLabel('检查执行器')).toBeVisible()
  await expect(page.locator('.sup-role-advanced > summary')).toHaveText('高级设置')

  // No English or jargon left on the page.
  const body = await page.locator('.sup-workspace').innerText()
  for (const jargon of ['Worktree', '托管', '作用范围', '协调对话', '主会话', '分析角色', '实现角色', '审查角色', '模型与角色高级配置']) {
    expect(body).not.toContain(jargon)
  }
})

test('N20: the phone 项目 tab calls the worktree conversation 讨论 too', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
  const phone = await context.newPage()
  await install(phone, { repositories: [repository()], providerConfigured: true, workerReady: true })
  await phone.goto('/#code')
  await expect(phone.getByRole('tab', { name: '讨论' })).toBeVisible()
  await expect(phone.getByRole('tab', { name: '工作目录对话' })).toHaveCount(0)
  await context.close()
})

test('N14: 自定义 preset placeholders are neutral examples', async ({ page }) => {
  await install(page, { sessions: [], providerConfigured: false, workerReady: false })
  await page.goto('/#tasks')
  await page.locator('.setup-checklist-card li[data-step="2"] .setup-checklist-action-btn').click()
  const dialog = page.locator('.settings-sheet.provider-settings')
  await dialog.getByRole('button', { name: '自定义', exact: true }).click()
  await expect(dialog.locator('#provider-base_url')).toHaveAttribute('placeholder', 'https://你的服务地址/v1')
  await expect(dialog.locator('#provider-model')).toHaveAttribute('placeholder', '模型名称，例如 gpt-4o-mini')
})
