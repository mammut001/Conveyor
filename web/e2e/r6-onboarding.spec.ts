import { expect, test, type Page, type Route } from '@playwright/test'

// QA r6 onboarding fixes:
//  N10  a 对话 conversation is listed in the 会话 sidebar after a reload and reopens with its messages
//  N2   checklist step ③ ticks from stored sessions and the card stays while steps are open
//  N11  a running turn renders the ordered reply text (no repeated lead sentence)
//  N14  自定义 preset: generic placeholder, hidden codes derived from 显示名称
//  N15  the phone composer hint drops the Enter-key part

const TOKEN = 'r6-e2e-token-not-a-secret-0001'
const createdAt = '2026-11-08T03:00:00.000Z'
const finishedAt = '2026-11-08T03:02:00.000Z'

const chatSessionId = 'web:web-console:webchat-39f8524ffda7'
const chatTitle = '你好，请用一句话介绍你自己。'
const chatReply = '你好，我是 Conveyor，可以帮你查资料、跑命令、改代码。'
const taskSessionId = 'web:web-console:readme-task'
const taskTitle = '在 README 末尾加一行说明'

type Options = {
  sessions?: any[]
  jobs?: any[]
  providerConfigured?: boolean
  workerReady?: boolean
  workersEnabled?: boolean
  events?: any[]
  providerPosts?: any[]
}

function chatSession() {
  return {
    id: chatSessionId, channel: 'web', operator_id: 'web-console', source_chat_id: 'webchat-39f8524ffda7',
    title: chatTitle, created_at: createdAt, last_activity: finishedAt,
    job_count: 0, message_count: 2, latest_job: null, active_refinement: null,
  }
}

function runningJob() {
  return {
    id: 'q1', state: 'running', mode: 'fix', channel: 'web', chat_id: 'readme-task', operator_id: 'web-console',
    created_at: createdAt, started_at: createdAt, updated_at: finishedAt,
    prompt_preview: taskTitle, refinement_intent: false, metadata: {}, changed_files: [],
  }
}

function taskSession() {
  return {
    id: taskSessionId, channel: 'web', operator_id: 'web-console', source_chat_id: 'readme-task',
    title: taskTitle, created_at: createdAt, last_activity: finishedAt,
    job_count: 1, message_count: 2, latest_job: runningJob(), active_refinement: null,
  }
}

function chatHistory() {
  return [
    { id: 'c1', session_id: chatSessionId, role: 'user', kind: 'chat', content: chatTitle, created_at: createdAt },
    { id: 'c2', session_id: chatSessionId, role: 'assistant', kind: 'chat', content: chatReply, created_at: finishedAt },
  ]
}

// GET /api/chat/history really returns {session_id, messages:[{role, text, created_at, kind, origin_channel}]};
// only /api/sessions/<id> messages use the 'content' field above.
function chatTranscript() {
  return [
    { role: 'user', text: chatTitle, created_at: createdAt, kind: 'chat', origin_channel: 'web' },
    { role: 'assistant', text: chatReply, created_at: finishedAt, kind: 'chat', origin_channel: 'web' },
  ]
}

// The reply as Pi sends it: incremental deltas from the runner plus the cumulative
// snapshots the queue's progress callback re-sends for the same job.
function deltaEvents() {
  const rows: [number, string][] = [
    [1, '好的'],
    [2, '，这是'],
    [3, '好的，这是'],
    [4, '当前'],
    [5, '好的，这是当前'],
    [6, '系统状态。'],
    [7, '好的，这是当前系统状态。'],
  ]
  return rows.map(([sequence, text]) => ({
    schema_version: 1, event_id: `ev-${sequence}`, sequence, timestamp: finishedAt,
    kind: 'assistant.delta', job_id: 'q1', payload: { text },
  }))
}

