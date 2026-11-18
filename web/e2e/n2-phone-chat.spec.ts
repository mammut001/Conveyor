import { expect, test, type Page, type Route } from '@playwright/test'

// N2 — on a phone the 对话 screen keeps the whole width: the session rail and the agent avatar
// rail are desktop-only (the top 对话 pickers switch sessions and agents), the message column is
// never squeezed to a third of the screen, and on desktop the session rail starts collapsed and
// remembers the owner's choice per browser.
// Screenshots: web/test-results/n2-phone-chat/<view>-<width>.png

const now = Date.now()
const iso = (minutesAgo: number) => new Date(now - minutesAgo * 60_000).toISOString()
const epoch = (minutesAgo: number) => Math.floor((now - minutesAgo * 60_000) / 1000)
const agentBase = { instructions: '负责日常运维和发布。', workspace_path: '', status: 'idle', last_message: '', message_count: 4 }
const conveyor = { ...agentBase, id: 'default', name: 'Conveyor', color: '#bd5c40', is_default: true, session_id: 'web:web-console:agent-default' }
const finance = { ...agentBase, id: '7c1d2e3f4a', name: 'Finance', color: '#10b981', is_default: false, session_id: 'web:web-console:agent-7c1d2e3f4a' }
const researcher = { ...agentBase, id: '3f2a9c1b0d', name: 'Researcher', color: '#8b5cf6', is_default: false, session_id: 'web:web-console:agent-3f2a9c1b0d' }
const coder = { ...agentBase, id: '9e8d7c6b5a', name: 'Coder', color: '#2f7df6', is_default: false, session_id: 'web:web-console:agent-9e8d7c6b5a' }
const telegramTopic = `web:web-console:topic-${'a'.repeat(32)}`
const planTopic = `web:web-console:topic-${'b'.repeat(32)}`

const history = [
  { role: 'user', text: '帮我看看昨晚的部署有没有问题', origin_channel: 'telegram', created_at: iso(60 * 26) },
  { role: 'assistant', text: '昨晚 23:40 的部署已经完成，所有健康检查都通过了。', origin_channel: 'telegram', created_at: iso(60 * 26 - 1) },
  { role: 'user', text: '把这周的服务器开销整理成表格，再给一段查询脚本', origin_channel: 'web', created_at: iso(12) },
  { role: 'assistant', text: '## 本周服务器开销\n\n整体比上周下降 **12%**，主要来自对象存储的清理。', origin_channel: 'web', created_at: iso(11) },
]
const inbox = [
  { id: 'm1', from_agent: researcher.id, to_agent: conveyor.id, operator: 'web-console', kind: 'message',
    body: '我整理好了三家云厂商的价格对比，放在共享资料里了。', status: 'done', created_at: epoch(35), untrusted: true },
]
const tasks = [
  { id: 'c'.repeat(32), from_agent: conveyor.id, to_agent: coder.id, status: 'leased', attempts: 1, created_at: epoch(8), deadline: epoch(-60) },
]

type Options = { chat?: (route: Route) => Promise<void> | void }

async function fixture(page: Page, options: Options = {}) {
  // Skip the unlock form: a remembered token unlocks on load.
  await page.addInitScript(() => { localStorage.setItem('conveyor-token', 'n2-phone-chat-token') })
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
    if (path === '/api/chat' && options.chat) return options.chat(route)
    if (path === '/api/agents') return json({ enabled: true, agents: [conveyor, finance, researcher, coder] })
    if (path === '/api/agent-screens/config') return json({ enabled: true, inbox: true, tasks: true })
    if (path === '/api/agent-screens') return json({ agent_id: url.searchParams.get('agent_id'), items: url.searchParams.get('view') === 'inbox' ? inbox : tasks })
    if (path === '/api/agent-conversations') {
      const agentId = url.searchParams.get('agent_id')
      const agent = [conveyor, finance, researcher, coder].find(item => item.id === agentId) || conveyor
      return json({ conversations: [
        { id: `${agent.id}-web`, title: 'Web 会话（原有记录）', origin_channel: 'web', session_id: agent.session_id },
        { id: 'c-tg', title: 'Telegram 独立会话', origin_channel: 'telegram', session_id: telegramTopic },
        { id: 'c-plan', title: '发布计划', origin_channel: 'web', session_id: planTopic },
      ] })
    }
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'),
      messages: url.searchParams.get('session_id') === conveyor.session_id ? history : [] })
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0, 0, 0], cpu_count: 1,
      memory: { total: null, available: null }, disk: { total: 1000, used: 0, free: 1000 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: true }, feishu: { configured: false } }, nodes: [],
      features: { agents: true, long_term_memory: true, routines: true, teammate: true, live_screen: true,
        mobile_ui: false, pwa: false, approval_inbox: false, skills: false, workflows: false, mcp: false, webhooks: false, subagents: false },
    })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    return json({ items: [], approvals: [], sessions: [], jobs: [], nodes: [] })
  })
}

