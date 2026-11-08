import { expect, test, type Page, type Route } from '@playwright/test'
import { mkdirSync } from 'node:fs'

// Screenshots go where SHOT_DIR says (the QA run points it at the shared shots folder).
const SHOTS = process.env.SHOT_DIR || 'test-results/r4-onboard-fix'
mkdirSync(SHOTS, { recursive: true })
const PHASE = process.env.SHOT_PHASE || 'after'

type Opts = {
  modelConfigured?: boolean
  jobs?: any[]
  sessions?: any[]
  chatSse?: string
  modelsStatus?: number
  modelsBody?: unknown
  submittedTasks?: any[]
  chatCalls?: any[]
}

const failedJob = (error: string) => ({
  id: 'job-r4-fail',
  state: 'failed',
  mode: 'run',
  channel: 'web',
  chat_id: 'default',
  created_at: new Date(Date.now() - 60_000).toISOString(),
  updated_at: new Date(Date.now() - 30_000).toISOString(),
  prompt_preview: '在 README 末尾加一行说明',
  error,
  changed_files: [],
  metadata: {},
})

const piWorker = {
  id: 'pi',
  label: 'Pi（内置）',
  installed: false,
  version: null,
  path: null,
  auth: 'unknown',
  login_command: 'pi',
  install_command: 'npm ci',
  install_supported: true,
  runnable: true,
  enabled: true,
  priority: 1,
  quota: { state: 'available', reset_at: null, reason: null },
}

async function setupMocks(page: Page, opts: Opts = {}) {
  const modelConfigured = opts.modelConfigured ?? false
  await page.addInitScript(() => {
    localStorage.setItem('conveyor-token', 'test-token')
    sessionStorage.setItem('conveyor-token', 'test-token')
  })
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const path = decodeURIComponent(new URL(request.url()).pathname)
    const method = request.method()
    const json = (data: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })

    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (path === '/api/health') return json({ ok: true })
    if (path === '/api/sessions') return json({ sessions: opts.sessions ?? [] })
    if (path === '/api/jobs') return json({ jobs: opts.jobs ?? [] })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json({ events: [] })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) return json({ diff: '', stats: {}, files: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/chat/history') return json({ session_id: 'default-session', messages: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/screen/status') return json({ enabled: false, available: false, reason: '', control_enabled: false, controlling: false, agent_paused: false, blocked_by: null, width: 0, height: 0 })
    if (path === '/api/takeover/status') return json({ enabled: false, takeover: null, privacy_mode: false, closing: null, transport_allowed: false, transport: { phase: 'idle', running: false, ready: false } })
    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 10, load_average: [0, 0, 0], cpu_count: 4, memory: { total: null, available: null },
        disk: { total: 1000, used: 0, free: 1000 }, queue: { depth: 0, paused: false, states: {} },
        channels: { telegram: { configured: false }, feishu: { configured: false } }, nodes: [],
        features: { workers: true, mobile_ui: false },
      })
    }
    if (path === '/api/config/provider/models' && method === 'POST') {
      return json(opts.modelsBody ?? { code: 'models_fetch_failed', error: '供应商服务返回 HTTP 501，请检查接口地址和 API 密钥。' }, opts.modelsStatus ?? 502)
    }
    if (path === '/api/config/provider' && method === 'GET') {
      return json({
        provider_id: modelConfigured ? 'deepseek' : '',
        provider_name: modelConfigured ? 'DeepSeek' : '',
        model: modelConfigured ? 'deepseek-chat' : '',
        reasoning_effort: 'minimal',
        base_url: modelConfigured ? 'https://api.deepseek.com/v1' : '',
        wire_api: 'chat',
        env_key: 'DEEPSEEK_API_KEY',
        api_key_configured: modelConfigured,
        api_key_hint: modelConfigured ? '••••2345' : '',
        config_path: '/tmp/config.toml',
      })
    }
    if (path === '/api/config/provider' && method === 'POST') {
      const body = request.postDataJSON()
      return json({ config: { ...body, reasoning_effort: 'minimal', api_key_configured: true, api_key_hint: '••••2345', wire_api: body.wire_api || 'chat', env_key: body.env_key || 'OPENAI_API_KEY' } })
    }
    if (path === '/api/workers' && method === 'GET') {
      return json({ enabled: true, install_enabled: true, workers: [piWorker], active: null })
    }
    if (path === '/api/tasks' && method === 'POST') {
      opts.submittedTasks?.push(request.postDataJSON())
      return json({ job_id: 'job-new-1', session_id: 'sess-new-1' }, 202)
    }
    if (path === '/api/chat' && method === 'POST') {
      opts.chatCalls?.push(request.postDataJSON())
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: opts.chatSse ?? '' })
    }
    return json({})
  })
}

