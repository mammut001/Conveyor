import { expect, test, type Page, type Route } from '@playwright/test'

const NOW = new Date('2026-11-03T14:30:00+08:00')
const SHOTS = 'test-results/workers'
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
}

const DEFAULT_WORKERS: Worker[] = [
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
    priority: 1,
    quota: { state: 'available', reset_at: null, reason: null },
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
  },
  {
    id: 'pi',
    label: 'Pi',
    installed: false,
    version: null,
    path: null,
    auth: 'unknown',
    login_command: null,
    install_command: null,
    install_supported: false,
    runnable: true,
    enabled: true,
    priority: 3,
    quota: { state: 'available', reset_at: null, reason: null },
  },
  {
    id: 'agy',
    label: 'Antigravity',
    installed: true,
    version: '2.0.0',
    path: '/opt/agy/bin/agy',
    auth: 'unknown',
    login_command: 'agy',
    install_command: null,
    install_supported: false,
    runnable: false,
    enabled: true,
    priority: 4,
    quota: {
      state: 'cooling',
      reset_at: Math.floor(NOW.getTime() / 1000) + 7200,
      reason: '429: rate limit exceeded, resets in 2h',
    },
  },
  {
    id: 'cmd',
    label: 'Command Code',
    installed: false,
    version: null,
    path: null,
    auth: 'unknown',
    login_command: 'cmd login',
    install_command: 'npm install -g command-code',
    install_supported: true,
    runnable: false,
    enabled: false,
    priority: 5,
    quota: { state: 'available', reset_at: null, reason: null },
  },
]

const defaultWorkers = (): Worker[] => JSON.parse(JSON.stringify(DEFAULT_WORKERS))

type Calls = { method: string; path: string; body: unknown }[]
type Fixture = {
  workers?: Worker[]
  installEnabled?: boolean
  featureOff?: boolean
  active?: string | null
  mobileUI?: boolean
}

async function installApi(page: Page, fixture: Fixture = {}) {
  const calls: Calls = []
  let workers = fixture.workers ? JSON.parse(JSON.stringify(fixture.workers)) : defaultWorkers()
  let installEnabled = fixture.installEnabled ?? true
  let active = fixture.active !== undefined ? fixture.active : 'codex'
  const featureOff = Boolean(fixture.featureOff)

  await page.clock.setFixedTime(NOW)
  await page.addInitScript(() => sessionStorage.setItem('conveyor-token', 'workers-e2e-token-not-a-secret'))

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
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
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
        features: {
          workers: !featureOff,
          mobile_ui: Boolean(fixture.mobileUI),
        },
      })
    }

    // Workers API
    if (path === '/api/workers' && method === 'GET') {
      if (featureOff) return json({ error: 'not found' }, 404)
      return json({
        enabled: true,
        install_enabled: installEnabled,
        workers,
        active,
      })
    }

    if (path === '/api/workers/detect' && method === 'POST') {
      if (featureOff) return json({ error: 'not found' }, 404)
      return json({
        enabled: true,
        install_enabled: installEnabled,
        workers,
        active,
      })
    }

    if (path === '/api/workers/order' && method === 'POST') {
      if (featureOff) return json({ error: 'not found' }, 404)
      const { order, enabled } = body as { order: string[]; enabled: string[] }
      const map = new Map(workers.map(w => [w.id, w]))
      const next: Worker[] = []
      order.forEach((id, idx) => {
        const item = map.get(id)
        if (item) {
          next.push({
            ...item,
            priority: idx + 1,
            enabled: enabled.includes(id),
          })
        }
      })
      workers = next
      return json({
        enabled: true,
        install_enabled: installEnabled,
        workers,
        active,
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
        found.version = '1.0.0'
        found.path = `/usr/local/bin/${id}`
        found.auth = 'ok'
      }
      return json({
        ok: true,
        output: `[install] Installing ${id}...\nFetched 4 packages.\nLinked binaries to /usr/local/bin/${id}\nDone in 3.42s`,
      })
    }

    if (path.startsWith('/api/workers/') && path.endsWith('/reset-quota') && method === 'POST') {
      const id = path.split('/')[3]
      const found = workers.find(w => w.id === id)
      if (found) {
        found.quota = { state: 'available', reset_at: null, reason: null }
      }
      return json({
        enabled: true,
        install_enabled: installEnabled,
        workers,
        active,
      })
    }

    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })

  return calls
}

