import { expect, test, type Page, type Route } from '@playwright/test'

// Two small fixes found while testing the goal loop by hand:
//  1. 带着记住的令牌刷新时，解锁表单先闪一秒（还会吃掉这期间打的字）→ 先显示「正在打开…」
//  2. 首览清单第 ② 步只认「模型设置」里存过的模型，服务器用环境变量配好的对话模型不算
// All copy the owner sees is Simplified Chinese.

const TOKEN = 'fix-unlock-e2e-token-not-a-secret-0001'

const systemStatus = (features: Record<string, unknown> = {}) => ({
  uptime_seconds: 60,
  load_average: [0.1, 0.1, 0.1],
  cpu_count: 4,
  memory: { total: null, available: null },
  disk: { total: 1024, used: 128, free: 896 },
  queue: { depth: 0, paused: false, states: {} },
  channels: { telegram: { configured: false }, feishu: { configured: false } },
  nodes: [],
  features: { agents: false, long_term_memory: false, routines: false, teammate: true, ...features },
})

type Options = {
  /** Where the token is remembered: this tab's sessionStorage, the device, or nowhere. */
  storedToken?: 'session' | 'device' | 'none'
  /** How long the first API response takes (ms). */
  slowFirstResponse?: number
  /** The server answers every request with 401 (a token it no longer accepts). */
  rejectToken?: boolean
  /** GET /api/config/provider reports a chat model even with nothing saved in 模型设置. */
  providerChatModel?: boolean
  /** GET /api/system/status reports a chat model even with nothing saved in 模型设置. */
  statusChatModel?: boolean
}

async function install(page: Page, options: Options = {}) {
  const storedToken = options.storedToken ?? 'session'
  const slowFirstResponse = options.slowFirstResponse ?? 0
  await page.addInitScript(([token, where]) => {
    if (where === 'session') sessionStorage.setItem('conveyor-token', token as string)
    if (where === 'device') {
      localStorage.setItem('conveyor-token', token as string)
      sessionStorage.setItem('conveyor-token', token as string)
    }
  }, [TOKEN, storedToken] as const)

  let firstResponse = true
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    // Only the very first response is slow: that is the second the owner sees on a reload.
    if (firstResponse && slowFirstResponse > 0 && path === '/api/system/status') {
      firstResponse = false
      await new Promise(resolve => setTimeout(resolve, slowFirstResponse))
    }

    if (options.rejectToken && path !== '/api/health') return json({ error: 'unauthorized' }, 401)
    if (path === '/api/health') return json({ ok: true, service: 'conveyor-web', schema_version: 1 })
    if (path === '/api/sessions' && method === 'GET') return json({ sessions: [] })
    if (path === '/api/jobs' && method === 'GET') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [] })
    if (path === '/api/events/stream') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    }
    if (path === '/api/system/status') {
      return json(systemStatus(options.statusChatModel ? { chat_model_configured: true } : {}))
    }
    if (path === '/api/config/provider' && method === 'GET') {
      // Nothing saved through 模型设置: an empty model and no key of its own.
      return json({
        config: {
          provider_id: '',
          provider_name: '',
          model: '',
          reasoning_effort: 'minimal',
          base_url: '',
          wire_api: 'chat',
          env_key: 'OPENAI_API_KEY',
          api_key_configured: false,
          api_key_hint: '',
          config_path: '/tmp/config.toml',
          ...(options.providerChatModel ? { chat_model_configured: true } : {}),
        },
      })
    }
    if (path === '/api/workers' && method === 'GET') return json({ enabled: false, workers: [], active: null })
    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })
}

const appShell = (page: Page) => page.locator('main.app-shell')
const tokenInput = (page: Page) => page.locator('#unlock-token')