const shot = (page: Page, view: string, width: number) =>
  page.screenshot({ path: `test-results/n2-phone-chat/${view}-${width}.png`, fullPage: true })

async function openChat(page: Page, width: number, height: number, options: Options = {}) {
  await page.setViewportSize({ width, height })
  await fixture(page, options)
  await page.goto('/')
  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await expect(composer).toBeVisible()
  // The message list is rendered before anything is measured.
  await expect(page.getByRole('heading', { name: '本周服务器开销' })).toBeVisible()
  return composer
}

/** Nothing in the chat column may scroll sideways. */
async function noSidewaysScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  for (const selector of ['.chat-main-column', '.chat-panel-container', '.chat-stream-content']) {
    expect(await page.locator(selector).evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
  }
}

// Phone (390x844): both rails are gone, the message column and the composer take the full width.
test('phone (390x844): the two chat rails are gone and 对话 gets the full width', async ({ page }) => {
  await openChat(page, 390, 844)

  // (a) the session rail and (b) the agent avatar rail are not in the chat layout at all.
  await expect(page.locator('.chat-topic-rail')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^话题：/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /（直达）$/ })).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: '话题栏' })).toHaveCount(0)

  // The message column is the screen width, not the ~240px the two rails used to leave.
  const column = (await page.locator('.chat-main-column').boundingBox())!
  expect(column.width).toBeGreaterThanOrEqual(340)
  const list = (await page.locator('.chat-panel-container').boundingBox())!
  expect(list.width).toBeGreaterThanOrEqual(340)
  expect(Math.abs(list.width - column.width)).toBeLessThanOrEqual(2)

  // No horizontal scroll anywhere in the chat column (tables scroll inside their own box).
  await noSidewaysScroll(page)

  // The composer is full width too.
  const composer = (await page.locator('.chat-panel-container .composer.chat-composer').boundingBox())!
  expect(composer.width).toBeGreaterThanOrEqual(340)
  const textarea = (await page.locator('.chat-panel-container .composer.chat-composer textarea').boundingBox())!
  expect(textarea.width).toBeGreaterThanOrEqual(300)
  await expect(page.locator('.chat-send-button')).toBeVisible()
  await shot(page, 'phone-full-width', 390)
})

// The top 会话 picker is the phone way to switch: it lists every conversation the rail showed.
test('phone (390x844): the 会话 picker switches to the Telegram conversation', async ({ page }) => {
  await openChat(page, 390, 844)

  const trigger = page.getByRole('button', { name: /^当前会话/ })
  await expect(trigger.locator('.chat-channel-tag')).toHaveText('网页')
  await expect(trigger.locator('.chat-picker-title')).toHaveText('主会话')
  await trigger.click()

  const list = page.getByRole('listbox', { name: '选择会话' })
  await expect(list).toBeVisible()
  await expect(list.getByRole('option')).toHaveCount(3)
  await expect(list.getByRole('option', { name: /网页.*主会话/ })).toHaveAttribute('aria-selected', 'true')
  const telegram = list.getByRole('option', { name: /Telegram.*主会话/ })
  await expect(telegram).toBeVisible()
  await expect(list.getByRole('option', { name: /发布计划/ })).toBeVisible()

  await telegram.click()
  await expect(list).toBeHidden()
  await expect(trigger).toHaveAttribute('data-session', telegramTopic)
  await expect(trigger.locator('.chat-channel-tag')).toHaveText('Telegram')
  // The other conversation has no history here, so its own welcome state shows.
  await expect(page.getByRole('heading', { name: '和 Conveyor 聊点什么？' })).toBeVisible()

  // Switching sessions keeps the full width (no rail sneaks back in).
  await noSidewaysScroll(page)
  const column2 = (await page.locator('.chat-panel-container').boundingBox())!
  expect(column2.width).toBeGreaterThanOrEqual(340)
  await shot(page, 'phone-session-switch', 390)

  // …and back to the main web conversation.
  await trigger.click()
  await list.getByRole('option', { name: /网页.*主会话/ }).click()
  await expect(trigger).toHaveAttribute('data-session', conveyor.session_id)
  await expect(page.getByRole('heading', { name: '本周服务器开销' })).toBeVisible()
})

