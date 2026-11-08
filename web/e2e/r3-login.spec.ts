import { expect, test, type Page, type Route } from '@playwright/test'

const NOW = new Date('2026-11-08T12:00:00+08:00')
const SHOTS = 'test-results/r3-login'
const SIZES = [{ width: 1440, height: 900 }, { width: 390, height: 844 }] as const
type Size = typeof SIZES[number]

export type WorkerQuota = {
  state: 'available' | 'cooling'
  reset_at: number | null
  reason: string | null
}

export type Worker = {
  id: string
  label: string
  installed: boolean
  version: string | null
  path: string | null
  auth: 'ok' | 'needs_login' | 'unknown'
  login_command: string | null
  install_command: string | null
  install_supported: boolean
  runnable: boolean
  enabled: boolean
  priority: number
  quota: WorkerQuota
  models_supported?: boolean
  model?: string | null
  login_supported?: boolean
}

const R3_WORKERS: Worker[] = [
  {
    id: 'agy',
    label: 'Antigravity',
    installed: false,
    version: null,
    path: null,
    auth: 'unknown',
    login_command: 'agy',
    install_command: 'curl -fsSL https://antigravity.google/cli/install.sh | bash',
    install_supported: true,
    runnable: false,
    enabled: true,
    priority: 1,
    quota: { state: 'available', reset_at: null, reason: null },
    models_supported: true,
    model: null,
    login_supported: true,
  },
  {
    id: 'claude-code',
    label: 'Claude Code',
    installed: true,
    version: '1.0.14',
    path: '/usr/local/bin/claude',
    auth: 'needs_login',
    login_command: 'claude auth login',
    install_command: 'npm install -g @anthropic-ai/claude-code',
    install_supported: true,
    runnable: true,
    enabled: true,
    priority: 2,
    quota: { state: 'available', reset_at: null, reason: null },
    models_supported: true,
    model: null,
    login_supported: true,
  },
  {
    id: 'codex',
    label: 'Codex',
    installed: true,
    version: 'v0.1.2',
    path: '/usr/local/bin/codex',
    auth: 'ok',
    login_command: 'codex login',
    install_command: 'npm install -g @openai/codex',
    install_supported: true,
    runnable: true,
    enabled: true,
    priority: 3,
    quota: { state: 'available', reset_at: null, reason: null },
    models_supported: false,
    model: null,
    login_supported: false,
  },
]

type Calls = { method: string; path: string; body: unknown }[]
type Fixture = {
  workers?: Worker[]
  installEnabled?: boolean
  loginResponses?: Record<string, unknown>
  loginPollState?: 'open_url' | 'needs_code' | 'done' | 'failed'
  providerModelsFail?: boolean
}