test('a remembered token shows 正在打开… (no input at all) until the server answers', async ({ page }) => {
  await install(page, { storedToken: 'device', slowFirstResponse: 1000 })
  await page.goto('/#tasks')

  // 令牌确认中的那一秒：只有一个牌子，没有任何输入框可以掉字进去。
  await expect(page.getByText('正在打开…')).toBeVisible()
  await expect(tokenInput(page)).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '解锁 Conveyor 控制台' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '解锁', exact: true })).toHaveCount(0)
  await page.waitForTimeout(400)
  await expect(tokenInput(page)).toHaveCount(0)
  await expect(appShell(page)).toHaveCount(0)

  // 服务器认下令牌后才进应用，解锁表单从头到尾没出现过。
  await expect(appShell(page)).toBeVisible()
  await expect(page.getByText('正在打开…')).toHaveCount(0)
  await expect(tokenInput(page)).toHaveCount(0)
})

test('a token kept only for this tab also opens without the unlock form flashing', async ({ page }) => {
  await install(page, { storedToken: 'session', slowFirstResponse: 500 })
  await page.goto('/#tasks')
  await expect(page.getByText('正在打开…')).toBeVisible()
  await expect(tokenInput(page)).toHaveCount(0)
  await expect(appShell(page)).toBeVisible()
  await expect(tokenInput(page)).toHaveCount(0)
})

test('with no stored token the unlock form is the first thing on screen', async ({ page }) => {
  await install(page, { storedToken: 'none', slowFirstResponse: 500 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '解锁 Conveyor 控制台' })).toBeVisible()
  await expect(tokenInput(page)).toBeVisible()
  await expect(page.getByText('正在打开…')).toHaveCount(0)
})

test('a remembered token the server rejects still asks again, with its error', async ({ page }) => {
  await install(page, { storedToken: 'device', rejectToken: true })
  await page.goto('/')
  // 先「正在打开…」，令牌被拒后落回解锁表单，错误照旧。
  await expect(page.getByRole('heading', { name: '解锁 Conveyor 控制台' })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveText('访问令牌不正确，请检查后重试。')
  await expect(tokenInput(page)).toHaveAttribute('aria-invalid', 'true')
  await expect(page.getByText(/Token rejected/)).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem('conveyor-token'))).toBeNull()
})

test('a remembered token the server cannot reach keeps the unlock form and its 重试', async ({ page }) => {
  await install(page, { storedToken: 'device' })
  // Everything except the health probe fails at the network level.
  await page.route('**/api/sessions', route => route.abort('failed'))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '解锁 Conveyor 控制台' })).toBeVisible()
  await expect(page.getByRole('alert')).toContainText('连不上服务器，请检查网络后重试。')
  await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
  await expect(tokenInput(page)).toBeVisible()
})

test('checklist step ② ticks when /api/config/provider reports a chat model', async ({ page }) => {
  await install(page, { providerChatModel: true })
  await page.goto('/#tasks')
  await expect(appShell(page)).toBeVisible()

  const step2 = page.locator('.setup-checklist-card li[data-step="2"]')
  await expect(step2).toHaveClass(/is-done/)
  await expect(step2.locator('.setup-checklist-badge')).toHaveText('✓')
})

test('checklist step ② ticks when /api/system/status reports a chat model', async ({ page }) => {
  await install(page, { statusChatModel: true })
  await page.goto('/#tasks')
  await expect(appShell(page)).toBeVisible()

  const step2 = page.locator('.setup-checklist-card li[data-step="2"]')
  await expect(step2).toHaveClass(/is-done/)
  await expect(step2.locator('.setup-checklist-badge')).toHaveText('✓')
})

test('checklist step ② stays open when the server has no chat model at all', async ({ page }) => {
  await install(page)
  await page.goto('/#tasks')
  await expect(appShell(page)).toBeVisible()

  const checklist = page.locator('.setup-checklist-card')
  await expect(checklist).toBeVisible()
  await expect(checklist.locator('li[data-step="2"]')).not.toHaveClass(/is-done/)
  await expect(checklist.locator('li[data-step="2"] .setup-checklist-badge')).toHaveText('②')
  // ③ 也没有消息，所以卡片留在原处。
  await expect(checklist.locator('li[data-step="3"]')).not.toHaveClass(/is-done/)
})
