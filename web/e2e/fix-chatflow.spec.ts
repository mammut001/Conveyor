import { expect, test, type Browser, type Page, type Route } from '@playwright/test'

// Chat-flow fixes: send on phones, interrupted replies, raw backend errors, touch Enter, retry, counter, approvals, tap targets.
// Screenshots: web/test-results/fix-chatflow/

const sse = (events: Array<[string, unknown]>) => events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join('')
const okReply = (text = '收到，已经处理好了。') => sse([['session', { session_id: 's1' }], ['message', { text }], ['done', {}]])

type Handler = (route: Route, count: number) => Promise<void> | void

async function setup(page: Page, chat: Handler, history: unknown[] = []) {
  let count = 0
  await page.addInitScript(() => { localStorage.setItem('conveyor-token', 't') })
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
    if (path === '/api/chat') return chat(route, ++count)
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/chat/history') return json({ session_id: 's1', messages: history })
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0, 0, 0], cpu_count: 1, memory: { total: null, available: null },
      disk: { total: 1000, used: 0, free: 1000 }, queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: false }, feishu: { configured: false } }, nodes: [],
      features: { agents: false, long_term_memory: true, routines: true, teammate: true, live_screen: true, mobile_ui: false, pwa: false, approval_inbox: false, skills: false, workflows: false, mcp: false, webhooks: false, subagents: false },
    })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    return json({ items: [], approvals: [], sessions: [], jobs: [], nodes: [] })
  })
}
const sseRoute = (body: string): Handler => route => route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body })
const composer = (page: Page) => page.locator('.chat-panel-container textarea')
const shot = (page: Page, name: string) => page.screenshot({ path: `test-results/fix-chatflow/${name}.png` })

async function open(page: Page, width = 1440, height = 900) {
  await page.setViewportSize({ width, height })
  await page.goto('/')
  if (!(await composer(page).first().isVisible())) {
    const nav = page.getByRole('button', { name: /^(💬\s*)?对话$/ }).or(page.getByRole('tab', { name: /^(💬\s*)?对话$/ })).or(page.getByRole('link', { name: /^(💬\s*)?对话$/ }))
    await nav.first().click()
  }
  await expect(composer(page)).toBeVisible()
}

async function phone(browser: Browser) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 })
  return { context, page: await context.newPage() }
}

test('phone: tapping 发送 while the keyboard is open sends', async ({ browser }) => {
  const { context, page } = await phone(browser)
  let sent = ''
  await setup(page, async route => { sent = JSON.parse(route.request().postData() || '{}').message; await sseRoute(okReply())(route, 0) })
  await open(page, 390, 844)
  await composer(page).tap()
  await composer(page).fill('帮我看看磁盘')
  await expect(composer(page)).toBeFocused()
  const button = page.getByRole('button', { name: '发送', exact: true })
  const before = await button.boundingBox()
  await shot(page, 'phone-composer-focused-390')
  const box = (await button.boundingBox())!
  await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2)
  await expect(page.getByText('收到，已经处理好了。')).toBeVisible()
  expect(sent).toBe('帮我看看磁盘')
  expect(box.y).toBeCloseTo(before!.y, 0)
  await shot(page, 'phone-after-send-390')
  await context.close()
})

test('phone: Enter makes a new line, no Enter hint', async ({ browser }) => {
  const { context, page } = await phone(browser)
  let calls = 0
  await setup(page, async route => { calls++; await sseRoute(okReply())(route, 0) })
  await open(page, 390, 844)
  await composer(page).tap()
  await page.keyboard.type('第一行')
  await page.keyboard.press('Enter')
  await page.keyboard.type('第二行')
  await expect(composer(page)).toHaveValue('第一行\n第二行')
  expect(calls).toBe(0)
  await expect(page.locator('.chat-composer-hint')).toHaveText('')
  await context.close()
})