async function install(page: Page, options: Options = {}) {
  const sessions = options.sessions ?? []
  const jobs = options.jobs ?? []
  const providerConfigured = options.providerConfigured ?? false
  const workerReady = options.workerReady ?? false
  const workersEnabled = options.workersEnabled ?? true
  const events = options.events ?? []
  await page.addInitScript(([token, chatSession]) => {
    sessionStorage.setItem('conveyor-token', token as string)
    localStorage.setItem('conveyor-token', token as string)
    // The owner already talked in this conversation before the reload.
    localStorage.setItem('conveyor-chat-session', chatSession as string)
  }, [TOKEN, chatSessionId] as const)

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
      if (id === chatSessionId) return json({ ...chatSession(), messages: chatHistory(), runs: [], jobs: [] })
      if (id === taskSessionId) return json({ ...taskSession(), messages: [], runs: [], jobs: [runningJob()] })
      return json({ error: 'not found' }, 404)
    }
    if (path === '/api/chat/history') return json({ session_id: chatSessionId, messages: chatTranscript() })
    if (path === '/api/chat' && request.method() === 'POST') {
      return route.fulfill({
        status: 200, headers: { 'content-type': 'text/event-stream' },
        body: 'event: session\ndata: {"session_id":"web:web-console:webchat-new0001"}\n\nevent: done\ndata: {"outcome":"answered"}\n\n',
      })
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
      nodes: [], features: { workers: workersEnabled, mobile_ui: false },
    })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json({ events })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) return json({ job_id: 'q1', diff: '' })
    if (path === '/api/config/provider' && request.method() === 'GET') return json({
      provider_id: providerConfigured ? 'pi-local' : '',
      provider_name: providerConfigured ? 'Pi Local' : '',
      model: providerConfigured ? 'fake-model' : '',
      reasoning_effort: 'minimal',
      base_url: providerConfigured ? 'http://127.0.0.1:18795/v1' : '',
      wire_api: 'chat', env_key: 'PROVIDER_PI_LOCAL_API_KEY',
      api_key_configured: providerConfigured, api_key_hint: providerConfigured ? '••••1234' : '',
      config_path: '/tmp/config.toml',
    })
    if (path === '/api/config/provider' && request.method() === 'POST') {
      const body = request.postDataJSON()
      options.providerPosts?.push(body)
      return json({ config: { ...body, api_key_configured: true, api_key_hint: '••••1234' } })
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
    return json({ error: `unhandled fixture request: ${request.method()} ${path}` }, 404)
  })
}

const sidebarRow = (page: Page, title: string) => page.locator('button.session-item', { hasText: title })

test('N10: a 对话 conversation stays in the sidebar after a reload and reopens with its messages', async ({ page }) => {
  await install(page, { sessions: [chatSession()] })
  await page.goto('/#chat')

  // Listed in the 会话 sidebar, with its two messages in the panel.
  await expect(sidebarRow(page, chatTitle)).toBeVisible()
  await expect(page.getByText('还没有会话，点“新建”开始。')).toHaveCount(0)
  const stream = page.locator('.chat-stream-content')
  await expect(stream.locator('.transcript-message.role-user')).toHaveCount(1)
  await expect(stream.locator('.transcript-message.role-user')).toContainText(chatTitle)
  await expect(stream.locator('.transcript-message.role-assistant')).toContainText(chatReply)

  // After a reload it is still there, still with its messages.
  await page.reload()
  await expect(sidebarRow(page, chatTitle)).toBeVisible()
  await expect(stream.locator('.transcript-message.role-assistant')).toContainText(chatReply)

  // 新会话 empties the panel; the sidebar row reopens the conversation.
  await page.locator('.chat-new-button').click()
  await expect(stream.locator('.transcript-message')).toHaveCount(0)
  await expect(sidebarRow(page, chatTitle)).toBeVisible()
  await sidebarRow(page, chatTitle).click()
  await expect(stream.locator('.transcript-message.role-user')).toContainText(chatTitle)
  await expect(stream.locator('.transcript-message.role-assistant')).toContainText(chatReply)
})

test('N10: a chat-only conversation never joins the 任务 list', async ({ page }) => {
  await install(page, { sessions: [chatSession()] })
  await page.goto('/#tasks')
  await expect(sidebarRow(page, chatTitle)).toHaveCount(0)
  await expect(page.getByText('还没有会话，点“新建”开始。')).toBeVisible()
})

test('N2: checklist step ③ ticks from stored sessions and the card stays while steps are open', async ({ page }) => {
  await install(page, { sessions: [chatSession()], providerConfigured: false, workerReady: false })
  await page.goto('/#tasks')
  const checklist = page.locator('.setup-checklist-card')
  await expect(checklist).toBeVisible()
  // ③ comes from the session that already has messages, not from this tab's memory.
  await expect(checklist.locator('li[data-step="3"]')).toHaveClass(/is-done/)
  await expect(checklist.locator('li[data-step="3"] .setup-checklist-badge')).toHaveText('✓')
  // ① and ② are still open, so the checklist must not vanish (not even after a poll).
  await expect(checklist.locator('li[data-step="1"]')).not.toHaveClass(/is-done/)
  await expect(checklist.locator('li[data-step="2"]')).not.toHaveClass(/is-done/)
  await page.waitForTimeout(3400)
  await expect(checklist).toBeVisible()
  await expect(checklist.locator('li[data-step="3"]')).toHaveClass(/is-done/)
})

