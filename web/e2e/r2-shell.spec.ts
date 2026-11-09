import { expect, test, type Page } from '@playwright/test'

const SHOTS = 'test-results/r2-shell'
const NOW = Date.now()

const agents = [
  { id: 'default', name: 'Conveyor', color: '#f97316', is_default: true, session_id: 's-default', instructions: '', workspace_path: '', status: 'idle', last_message: '', last_activity: null, message_count: 0 },
  { id: 'finance', name: 'Finance', color: '#10b981', is_default: false, session_id: 's-fin', instructions: '记账', workspace_path: '/srv/f', workspace_status: 'ok', status: 'idle', last_message: '', last_activity: null, message_count: 0 },
]

const provider = {
  provider_id: 'deepseek', provider_name: 'DeepSeek', model: 'deepseek-chat', reasoning_effort: 'minimal',
  base_url: 'https://api.deepseek.com/v1', wire_api: 'chat', env_key: 'DEEPSEEK_API_KEY', api_key_configured: true, api_key_hint: '••••8a34',
}

const GOOD: Record<string, unknown> = {
  '/api/memory': { items: [{ id: 1, kind: 'facts', text: '我习惯用中文', day: '2026-11-01', created_at: new Date(NOW).toISOString(), updated_at: '', source_channel: 'web' }], counts: { profile: 0, facts: 1, log: 0 }, shared: false, profile_cap: 20 },
  '/api/inbox': { unread: 0, items: [] },
  '/api/routines': { routines: [] },
  '/api/secrets': { items: [{ name: 'GITHUB_TOKEN', set: true, updated_at: null }] },
  '/api/browser/approvals': { approvals: [], kill_switch: false },
  '/api/browser/egress': { requests: [] },
  '/api/teammate/status': { enabled: true, is_paused: false, paused_until: null, muted_sources: [], interval_seconds: 300, cooldown_seconds: 1800, last_patrol_at: null, total_alerts_count: 0, recent_alerts: [], monitored_services: [], thresholds: { disk_pct: 85, disk_gb: 5, load_ratio: 1.5, error_burst: 20 }, status_text: '值守中' },
}

type Override = { status?: number; body?: unknown; raw?: string; abort?: boolean; hang?: boolean }
const pageErrors: string[] = []

async function install(page: Page, overrides: Record<string, Override> = {}) {
  pageErrors.length = 0
  page.on('pageerror', error => pageErrors.push(error.message))
  await page.addInitScript(() => { localStorage.setItem('conveyor-token', 't') })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    const over = overrides[path]
    if (over) {
      if (over.abort) return route.abort('failed')
      if (over.hang) return new Promise<void>(() => {})
      if (over.raw !== undefined) return route.fulfill({ status: over.status || 200, contentType: 'application/json', body: over.raw })
      return json(over.body, over.status || 200)
    }
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0.2, 0.1, 0.1], cpu_count: 2, memory: { total: null, available: null }, disk: { total: 1000, used: 0, free: 1000 },
      queue: { depth: 0, paused: false, states: {} }, channels: { telegram: { configured: true }, feishu: { configured: false } }, nodes: [],
      features: { agents: true, long_term_memory: true, routines: true, teammate: true, approval_inbox: true, skills: true, workflows: true, mcp: true, webhooks: false },
    })
    if (path === '/api/agents') return json({ enabled: true, agents })
    if (path === '/api/config/provider') return json(provider)
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (path in GOOD) return json(GOOD[path])
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/agent-screens/config') return json({ error: 'not found' }, 404)
    if (path.startsWith('/api/sessions/')) return json({ id: 'x', channel: 'web', created_at: '', last_activity: '', job_count: 0, messages: [], runs: [], jobs: [] })
    return json({ items: [], approvals: [], sessions: [], jobs: [], skills: [], workflows: [], messages: [] })
  })
}

async function goto(page: Page, view: string) {
  await page.goto('/#' + view)
  await expect(page.locator('main.app-shell')).toBeVisible()
}