test('desktop: Enter sends, Shift+Enter is a new line', async ({ page }) => {
  let calls = 0
  await setup(page, async route => { calls++; await sseRoute(okReply())(route, 0) })
  await open(page)
  await expect(page.locator('.chat-composer-hint')).toContainText('Enter 发送')
  await composer(page).fill('甲')
  await page.keyboard.press('Shift+Enter')
  await page.keyboard.type('乙')
  await expect(composer(page)).toHaveValue('甲\n乙')
  await page.keyboard.press('Enter')
  await expect(page.getByText('收到，已经处理好了。')).toBeVisible()
  expect(calls).toBe(1)
})

test('leaving mid-reply puts the message back in the box', async ({ page }) => {
  await setup(page, () => new Promise<void>(() => { /* never answers */ }))
  await open(page)
  await composer(page).fill('请整理本周日志')
  await page.keyboard.press('Enter')
  await expect(page.getByText('请整理本周日志')).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: /^(💬\s*)?对话$/ }).or(page.getByRole('tab', { name: /^(💬\s*)?对话$/ })).first().click()
  await expect(composer(page)).toHaveValue('请整理本周日志')
  await expect(page.getByText('上次的回复被打断了，消息已放回输入框，可以重新发送')).toBeVisible()
  await shot(page, 'interrupted-1440')
})

test('a missing executor is not offered a pointless retry', async ({ page }) => {
  await setup(page, async (route, count) => sseRoute(sse([['session', { session_id: 's1' }], ['message', { text: "[Errno 2] No such file or directory: 'codex'" }], ['done', {}]]))(route, count))
  await open(page)
  await composer(page).fill('跑一下检查')
  await page.keyboard.press('Enter')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('还没有可用的执行者')
  await expect(alert.getByRole('button', { name: '重试' })).toHaveCount(0)
  await expect(page.locator('.transcript-content', { hasText: 'Errno' })).toHaveCount(0)
  await expect(composer(page)).toHaveValue('跑一下检查')
})

test('raw backend error becomes an error card with retry, not a bubble', async ({ page }) => {
  await setup(page, async (route, count) => {
    if (count === 1) return sseRoute(sse([['session', { session_id: 's1' }], ['message', { text: 'Traceback (most recent call last):\n  File "x.py", line 1\nValueError: boom' }], ['done', {}]]))(route, count)
    return sseRoute(okReply('这次好了。'))(route, count)
  })
  await open(page)
  await composer(page).fill('跑一下检查')
  await page.keyboard.press('Enter')
  const alert = page.getByRole('alert')
  await expect(alert).toBeVisible()
  await expect(alert.getByRole('button', { name: '重试' })).toBeVisible()
  await expect(page.locator('.transcript-content', { hasText: 'Traceback' })).toHaveCount(0)
  // The failed bubble is gone, the words are back in the box.
  await expect(page.locator('.role-user')).toHaveCount(0)
  await expect(composer(page)).toHaveValue('跑一下检查')
  await shot(page, 'raw-error-1440')
  await alert.getByRole('button', { name: '重试' }).click()
  await expect(page.getByText('这次好了。')).toBeVisible()
  await expect(page.locator('.role-user')).toHaveCount(1)
  await expect(composer(page)).toHaveValue('')
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('failed send: one retry sends exactly once and the card clears', async ({ page }) => {
  const bodies: string[] = []
  await setup(page, async (route, count) => {
    bodies.push(route.request().postData() || '')
    if (count === 1) return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'internal error' }) })
    return sseRoute(okReply())(route, count)
  })
  await open(page)
  await composer(page).fill('同步一下记忆')
  await page.keyboard.press('Enter')
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.locator('.role-user')).toHaveCount(0)
  await expect(composer(page)).toHaveValue('同步一下记忆')
  await page.getByRole('button', { name: '重试' }).click()
  await expect(page.getByText('收到，已经处理好了。')).toBeVisible()
  expect(bodies).toHaveLength(2)
  await expect(page.locator('.role-user')).toHaveCount(1)
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('history reply that is a raw error is a failed card; /deep hint is hidden', async ({ page }) => {
  await setup(page, sseRoute(okReply()), [
    { role: 'user', text: '查一下', created_at: new Date().toISOString() },
    { role: 'assistant', text: 'Traceback (most recent call last):\n  File "x.py", line 1\nValueError: boom', created_at: new Date().toISOString() },
    { role: 'assistant', text: '大致是这样。\n\n发 /deep 让 Codex 深入查证。', created_at: new Date().toISOString() },
  ])
  await page.addInitScript(() => localStorage.setItem('conveyor-chat-session', 's1'))
  await open(page)
  await expect(page.getByText('这条消息没有得到回复')).toBeVisible()
  await expect(page.getByText('大致是这样。')).toBeVisible()
  await expect(page.getByText('/deep')).toHaveCount(0)
  await expect(page.locator('.chat-failed-reply code')).toBeHidden()
})

