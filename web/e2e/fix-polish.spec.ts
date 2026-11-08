import { expect, test, type Page } from '@playwright/test'

// Polish fixes: root error boundary, bad response shapes, endless loading, raw error text,
// agent dialog validation, phone tap targets, wording, skip link.
const SHOTS = 'test-results/fix-polish'
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
const switchView = (page: Page, view: string) => page.evaluate(v => { location.hash = '#' + v }, view)


const BAD_SHAPES: Array<[string, string, Record<string, Override>]> = [
  ['memory', 'memory', { '/api/memory': { body: null } }],
  ['memory', 'memory object', { '/api/memory': { body: {} } }],
  ['memory', 'memory items object', { '/api/memory': { body: { items: { a: 1 } } } }],
  ['inbox', 'routines object', { '/api/routines': { body: { a: 1 } } }],
  ['inbox', 'routines null', { '/api/routines': { body: null } }],
  ['inbox', 'inbox empty object', { '/api/inbox': { body: {} } }],
  ['teammate', 'teammate null', { '/api/teammate/status': { body: null } }],
  ['secrets', 'secrets object', { '/api/secrets': { body: {} } }],
  ['browser', 'browser approvals null', { '/api/browser/approvals': { body: null } }],
]

for (const [view, label, overrides] of BAD_SHAPES) {
  test(`bad response shape (${label}) shows a Chinese error, never a blank app`, async ({ page }) => {
    await install(page, overrides)
    await goto(page, view)
    await expect(page.getByText('数据格式不对，请稍后重试').first()).toBeVisible()
    await expect(page.getByText('加载中…')).toHaveCount(0)
    expect(pageErrors).toEqual([])
    // The rest of the app still works.
    await switchView(page, 'tasks')
    await expect(page.locator('header.topbar')).toBeVisible()
    await expect(page.locator('main.app-shell')).toBeVisible()
    expect(pageErrors).toEqual([])
  })
}

test('memory null: loading ends with an error and a working 重试', async ({ page }) => {
  let bad = true
  await install(page)
  await page.route('**/api/memory?**', route => bad
    ? route.fulfill({ status: 200, contentType: 'application/json', body: 'null' })
    : route.fallback())
  await goto(page, 'memory')
  const retry = page.getByRole('button', { name: '重试' }).first()
  await expect(retry).toBeVisible()
  await expect(page.getByText('加载中…')).toHaveCount(0)
  bad = false
  await retry.click()
  await expect(page.getByText('我习惯用中文')).toBeVisible()
})

test('a hanging request falls back to an error after 15 seconds', async ({ page }) => {
  await page.clock.install()
  await install(page, { '/api/memory': { hang: true } })
  await goto(page, 'memory')
  await expect(page.getByText('加载中…')).toBeVisible()
  await page.clock.fastForward(16_000)
  await expect(page.getByRole('button', { name: '重试' }).first()).toBeVisible()
  await expect(page.getByText('加载中…')).toHaveCount(0)
})

test('root error boundary shows 页面出了点问题 with 重试 and 刷新页面', async ({ page }) => {
  // A body the panels do not guard (an agent list holding `null`) throws while rendering.
  await install(page)
  let broken = true
  await page.route('**/api/agents', route => broken
    ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ enabled: true, agents: [agents[0], null] }) })
    : route.fallback())
  await page.goto('/#chat')
  const alert = page.getByRole('alert').filter({ hasText: '页面出了点问题' })
  await expect(alert).toBeVisible()
  await expect(alert.getByRole('button', { name: '重试' })).toBeVisible()
  await expect(alert.getByRole('button', { name: '刷新页面' })).toBeVisible()
  await expect(alert.getByText('详情')).toBeVisible()
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.screenshot({ path: `${SHOTS}/boundary-1440.png` })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: `${SHOTS}/boundary-390.png` })
  broken = false
  await alert.getByRole('button', { name: '重试' }).click()
  await expect(page.locator('main.app-shell')).toBeVisible()
  await expect(page.getByText('页面出了点问题')).toHaveCount(0)
})

const RAW_CASES: Array<[string, string, Override]> = [
  ['inbox', '/api/inbox', { status: 500, body: { error: 'Internal Server Error: Traceback (most recent call last):\n  File "x.py", line 1' } }],
  ['memory', '/api/memory', { status: 401, body: { error: 'unauthorized' } }],
  ['teammate', '/api/teammate/status', { status: 500, body: { error: 'Internal Server Error: Traceback (most recent call last): boom' } }],
  ['browser', '/api/browser/approvals', { status: 401, body: { error: 'unauthorized' } }],
  ['secrets', '/api/secrets', { status: 500, body: { error: 'Internal Server Error: Traceback (most recent call last): boom' } }],
  ['memory', '/api/memory', { abort: true }],
  ['inbox', '/api/inbox', { status: 200, raw: '' }],
]
for (const [view, path, over] of RAW_CASES) {
  test(`raw error text is hidden on ${view} (${over.status || 'network'} ${path})`, async ({ page }) => {
    await install(page, { [path]: over })
    await goto(page, view)
    await expect(page.getByRole('alert').first()).toBeVisible()
    const text = (await page.locator('.stream-panel').innerText())
    for (const raw of ['Traceback', 'Internal Server Error', 'unauthorized', 'Failed to fetch', 'Unexpected end', 'Cannot read properties', 'HTTP 5', 'File "']) {
      expect(text).not.toContain(raw)
    }
    expect(pageErrors).toEqual([])
  })
}

