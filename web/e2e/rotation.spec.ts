import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

// Usage-based rotation on the 执行者 page. The backend is mocked per rotation-contract.md.
const NOW = new Date('2026-11-08T12:00:00+08:00')
const NOW_S = Math.floor(NOW.getTime() / 1000)
const SHOTS = 'test-results/rotation'
/** Round 4 evidence shots; set R4_SHOTS_DIR to save them elsewhere. */
const R4_SHOTS = process.env.R4_SHOTS_DIR || 'test-results/r4-fix'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

test.use({ timezoneId: 'Asia/Shanghai', locale: 'zh-CN' })

type MockWorker = {
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
  quota: { state: 'available' | 'cooling'; reset_at: number | null; reason: string | null }
  models_supported: boolean
  model: string | null
  login_supported: boolean
  usage: { windows: { name: string; used_percent: number; resets_at: number | null }[]; plan: string | null; fetched_at: number | null; available: boolean }
  threshold_percent: number
  over_threshold: boolean
}

function worker(over: Partial<MockWorker> & Pick<MockWorker, 'id' | 'label'>): MockWorker {
  return {
    installed: true,
    version: '1.0.0',
    path: `/usr/local/bin/${over.id}`,
    auth: 'ok',
    login_command: null,
    install_command: null,
    install_supported: false,
    runnable: true,
    enabled: true,
    priority: 1,
    quota: { state: 'available', reset_at: null, reason: null },
    models_supported: false,
    model: null,
    login_supported: false,
    usage: { windows: [], plan: null, fetched_at: NOW_S - 120, available: true },
    threshold_percent: 95,
    over_threshold: false,
    ...over,
  }
}

const FIXTURE = (): MockWorker[] => [
  worker({
    id: 'agy',
    label: 'Antigravity',
    priority: 1,
    over_threshold: true,
    models_supported: true,
    usage: {
      windows: [
        { name: '5h', used_percent: 38, resets_at: NOW_S + 2 * 3600 },
        { name: 'weekly', used_percent: 96, resets_at: NOW_S + 3 * 24 * 3600 },
      ],
      plan: 'Google AI Ultra',
      fetched_at: NOW_S - 300,
      available: true,
    },
  }),
  worker({
    id: 'grok',
    label: 'Super Grok',
    priority: 2,
    usage: {
      windows: [{ name: 'weekly', used_percent: 41, resets_at: NOW_S + 5 * 24 * 3600 }],
      plan: 'SuperGrok',
      fetched_at: NOW_S - 60,
      available: true,
    },
  }),
  worker({
    id: 'codex',
    label: 'Codex',
    priority: 3,
    threshold_percent: 90,
    usage: {
      windows: [
        { name: '5h', used_percent: 72, resets_at: NOW_S + 95 * 60 },
        { name: 'weekly', used_percent: 41, resets_at: NOW_S + 2 * 24 * 3600 },
      ],
      plan: 'ChatGPT Plus',
      fetched_at: NOW_S - 600,
      available: true,
    },
  }),
  worker({
    id: 'claude-code',
    label: 'Claude Code',
    priority: 4,
    models_supported: true,
    usage: { windows: [], plan: null, fetched_at: null, available: false },
  }),
]

const SWITCH_LOG = [
  { at: Math.floor(new Date('2026-11-08T02:10:00+08:00').getTime() / 1000), from: 'agy', to: 'grok', reason: 'threshold', detail: 'weekly 95%' },
  { at: Math.floor(new Date('2026-11-07T21:30:00+08:00').getTime() / 1000), from: 'codex', to: 'agy', reason: 'quota_error', detail: '' },
]

type Calls = { method: string; path: string; body: unknown }[]
type Fixture = {
  workers?: MockWorker[]
  active?: string | null
  switchLog?: typeof SWITCH_LOG
  workersDelayMs?: number
  modelsFail?: boolean
  manyModels?: boolean
}

