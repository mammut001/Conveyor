import { expect, test, type Page, type Route } from '@playwright/test'
import type { SupervisorCapabilities } from '../src/components/SupervisorWorkspace'

// U2 — executors must be shown to actually work, and failures must be actionable.
//  - 执行者 page: a 测试 button really calls the executor and shows 可以用 / a red failure + 技术细节.
//  - 项目 role cards: the badge reflects Pi's last 测试 (已测试可用 / 上次测试失败 / 已就绪 + 测试一下).
//  - 目标 / 任务与结果 start: an executor_check_failed 409 offers 去执行者页面测试.
//  - First-run checklist: step ① is honest and can run the same 测试 inline.
//  - 对话 failure bubble: the raw provider text hides behind 技术细节; 打开模型设置 opens the dialog.

const TOKEN = 'u2-executor-check-token-0001'
const NOW_ISO = new Date().toISOString()

type CheckResult = {
  ok: boolean | null
  worker_id: string
  provider: string | null
  model: string | null
  latency_ms: number | null
  kind: string
  message: string
  detail?: string | null
  checked_at: string | null
}

function piWorker(overrides: Record<string, unknown> = {}) {
  return {
    id: 'pi',
    label: 'Pi（内置）',
    installed: true,
    version: '0.5.0',
    path: null,
    auth: 'ok',
    login_command: '',
    install_command: null,
    install_supported: false,
    runnable: true,
    enabled: true,
    priority: 0,
    quota: { state: 'available', reset_at: null, reason: null },
    provider: { configured: true, provider_name: 'local', model: 'fake-model' },
    last_check: null,
    ...overrides,
  }
}

function codexWorker(overrides: Record<string, unknown> = {}) {
  return {
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
    priority: 1,
    quota: { state: 'available', reset_at: null, reason: null },
    last_check: null,
    ...overrides,
  }
}

const OK_CHECK: CheckResult = {
  ok: true,
  worker_id: 'pi',
  provider: 'local',
  model: 'fake-model',
  latency_ms: 132,
  kind: 'chat',
  message: '可以用',
  checked_at: NOW_ISO,
}

const capabilities = (): SupervisorCapabilities => ({
  enabled: true,
  coordinator: { executor: 'chat', model: 'fake-model' },
  roles: {
    analyst: { executor: 'pool', model: null, available: true },
    implementer: { executor: 'pool', model: null, available: true },
    reviewer: { executor: 'pool', model: null, available: true },
  },
  executors: [{ name: 'pool', label: '执行者池（自动轮换）', available: true }],
  deployment: { configured: false, can_start: false },
  source_machine: 'dev-box',
})