// Desktop (1400x900): the rail duplicates the 当前会话 picker, so it starts collapsed (icon only),
// expands on click, and the choice is remembered per browser.
test('desktop (1400x900): the session rail starts collapsed, expands on click and is remembered', async ({ page }) => {
  await openChat(page, 1400, 900)

  const rail = page.locator('.chat-topic-rail')
  await expect(rail).toBeVisible()
  await expect(rail).toHaveClass(/is-collapsed/)
  await expect(page.getByRole('button', { name: '展开话题栏' })).toBeVisible()
  const collapsed = (await rail.boundingBox())!
  expect(collapsed.width).toBeLessThanOrEqual(44)
  // Collapsed means the icon only: no session items behind it.
  await expect(page.getByRole('button', { name: /^话题：/ })).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem('conveyor-topic-rail-collapsed'))).toBeNull()

  // Expanding shows the same conversations the picker lists, the Telegram one included.
  await page.getByRole('button', { name: '展开话题栏' }).click()
  await expect(rail).not.toHaveClass(/is-collapsed/)
  await expect(page.getByRole('button', { name: '收起话题栏' })).toBeVisible()
  const expanded = (await rail.boundingBox())!
  expect(expanded.width).toBeGreaterThan(collapsed.width + 10)
  await expect(page.getByRole('button', { name: '话题：网页 · 主会话' })).toHaveAttribute('aria-current', 'true')
  await expect(page.getByRole('button', { name: '话题：Telegram · 主会话' })).toBeVisible()
  await expect(page.getByRole('button', { name: '话题：网页 · 发布计划' })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('conveyor-topic-rail-collapsed'))).toBe('0')
  await shot(page, 'desktop-rail-expanded', 1400)

  // The choice survives a reload (per browser, not per tab).
  await page.reload()
  await expect(page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })).toBeVisible()
  await expect(page.locator('.chat-topic-rail')).not.toHaveClass(/is-collapsed/)
  await expect(page.getByRole('button', { name: '话题：Telegram · 主会话' })).toBeVisible()

  // Collapsing again is remembered too.
  await page.getByRole('button', { name: '收起话题栏' }).click()
  await expect(page.locator('.chat-topic-rail')).toHaveClass(/is-collapsed/)
  expect(await page.evaluate(() => localStorage.getItem('conveyor-topic-rail-collapsed'))).toBe('1')
  await page.reload()
  await expect(page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })).toBeVisible()
  await expect(page.locator('.chat-topic-rail')).toHaveClass(/is-collapsed/)
  await expect(page.getByRole('button', { name: '展开话题栏' })).toBeVisible()
  await shot(page, 'desktop-rail-collapsed', 1400)
})

// Widening a phone past 820px (tablet rotation) brings the collapsed rail back, not the wide one.
test('resize from phone to desktop keeps the rail collapsed', async ({ page }) => {
  await openChat(page, 390, 844)
  await expect(page.locator('.chat-topic-rail')).toHaveCount(0)
  await page.setViewportSize({ width: 1400, height: 900 })
  await expect(page.locator('.chat-topic-rail')).toBeVisible()
  await expect(page.locator('.chat-topic-rail')).toHaveClass(/is-collapsed/)
  await expect(page.getByRole('button', { name: '展开话题栏' })).toBeVisible()
})