async function installApi(page: Page, fixture: Fixture = {}) {
  const calls: Calls = []
  let workers: Worker[] = fixture.workers
    ? JSON.parse(JSON.stringify(fixture.workers))
    : JSON.parse(JSON.stringify(R3_WORKERS))
  const installEnabled = fixture.installEnabled ?? true

  let loginSessionState: 'waiting_url' | 'open_url' | 'needs_code' | 'done' | 'failed' =
    fixture.loginPollState || 'open_url'
  let loginCodeSubmitted = ''
  let loginCancelled = false

  const providerConfig = {
    provider_id: 'deepseek',
    provider_name: 'DeepSeek',
    base_url: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat',
    wire_api: 'chat',
    reasoning_effort: 'minimal',
    env_key: 'DEEPSEEK_API_KEY',
    api_key_configured: true,
    api_key_hint: '••••8a34',
  }

  await page.clock.setFixedTime(NOW)
  await page.addInitScript(() => sessionStorage.setItem('conveyor-token', 'r3-test-token-valid'))

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    const body = method === 'GET' ? null : request.postDataJSON()
    if (method !== 'GET') calls.push({ method, path, body })
    const json = (value: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })

    if (path === '/api/health') return json({ ok: true })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: true, agents: [] })
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    if (path === '/api/events/stream')
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (path === '/api/computer/status')
      return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 10,
        load_average: [0, 0, 0],
        cpu_count: 4,
        memory: { total: null, available: null },
        disk: { total: 1000, used: 0, free: 1000 },
        queue: { depth: 0, paused: false, states: {} },
        channels: { telegram: { configured: true }, feishu: { configured: false } },
        nodes: [],
        features: { workers: true, mobile_ui: false },
      })
    }

    // Provider settings API
    if (path === '/api/config/provider' && method === 'GET') {
      return json({ config: providerConfig })
    }
    if (path === '/api/config/provider' && method === 'POST') {
      Object.assign(providerConfig, body)
      return json({ config: providerConfig })
    }
    if (path === '/api/config/provider/models' && method === 'GET') {
      if (fixture.providerModelsFail) {
        return json({ code: 'models_fetch_failed', error: '无法连接服务商接口' }, 502)
      }
      return json({
        models: [{ id: 'deepseek-chat' }, { id: 'deepseek-reasoner' }, { id: 'deepseek-coder' }],
      })
    }

    // Workers API
    if (path === '/api/workers' && method === 'GET') {
      return json({
        enabled: true,
        install_enabled: installEnabled,
        workers,
        active: 'codex',
      })
    }

    if (path.startsWith('/api/workers/') && path.endsWith('/install') && method === 'POST') {
      if (!installEnabled) return json({ error: 'install disabled', code: 'install_disabled' }, 403)
      const id = path.split('/')[3]
      const { confirm } = (body as { confirm?: boolean }) || {}
      if (!confirm) return json({ code: 'confirm_required' }, 400)
      const found = workers.find(w => w.id === id)
      if (found) {
        found.installed = true
        found.version = '2.1.0'
        found.path = `/usr/local/bin/${id}`
        found.auth = 'ok'
      }
      return json({
        ok: true,
        output: `[install] Running curl -fsSL https://antigravity.google/cli/install.sh | bash...\nDownloaded package v2.1.0\nInstalled agy to /usr/local/bin/agy\nDone.`,
      })
    }

    // Worker models API
    if (path.startsWith('/api/workers/') && path.endsWith('/models') && method === 'GET') {
      const id = path.split('/')[3]
      if (id === 'agy') {
        return json({
          models: [
            { id: 'gemini-3.8-flash-high', label: 'Gemini 3.8 Flash (High)' },
            { id: 'gemini-4-argon', label: 'Gemini 4 Argon' },
          ],
          cached_at: 1700000000,
        })
      }
      if (id === 'claude-code') {
        return json({
          models: [
            { id: 'claude-haiku-5-5', label: 'Claude 3.5 Haiku' },
            { id: 'claude-sonnet-5-5', label: 'Claude 3.5 Sonnet' },
          ],
          cached_at: 1700000000,
        })
      }
      return json({ code: 'models_unsupported' }, 400)
    }

    if (path.startsWith('/api/workers/') && path.endsWith('/model') && method === 'POST') {
      const id = path.split('/')[3]
      const { model } = (body as { model: string | null }) || {}
      const found = workers.find(w => w.id === id)
      if (found) {
        found.model = model
      }
      return json({
        enabled: true,
        install_enabled: installEnabled,
        workers,
        active: 'codex',
      })
    }

    // Worker login API
    if (path.startsWith('/api/workers/') && path.endsWith('/login/start') && method === 'POST') {
      const id = path.split('/')[3]
      loginCancelled = false
      if (fixture.loginPollState === 'failed') {
        loginSessionState = 'failed'
        return json({
          session: `sess-${id}-failed`,
          state: 'failed',
          url: null,
          message: '登录启动失败，服务未响应',
        })
      }
      return json({
        session: `sess-${id}-123`,
        state: 'open_url',
        url: `https://accounts.google.com/o/oauth2/v2/auth?client_id=${id}`,
        message: '请在浏览器中打开授权链接',
      })
    }

    if (path.startsWith('/api/workers/') && path.includes('/login/') && path.endsWith('/code') && method === 'POST') {
      const { code } = (body as { code: string }) || {}
      loginCodeSubmitted = code
      loginSessionState = 'done'
      const id = path.split('/')[3]
      const found = workers.find(w => w.id === id)
      if (found) {
        found.auth = 'ok'
      }
      return json({ ok: true })
    }

    if (path.startsWith('/api/workers/') && path.includes('/login/') && path.endsWith('/cancel') && method === 'POST') {
      loginCancelled = true
      loginSessionState = 'failed'
      return json({ session: 'sess-cancelled', state: 'failed', message: '已取消' })
    }

    if (path.startsWith('/api/workers/') && path.includes('/login/') && method === 'GET') {
      const parts = path.split('/')
      const id = parts[3]
      const sess = parts[5]
      if (loginCancelled) {
        return json({ session: sess, state: 'failed', url: null, message: '已取消' })
      }
      return json({
        session: sess,
        state: loginSessionState,
        url: `https://accounts.google.com/o/oauth2/v2/auth?client_id=${id}`,
        message: loginSessionState === 'done' ? '登录成功。' : '等待用户在浏览器中授权',
      })
    }

    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })

  return {
    calls,
    setLoginSessionState: (s: typeof loginSessionState) => {
      loginSessionState = s
    },
    getSubmittedCode: () => loginCodeSubmitted,
    isCancelled: () => loginCancelled,
  }
}