test('character counter: hidden, muted at 90%, red at the limit, never truncates', async ({ page }) => {
  await setup(page, sseRoute(okReply()))
  await open(page)
  await composer(page).fill('字'.repeat(100))
  await expect(page.locator('.chat-composer-count')).toHaveCount(0)
  await composer(page).fill('字'.repeat(7950))
  const count = page.locator('.chat-composer-count')
  await expect(count).toHaveText('7,950 / 8,000')
  const muted = await count.evaluate(n => getComputedStyle(n).color)
  await composer(page).fill('字'.repeat(8000))
  await expect(count).toHaveClass(/is-limit/)
  expect(await count.evaluate(n => getComputedStyle(n).color)).not.toBe(muted)
  await shot(page, 'counter-limit-1440')
  await composer(page).fill('字'.repeat(8005))
  expect(await composer(page).inputValue()).toHaveLength(8005)
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled()
})

test('approval card shows plain Chinese with the raw id as muted detail', async ({ page }) => {
  await setup(page, sseRoute(sse([
    ['session', { session_id: 's1' }],
    ['approval', { id: 'ap1', tool_name: 'pi.destructive_task', arg: '/srv/old', summary: '', text: '工具: Pi\n风险: destructive\n删除旧目录', expires_in_seconds: 300 }],
    ['done', {}],
  ])))
  await open(page)
  await composer(page).fill('清理旧目录')
  await page.keyboard.press('Enter')
  const card = page.locator('.chat-approval-card')
  await expect(card).toContainText('操作：删除类任务')
  await expect(card).toContainText('风险：高')
  await expect(card.locator('.chat-approval-summary')).not.toContainText('destructive')
  await expect(card.locator('.chat-approval-summary')).not.toContainText('pi.')
  await expect(card.locator('details code')).toHaveText('pi.destructive_task')
  await shot(page, 'approval-1440')
})

test('phone tap targets: 复制 and the new-message pill are at least 40px tall', async ({ browser }) => {
  const { context, page } = await phone(browser)
  const long = Array.from({ length: 40 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', text: `第 ${i} 条消息，内容比较长一点用来撑满屏幕。`, created_at: new Date().toISOString() }))
  await setup(page, sseRoute(okReply()), long)
  await page.addInitScript(() => localStorage.setItem('conveyor-chat-session', 's1'))
  await open(page, 390, 844)
  const copy = page.locator('.chat-copy-button').last()
  await expect(copy).toBeVisible()
  expect((await copy.boundingBox())!.height).toBeGreaterThanOrEqual(40)
  await page.locator('.event-stream').evaluate(n => { n.scrollTop = 0 })
  await page.locator('.event-stream').evaluate(n => n.dispatchEvent(new Event('scroll')))
  const pill = page.locator('.chat-jump-latest')
  await expect(pill).toBeVisible()
  expect((await pill.boundingBox())!.height).toBeGreaterThanOrEqual(40)
  await shot(page, 'phone-targets-390')
  await context.close()
})