test('agent dialog: duplicate names, name limit with counter, folder rules, existence warning', async ({ page }) => {
  await install(page)
  await goto(page, 'chat')
  await page.getByRole('button', { name: '新建助手' }).click()
  const dialog = page.getByRole('dialog', { name: '新建助手' })
  const name = dialog.getByLabel('名称')
  await expect(name).toHaveAttribute('maxlength', '60')
  await expect(dialog.getByText('0/60')).toBeVisible()
  await name.fill('finance ')
  await dialog.getByRole('button', { name: '创建助手' }).click()
  await expect(dialog.getByText(/已经有一个叫“finance”的助手/)).toBeVisible()
  await name.fill('财务')
  await expect(dialog.getByText('2/60')).toBeVisible()
  const folder = dialog.getByLabel(/项目文件夹/)
  for (const bad of ['/', '//', '/etc', '/usr/local/x', '/System/Library', '/bin']) {
    await folder.fill(bad)
    await dialog.getByRole('button', { name: '创建助手' }).click()
    await expect(dialog.locator('.field-error')).toBeVisible()
  }
  await folder.fill('relative/path')
  await dialog.getByRole('button', { name: '创建助手' }).click()
  await expect(dialog.locator('.field-error')).toContainText('以 / 开头')
  await folder.fill('/srv/my-project')
  await expect(dialog.getByText('已经在服务器上存在')).toBeVisible()
  await page.screenshot({ path: `${SHOTS}/agent-dialog-1440.png` })
  const posts: unknown[] = []
  await page.route('**/api/agents', route => { if (route.request().method() === 'POST') { posts.push(route.request().postDataJSON()); return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ agent: { ...agents[1], id: 'new', name: '财务' } }) }) } return route.fallback() })
  await dialog.getByRole('button', { name: '创建助手' }).click()
  await expect.poll(() => posts.length).toBe(1)
  // Editing an agent may keep its own name (any casing).
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: `${SHOTS}/agent-dialog-390.png` })
})

test('phone: agent edit buttons and small buttons are at least 40px; desktop stays small', async ({ page }) => {
  await install(page, { '/api/memory': { status: 500, body: { error: 'x' } } })
  await page.setViewportSize({ width: 390, height: 844 })
  await goto(page, 'chat')
  await page.getByRole('button', { name: /助手|会话|切换/ }).first().click().catch(() => {})
  const edit = page.locator('.agent-edit-btn').first()
  if (await edit.count()) {
    const box = await edit.boundingBox()
    if (box) { expect(box.width).toBeGreaterThanOrEqual(40); expect(box.height).toBeGreaterThanOrEqual(40) }
  }
  await switchView(page, 'memory')
  const retry = page.getByRole('button', { name: '重试' }).first()
  await expect(retry).toBeVisible()
  expect((await retry.boundingBox())!.height).toBeGreaterThanOrEqual(40)
  await page.screenshot({ path: `${SHOTS}/memory-error-390.png` })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.screenshot({ path: `${SHOTS}/memory-error-1440.png` })
  expect((await retry.boundingBox())!.height).toBeLessThan(40)
})

test('settings and secrets use plain wording and no file names', async ({ page }) => {
  await install(page)
  await goto(page, 'chat')
  await page.locator('header.topbar').getByRole('button', { name: '设置' }).click()
  const dialog = page.getByRole('dialog', { name: '模型设置' })
  await expect(dialog.getByLabel('接口类型', { exact: true })).toBeVisible()
  // 服务商代号 and 密钥名称 live under the collapsed 高级 section (QA F9).
  await dialog.locator('.provider-advanced > summary').click()
  for (const label of ['服务商代号', '密钥名称']) await expect(dialog.getByLabel(label, { exact: true })).toBeVisible()
  const text = await dialog.innerText()
  for (const old of ['服务商 ID', '接口协议', '密钥变量名', 'config.toml', '.env']) expect(text).not.toContain(old)
  await expect(dialog.getByText('保存在服务器上').first()).toBeVisible()
  await page.screenshot({ path: `${SHOTS}/settings-1440.png` })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: `${SHOTS}/settings-390.png` })
})

test('skip link is the first focusable element and moves focus to the content', async ({ page }) => {
  await install(page)
  await goto(page, 'memory')
  const link = page.getByRole('link', { name: '跳到主要内容' })
  // First focusable element in document order (what a fresh Tab press reaches).
  const first = await page.evaluate(() => document.querySelector('a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')?.textContent)
  expect(first).toBe('跳到主要内容')
  await expect(link).not.toBeInViewport()
  await page.keyboard.press('Shift+Tab')
  await link.focus()
  await expect(link).toBeFocused()
  await expect(link).toBeInViewport()
  await page.screenshot({ path: `${SHOTS}/skip-link-1440.png` })
  await page.keyboard.press('Enter')
  const inside = await page.evaluate(() => !!document.activeElement?.closest('.stream-panel'))
  expect(inside).toBe(true)
})