async function openWorkers(page: Page, size: Size = SIZES[0]) {
  await page.setViewportSize(size)
  await page.goto('/#workers')
  const panel = page.locator('.guard-page.guard-workers')
  await expect(panel).toBeVisible()
  return panel
}

async function openSettings(page: Page, size: Size = SIZES[0]) {
  await page.setViewportSize(size)
  await page.goto('/')
  await expect(page.locator('main.app-shell')).toBeVisible()
  const more = page.getByRole('navigation', { name: '底部导航' }).getByRole('button', { name: '更多' })
  if (await more.isVisible()) {
    await more.click()
    await page.getByRole('dialog', { name: '更多' }).getByRole('button', { name: '设置' }).click()
  } else {
    await page.locator('header.topbar').getByRole('button', { name: '设置' }).click()
  }
  const dialog = page.getByRole('dialog', { name: '模型设置' })
  await expect(dialog).toBeVisible()
  return dialog
}

async function shoot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready)
  const size = page.viewportSize()!
  await page.screenshot({
    path: `${SHOTS}/${name}-${size.width}.png`,
    fullPage: true,
    animations: 'disabled',
  })
}

test.describe('Round 3 UI (e2e/r3-login.spec.ts)', () => {
  test('install agy confirm flow and details output', async ({ page }) => {
    const api = await installApi(page)
    await openWorkers(page, SIZES[0])

    const agyCard = page.locator('li.worker-card', { hasText: 'Antigravity' })
    await expect(agyCard).toBeVisible()
    await expect(agyCard.getByText('未安装')).toBeVisible()

    // Uninstalled worker must NOT show model select even if models_supported is true
    await expect(agyCard.locator('.worker-model-box')).toHaveCount(0)
    await expect(agyCard.getByRole('combobox', { name: 'Antigravity 模型选择' })).toHaveCount(0)

    // Click 安装 button
    const installBtn = agyCard.getByRole('button', { name: '安装' })
    await expect(installBtn).toBeVisible()
    await installBtn.click()

    // Inline confirm showing the exact command
    const confirmBox = agyCard.locator('.guard-inline-confirm')
    await expect(confirmBox).toBeVisible()
    await expect(confirmBox.getByText('将在服务器上运行：')).toBeVisible()
    await expect(confirmBox.getByText('curl -fsSL https://antigravity.google/cli/install.sh | bash')).toBeVisible()

    // Confirm install
    const confirmBtn = confirmBox.getByRole('button', { name: '确认安装' })
    await confirmBtn.click()

    // Assert POST body
    const installCall = api.calls.find(c => c.path === '/api/workers/agy/install')
    expect(installCall).toBeTruthy()
    expect(installCall?.body).toEqual({ confirm: true })

    // Details output should be shown
    const details = agyCard.locator('details.worker-details')
    await expect(details).toBeVisible()
    await expect(details.getByText('Running curl -fsSL https://antigravity.google/cli/install.sh')).toBeVisible()
    await expect(agyCard.getByText('可用')).toBeVisible()

    // After install, models_supported worker now shows the model select
    await expect(agyCard.getByRole('combobox', { name: 'Antigravity 模型选择' })).toBeVisible()

    // Take screenshots
    await shoot(page, 'install-agy-desktop')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'install-agy-phone')
  })

  test('login flow open_url -> needs_code -> done (assert code POST body)', async ({ page }) => {
    const api = await installApi(page, { loginPollState: 'open_url' })
    await openWorkers(page, SIZES[0])

    const claudeCard = page.locator('li.worker-card', { hasText: 'Claude Code' })
    await expect(claudeCard).toBeVisible()
    await expect(claudeCard.getByText('需要登录')).toBeVisible()

    // Click 登录 button
    const loginBtn = claudeCard.getByRole('button', { name: '登录' })
    await expect(loginBtn).toBeVisible()
    await loginBtn.click()

    // Assert /login/start was called
    const startCall = api.calls.find(c => c.path === '/api/workers/claude-code/login/start')
    expect(startCall).toBeTruthy()

    // Primary 打开登录页面 button
    const openUrlBtn = claudeCard.locator('a[role="button"]', { hasText: '打开登录页面' })
    await expect(openUrlBtn).toBeVisible()
    await expect(openUrlBtn).toHaveAttribute('target', '_blank')
    await expect(openUrlBtn).toHaveAttribute('rel', 'noopener')
    await expect(openUrlBtn).toHaveAttribute(
      'href',
      'https://accounts.google.com/o/oauth2/v2/auth?client_id=claude-code'
    )
    await expect(claudeCard.getByText('在浏览器打开的页面完成授权后返回此处')).toBeVisible()

    await shoot(page, 'login-open-url-desktop')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'login-open-url-phone')
    await page.setViewportSize(SIZES[0])

    // Transition to needs_code state
    api.setLoginSessionState('needs_code')

    // Wait for polling (or trigger status) to display the code input
    const codeInput = claudeCard.getByPlaceholder('把登录页面显示的登录码粘贴到这里')
    await expect(codeInput).toBeVisible({ timeout: 5000 })

    await shoot(page, 'login-needs-code-desktop')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'login-needs-code-phone')
    await page.setViewportSize(SIZES[0])

    // Fill the code and submit
    await codeInput.fill('AUTH-CODE-98765')
    const submitBtn = claudeCard.getByRole('button', { name: '提交' })
    await submitBtn.click()

    // Assert code POST body
    const codeCall = api.calls.find(c => c.path.endsWith('/code'))
    expect(codeCall).toBeTruthy()
    expect(codeCall?.body).toEqual({ code: 'AUTH-CODE-98765' })

    // Status advances to done
    await expect(claudeCard.getByText('登录成功。')).toBeVisible()
    // Worker list refreshed, auth ok
    await expect(claudeCard.getByText('可用')).toBeVisible()

    // Take screenshots
    await shoot(page, 'login-flow-done-desktop')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'login-flow-done-phone')
  })

  test('login failure + 重试', async ({ page }) => {
    const api = await installApi(page, { loginPollState: 'failed' })
    await openWorkers(page, SIZES[0])

    const claudeCard = page.locator('li.worker-card', { hasText: 'Claude Code' })
    const loginBtn = claudeCard.getByRole('button', { name: '登录' })
    await loginBtn.click()

    // Assert error message and 重试 button
    await expect(claudeCard.getByText('登录启动失败，服务未响应')).toBeVisible()
    const retryBtn = claudeCard.getByRole('button', { name: '重试' })
    await expect(retryBtn).toBeVisible()

    // Next time succeed
    api.setLoginSessionState('open_url')
    await retryBtn.click()

    // Assert a second /login/start call was dispatched
    const startCalls = api.calls.filter(c => c.path === '/api/workers/claude-code/login/start')
    expect(startCalls.length).toBe(2)
  })

  test('cancel login flow', async ({ page }) => {
    const api = await installApi(page, { loginPollState: 'open_url' })
    await openWorkers(page, SIZES[0])

    const claudeCard = page.locator('li.worker-card', { hasText: 'Claude Code' })
    await claudeCard.getByRole('button', { name: '登录' }).click()

    const cancelBtn = claudeCard.getByRole('button', { name: '取消' })
    await expect(cancelBtn).toBeVisible()
    await cancelBtn.click()

    // Assert cancel POST was called
    const cancelCall = api.calls.find(c => c.path.endsWith('/cancel'))
    expect(cancelCall).toBeTruthy()
    expect(api.isCancelled()).toBe(true)

    // Failed message says "已取消"
    await expect(claudeCard.getByText('已取消')).toBeVisible()
    await expect(claudeCard.getByRole('button', { name: '重试' })).toBeVisible()
  })

  test('model select load/save on workers', async ({ page }) => {
    const api = await installApi(page)
    await openWorkers(page, SIZES[0])

    const claudeCard = page.locator('li.worker-card', { hasText: 'Claude Code' })
    const modelSelect = claudeCard.getByRole('combobox', { name: 'Claude Code 模型选择' })
    await expect(modelSelect).toBeVisible()

    // Focus triggers lazy load of /models
    await modelSelect.focus()
    const modelsCall = api.calls.find(c => c.path === '/api/workers/claude-code/models')
    // Wait for lazy fetch
    await expect(modelSelect.locator('option[value="claude-haiku-5-5"]')).toBeAttached({ timeout: 5000 })

    // Select Claude 3.5 Sonnet
    await modelSelect.selectOption('claude-sonnet-5-5')

    // Assert POST /api/workers/claude-code/model
    const saveCall = api.calls.find(c => c.path === '/api/workers/claude-code/model')
    expect(saveCall).toBeTruthy()
    expect(saveCall?.body).toEqual({ model: 'claude-sonnet-5-5' })

    // Small saved tick is displayed
    await expect(claudeCard.getByText('✓ 已保存')).toBeVisible()

    // Choose "使用默认" (value = null)
    await modelSelect.selectOption('')
    const saveDefaultCall = api.calls.filter(c => c.path === '/api/workers/claude-code/model').at(-1)
    expect(saveDefaultCall?.body).toEqual({ model: null })

    // Snapshots
    await shoot(page, 'worker-model-select-desktop')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'worker-model-select-phone')
  })

  test('provider preset fill chips', async ({ page }) => {
    await installApi(page)
    const dialog = await openSettings(page, SIZES[0])

    const presetsGroup = dialog.getByRole('group', { name: '服务商预设' })
    await expect(presetsGroup).toBeVisible()

    for (const name of ['DeepSeek', 'StepFun', 'Moonshot', 'OpenAI', '自定义']) {
      await expect(presetsGroup.getByRole('button', { name, exact: true })).toBeVisible()
    }

    // 1. Click OpenAI preset
    await presetsGroup.getByRole('button', { name: 'OpenAI', exact: true }).click()
    await expect(dialog.getByLabel('服务商代号')).toHaveValue('openai')
    await expect(dialog.getByLabel('显示名称')).toHaveValue('OpenAI')
    await expect(dialog.getByLabel('接口地址')).toHaveValue('https://api.openai.com/v1')
    await expect(dialog.getByLabel('接口类型')).toHaveValue('responses')
    await expect(dialog.getByLabel('密钥名称')).toHaveValue('OPENAI_API_KEY')

    // 2. Click StepFun preset
    await presetsGroup.getByRole('button', { name: 'StepFun', exact: true }).click()
    await expect(dialog.getByLabel('服务商代号')).toHaveValue('stepfun')
    await expect(dialog.getByLabel('显示名称')).toHaveValue('StepFun')
    await expect(dialog.getByLabel('接口地址')).toHaveValue('https://api.stepfun.com/v1')
    await expect(dialog.getByLabel('接口类型')).toHaveValue('chat')
    await expect(dialog.getByLabel('密钥名称')).toHaveValue('STEPFUN_API_KEY')

    // 3. Click Moonshot preset
    await presetsGroup.getByRole('button', { name: 'Moonshot', exact: true }).click()
    await expect(dialog.getByLabel('服务商代号')).toHaveValue('moonshot')
    await expect(dialog.getByLabel('显示名称')).toHaveValue('Moonshot')
    await expect(dialog.getByLabel('接口地址')).toHaveValue('https://api.moonshot.cn/v1')
    await expect(dialog.getByLabel('接口类型')).toHaveValue('chat')
    await expect(dialog.getByLabel('密钥名称')).toHaveValue('MOONSHOT_API_KEY')

    // 4. Click DeepSeek preset
    await presetsGroup.getByRole('button', { name: 'DeepSeek', exact: true }).click()
    await expect(dialog.getByLabel('服务商代号')).toHaveValue('deepseek')
    await expect(dialog.getByLabel('显示名称')).toHaveValue('DeepSeek')
    await expect(dialog.getByLabel('接口地址')).toHaveValue('https://api.deepseek.com/v1')
    await expect(dialog.getByLabel('接口类型')).toHaveValue('chat')
    await expect(dialog.getByLabel('密钥名称')).toHaveValue('DEEPSEEK_API_KEY')

    // Snapshots
    await shoot(page, 'provider-presets-desktop')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'provider-presets-phone')
  })

  test('model list fetch success + error', async ({ page }) => {
    // 1. Success case
    await installApi(page)
    const dialog = await openSettings(page, SIZES[0])

    const fetchBtn = dialog.getByRole('button', { name: '获取模型列表' })
    await expect(fetchBtn).toBeVisible()
    await fetchBtn.click()

    // Check datalist populated
    const datalist = dialog.locator('#provider-models-datalist')
    await expect(datalist.locator('option[value="deepseek-chat"]')).toBeAttached()
    await expect(datalist.locator('option[value="deepseek-reasoner"]')).toBeAttached()
    await expect(datalist.locator('option[value="deepseek-coder"]')).toBeAttached()

    // Model input has list attribute
    const modelInput = dialog.getByLabel('模型')
    await expect(modelInput).toHaveAttribute('list', 'provider-models-datalist')

    // 2. Error case
    await page.close()
    const errorPage = await page.context().newPage()
    await installApi(errorPage, { providerModelsFail: true })
    const errDialog = await openSettings(errorPage, SIZES[0])

    const errFetchBtn = errDialog.getByRole('button', { name: '获取模型列表' })
    await errFetchBtn.click()

    // Assert short Chinese error line
    await expect(errDialog.getByText('无法连接服务商接口')).toBeVisible()

    // Snapshots
    await shoot(errorPage, 'provider-model-fetch-error-desktop')
    await errorPage.setViewportSize(SIZES[1])
    await shoot(errorPage, 'provider-model-fetch-error-phone')
  })
})