const sse = (events: [string, unknown][]) => events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join('')

async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready)
  await page.screenshot({ path: `${SHOTS}/r4-onb-fix-${PHASE}-${name}.png`, animations: 'disabled' })
}

async function openSettings(page: Page) {
  await page.goto('/#tasks')
  const checklist = page.locator('.setup-checklist-card')
  await expect(checklist).toBeVisible()
  await checklist.locator('li[data-step="2"] .setup-checklist-action-btn').click()
  const dialog = page.locator('.settings-sheet.provider-settings')
  await expect(dialog).toBeVisible()
  return dialog
}

test.describe('r4 onboarding QA fixes (e2e/r4-onboard-fix.spec.ts)', () => {
  test('F1: typed model settings survive the 3-second refresh poll', async ({ page }) => {
    await setupMocks(page, { modelConfigured: true })
    await page.goto('/#tasks')
    await page.locator('.setup-checklist-card').waitFor()
    await page.locator('.setup-checklist-card li[data-step="2"] .setup-checklist-action-btn').click()
    const dialog = page.locator('.settings-sheet.provider-settings')
    await dialog.getByRole('button', { name: '自定义' }).click()
    await dialog.locator('#provider-base_url').fill('http://127.0.0.1:18795/v1')
    await dialog.locator('#provider-model').fill('fake-model')
    // Let at least one poll (every 3 s) land while the owner has unsaved edits.
    await page.waitForTimeout(3_500)
    await expect(dialog.locator('#provider-base_url')).toHaveValue('http://127.0.0.1:18795/v1')
    await expect(dialog.locator('#provider-model')).toHaveValue('fake-model')
  })

  test('F2: 自定义 starts on Chat Completions; OpenAI preset still uses Responses', async ({ page }) => {
    await setupMocks(page, { modelConfigured: false })
    const dialog = await openSettings(page)
    await dialog.getByRole('button', { name: '自定义' }).click()
    await expect(dialog.locator('#provider-wire_api')).toHaveValue('chat')
    await dialog.getByRole('button', { name: 'OpenAI' }).click()
    await expect(dialog.locator('#provider-wire_api')).toHaveValue('responses')
  })

  test('F3: a Responses stream cut gets a Chinese next step, no 重新发送 advice, raw text only under 详情', async ({ page }) => {
    const job = failedJob('OpenAI Responses stream ended before a terminal response event')
    await setupMocks(page, { modelConfigured: true, jobs: [job], sessions: [{ id: 'sess-1', channel: 'web', title: '会话 1', created_at: new Date().toISOString(), last_activity: new Date().toISOString(), job_count: 1, message_count: 1, latest_job: job }] })
    await page.goto('/#tasks')
    const notice = page.locator('.job-notice.failed.task-notice')
    await expect(notice).toBeVisible()
    await shot(page, 'F3-failed-task')
    await expect(notice).toContainText('模型服务返回不完整，可以在模型设置里把接口类型换成 Chat Completions 再试')
    await expect(notice).not.toContainText('补充说明后重新发送')
    await expect(notice.locator('button.task-notice-action')).toHaveText('去设置')
    await expect(notice.locator('.task-notice-detail')).toBeHidden()
    await notice.locator('details.task-notice-raw summary').click()
    await expect(notice.locator('.task-notice-detail')).toContainText('OpenAI Responses stream ended before a terminal response event')
  })

  test('F3: provider 401 names the API key, 429 names quota', async ({ page }) => {
    const job401 = failedJob('provider HTTP 401: invalid api key')
    await setupMocks(page, { modelConfigured: true, jobs: [job401], sessions: [{ id: 'sess-1', channel: 'web', title: '会话 1', created_at: new Date().toISOString(), last_activity: new Date().toISOString(), job_count: 1, message_count: 1, latest_job: job401 }] })
    await page.goto('/#tasks')
    const notice = page.locator('.job-notice.failed.task-notice')
    await expect(notice).toContainText('模型服务拒绝了这次请求（401）')
    await expect(notice).toContainText('API 密钥')
    await expect(notice).not.toContainText('补充说明后重新发送')
  })

  test('F3: provider 429 names quota and the next step', async ({ page }) => {
    const job429 = failedJob('provider HTTP 429: rate limit exceeded')
    await setupMocks(page, { modelConfigured: true, jobs: [job429], sessions: [{ id: 'sess-1', channel: 'web', title: '会话 1', created_at: new Date().toISOString(), last_activity: new Date().toISOString(), job_count: 1, message_count: 1, latest_job: job429 }] })
    await page.goto('/#tasks')
    const notice = page.locator('.job-notice.failed.task-notice')
    await expect(notice).toContainText('模型服务请求太频繁或额度已用完（429）')
    await expect(notice).not.toContainText('补充说明后重新发送')
  })

  test('F4: a provider HTTP 500 in 对话 shows a Chinese error bubble, raw text under 详情, and starts no agent run', async ({ page }) => {
    const submitted: any[] = []
    const chatCalls: any[] = []
    await setupMocks(page, {
      modelConfigured: true,
      submittedTasks: submitted,
      chatCalls,
      chatSse: sse([
        ['session', { session_id: 'default-session' }],
        ['message', { text: '模型服务暂时出错（500），可以稍后再试。', failure: { detail: 'HTTP 500' } }],
        ['done', { outcome: 'error' }],
      ]),
    })
    await page.goto('/#chat')
    const input = page.getByRole('textbox', { name: '给 Conveyor 发消息' })
    await input.fill('报错测试一下')
    await input.press('Enter')
    await page.waitForTimeout(800)
    await shot(page, 'F4-chat-error')
    const bubble = page.locator('.chat-failed-reply')
    await expect(bubble).toContainText('模型服务暂时出错（500），可以稍后再试。')
    await expect(bubble).not.toContainText('"message"')
    await bubble.locator('details summary').click()
    await expect(bubble.locator('code')).toHaveText('HTTP 500')
    await expect(page.getByText('转交 Codex')).toHaveCount(0)
    expect(chatCalls.length).toBe(1)
    expect(submitted).toHaveLength(0)
  })

  test('F5: checklist stays after the model is saved and the page reloads (before the first message)', async ({ page }) => {
    await setupMocks(page, { modelConfigured: true })
    await page.goto('/#tasks')
    const checklist = page.locator('.setup-checklist-card')
    await page.waitForTimeout(800)
    await shot(page, 'F5-checklist-after-save-reload')
    await expect(checklist).toBeVisible()
    await expect(checklist.locator('li[data-step="2"]')).toHaveClass(/is-done/)
    await expect(checklist.locator('li[data-step="3"]')).not.toHaveClass(/is-done/)
    await page.reload()
    await expect(checklist).toBeVisible()
    await expect(checklist.locator('li[data-step="3"] .setup-checklist-label')).toHaveText('发第一条消息')
  })

  test('F5: checklist hides once the first message has been sent', async ({ page }) => {
    const job = failedJob('')
    job.state = 'ok'
    await setupMocks(page, { modelConfigured: true, jobs: [job], sessions: [{ id: 'sess-1', channel: 'web', title: '会话 1', created_at: new Date().toISOString(), last_activity: new Date().toISOString(), job_count: 1, message_count: 2, latest_job: job }] })
    await page.goto('/#tasks')
    await expect(page.locator('.setup-checklist-card')).toHaveCount(0)
  })

  test('F6: a provider without a model list gets a plain sentence, not an HTTP code', async ({ page }) => {
    await setupMocks(page, { modelConfigured: false, modelsStatus: 502 })
    const dialog = await openSettings(page)
    await dialog.getByRole('button', { name: 'DeepSeek' }).click()
    await dialog.getByRole('button', { name: '获取模型列表' }).click()
    const error = dialog.locator('.provider-fetch-error')
    await expect(error).toContainText('这个服务商不支持自动获取模型列表，请直接在「模型」里填写模型名称。')
    await expect(error).not.toContainText('HTTP 501')
  })

  test('F7: 推理强度 lives under 高级, so a first-time owner does not see it', async ({ page }) => {
    await setupMocks(page, { modelConfigured: false })
    const dialog = await openSettings(page)
    await dialog.getByRole('button', { name: '自定义' }).click()
    await expect(dialog.locator('#provider-reasoning_effort')).toBeHidden()
    await dialog.locator('details.provider-advanced > summary').click()
    await expect(dialog.locator('#provider-reasoning_effort')).toBeVisible()
  })

  test('F9: checklist says 提问 needs no executor and 改代码 does', async ({ page }) => {
    await setupMocks(page, { modelConfigured: false })
    await page.goto('/#tasks')
    const step1 = page.locator('.setup-checklist-card li[data-step="1"]')
    await expect(step1.locator('.setup-checklist-muted').first()).toContainText('提问不需要执行者；改代码需要')
  })
})