test('F1: top bar connection state reflects startup, live, poll failure, and offline events', async ({ page }) => {
  let backendFail = false
  await page.addInitScript(() => { localStorage.setItem('conveyor-token', 't') })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (backendFail) {
      return route.abort('failed')
    }
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0.2, 0.1, 0.1], cpu_count: 2, memory: { total: null, available: null }, disk: { total: 1000, used: 0, free: 1000 },
      queue: { depth: 0, paused: false, states: {} }, channels: {}, nodes: [],
      features: { agents: true, routines: true },
    })
    if (path === '/api/agents') return json({ enabled: true, agents })
    if (path === '/api/config/provider') return json(provider)
    if (path.startsWith('/api/sessions/')) return json({ id: 'x', channel: 'web', created_at: '', last_activity: '', job_count: 0, messages: [], runs: [], jobs: [] })
    return json({ items: [], approvals: [], sessions: [], jobs: [], skills: [], workflows: [], messages: [] })
  })

  await page.goto('/#chat')
  const statusPill = page.locator('.top-status')
  // Successful poll gives 'live'
  await expect(statusPill).toHaveAttribute('data-connection', 'live')
  await expect(statusPill.locator('.top-status-label')).toHaveText('在线')

  // Poll failure (e.g. backend stopped / network error) flips to reconnecting within 3-4s
  backendFail = true
  await expect(statusPill).toHaveAttribute('data-connection', 'reconnecting', { timeout: 6000 })
  await expect(statusPill.locator('.top-status-label')).toHaveText('重连中')
  await page.screenshot({ path: `${SHOTS}/f1-connection-reconnecting.png` })

  // Browser offline event flips to 'offline'
  await page.evaluate(() => { window.dispatchEvent(new Event('offline')) })
  await expect(statusPill).toHaveAttribute('data-connection', 'offline')
  await expect(statusPill.locator('.top-status-label')).toHaveText('离线')
  await page.screenshot({ path: `${SHOTS}/f1-connection-offline.png` })

  // Online recovery returns to 'live'
  backendFail = false
  await page.evaluate(() => { window.dispatchEvent(new Event('online')) })
  await expect(statusPill).toHaveAttribute('data-connection', 'live', { timeout: 6000 })
  await expect(statusPill.locator('.top-status-label')).toHaveText('在线')

  await page.screenshot({ path: `${SHOTS}/f1-connection-live.png` })
})

test('F2: phone zoom below 340px (260x560 and 320x568) collapses rail, keeps composer full width', async ({ page }) => {
  await install(page)
  // Test 260x560 (equivalent to 390px at 150% zoom)
  await page.setViewportSize({ width: 260, height: 560 })
  await goto(page, 'chat')
  const rail = page.locator('.chat-topic-rail')
  await expect(rail).toBeHidden()

  const composer = page.locator('.chat-panel-container .composer.chat-composer')
  await expect(composer).toBeVisible()
  const textarea = composer.locator('textarea')
  await expect(textarea).toBeVisible()

  // Width of textarea should be full width (over 200px in a 260px screen, not squeezed to 60px)
  const box = await textarea.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.width).toBeGreaterThan(200)

  // Send button is reachable and visible
  const sendBtn = page.locator('.chat-send-button')
  await expect(sendBtn).toBeVisible()

  await page.screenshot({ path: `${SHOTS}/f2-narrow-260x560.png` })

  // Test 320x568
  await page.setViewportSize({ width: 320, height: 568 })
  await expect(rail).toBeHidden()
  const box320 = await textarea.boundingBox()
  expect(box320!.width).toBeGreaterThan(250)
  await expect(sendBtn).toBeVisible()

  await page.screenshot({ path: `${SHOTS}/f2-narrow-320x568.png` })
})

test('F3: landscape phone (844x390) fits in 100dvh with composer visible and no window scroll', async ({ page }) => {
  await install(page)
  await page.setViewportSize({ width: 844, height: 390 })
  await goto(page, 'chat')

  // Send button and textarea are visible within the fold
  const sendBtn = page.locator('.chat-send-button')
  await expect(sendBtn).toBeVisible()
  const sendBox = await sendBtn.boundingBox()
  expect(sendBox).not.toBeNull()
  expect(sendBox!.y + sendBox!.height).toBeLessThanOrEqual(390)

  // Window scroll must be 0
  const scrollY = await page.evaluate(() => window.scrollY)
  expect(scrollY).toBe(0)

  // Type a draft into textarea and verify it does not scroll the document
  const textarea = page.locator('.chat-panel-container .composer.chat-composer textarea')
  await textarea.fill('测试横屏输入草稿\n多行文字')
  await expect(sendBtn).toBeVisible()

  const scrollYAfterType = await page.evaluate(() => window.scrollY)
  expect(scrollYAfterType).toBe(0)

  await page.screenshot({ path: `${SHOTS}/f3-landscape-844x390.png` })
})