async function installApi(page: Page, fixture: Fixture = {}) {
  const calls: Calls = []
  let workers = fixture.workers ? JSON.parse(JSON.stringify(fixture.workers)) as MockWorker[] : FIXTURE()
  let active: string | null = fixture.active !== undefined ? fixture.active : 'grok'
  let refreshCount = 0
  let modelsFailNow = Boolean(fixture.modelsFail)

  await page.clock.setFixedTime(NOW)
  await page.addInitScript(() => sessionStorage.setItem('conveyor-token', 'rotation-e2e-token-not-a-secret'))

  const payload = () => ({
    enabled: true,
    install_enabled: true,
    workers,
    active,
    switch_log: fixture.switchLog ?? SWITCH_LOG,
  })

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
        features: { workers: true, mobile_ui: false },
      })
    }

    if (path === '/api/workers' && method === 'GET') {
      if (fixture.workersDelayMs) await new Promise(resolve => setTimeout(resolve, fixture.workersDelayMs))
      return json(payload())
    }
    if (path === '/api/workers/detect' && method === 'POST') return json(payload())
    if (path === '/api/workers/usage/refresh' && method === 'POST') {
      refreshCount += 1
      workers = workers.map(w => w.id === 'grok'
        ? { ...w, usage: { ...w.usage, windows: [{ name: 'weekly', used_percent: 47, resets_at: NOW_S + 5 * 24 * 3600 }], fetched_at: NOW_S } }
        : w)
      return json(payload())
    }
    if (path === '/api/workers/order' && method === 'POST') {
      const { order, enabled } = body as { order: string[]; enabled: string[] }
      const map = new Map(workers.map(w => [w.id, w]))
      workers = order.flatMap((id, idx) => {
        const item = map.get(id)
        return item ? [{ ...item, priority: idx + 1, enabled: enabled.includes(id) }] : []
      })
      return json(payload())
    }
    const threshold = path.match(/^\/api\/workers\/([^/]+)\/threshold$/)
    if (threshold && method === 'POST') {
      const { threshold_percent } = body as { threshold_percent: number }
      if (!Number.isInteger(threshold_percent) || threshold_percent < 1 || threshold_percent > 100) {
        return json({ error: 'bad threshold', code: 'bad_threshold' }, 400)
      }
      workers = workers.map(w => w.id === threshold[1]
        ? { ...w, threshold_percent, over_threshold: w.usage.windows.some(x => x.used_percent >= threshold_percent) }
        : w)
      return json(payload())
    }
    const models = path.match(/^\/api\/workers\/([^/]+)\/models$/)
    if (models && method === 'GET') {
      if (modelsFailNow) {
        modelsFailNow = false
        return json({ error: 'boom' }, 500)
      }
      if (fixture.manyModels) {
        return json({
          models: Array.from({ length: 40 }, (_, i) => ({
            id: `${i % 2 ? 'deepseek' : 'moonshotai'}/model-${i}`,
            label: i < 2 ? 'fast low-cost agentic coding & reasoning' : `A longer description for model number ${i}`,
          })),
          cached_at: NOW_S,
        })
      }
      return json({ models: [{ id: 'gemini-4-argon', label: 'Gemini 4 Argon (High)' }, { id: 'gemini-3.8-flash', label: 'Gemini 3.8 Flash' }], cached_at: NOW_S })
    }
    if (path.endsWith('/model') && method === 'POST') return json(payload())

    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })

  return { calls, getRefreshCount: () => refreshCount, setWorkers: (next: MockWorker[]) => { workers = next } }
}

async function open(page: Page, size = DESKTOP) {
  await page.setViewportSize(size)
  await page.goto('/#workers')
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
  // The shell nav settles a moment after a viewport change; poll the measurement instead of sampling it once.
  await expect.poll(async () => {
    const overflow = await page.locator('.guard-page').evaluate(el => ({
      inner: el.scrollWidth - el.clientWidth,
      right: el.getBoundingClientRect().right - window.innerWidth,
    }))
    return Math.max(overflow.inner, overflow.right)
  }).toBeLessThanOrEqual(1)
}

const cardByLabel = (page: Page, label: string): Locator => page.locator(`li.worker-card[aria-label="${label}"]`)
const orderOf = (page: Page) => page.locator('li.worker-card').evaluateAll(els => els.map(el => el.getAttribute('aria-label')))