const repositories = () => [
  {
    id: 'repo-ws',
    name: 'ws',
    path: '/tmp/ws',
    source_machine: 'dev-box',
    validation_source: 'none',
    worktrees: [
      {
        id: 'wt-managed',
        path: '/tmp/ws-wt/fix',
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

type Options = {
  workers?: Record<string, unknown>[]
  active?: string | null
  /** Result of POST /api/workers/<id>/check. */
  checkResult?: CheckResult
  /** Hold the check request open this many ms so 正在测试… is observable. */
  checkDelayMs?: number
  /** Make the 目标 start fail with executor_check_failed (409). */
  failGoalStart?: boolean
  /** SSE body for POST /api/chat (failure bubble). */
  chatSse?: string
  providerConfigured?: boolean
}

async function install(page: Page, options: Options = {}) {
  const workers = options.workers ?? [piWorker()]
  const active = options.active !== undefined ? options.active : 'pi'
  const checkResult = options.checkResult ?? OK_CHECK
  const providerConfigured = options.providerConfigured ?? false

  await page.addInitScript(token => {
    sessionStorage.setItem('conveyor-token', token)
    localStorage.removeItem('conveyor-onboarding-dismissed')
  }, TOKEN)

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/health') return json({ ok: true })
    if (path === '/api/events/stream') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    }
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/computer/status') {
      return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    }
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'), messages: [] })
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
        features: { workers: true, agent_supervisor: true, mobile_ui: false },
      })
    }
    if (path === '/api/config/provider' && method === 'GET') {
      return json({
        provider_id: providerConfigured ? 'pi-local' : '',
        provider_name: providerConfigured ? 'Pi Local' : '',
        model: providerConfigured ? 'fake-model' : '',
        base_url: providerConfigured ? 'http://127.0.0.1:18795/v1' : '',
        wire_api: 'chat',
        env_key: 'PROVIDER_PI_LOCAL_API_KEY',
        api_key_configured: providerConfigured,
        api_key_hint: providerConfigured ? '••••1234' : '',
        config_path: '/tmp/config.toml',
        chat_model_configured: providerConfigured,
      })
    }

    // Workers API
    if (path === '/api/workers' && method === 'GET') {
      return json({ enabled: true, install_enabled: true, workers, active })
    }
    const checkMatch = path.match(/^\/api\/workers\/([^/]+)\/check$/)
    if (checkMatch && method === 'POST') {
      if (options.checkDelayMs) await new Promise(resolve => setTimeout(resolve, options.checkDelayMs))
      const id = checkMatch[1]
      const check: CheckResult = { ...checkResult, worker_id: id }
      const updated = workers.map(w => (w.id === id ? { ...w, last_check: check } : w))
      return json({ check, workers: updated, enabled: true, install_enabled: true, active })
    }

    // Supervisor contract
    if (path === '/api/supervisor/capabilities' && method === 'GET') return json(capabilities())
    if (path === '/api/supervisor/repositories' && method === 'GET') return json({ repositories: repositories() })
    if (path === '/api/supervisor/tasks' && method === 'GET') return json({ tasks: [], count: 0 })
    if (/^\/api\/supervisor\/repositories\/[^/]+\/validation$/.test(path)) {
      return json({ repo_id: 'repo-ws', candidates: [], selected: [], source: 'none', env_commands: [], effective: [] })
    }
    if (path === '/api/supervisor/goals' && method === 'GET') return json({ goals: [], count: 0 })
    if (path === '/api/supervisor/goals' && method === 'POST') {
      if (options.failGoalStart) {
        return json({ code: 'executor_check_failed', error: '执行者（Pi）上次测试没有通过，先去「执行者」页测好再开始。' }, 409)
      }
      return json({
        id: 'goal-new', repo_id: 'repo-ws', worktree_id: 'wt-managed', title: '新目标', outcome: 'x',
        state: 'running', created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        question: '', summary: '', stop_reason: '', last_next_prompt: '', iterations: [], iterations_used: 0,
      }, 201)
    }

    // Chat SSE (failure bubble)
    if (path === '/api/chat' && method === 'POST') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: options.chatSse ?? '' })
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })
}

// ---------------------------------------------------------------------------
// 执行者 page
// ---------------------------------------------------------------------------

test('执行者: 测试 shows 正在测试… then the 可以用 line', async ({ page }) => {
  await install(page, { workers: [codexWorker()], active: 'codex', checkDelayMs: 400 })
  await page.goto('/#workers')

  const codex = page.locator('li.worker-card', { hasText: 'Codex' })
  await expect(codex.getByText('还没测试过')).toBeVisible()

  const checkBtn = codex.getByRole('button', { name: '测试', exact: true })
  await checkBtn.click()

  // While running: disabled + 正在测试… + the 45s hint as the button title.
  await expect(codex.getByRole('button', { name: '正在测试…', exact: true })).toBeVisible()
  await expect(codex.getByRole('button', { name: '正在测试…', exact: true })).toBeDisabled()
  await expect(codex.getByRole('button', { name: '正在测试…', exact: true })).toHaveAttribute('title', '最多等 45 秒')

  // Then the green ok line with provider/model (relative time lives in its title).
  await expect(codex.getByText('可以用 · local/fake-model', { exact: false })).toBeVisible()
  await expect(codex.getByText('还没测试过')).toHaveCount(0)
})

test('执行者: bridged Pi hides internal conveyor-chat id and shows plain Chinese label', async ({ page }) => {
  await install(page, {
    workers: [
      piWorker({
        provider: {
          configured: true,
          provider_name: '和对话用同一个模型（fake-model）',
          model: 'fake-model',
        },
        last_check: {
          ok: true,
          worker_id: 'pi',
          provider: 'conveyor-chat',
          provider_name: '和对话用同一个模型（fake-model）',
          model: 'fake-model',
          latency_ms: 498,
          kind: 'ok',
          message: '可以用：和对话用同一个模型（fake-model） 回复正常（498 毫秒）。',
          checked_at: NOW_ISO,
        },
      }),
    ],
    active: 'pi',
  })
  await page.goto('/#workers')

  const pi = page.locator('li.worker-card', { hasText: 'Pi（内置）' })
  await expect(pi.getByText('可以用 · 和对话用同一个模型（fake-model）')).toBeVisible()
  await expect(pi.getByTestId('pi-provider-box')).toContainText('和对话用同一个模型（fake-model）')
  await expect(pi).not.toContainText('conveyor-chat')
})