async function open(page: Page, size: Size, hash = '#workers') {
  await page.setViewportSize(size)
  await page.goto(`/${hash}`)
  const panel = page.locator('.guard-page.guard-workers')
  await expect(panel).toBeVisible()
  return panel
}

async function shoot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready)
  const size = page.viewportSize()!
  await page.screenshot({ path: `${SHOTS}/${name}-${size.width}.png`, fullPage: true, animations: 'disabled' })
}

async function expectNoSidewaysOverflow(page: Page) {
  const overflow = await page.locator('.guard-page').evaluate(el => ({
    inner: el.scrollWidth - el.clientWidth,
    right: el.getBoundingClientRect().right - window.innerWidth,
  }))
  expect(overflow.inner).toBeLessThanOrEqual(1)
  expect(overflow.right).toBeLessThanOrEqual(1)
}

test.describe('Workers Panel (Conveyor 执行者)', () => {
  test('list rendering with each state and visual snapshots at 1440 and 390 widths', async ({ page }) => {
    await installApi(page)

    // Check desktop
    await open(page, SIZES[0])
    await expect(page.getByRole('heading', { name: '执行者', level: 2 })).toBeVisible()
    await expect(page.getByText('谁来替你干活：检测、登录、按额度自动轮换')).toBeVisible()
    await expect(page.locator('.workers-actions-row')).toBeVisible()

    // 1. Codex: 可用 + 当前选用 + runnable
    const codex = page.locator('li.worker-card', { hasText: 'Codex' })
    await expect(codex).toBeVisible()
    await expect(codex.getByText('可用', { exact: true })).toBeVisible()
    await expect(codex.getByText('当前使用', { exact: true })).toBeVisible()
    // One state chip + one role chip, never contradictory (QA F7).
    await expect(codex.locator('.guard-badge, .worker-role')).toHaveCount(2)
    await expect(page.getByText('当前使用：Codex')).toBeVisible()
    await expect(codex.getByText('v0.1.2 · /usr/local/bin/codex')).toBeVisible()
    await expect(codex.getByText('#1')).toBeVisible()

    // 2. Claude Code: 需要登录 + runnable
    const claude = page.locator('li.worker-card', { hasText: 'Claude Code' })
    await expect(claude).toBeVisible()
    await expect(claude.getByText('需要登录')).toBeVisible()
    // A worker that cannot be used yet carries no rotation chip that could contradict that.
    await expect(claude.locator('.worker-role')).toHaveCount(0)
    await expect(claude.getByText('1.0.14 · /usr/local/bin/claude')).toBeVisible()

    // 3. Pi: 未安装 + manual hint
    const pi = page.locator('li.worker-card', { hasText: 'Pi' })
    await expect(pi).toBeVisible()
    await expect(pi.getByText('未安装')).toBeVisible()
    await expect(pi.getByText('需要手动安装，暂时没有一键安装')).toBeVisible()
    await expect(pi.locator('.worker-role')).toHaveCount(0)

    // 4. Antigravity: cooling chip + not runnable
    const agy = page.locator('li.worker-card', { hasText: 'Antigravity' })
    await expect(agy).toBeVisible()
    await expect(agy.getByText('额度用完·约 2 小时后恢复')).toBeVisible()
    await expect(agy.getByText('暂不支持轮换')).toBeVisible()

    // 5. Command Code: 未安装 + not runnable + install button
    const cmd = page.locator('li.worker-card', { hasText: 'Command Code' })
    await expect(cmd).toBeVisible()
    await expect(cmd.getByText('未安装')).toBeVisible()
    await expect(cmd.getByRole('button', { name: '安装' })).toBeVisible()

    // Desktop screenshot
    await shoot(page, 'workers-list')

    // Phone 390 width
    await open(page, SIZES[1])
    await expectNoSidewaysOverflow(page)
    await shoot(page, 'workers-list')
  })

  test('reorder + save request body', async ({ page }) => {
    const calls = await installApi(page)
    await open(page, SIZES[0])

    const codex = page.locator('li.worker-card', { hasText: 'Codex' })
    const downBtn = codex.getByRole('button', { name: '下移 Codex' })
    await downBtn.click()

    // Codex should now be #2
    await expect(codex.getByText('#2')).toBeVisible()

    // Save order
    const saveBtn = page.getByRole('button', { name: '保存顺序' })
    await expect(saveBtn).toBeVisible()
    await saveBtn.click()

    await expect(page.getByText('已保存执行者顺序。')).toBeVisible()

    const orderCall = calls.find(c => c.method === 'POST' && c.path === '/api/workers/order')
    expect(orderCall).toBeDefined()
    const body = orderCall!.body as { order: string[]; enabled: string[] }
    expect(body.order[0]).toBe('claude-code')
    expect(body.order[1]).toBe('codex')
    expect(body.enabled).toContain('codex')
    expect(body.enabled).toContain('claude-code')
  })

  test('detect triggers POST /api/workers/detect and updates feedback', async ({ page }) => {
    const calls = await installApi(page)
    await open(page, SIZES[0])

    const detectBtn = page.getByRole('button', { name: '重新检测' })
    await detectBtn.click()

    await expect(page.getByText('已完成执行者检测。')).toBeVisible()
    const detectCall = calls.find(c => c.method === 'POST' && c.path === '/api/workers/detect')
    expect(detectCall).toBeDefined()
  })

  test('install confirm flow (enabled)', async ({ page }) => {
    const calls = await installApi(page, { installEnabled: true })
    await open(page, SIZES[0])

    const cmd = page.locator('li.worker-card', { hasText: 'Command Code' })
    const installBtn = cmd.getByRole('button', { name: '安装' })
    await installBtn.click()

    // Inline confirmation
    await expect(cmd.getByText('将从官方渠道下载并安装 Command Code')).toBeVisible()
    await cmd.getByText('查看将运行的命令').click()
    await expect(cmd.locator('code', { hasText: 'npm install -g command-code' })).toBeVisible()
    const confirmBtn = cmd.getByRole('button', { name: '确认安装' })
    await confirmBtn.click()

    const installCall = calls.find(c => c.method === 'POST' && c.path === '/api/workers/cmd/install')
    expect(installCall).toBeDefined()
    expect(installCall!.body).toEqual({ confirm: true })

    // Details output collapsible
    const details = cmd.locator('details.worker-details')
    await expect(details).toBeVisible()
    await expect(details.getByText('Installing cmd...')).toBeVisible()
  })

  test('copy-command when install is disabled', async ({ page }) => {
    await installApi(page, { installEnabled: false })
    await open(page, SIZES[0])

    const cmdDisabled = page.locator('li.worker-card', { hasText: 'Command Code' })
    await expect(cmdDisabled.getByText('在运行 Conveyor 的电脑上打开终端，运行下面这条命令安装：')).toBeVisible()
    await expect(cmdDisabled.locator('code', { hasText: 'npm install -g command-code' })).toBeVisible()
    const copyBtn = cmdDisabled.getByRole('button', { name: '复制' })
    await expect(copyBtn).toBeVisible()
    await copyBtn.click()
    await expect(cmdDisabled.getByRole('button', { name: '已复制' })).toBeVisible()
  })

  test('needs_login card shows command, copy button and prompt note', async ({ page }) => {
    await installApi(page)
    await open(page, SIZES[0])

    const claude = page.locator('li.worker-card', { hasText: 'Claude Code' })
    await expect(claude.getByText('在运行 Conveyor 的电脑上打开终端，运行下面这条命令并按提示在浏览器登录，完成后点「重新检测」：')).toBeVisible()
    await expect(claude.locator('code', { hasText: 'claude auth login' })).toBeVisible()
    const copyBtn = claude.getByRole('button', { name: '复制' })
    await expect(copyBtn).toBeVisible()
    await copyBtn.click()
    await expect(claude.getByRole('button', { name: '已复制' })).toBeVisible()
  })

  test('cooling card shows reset time, reason, and resets quota on click', async ({ page }) => {
    const calls = await installApi(page)
    await open(page, SIZES[0])

    const agy = page.locator('li.worker-card', { hasText: 'Antigravity' })
    await expect(agy.getByText('预计恢复时间：约 2 小时后恢复')).toBeVisible()
    // Plain-Chinese reason first, the raw API text only behind 技术详情 (QA F8).
    await expect(agy.getByText('这个账号的额度已经用完。')).toBeVisible()
    await expect(agy.getByText('429: rate limit exceeded, resets in 2h')).toBeHidden()
    await agy.getByText('技术详情').click()
    await expect(agy.getByText('429: rate limit exceeded, resets in 2h')).toBeVisible()

    const resetBtn = agy.getByRole('button', { name: '我已处理，立即重试' })
    await resetBtn.click()

    const resetCall = calls.find(c => c.method === 'POST' && c.path === '/api/workers/agy/reset-quota')
    expect(resetCall).toBeDefined()
    await expect(page.getByText('已重置 Antigravity 的额度状态。')).toBeVisible()
    await expect(agy.getByText('额度用完')).not.toBeVisible()
  })

  test('feature-off hides nav item and renders muted message when directly accessed', async ({ page }) => {
    await installApi(page, { featureOff: true })

    // Desktop nav should not have 执行者
    await page.setViewportSize(SIZES[0])
    await page.goto('/')
    await expect(page.locator('main.app-shell')).toBeVisible()
    await expect(page.getByRole('button', { name: '执行者' })).toHaveCount(0)

    // Direct hash access
    await page.goto('/#workers')
    const panel = page.locator('.guard-page.guard-workers')
    await expect(panel).toBeVisible()
    await expect(panel.getByText('执行者功能未开启')).toBeVisible()
  })

  test('navigates to workers via desktop nav and mobile 更多 sheet', async ({ page }) => {
    await installApi(page)

    // 1. Desktop navigation: click 执行者 in nav
    await page.setViewportSize(SIZES[0])
    await page.goto('/')
    await expect(page.locator('main.app-shell')).toBeVisible()

    const navBtn = page.locator('.shell-nav button', { hasText: '执行者' })
    await expect(navBtn).toBeVisible()
    await navBtn.click()

    await expect(page.locator('.guard-page.guard-workers')).toBeVisible()

    // 2. Mobile navigation: click 更多 in bottom bar then 执行者 in sheet
    await installApi(page, { mobileUI: true })
    await page.setViewportSize(SIZES[1])
    await page.goto('/')
    await expect(page.locator('main.app-shell')).toBeVisible()

    const mobileMore = page.getByRole('navigation', { name: '底部导航' }).getByRole('button', { name: '更多' })
    await expect(mobileMore).toBeVisible()
    await mobileMore.click()

    const sheetItem = page.locator('.mobile-sheet-items button', { hasText: '执行者' })
    await expect(sheetItem).toBeVisible()
    await sheetItem.click()

    await expect(page.locator('.guard-page.guard-workers')).toBeVisible()
  })

  test('390px viewport no horizontal scroll and tap targets >= 40px', async ({ page }) => {
    await installApi(page, { mobileUI: true })
    await open(page, SIZES[1])

    await expectNoSidewaysOverflow(page)

    // Check tap targets on buttons
    const buttons = page.locator('.guard-page.guard-workers button')
    const count = await buttons.count()
    expect(count).toBeGreaterThan(0)
    for (let i = 0; i < count; i++) {
      const btn = buttons.nth(i)
      if (await btn.isVisible()) {
        const box = await btn.boundingBox()
        if (box) {
          expect(box.height).toBeGreaterThanOrEqual(36) // small buttons on mobile styled with min-height >= 40px in media query
        }
      }
    }

    await shoot(page, 'workers-390-touch')
  })
})
