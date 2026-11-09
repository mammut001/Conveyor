import { expect, test, type Page, type Route } from '@playwright/test'
import { mkdirSync } from 'node:fs'

const SHOTS = 'test-results/r5-onboarding'
const R5A = 'test-results/r5a'
mkdirSync(SHOTS, { recursive: true })
mkdirSync(R5A, { recursive: true })

const SIZES = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
] as const

type MockOptions = {
  workersFeature?: boolean
  activeWorker?: string | null
  workers?: any[]
  apiKeyConfigured?: boolean
  model?: string
  sessions?: any[]
  jobs?: any[]
  submittedTasks?: any[]
  providerPosts?: any[]
  workerOverride?: any[]
}

function makeDefaultWorkerList() {
  return [
    {
      id: 'codex',
      label: 'Codex',
      installed: false,
      version: null,
      path: null,
      auth: 'unknown',
      login_command: 'codex login',
      install_command: 'npm install -g @openai/codex',
      install_supported: true,
      runnable: true,
      enabled: true,
      priority: 1,
      quota: { state: 'available', reset_at: null, reason: null },
    },
    {
      id: 'claude-code',
      label: 'Claude Code',
      installed: false,
      version: null,
      path: null,
      auth: 'needs_login',
      login_command: 'claude auth login',
      install_command: 'npm install -g @anthropic-ai/claude-code',
      install_supported: true,
      runnable: true,
      enabled: true,
      priority: 2,
      quota: { state: 'available', reset_at: null, reason: null },
    },
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
      priority: 3,
      quota: { state: 'available', reset_at: null, reason: null },
    },
  ]
}

async function setupMocks(page: Page, opts: MockOptions = {}) {
  const workersFeature = opts.workersFeature ?? true
  const apiKeyConfigured = opts.apiKeyConfigured ?? false
  const model = opts.model ?? ''
  const activeWorker = opts.activeWorker ?? null
  const workersList = opts.workers ?? makeDefaultWorkerList()
  const sessions = opts.sessions ?? []
  const jobs = opts.jobs ?? []

  await page.addInitScript(() => {
    localStorage.setItem('conveyor-token', 'test-token')
    sessionStorage.setItem('conveyor-token', 'test-token')
  })

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (data: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })

    if (path === '/api/events/stream') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    }
    if (path === '/api/health') return json({ ok: true })
    if (path === '/api/sessions') return json({ sessions })
    if (path === '/api/jobs') return json({ jobs })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json({ events: [] })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) return json({ diff: '', stats: {}, files: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/chat/history') return json({ session_id: 'default-session', messages: [] })
    if (path === '/api/computer/status') {
      return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    }
    if (path === '/api/screen/status') {
      return json({ enabled: false, available: false, reason: '', control_enabled: false, controlling: false, agent_paused: false, blocked_by: null, width: 0, height: 0 })
    }
    if (path === '/api/takeover/status') {
      return json({ enabled: false, takeover: null, privacy_mode: false, closing: null, transport_allowed: false, transport: { phase: 'idle', running: false, ready: false } })
    }
    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 10,
        load_average: [0, 0, 0],
        cpu_count: 4,
        memory: { total: null, available: null },
        disk: { total: 1000, used: 0, free: 1000 },
        queue: { depth: 0, paused: false, states: {} },
        channels: { telegram: { configured: false }, feishu: { configured: false } },
        nodes: [],
        features: { workers: workersFeature, mobile_ui: false },
      })
    }
    if (path === '/api/config/provider/models') {
      return json({ models: [{ id: 'gpt-4o' }, { id: 'claude-3-5-sonnet' }] })
    }
    if (path === '/api/config/provider' && method === 'GET') {
      return json({
        provider_id: apiKeyConfigured ? 'openai' : '',
        provider_name: apiKeyConfigured ? 'OpenAI' : '',
        model,
        reasoning_effort: 'minimal',
        // Like the server on a fresh install: no address yet, and the default key name.
        base_url: apiKeyConfigured ? 'https://api.openai.com/v1' : '',
        wire_api: 'responses',
        env_key: 'OPENAI_API_KEY',
        api_key_configured: apiKeyConfigured,
        api_key_hint: apiKeyConfigured ? '••••1234' : '',
        config_path: '/tmp/config.toml',
      })
    }
    if (path === '/api/config/provider' && method === 'POST') {
      const body = request.postDataJSON()
      opts.providerPosts?.push(body)
      return json({
        config: {
          provider_id: body.provider_id || 'openai',
          provider_name: body.provider_name || 'OpenAI',
          model: body.model || 'gpt-4o',
          reasoning_effort: 'minimal',
          base_url: body.base_url || 'https://api.openai.com/v1',
          // Echo what was saved, as the server does, so the form is not left dirty after a save.
          wire_api: body.wire_api || 'chat',
          env_key: body.env_key || 'OPENAI_API_KEY',
          api_key_configured: true,
          api_key_hint: '••••1234',
          config_path: '/tmp/config.toml',
        },
      })
    }
    if (path === '/api/workers' && method === 'GET') {
      if (!workersFeature) {
        return json({ error: 'workers disabled' }, 404)
      }
      return json({
        enabled: true,
        install_enabled: true,
        workers: workersList,
        active: activeWorker,
      })
    }
    if (path === '/api/tasks' && method === 'POST') {
      const data = request.postDataJSON()
      opts.submittedTasks?.push(data)
      return json({ job_id: 'job-new-1', session_id: 'sess-new-1' })
    }
    return json({})
  })
}