test('执行者: real_run check shows provider/model from worker.provider', async ({ page }) => {
  await install(page, {
    workers: [
      piWorker({
        provider: { configured: true, provider_name: 'StepFun', model: 'step-5-preview' },
        last_check: {
          ok: true, worker_id: 'pi', provider: null, provider_name: null, model: null, latency_ms: null, kind: 'real_run', message: '', checked_at: NOW_ISO,
        },
      }),
    ],
    active: 'pi',
  })
  await page.goto('/#workers')
  const pi = page.locator('li.worker-card', { hasText: 'Pi（内置）' })
  await expect(pi.getByText('可以用 · StepFun/step-5-preview')).toBeVisible()
  await expect(pi.getByText('未知服务商')).toBeHidden()
})

test('执行者: real_run check shows 最近一次真实任务跑通了 when no provider', async ({ page }) => {
  await install(page, {
    workers: [
      piWorker({
        provider: null,
        last_check: {
          ok: true, worker_id: 'pi', provider: null, provider_name: null, model: null, latency_ms: null, kind: 'real_run', message: '', checked_at: NOW_ISO,
        },
      }),
    ],
    active: 'pi',
  })
  await page.goto('/#workers')
  const pi = page.locator('li.worker-card', { hasText: 'Pi（内置）' })
  await expect(pi.getByText('可以用 · 最近一次真实任务跑通了')).toBeVisible()
})

test('执行者: a failing 测试 shows the red message and a collapsed 技术细节', async ({ page }) => {
  await install(page, {
    workers: [codexWorker()],
    active: 'codex',
    checkResult: {
      ok: false, worker_id: 'codex', provider: 'local', model: 'fake-model', latency_ms: 220,
      kind: 'chat', message: '这个执行者现在连不上模型服务。', detail: '401: {"message":"Authentication Fails"}', checked_at: NOW_ISO,
    },
  })
  await page.goto('/#workers')

  const codex = page.locator('li.worker-card', { hasText: 'Codex' })
  await codex.getByRole('button', { name: '测试', exact: true }).click()

  await expect(codex.getByText('这个执行者现在连不上模型服务。')).toBeVisible()
  // The raw provider text stays hidden until 技术细节 is opened.
  const rawTech = codex.getByText('Authentication Fails')
  await expect(rawTech).toBeHidden()
  await codex.locator('details.guard-tech-details', { hasText: 'Authentication Fails' }).locator('summary').click()
  await expect(rawTech).toBeVisible()
})

// ---------------------------------------------------------------------------
// 项目 role cards
// ---------------------------------------------------------------------------

const analystCard = (page: Page) => page.locator('.sup-role-card', { hasText: '分析' })

test('项目 role card: last_check ok → 已测试可用', async ({ page }) => {
  await install(page, { workers: [piWorker({ last_check: OK_CHECK })] })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'ws / fix/console' })).toBeVisible()
  await expect(analystCard(page).locator('.sup-role-badge')).toHaveText('已测试可用')
  await expect(analystCard(page).locator('.sup-role-badge')).toHaveClass(/is-ok/)
})

test('项目 role card: last_check ok false → 上次测试失败 with the message as title', async ({ page }) => {
  const failed = { ...OK_CHECK, ok: false, message: 'Pi 的密钥不对，模型服务拒绝了请求' }
  await install(page, { workers: [piWorker({ last_check: failed })] })
  await page.goto('/#code')
  const badge = analystCard(page).locator('.sup-role-badge')
  await expect(badge).toHaveText('上次测试失败')
  await expect(badge).toHaveClass(/is-err/)
  await expect(badge).toHaveAttribute('title', 'Pi 的密钥不对，模型服务拒绝了请求')
})

test('项目 role card: no check → 已就绪 + 测试一下 that updates the badge', async ({ page }) => {
  await install(page, { workers: [piWorker({ last_check: null })] })
  await page.goto('/#code')
  const card = analystCard(page)
  await expect(card.locator('.sup-role-badge')).toHaveText('已就绪')

  await card.getByRole('button', { name: '测试一下' }).click()
  await expect(card.locator('.sup-role-badge')).toHaveText('已测试可用')
})

// ---------------------------------------------------------------------------
// 目标 / 任务与结果 start: executor_check_failed
// ---------------------------------------------------------------------------