/** Touch drag on the handle: pointer events with pointerType "touch" (no OS drag is involved). */
async function touchDrag(page: Page, handle: Locator, toY: number, holdMs = 0) {
  const box = (await handle.boundingBox())!
  const x = box.x + box.width / 2
  const y = box.y + box.height / 2
  await handle.evaluate((el, p) => {
    el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: p.x, clientY: p.y }))
  }, { x, y })
  const steps = 6
  for (let i = 1; i <= steps; i++) {
    const yy = y + ((toY - y) * i) / steps
    await page.evaluate(py => window.dispatchEvent(new PointerEvent('pointermove', { pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: 100, clientY: py })), yy)
  }
  if (holdMs) await page.waitForTimeout(holdMs)
  await page.evaluate(py => window.dispatchEvent(new PointerEvent('pointerup', { pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: 100, clientY: py })), toY)
}

test.describe('Usage-based rotation (执行者)', () => {
  test('usage bars, plan, reset time, unavailable note, summary and switch log', async ({ page }) => {
    await installApi(page)
    await open(page)

    await expect(page.getByText('当前使用：Super Grok')).toBeVisible()

    const agy = cardByLabel(page, 'Antigravity')
    await expect(agy.getByText('5 小时 38%')).toBeVisible()
    await expect(agy.getByText('本周 96%')).toBeVisible()
    await expect(agy.getByText('2 小时后重置')).toBeVisible()
    await expect(agy.getByText('3 天后重置')).toBeVisible()
    await expect(agy.getByText('套餐：Google AI Ultra')).toBeVisible()
    await expect(agy.getByRole('progressbar', { name: '本周用量' })).toHaveAttribute('aria-valuenow', '96')

    const codex = cardByLabel(page, 'Codex')
    await expect(codex.getByText('5 小时 72%')).toBeVisible()
    await expect(codex.getByText('本周 41%')).toBeVisible()
    await expect(codex.getByText('1 小时后重置').or(codex.getByText('2 小时后重置'))).toBeVisible()

    // No live usage: say so, and promise the quota-error fallback.
    const claude = cardByLabel(page, 'Claude Code')
    await expect(claude.getByText('实时用量不可用，额度用完时自动切换')).toBeVisible()
    await expect(claude.getByRole('progressbar')).toHaveCount(0)

    // 切换记录 is collapsed by default and reads as Chinese sentences.
    const log = page.locator('details.workers-switch-log')
    await expect(log).not.toHaveAttribute('open', '')
    await log.locator('summary').click()
    // The detail's raw "weekly 95%" becomes the window name and percentage, with no repeated "用量" (QA F10).
    await expect(log.getByText('11月8日 02:10 Antigravity 本周用量已用 95%，换到 Super Grok')).toBeVisible()
    await expect(log.getByText('11月7日 21:30 Codex 额度用完 → 换到 Antigravity')).toBeVisible()

    await shoot(page, 'usage')
    await page.setViewportSize(PHONE)
    await expectNoSidewaysOverflow(page)
    await shoot(page, 'usage')
  })

  test('over-threshold chip and one state chip + one role chip per worker', async ({ page }) => {
    await installApi(page)
    await open(page)

    const agy = cardByLabel(page, 'Antigravity')
    await expect(agy.getByText('已达上限 · 约 3 天后恢复')).toBeVisible()
    await expect(agy.getByText('可用', { exact: true })).toHaveCount(0)
    // Over the line, so the role chip says it is skipped, never 参与轮换 (QA F3).
    await expect(agy.locator('.worker-role')).toHaveText('暂时跳过')
    await expect(agy.getByText('参与轮换')).toHaveCount(0)
    await expect(cardByLabel(page, 'Codex').locator('.worker-role')).toHaveText('参与轮换')

    const grok = cardByLabel(page, 'Super Grok')
    await expect(grok.getByText('当前使用', { exact: true })).toBeVisible()
    await expect(grok.getByText('可用', { exact: true })).toBeVisible()
    for (const label of ['Antigravity', 'Super Grok', 'Codex', 'Claude Code']) {
      await expect(cardByLabel(page, label).locator('.worker-card-head .guard-badge')).toHaveCount(1)
      await expect(cardByLabel(page, label).locator('.worker-card-head .worker-role')).toHaveCount(1)
    }
  })

  test('threshold: number input saves via POST /threshold', async ({ page }) => {
    const api = await installApi(page)
    await open(page)

    const codex = cardByLabel(page, 'Codex')
    await expect(codex.getByText('% 时换下一个')).toBeVisible()
    const input = codex.getByRole('spinbutton', { name: 'Codex 换班线百分比' })
    await expect(input).toHaveValue('90')
    await expect(codex.getByRole('button', { name: '保存 Codex 的换班线' })).toHaveCount(0)

    await input.fill('70')
    await codex.getByRole('button', { name: '保存 Codex 的换班线' }).click()
    await expect(page.getByText('已保存：Codex 用量到 70% 时换下一个。')).toBeVisible()
    const call = api.calls.find(c => c.path === '/api/workers/codex/threshold')
    expect(call?.body).toEqual({ threshold_percent: 70 })
    // 5h 72% is now past the new line, so the server flags it and the chip follows.
    await expect(codex.getByText(/已达上限/)).toBeVisible()

    // Out of range never reaches the server.
    await input.fill('20')
    await input.press('Enter')
    await expect(codex.getByText('请填 50 到 100 之间的整数。')).toBeVisible()
    expect(api.calls.filter(c => c.path === '/api/workers/codex/threshold')).toHaveLength(1)
  })

  test('refresh usage posts /usage/refresh and updates the bars', async ({ page }) => {
    const api = await installApi(page)
    await open(page)
    const grok = cardByLabel(page, 'Super Grok')
    await expect(grok.getByText('本周 41%')).toBeVisible()
    await page.getByRole('button', { name: '刷新用量' }).click()
    await expect(page.getByText('已刷新用量。')).toBeVisible()
    await expect(grok.getByText('本周 47%')).toBeVisible()
    await expect(grok.getByText('刚刚更新')).toBeVisible()
    expect(api.getRefreshCount()).toBe(1)
  })

  test('drag the ⠿ handle with the mouse: reorder, 未保存, then 保存顺序 posts the order', async ({ page }) => {
    const api = await installApi(page)
    await open(page)
    expect(await orderOf(page)).toEqual(['Antigravity', 'Super Grok', 'Codex', 'Claude Code'])
    await expect(page.getByText('未保存')).toHaveCount(0)

    const handle = cardByLabel(page, 'Codex').locator('.worker-drag-handle')
    const target = cardByLabel(page, 'Antigravity')
    await handle.dragTo(target, { targetPosition: { x: 200, y: 6 } })

    expect(await orderOf(page)).toEqual(['Codex', 'Antigravity', 'Super Grok', 'Claude Code'])
    await expect(page.getByText('未保存', { exact: true })).toBeVisible()
    const save = page.getByRole('button', { name: '保存顺序' })
    await expect(save).toHaveClass(/guard-btn-primary/)

    await save.click()
    await expect(page.getByText('已保存执行者顺序。')).toBeVisible()
    await expect(page.getByText('未保存', { exact: true })).toHaveCount(0)
    const call = api.calls.find(c => c.path === '/api/workers/order')
    expect(call?.body).toEqual({
      order: ['codex', 'agy', 'grok', 'claude-code'],
      enabled: ['codex', 'agy', 'grok', 'claude-code'],
    })
  })

  test('the drop indicator shows while dragging over a card', async ({ page }) => {
    await installApi(page)
    await open(page)
    const handle = cardByLabel(page, 'Codex').locator('.worker-drag-handle')
    const target = cardByLabel(page, 'Super Grok')
    const hb = (await handle.boundingBox())!
    const tb = (await target.boundingBox())!
    await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2)
    await page.mouse.down()
    await page.mouse.move(tb.x + 200, tb.y + 20, { steps: 8 })
    await page.mouse.move(tb.x + 200, tb.y + 8, { steps: 4 })
    await expect(page.locator('li.worker-card.drop-before')).toHaveAttribute('aria-label', 'Super Grok')
    await shoot(page, 'drop-indicator')
    await page.mouse.up()
    expect(await orderOf(page)).toEqual(['Antigravity', 'Codex', 'Super Grok', 'Claude Code'])
    await expect(page.locator('li.worker-card.drop-before')).toHaveCount(0)
  })

  test('touch drag on the handle (pointer events) reorders and auto-scrolls near the edge', async ({ page }) => {
    const api = await installApi(page)
    await open(page, PHONE)

    const handle = cardByLabel(page, 'Claude Code').locator('.worker-drag-handle')
    await handle.scrollIntoViewIfNeeded()
    const first = (await cardByLabel(page, 'Antigravity').boundingBox())!
    await touchDrag(page, handle, first.y + 6)
    expect(await orderOf(page)).toEqual(['Claude Code', 'Antigravity', 'Super Grok', 'Codex'])
    await expect(page.getByText('未保存', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '保存顺序' }).click()
    await expect(page.getByText('已保存执行者顺序。')).toBeVisible()
    const call = api.calls.find(c => c.path === '/api/workers/order')
    expect((call?.body as { order: string[] }).order).toEqual(['claude-code', 'agy', 'grok', 'codex'])

    // Holding a finger near the bottom edge scrolls whatever scrolls (window or the app's pane).
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.locator('li.worker-card').first().scrollIntoViewIfNeeded()
    const scrollSum = () => page.evaluate(() => {
      let total = window.scrollY
      document.querySelectorAll<HTMLElement>('*').forEach(el => { total += el.scrollTop })
      return total
    })
    const before = await scrollSum()
    const top = cardByLabel(page, 'Claude Code').locator('.worker-drag-handle')
    await touchDrag(page, top, PHONE.height - 8, 600)
    expect(await scrollSum()).toBeGreaterThan(before)
  })

  test('keyboard ↑/↓ still reorder and mark the list 未保存', async ({ page }) => {
    await installApi(page)
    await open(page)
    await page.getByRole('button', { name: '下移 Antigravity' }).click()
    expect(await orderOf(page)).toEqual(['Super Grok', 'Antigravity', 'Codex', 'Claude Code'])
    await expect(page.getByText('未保存', { exact: true })).toBeVisible()
    await expect(page.getByText('顺序或开关改过了')).toBeVisible()
  })

  test('重新检测 keeps unsaved order and switches (QA F5)', async ({ page }) => {
    const api = await installApi(page)
    await open(page)
    await page.getByRole('button', { name: '下移 Antigravity' }).click()
    await page.locator('label.worker-toggle', { has: page.getByLabel('启用 Codex') }).click()
    expect(await orderOf(page)).toEqual(['Super Grok', 'Antigravity', 'Codex', 'Claude Code'])

    await page.getByRole('button', { name: '重新检测' }).click()
    await expect(page.getByText('你还没保存的顺序和开关改动都还在')).toBeVisible()
    expect(await orderOf(page)).toEqual(['Super Grok', 'Antigravity', 'Codex', 'Claude Code'])
    await expect(page.getByLabel('启用 Codex')).not.toBeChecked()
    await expect(page.getByText('未保存', { exact: true })).toBeVisible()
    expect(api.calls.some(c => c.path === '/api/workers/detect')).toBe(true)

    await page.getByRole('button', { name: '保存顺序' }).click()
    const call = api.calls.find(c => c.path === '/api/workers/order')
    expect(call?.body).toEqual({
      order: ['grok', 'agy', 'codex', 'claude-code'],
      enabled: ['grok', 'agy', 'claude-code'],
    })
  })

  test('model picker shows ids and is searchable past 15 models (QA F2)', async ({ page }) => {
    const api = await installApi(page, { manyModels: true })
    await open(page)
    const agy = cardByLabel(page, 'Antigravity')
    const select = agy.getByRole('combobox', { name: 'Antigravity 模型选择' })
    await expect(agy.getByText('点一下选择框，读取可选的模型。')).toBeVisible()
    await select.focus()
    await expect(agy.getByText('共 40 个模型')).toBeVisible()
    // The id is the option text, and duplicated descriptions no longer hide which model is which.
    // The human label comes first, the id second (QA F8).
    await expect(select.locator('option[value="moonshotai/model-0"]')).toHaveText(/^fast low-cost agentic coding & reasoning · moonshotai\/model-0$/)
    await expect(select.locator('option[value="deepseek/model-1"]')).toHaveText(/^fast low-cost agentic coding & reasoning · deepseek\/model-1$/)

    const search = agy.getByRole('searchbox', { name: '搜索 Antigravity 的模型' })
    await search.fill('model-3')
    await expect(agy.getByText(/^找到 \d+ 个/)).toBeVisible()
    await expect(select.locator('option[value="deepseek/model-3"]')).toBeAttached()
    await expect(select.locator('option[value="deepseek/model-1"]')).toHaveCount(0)
    await select.selectOption('deepseek/model-3')
    await expect(agy.getByText('✓ 已保存')).toBeVisible()
    expect(api.calls.find(c => c.path === '/api/workers/agy/model')?.body).toEqual({ model: 'deepseek/model-3' })
    await shoot(page, 'model-picker')

    // Small lists get no search box.
    const claude = cardByLabel(page, 'Claude Code')
    await expect(claude.getByRole('searchbox')).toHaveCount(0)
  })

  test('model list failure shows 重试 and recovers (QA F4)', async ({ page }) => {
    await installApi(page, { modelsFail: true })
    await open(page)
    const claude = cardByLabel(page, 'Claude Code')
    await claude.getByRole('combobox', { name: 'Claude Code 模型选择' }).focus()
    await expect(claude.getByText('读取模型列表失败。')).toBeVisible()
    await claude.getByRole('button', { name: '重试' }).click()
    await expect(claude.getByText('共 2 个模型')).toBeVisible()
    await expect(claude.getByText('读取模型列表失败。')).toHaveCount(0)
  })

  test('first load shows a skeleton with a progress line (QA F13)', async ({ page }) => {
    await installApi(page, { workersDelayMs: 1500 })
    await page.setViewportSize(DESKTOP)
    await page.goto('/#workers')
    await expect(page.getByText('正在检测已安装的执行者')).toBeVisible()
    await expect(page.locator('li.worker-card.is-skeleton')).toHaveCount(3)
    await shoot(page, 'loading')
    await expect(cardByLabel(page, 'Antigravity')).toBeVisible()
    await expect(page.locator('li.worker-card.is-skeleton')).toHaveCount(0)
  })

  test('first-step guide when nothing is installed (QA F6)', async ({ page }) => {
    const fresh = FIXTURE().map(w => ({ ...w, installed: false, version: null, path: null, usage: { ...w.usage, windows: [] }, install_command: 'npm install -g x', install_supported: true, over_threshold: false }))
    await installApi(page, { workers: fresh, active: null, switchLog: [] })
    await open(page)
    await expect(page.getByText(/^第一步：先装好至少一个执行者/)).toBeVisible()
    await expect(page.getByText('当前使用：暂时没有可用的执行者')).toBeVisible()
    await expect(page.getByText('服务器')).toHaveCount(0)
    await shoot(page, 'first-step')
    await page.setViewportSize(PHONE)
    await expectNoSidewaysOverflow(page)
    await shoot(page, 'first-step')
  })

  test('cooling card: Chinese reason, raw text behind 技术详情, clearer button (QA F8)', async ({ page }) => {
    const workers = FIXTURE()
    workers[2] = { ...workers[2], quota: { state: 'cooling', reset_at: NOW_S + 3 * 3600, reason: 'RESOURCE_EXHAUSTED: 429 Too Many Requests. Resets in 3h10m' } }
    await installApi(page, { workers })
    await open(page)
    const codex = cardByLabel(page, 'Codex')
    await expect(codex.getByText('额度用完·约 3 小时后恢复')).toBeVisible()
    await expect(codex.getByText('这个账号的额度已经用完。')).toBeVisible()
    await expect(codex.getByText('RESOURCE_EXHAUSTED')).toBeHidden()
    await expect(codex.getByRole('button', { name: '我已处理，立即重试' })).toBeVisible()
    await expect(codex.getByText('如果额度还没恢复，下一个任务还会失败')).toBeVisible()
    await shoot(page, 'cooling')
  })

  test('server error codes sit behind 技术详情 (QA F21)', async ({ page }) => {
    await installApi(page)
    await page.route('**/api/workers/order', route => route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'x', code: 'unknown_worker' }) }))
    await open(page)
    await page.getByRole('button', { name: '下移 Antigravity' }).click()
    await page.getByRole('button', { name: '保存顺序' }).click()
    const alert = page.getByRole('alert').filter({ hasText: '未找到指定的执行者。' })
    await expect(alert).toBeVisible()
    await expect(alert.getByText('unknown_worker')).toBeHidden()
    await alert.getByText('技术详情').click()
    await expect(alert.getByText('unknown_worker')).toBeVisible()
    // The edits survive a failed save.
    await expect(page.getByText('未保存', { exact: true })).toBeVisible()
  })

  test('phone 390: no sideways scroll, controls reachable, screenshot', async ({ page }) => {
    await installApi(page)
    await open(page, PHONE)
    await expectNoSidewaysOverflow(page)
    const handleBox = (await cardByLabel(page, 'Antigravity').locator('.worker-drag-handle').boundingBox())!
    expect(handleBox.width).toBeGreaterThanOrEqual(36)
    expect(handleBox.height).toBeGreaterThanOrEqual(36)
    await page.locator('details.workers-switch-log summary').click()
    await shoot(page, 'overview')
  })
  test('built-in Pi shows the 模型设置 provider and links to 模型设置', async ({ page }) => {
    const piBase = worker({
      id: 'pi', label: 'Pi（内置）', priority: 1, models_supported: true,
      usage: { windows: [], plan: null, fetched_at: null, available: false },
    })
    const configured = { ...piBase, auth: 'ok' as const, provider: { configured: true, provider_name: 'StepFun', model: 'step-3' } }
    const unconfigured = {
      ...piBase, auth: 'needs_login' as const, auth_hint: '先在「设置 → 模型设置」里填服务商',
      provider: { configured: false, provider_name: null, model: null },
    }
    const api = await installApi(page, { workers: [configured as unknown as MockWorker], active: 'pi' })
    const panel = await open(page)
    const card = cardByLabel(page, 'Pi（内置）')
    await expect(card.getByText('内置 · 使用「模型设置」里的服务商：StepFun / step-3')).toBeVisible()
    await expect(card.getByRole('button', { name: '去模型设置' })).toHaveCount(0)

    api.setWorkers([unconfigured as unknown as MockWorker])
    await panel.getByRole('button', { name: '重新检测' }).click()
    await expect(card.getByText('先在「设置 → 模型设置」里填服务商')).toBeVisible()
    await expect(card.getByText('未配置服务商')).toBeVisible()
    await card.getByRole('button', { name: '去模型设置' }).click()
    await expect(page.getByRole('dialog', { name: '模型设置' })).toBeVisible()
  })

  test('保存顺序 is disabled until the order or a switch changes, and sends nothing when quiet (QA F6)', async ({ page }) => {
    const api = await installApi(page)
    await open(page)
    const save = page.getByRole('button', { name: '保存顺序' })
    await expect(save).toBeDisabled()
    expect(api.calls.some(c => c.path === '/api/workers/order')).toBe(false)

    await page.getByRole('button', { name: '下移 Antigravity' }).click()
    await expect(save).toBeEnabled()
    await save.click()
    await expect(page.getByText('已保存执行者顺序。')).toBeVisible()
    await expect(save).toBeDisabled()
    expect(api.calls.filter(c => c.path === '/api/workers/order')).toHaveLength(1)
  })

  test('the lifted card is solid while dragging on a phone, so text underneath does not show through (QA F5)', async ({ page }) => {
    await installApi(page)
    await open(page, PHONE)
    const handle = cardByLabel(page, 'Claude Code').locator('.worker-drag-handle')
    const box = (await handle.boundingBox())!
    const x = box.x + box.width / 2
    const y = box.y + box.height / 2
    await handle.evaluate((el, p) => {
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: p.x, clientY: p.y }))
    }, { x, y })
    await page.evaluate(py => window.dispatchEvent(new PointerEvent('pointermove', { pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: 100, clientY: py })), y - 120)

    const dragged = page.locator('li.worker-card.is-dragged')
    await expect(dragged).toHaveCount(1)
    const style = await dragged.evaluate(el => {
      const s = getComputedStyle(el)
      return { opacity: s.opacity, background: s.backgroundColor, shadow: s.boxShadow }
    })
    expect(style.opacity).toBe('1')
    expect(style.background).toBe('rgb(255, 255, 255)')
    expect(style.shadow).not.toBe('none')
    await page.evaluate(py => window.dispatchEvent(new PointerEvent('pointerup', { pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: 100, clientY: py })), y - 120)
  })

  test('r4 screenshots: 执行者 at 1440 and 390 (QA fix evidence)', async ({ page, context }) => {
    for (const size of [DESKTOP, PHONE]) {
      const p = size === DESKTOP ? page : await context.newPage()
      await installApi(p)
      await open(p, size)
      await expect(cardByLabel(p, 'Codex')).toBeVisible()
      await p.evaluate(() => document.fonts.ready)
      await p.screenshot({ path: `${R4_SHOTS}/r4-fix-workers-${size.width}.png`, fullPage: true, animations: 'disabled' })
    }
  })
})