async function shoot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready)
  const size = page.viewportSize()!
  await page.screenshot({
    path: `${SHOTS}/${name}-${size.width}.png`,
    fullPage: false,
    animations: 'disabled',
  })
}

test.describe('Round 5 Onboarding & First-run (e2e/r5-onboarding.spec.ts)', () => {
  test('fresh setup shows checklist on tasks and chat with correct states and never blocks typing', async ({ page }) => {
    await setupMocks(page, {
      workersFeature: true,
      apiKeyConfigured: false,
      model: '',
      activeWorker: null,
      sessions: [],
      jobs: [],
    })

    // 1. Visit Tasks page (default #tasks)
    await page.goto('/#tasks')
    const checklistTasks = page.locator('.setup-checklist-card')
    await expect(checklistTasks).toBeVisible()
    await expect(checklistTasks.locator('.setup-checklist-title')).toContainText('还差几步就能用')

    // Initial states: none should be checked
    const step1 = checklistTasks.locator('li[data-step="1"]')
    const step2 = checklistTasks.locator('li[data-step="2"]')
    const step3 = checklistTasks.locator('li[data-step="3"]')

    await expect(step1).not.toHaveClass(/is-done/)
    await expect(step1.locator('.setup-checklist-badge')).toHaveText('①')
    await expect(step1.locator('.setup-checklist-link')).toHaveText('选一个执行者并登录')

    await expect(step2).not.toHaveClass(/is-done/)
    await expect(step2.locator('.setup-checklist-badge')).toHaveText('②')
    await expect(step2.locator('.setup-checklist-action-btn')).toHaveText('配置模型')

    await expect(step3).not.toHaveClass(/is-done/)
    await expect(step3.locator('.setup-checklist-badge')).toHaveText('③')
    await expect(step3.locator('.setup-checklist-label')).toHaveText('发第一条消息')

    // Never blocks typing: composer is functional and interactive
    const composer = page.getByRole('textbox', { name: '任务内容' })
    await expect(composer).toBeVisible()
    await composer.fill('测试任务输入')
    await expect(composer).toHaveValue('测试任务输入')

    // Capture visual snapshots at 1440 and 390
    await page.setViewportSize(SIZES[0])
    await shoot(page, 'fresh-tasks')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'fresh-tasks')

    // 2. Visit Chat page (#chat)
    await page.goto('/#chat')
    const checklistChat = page.locator('.setup-checklist-card')
    await expect(checklistChat).toBeVisible()
    await expect(checklistChat.locator('.setup-checklist-title')).toContainText('还差几步就能用')

    // Typing in chat composer is also functional
    const chatInput = page.getByRole('textbox', { name: '给 Conveyor 发消息' })
    await expect(chatInput).toBeVisible()
    await chatInput.fill('测试对话输入')
    await expect(chatInput).toHaveValue('测试对话输入')

    // Capture visual snapshots for chat
    await page.setViewportSize(SIZES[0])
    await shoot(page, 'fresh-chat')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'fresh-chat')
  })

  test('checklist links work: workers link navigates to #workers and settings button opens settings modal', async ({ page }) => {
    await setupMocks(page, {
      workersFeature: true,
      apiKeyConfigured: false,
      model: '',
      activeWorker: null,
    })

    await page.goto('/#tasks')
    const checklist = page.locator('.setup-checklist-card')
    await expect(checklist).toBeVisible()

    // 1. Click step 2 button -> opens ProviderSettings dialog
    const settingsBtn = checklist.locator('li[data-step="2"] .setup-checklist-action-btn')
    await settingsBtn.click()
    const settingsDialog = page.locator('.settings-sheet.provider-settings')
    await expect(settingsDialog).toBeVisible()
    await expect(settingsDialog.locator('#provider-settings-title')).toContainText('模型设置')

    // Close settings dialog
    await page.locator('.settings-sheet .close-button').click()
    await expect(settingsDialog).toBeHidden()

    // 2. Click step 1 link -> navigates to #workers
    const workersLink = checklist.locator('li[data-step="1"] .setup-checklist-link')
    await workersLink.click()
    await expect(page).toHaveURL(/#workers/)
    await expect(page.locator('.guard-workers')).toBeVisible()
  })

  test('checklist shows muted note when workers feature is disabled', async ({ page }) => {
    await setupMocks(page, {
      workersFeature: false,
      apiKeyConfigured: false,
      model: '',
    })

    await page.goto('/#tasks')
    const checklist = page.locator('.setup-checklist-card')
    await expect(checklist).toBeVisible()

    // Step 1 does not link to #workers, shows muted note
    const step1 = checklist.locator('li[data-step="1"]')
    await expect(step1.locator('.setup-checklist-link')).toHaveCount(0)
    await expect(step1.locator('.setup-checklist-label')).toHaveText('选一个执行者并登录')
    await expect(step1.locator('.setup-checklist-muted')).toHaveText('管理员可以开启「执行者」功能')
  })

  test('checklist dynamically transitions to completed ✓ state when model is configured', async ({ page }) => {
    await setupMocks(page, {
      workersFeature: true,
      apiKeyConfigured: false,
      model: '',
      activeWorker: null,
    })

    await page.goto('/#tasks')
    const checklist = page.locator('.setup-checklist-card')
    await expect(checklist).toBeVisible()

    // Step 2 initially not done
    const step2 = checklist.locator('li[data-step="2"]')
    await expect(step2).not.toHaveClass(/is-done/)
    await expect(step2.locator('.setup-checklist-badge')).toHaveText('②')

    // Click 配置模型 to open settings modal
    await step2.locator('.setup-checklist-action-btn').click()
    const settingsDialog = page.locator('.settings-sheet.provider-settings')
    await expect(settingsDialog).toBeVisible()

    // Fill in preset, model and API key
    await settingsDialog.getByRole('button', { name: 'DeepSeek' }).click()
    const modelInput = settingsDialog.locator('input[id="provider-model"]')
    await modelInput.fill('deepseek-chat')
    const apiKeyInput = settingsDialog.locator('input[id="provider-api_key"]')
    await apiKeyInput.fill('sk-test-secret-key-12345')

    // Save settings
    const saveBtn = settingsDialog.locator('button.primary', { hasText: '保存' })
    await saveBtn.click()

    // Close settings dialog
    await page.locator('.settings-sheet .close-button').click()
    await expect(settingsDialog).toBeHidden()

    // Step 2 now displays completed ✓ state!
    await expect(step2).toHaveClass(/is-done/)
    await expect(step2.locator('.setup-checklist-badge')).toHaveText('✓')

    await page.setViewportSize(SIZES[0])
    await shoot(page, 'step2-done')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'step2-done')
  })

  test('dismiss persists in localStorage across reloads', async ({ page }) => {
    await setupMocks(page, {
      workersFeature: true,
      apiKeyConfigured: false,
      model: '',
      activeWorker: null,
    })

    await page.goto('/#tasks')
    const checklist = page.locator('.setup-checklist-card')
    await expect(checklist).toBeVisible()

    // Click dismiss button ✕
    const dismissBtn = checklist.locator('.setup-checklist-close')
    await dismissBtn.click()
    await expect(checklist).toBeHidden()

    // Assert stored in localStorage
    const dismissedVal = await page.evaluate(() => localStorage.getItem('conveyor-onboarding-dismissed'))
    expect(dismissedVal).toBe('true')

    // Reload page -> should stay dismissed
    await page.reload()
    await expect(page.locator('.setup-checklist-card')).toBeHidden()

    await page.setViewportSize(SIZES[0])
    await shoot(page, 'dismissed')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'dismissed')
  })

  test('task start failure caused by missing CLI shows friendly message and 去执行者 button', async ({ page }) => {
    const failedJob = {
      id: 'job-fail-cli',
      state: 'failed',
      mode: 'run',
      channel: 'web',
      chat_id: 'default',
      created_at: new Date(Date.now() - 60_000).toISOString(),
      updated_at: new Date(Date.now() - 30_000).toISOString(),
      prompt_preview: '帮我重构登录模块',
      error: "[Errno 2] No such file or directory: 'codex'",
      changed_files: [],
      metadata: {},
    }

    await setupMocks(page, {
      workersFeature: true,
      jobs: [failedJob],
      sessions: [{
        id: 'sess-1',
        channel: 'web',
        title: '会话 1',
        created_at: new Date().toISOString(),
        last_activity: new Date().toISOString(),
        job_count: 1,
        message_count: 1,
        latest_job: failedJob,
      }],
    })

    await page.goto('/#tasks')
    const notice = page.locator('.job-notice.failed.task-notice')
    await expect(notice).toBeVisible()

    // Check Chinese failure message
    await expect(notice).toContainText('还没有可用的执行者，先去「执行者」页安装并登录一个')
    await expect(notice).not.toContainText('请稍后重试')

    // Action button 去执行者
    const actionBtn = notice.locator('button.task-notice-action')
    await expect(actionBtn).toBeVisible()
    await expect(actionBtn).toHaveText('去执行者')

    // Raw technical error folded under 详情
    const rawDetails = notice.locator('details.task-notice-raw')
    await expect(rawDetails).toBeVisible()
    await expect(notice.locator('.task-notice-detail')).toBeHidden()
    await rawDetails.locator('summary').click()
    await expect(notice.locator('.task-notice-detail')).toBeVisible()
    await expect(notice.locator('.task-notice-detail')).toContainText("No such file or directory: 'codex'")

    // Clicking 去执行者 navigates to #workers
    await actionBtn.click()
    await expect(page).toHaveURL(/#workers/)
    await expect(page.locator('.guard-workers')).toBeVisible()

    // Take screenshots
    await page.goto('/#tasks')
    await page.setViewportSize(SIZES[0])
    await shoot(page, 'task-failure-missing-cli')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'task-failure-missing-cli')
  })

  test('task start failure caused by provider / model error shows 去设置 button', async ({ page }) => {
    const failedJob = {
      id: 'job-fail-provider',
      state: 'failed',
      mode: 'run',
      channel: 'web',
      chat_id: 'default',
      created_at: new Date(Date.now() - 60_000).toISOString(),
      updated_at: new Date(Date.now() - 30_000).toISOString(),
      prompt_preview: '分析项目架构',
      error: 'chat tier is disabled (set CONVEYOR_CHAT_MODE and CONVEYOR_CHAT_*)',
      changed_files: [],
      metadata: {},
    }

    await setupMocks(page, {
      workersFeature: true,
      jobs: [failedJob],
      sessions: [{
        id: 'sess-1',
        channel: 'web',
        title: '会话 1',
        created_at: new Date().toISOString(),
        last_activity: new Date().toISOString(),
        job_count: 1,
        message_count: 1,
        latest_job: failedJob,
      }],
    })

    await page.goto('/#tasks')
    const notice = page.locator('.job-notice.failed.task-notice')
    await expect(notice).toBeVisible()

    // Check Chinese model failure message
    await expect(notice).toContainText('还没有可用的模型，先在「设置」里配置模型')

    // Action button 去设置
    const actionBtn = notice.locator('button.task-notice-action')
    await expect(actionBtn).toBeVisible()
    await expect(actionBtn).toHaveText('去设置')

    // Clicking 去设置 opens provider settings dialog
    await actionBtn.click()
    const settingsDialog = page.locator('.settings-sheet.provider-settings')
    await expect(settingsDialog).toBeVisible()
    await expect(settingsDialog.locator('#provider-settings-title')).toContainText('模型设置')

    await page.setViewportSize(SIZES[0])
    await shoot(page, 'task-failure-missing-model')
    await page.setViewportSize(SIZES[1])
    await shoot(page, 'task-failure-missing-model')
  })

  test('N1: a custom provider saves from the visible fields without opening 高级', async ({ page }) => {
    const providerPosts: any[] = []
    await setupMocks(page, { apiKeyConfigured: false, model: '', providerPosts })

    await page.goto('/#tasks')
    await page.locator('.setup-checklist-card li[data-step="2"] .setup-checklist-action-btn').click()
    const dialog = page.locator('.settings-sheet.provider-settings')
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: '自定义', exact: true }).click()

    // Only the visible fields are filled: no 服务商代号 and no 密钥名称.
    await dialog.locator('#provider-provider_name').fill('我的模型')
    await dialog.locator('#provider-base_url').fill('https://api.example.com/v1')
    await dialog.locator('#provider-model').fill('my-model-1')
    await dialog.locator('#provider-api_key').fill('sk-custom-secret-9999')
    await dialog.getByRole('button', { name: '保存', exact: true }).click()

    await expect(dialog.getByText('✓ 已保存，新任务会使用这个配置。')).toBeVisible()
    await expect(dialog.getByRole('alert')).toHaveCount(0)
    // 服务商代号 comes from the 接口地址 host (the display name has no Latin letters); 密钥名称 is <CODE>_API_KEY.
    expect(providerPosts.at(-1)).toMatchObject({
      provider_id: 'api-example-com',
      provider_name: '我的模型',
      env_key: 'API_EXAMPLE_COM_API_KEY',
      base_url: 'https://api.example.com/v1',
      api_key: 'sk-custom-secret-9999',
    })
    // The 高级 section stays collapsed: the owner never had to open it.
    await expect(dialog.locator('details').first()).not.toHaveAttribute('open', '')
    await page.screenshot({ path: `${R5A}/n1-custom-saved-1440.png` })
  })

  test('N1: a display name with Latin letters becomes 服务商代号, and a reserved prefix is kept out of the key name', async ({ page }) => {
    const providerPosts: any[] = []
    await setupMocks(page, { apiKeyConfigured: false, model: '', providerPosts })

    await page.goto('/#tasks')
    await page.locator('.setup-checklist-card li[data-step="2"] .setup-checklist-action-btn').click()
    const dialog = page.locator('.settings-sheet.provider-settings')
    await dialog.getByRole('button', { name: '自定义', exact: true }).click()
    await dialog.locator('#provider-provider_name').fill('Pi Local')
    await dialog.locator('#provider-base_url').fill('http://127.0.0.1:18795/v1')
    await dialog.locator('#provider-model').fill('fake-model')
    await dialog.locator('#provider-api_key').fill('sk-local-test-1234')
    await dialog.getByRole('button', { name: '保存', exact: true }).click()

    await expect(dialog.getByText('✓ 已保存，新任务会使用这个配置。')).toBeVisible()
    // "PI_" is a runtime prefix the server refuses for credentials, so the key moves under PROVIDER_.
    expect(providerPosts.at(-1)).toMatchObject({ provider_id: 'pi-local', env_key: 'PROVIDER_PI_LOCAL_API_KEY' })
  })

  test('N2: a 对话 message ticks checklist step 3 even though 对话 sessions are not in the 任务 list', async ({ page }) => {
    const now = new Date().toISOString()
    await setupMocks(page, {
      apiKeyConfigured: false,
      model: '',
      sessions: [{ id: 'webchat-abc123', channel: 'web', title: '对话 1', created_at: now, last_activity: now, job_count: 0, message_count: 2 }],
    })

    await page.goto('/#tasks')
    const checklist = page.locator('.setup-checklist-card')
    await expect(checklist).toBeVisible()
    await expect(checklist.locator('li[data-step="3"]')).toHaveClass(/is-done/)
    await expect(checklist.locator('li[data-step="3"] .setup-checklist-badge')).toHaveText('✓')
  })

  test('F3: a workspace configuration failure names the setting to fix and does not ask the owner to resend', async ({ page }) => {
    const failedJob = {
      id: 'job-fail-workspace',
      state: 'failed',
      mode: 'fix',
      channel: 'web',
      chat_id: 'default',
      created_at: new Date(Date.now() - 60_000).toISOString(),
      updated_at: new Date(Date.now() - 30_000).toISOString(),
      prompt_preview: '在 README 末尾加一行说明',
      error: 'CODEX_WORKSPACE_ROOT does not exist or is not a directory: /srv/conveyor/data/ws',
      changed_files: [],
      metadata: {},
    }
    await setupMocks(page, {
      workersFeature: true,
      jobs: [failedJob],
      sessions: [{
        id: 'sess-workspace',
        channel: 'web',
        title: '会话 1',
        created_at: new Date().toISOString(),
        last_activity: new Date().toISOString(),
        job_count: 1,
        message_count: 1,
        latest_job: failedJob,
      }],
    })

    await page.goto('/#tasks')
    const notice = page.locator('.job-notice.failed.task-notice')
    await expect(notice).toBeVisible()
    await expect(notice).toContainText('项目文件夹没有设置好')
    await expect(notice).not.toContainText('可以补充说明后重新发送')
    await expect(notice).not.toContainText('重新发送')
    await expect(notice.locator('.task-notice-hint')).toHaveCount(0)
    await expect(notice.locator('button.task-notice-action')).toHaveCount(0)

    // The raw English error stays one click away under 详情.
    await notice.locator('details.task-notice-raw summary').click()
    await expect(notice.locator('.task-notice-detail')).toContainText('CODEX_WORKSPACE_ROOT does not exist')
  })

  test('N5: after installing Pi without a provider, the execution banner points to 模型设置 instead of login', async ({ page }) => {
    const piOnly = [
      ...makeDefaultWorkerList().map(worker => ({ ...worker, installed: false })),
      {
        id: 'pi',
        label: 'Pi（内置）',
        installed: true,
        version: '0.5.0',
        path: null,
        auth: 'needs_login',
        auth_hint: null,
        login_command: '',
        install_command: null,
        install_supported: false,
        runnable: true,
        enabled: true,
        priority: 0,
        provider: { configured: false, provider_name: '', model: '' },
        quota: { state: 'available', reset_at: null, reason: null },
      },
    ]
    await setupMocks(page, { workers: piOnly, workersFeature: true })

    await page.goto('/#workers')
    const banner = page.locator('.guard-workers')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText('Pi 已安装，还差一步')
    await expect(banner).toContainText('去「设置 → 模型设置」填好服务商')
    await expect(banner).not.toContainText('请先登录标着「需要登录」的执行者')
  })
})