test('目标 start 409 executor_check_failed → error text + 去执行者页面测试 navigates to 执行者', async ({ page }) => {
  await install(page, { workers: [piWorker({ last_check: OK_CHECK })], failGoalStart: true })
  await page.goto('/#code')
  await page.getByRole('tab', { name: '目标' }).click()

  const form = page.getByRole('region', { name: '描述你想要的结果' })
  await form.getByRole('textbox', { name: '描述你想要的结果' }).fill('把构建时间压到 30 秒以内')
  await form.getByRole('button', { name: '开始', exact: true }).click()

  await expect(form).toContainText('执行者（Pi）上次测试没有通过，先去「执行者」页测好再开始。')
  const goBtn = form.getByRole('button', { name: '去执行者页面测试' })
  await expect(goBtn).toBeVisible()
  await goBtn.click()
  await expect(page.locator('.guard-page.guard-workers')).toBeVisible()
})

// ---------------------------------------------------------------------------
// First-run checklist
// ---------------------------------------------------------------------------

test('checklist: installed Pi with no check → no 都准备好了, 测试 button visible; after ok check ① ticks', async ({ page }) => {
  await install(page, { workers: [piWorker({ last_check: null })], active: 'pi' })
  await page.goto('/#tasks')

  const step1 = page.locator('.setup-checklist-card li[data-step="1"]')
  await expect(step1).toBeVisible()
  await expect(step1.locator('.setup-checklist-badge')).toHaveText('①')
  await expect(page.locator('.setup-checklist-done')).toHaveCount(0)

  const testBtn = step1.getByRole('button', { name: '测试' })
  await expect(testBtn).toBeVisible()
  await testBtn.click()

  await expect(step1.locator('.setup-checklist-badge')).toHaveText('✓')
  // ② and ③ are still open, so the card stays (都准备好了 only appears once all three pass).
  await expect(page.locator('.setup-checklist-card')).toBeVisible()
})

test('checklist: a failed executor 测试 keeps ① open and shows the failure message', async ({ page }) => {
  await install(page, {
    workers: [piWorker({ last_check: null })],
    active: 'pi',
    checkResult: {
      ok: false, worker_id: 'pi', provider: 'local', model: 'fake-model', latency_ms: 90,
      kind: 'chat', message: 'Pi 的密钥不对，去「模型设置」里检查 API 密钥', detail: '401 invalid key', checked_at: NOW_ISO,
    },
  })
  await page.goto('/#tasks')

  const step1 = page.locator('.setup-checklist-card li[data-step="1"]')
  await step1.getByRole('button', { name: '测试' }).click()

  await expect(step1.getByText('Pi 的密钥不对，去「模型设置」里检查 API 密钥')).toBeVisible()
  await expect(step1.locator('.setup-checklist-badge')).toHaveText('①')
  await expect(page.locator('.setup-checklist-done')).toHaveCount(0)
})

// ---------------------------------------------------------------------------
// 对话 failure bubble
// ---------------------------------------------------------------------------

test('对话 failure bubble: raw JSON hides behind 技术细节; 打开模型设置 opens the dialog', async ({ page }) => {
  const failureText = '连不上模型服务，请检查「模型设置」里的接口地址，或稍后再试。\n原始信息：401: {"message":"Authentication Fails, Your api key: ****-key is invalid (request_id 123)"}'
  const chatSse = [
    'event: session\ndata: {"session_id":"web:web-console:webchat-new0001"}\n\n',
    `event: message\ndata: ${JSON.stringify({ text: failureText, failure: { detail: '' } })}\n\n`,
    'event: done\ndata: {"outcome":"answered"}\n\n',
  ].join('')
  await install(page, { workers: [piWorker({ last_check: OK_CHECK })], chatSse })
  await page.goto('/#chat')

  await page.getByRole('textbox', { name: '给 Conveyor 发消息' }).fill('你好')
  await page.getByRole('button', { name: '发送', exact: true }).click()

  const bubble = page.locator('.chat-failed-reply')
  await expect(bubble).toBeVisible()
  // Only the explanation shows; the raw provider text is hidden behind 技术细节.
  await expect(bubble.locator('p.formatted-paragraph')).toContainText('连不上模型服务，请检查「模型设置」里的接口地址，或稍后再试。')
  const rawTech = bubble.locator('code', { hasText: 'Authentication Fails' })
  await expect(rawTech).toBeHidden()

  // The raw part is one 技术细节 away.
  await bubble.locator('details > summary').click()
  await expect(rawTech).toBeVisible()
  await expect(rawTech).toContainText('Authentication Fails, Your api key: ****-key is invalid')

  // 打开模型设置 opens the same dialog the 设置 button opens.
  await bubble.getByRole('button', { name: '打开模型设置' }).click()
  await expect(page.locator('.settings-sheet.provider-settings')).toBeVisible()
  await expect(page.locator('#provider-settings-title')).toContainText('模型设置')
})