test('N2: the checklist goes away once every step is done', async ({ page }) => {
  await install(page, { sessions: [chatSession()], providerConfigured: true, workerReady: true })
  await page.goto('/#tasks')
  await expect(page.locator('.setup-checklist-card')).toHaveCount(0)
})

test('N2: a 对话 message ticks step ③ right away', async ({ page }) => {
  await install(page, { sessions: [], providerConfigured: false, workerReady: false })
  await page.goto('/#chat')
  const checklist = page.locator('.setup-checklist-card')
  await expect(checklist).toBeVisible()
  await expect(checklist.locator('li[data-step="3"]')).not.toHaveClass(/is-done/)
  await page.getByRole('textbox', { name: '给 Conveyor 发消息' }).fill('你好')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(checklist.locator('li[data-step="3"]')).toHaveClass(/is-done/)
  // The card itself stays: ① and ② are still open.
  await expect(checklist).toBeVisible()
})

test('N11: a running turn shows the ordered reply text, never a repeated lead sentence', async ({ page }) => {
  await install(page, { jobs: [runningJob()], sessions: [taskSession()], events: deltaEvents() })
  await page.goto('/#tasks')
  const live = page.locator('.live-streaming .transcript-content')
  await expect(live).toBeVisible()
  await expect(live).toContainText('好的，这是当前系统状态。')
  // The lead sentence is rendered once, and nothing is duplicated mid-text.
  const rendered = ((await live.innerText()) || '').replace(/\s+/g, '')
  expect(rendered.split('好的，这是').length - 1).toBe(1)
  expect(rendered.replace('▌', '')).toBe('好的，这是当前系统状态。')
})

test('N14: 自定义 preset shows a generic placeholder and derives the hidden codes from 显示名称', async ({ page }) => {
  const providerPosts: any[] = []
  await install(page, { providerConfigured: false, providerPosts })
  await page.goto('/#tasks')
  await page.locator('.setup-checklist-card li[data-step="2"] .setup-checklist-action-btn').click()
  const dialog = page.locator('.settings-sheet.provider-settings')
  await dialog.getByRole('button', { name: '自定义', exact: true }).click()

  // The placeholder is generic, not another provider's name.
  await expect(dialog.locator('#provider-provider_name')).toHaveAttribute('placeholder', '我的模型')

  // A 显示名称 with Latin letters names the hidden 服务商代号 and 密钥名称.
  await dialog.locator('#provider-provider_name').fill('Pi Local')
  await dialog.locator('#provider-base_url').fill('http://127.0.0.1:18795/v1')
  await dialog.locator('#provider-model').fill('fake-model')
  await dialog.locator('#provider-api_key').fill('sk-local-test-1234')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByText('✓ 已保存，新任务会使用这个配置。')).toBeVisible()
  expect(providerPosts.at(-1)).toMatchObject({ provider_id: 'pi-local', env_key: 'PROVIDER_PI_LOCAL_API_KEY' })
})

test('N14: a 显示名称 without Latin letters still derives the codes from the 接口地址', async ({ page }) => {
  const providerPosts: any[] = []
  await install(page, { providerConfigured: false, providerPosts })
  await page.goto('/#tasks')
  await page.locator('.setup-checklist-card li[data-step="2"] .setup-checklist-action-btn').click()
  const dialog = page.locator('.settings-sheet.provider-settings')
  await dialog.getByRole('button', { name: '自定义', exact: true }).click()
  await dialog.locator('#provider-provider_name').fill('我的模型')
  await dialog.locator('#provider-base_url').fill('https://api.example.com/v1')
  await dialog.locator('#provider-model').fill('my-model-1')
  await dialog.locator('#provider-api_key').fill('sk-custom-secret-9999')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByText('✓ 已保存，新任务会使用这个配置。')).toBeVisible()
  expect(providerPosts.at(-1)).toMatchObject({
    provider_id: 'api-example-com', provider_name: '我的模型', env_key: 'API_EXAMPLE_COM_API_KEY',
  })
})

test('N15: the phone composer hint drops the Enter-key part, desktop keeps it', async ({ page, browser }) => {
  await install(page, { jobs: [runningJob()], sessions: [taskSession()], events: [] })
  await page.goto('/#tasks')
  const hint = page.locator('.task-composer-hint')
  await expect(hint).toContainText('Enter 发送')

  const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
  const phone = await context.newPage()
  await install(phone, { jobs: [runningJob()], sessions: [taskSession()], events: [] })
  await phone.goto('/#tasks')
  const phoneHint = phone.locator('.task-composer-hint')
  await expect(phoneHint).toHaveText('点「发送」提交')
  await expect(phoneHint).not.toContainText('回车')
  await expect(phoneHint).not.toContainText('Enter')
  await context.close()
})