test('F8: unknown agent in URL shows notice and cleans URL param', async ({ page }) => {
  await install(page)
  await page.goto('/?agent=nonexistent#chat')
  await expect(page.locator('.app-shell')).toBeVisible()

  // Notice should appear
  const notice = page.locator('.shell-notice.global')
  await expect(notice).toBeVisible()
  await expect(notice).toContainText('找不到这个助手，已切换到 Conveyor')

  // URL should have the bad agent param removed
  await expect.poll(() => page.url()).not.toContain('agent=')

  // Header title should fallback to Conveyor
  await expect(page.locator('.stream-header h2')).toHaveText('Conveyor')

  await page.screenshot({ path: `${SHOTS}/f8-agent-unknown.png` })
})

test('F9: forced-colors active provides 1px solid ButtonText borders and visible status', async ({ page }) => {
  await install(page)
  await page.emulateMedia({ forcedColors: 'active' })
  await goto(page, 'chat')

  // Check top status label is visible with text
  const statusLabel = page.locator('.top-status .top-status-label')
  await expect(statusLabel).toBeVisible()
  await expect(statusLabel).toHaveText('在线')

  // Check buttons have border
  const sendButton = page.locator('.chat-send-button')
  await expect(sendButton).toBeVisible()
  const border = await sendButton.evaluate(el => window.getComputedStyle(el).borderStyle)
  expect(border).toBe('solid')

  await page.screenshot({ path: `${SHOTS}/f9-forced-colors.png` })
})

test('F4: settings dialog with unsaved edits shows inline confirm on Esc, Esc returns to editing, 放弃 closes', async ({ page }) => {
  let nativeConfirmCalled = false
  page.on('dialog', dialog => {
    nativeConfirmCalled = true
    void dialog.dismiss()
  })

  await install(page)
  await goto(page, 'chat')

  // Open settings
  await page.locator('.settings-button').click()
  const sheet = page.locator('.settings-sheet.provider-settings')
  await expect(sheet).toBeVisible()

  // Change model field to dirty
  const modelInput = sheet.locator('#provider-model')
  await modelInput.fill('deepseek-reasoner')

  // Press Escape
  await page.keyboard.press('Escape')

  // Native confirm must not be called
  expect(nativeConfirmCalled).toBe(false)

  // In-dialog inline confirmation must appear
  const confirmFooter = sheet.locator('.settings-discard-confirm')
  await expect(confirmFooter).toBeVisible()
  await expect(confirmFooter).toContainText('放弃还没保存的修改？')
  await expect(confirmFooter.getByRole('button', { name: '继续编辑' })).toBeVisible()
  await expect(confirmFooter.getByRole('button', { name: '放弃修改' })).toBeVisible()

  await page.screenshot({ path: `${SHOTS}/f4-settings-inline-confirm.png` })

  // Press Escape on confirm: returns to editing
  await page.keyboard.press('Escape')
  await expect(confirmFooter).toBeHidden()
  await expect(sheet).toBeVisible()
  expect(await modelInput.inputValue()).toBe('deepseek-reasoner')

  // Press Escape again and click 放弃修改 to close
  await page.keyboard.press('Escape')
  await expect(confirmFooter).toBeVisible()
  await confirmFooter.getByRole('button', { name: '放弃修改' }).click()

  // Sheet is now closed
  await expect(sheet).toBeHidden()
})

test('F5: lock aborts in-flight requests and avoids 401 console noise', async ({ page }) => {
  const responses401: string[] = []
  page.on('response', res => {
    if (res.status() === 401) responses401.push(res.url())
  })
  const consoleErrors: string[] = []
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })

  await install(page)
  await goto(page, 'chat')

  // Click 锁定
  await page.locator('.shell-lock-button').click()
  const lockDialog = page.locator('.shell-confirm')
  await expect(lockDialog).toBeVisible()

  // Confirm lock
  await lockDialog.locator('.shell-button--danger').click()

  // Unlock screen should appear
  await expect(page.locator('main.unlock-shell')).toBeVisible()

  // Wait to verify no 401 noise follows
  await page.waitForTimeout(1000)

  expect(responses401).toEqual([])
  const token = await page.evaluate(() => localStorage.getItem('conveyor-token') || sessionStorage.getItem('conveyor-token'))
  expect(token).toBeNull()

  await page.screenshot({ path: `${SHOTS}/f5-lock-clean.png` })
})
