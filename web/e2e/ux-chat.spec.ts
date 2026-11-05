import { expect, test, type Page, type Route } from '@playwright/test'

// Visual + behaviour check for the conversation screen (对话) with realistic Chinese data.
// Screenshots: web/test-results/ux-chat/<view>-<width>.png

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

const markdownReply = [
  '## 本周服务器开销', '',
  '整体比上周下降 **12%**，主要来自对象存储的清理。', '',
  '| 项目 | 本周 | 上周 | 变化 | 说明 |', '| :--- | ---: | ---: | ---: | --- |',
  '| 计算实例 | ¥1,284.00 | ¥1,350.20 | -4.9% | 夜间自动缩容生效 |',
  '| 对象存储 | ¥312.40 | ¥498.75 | -37.4% | 清理了 2023 年的旧备份 |',
  '| 带宽 | ¥205.10 | ¥188.30 | +8.9% | 周三有一次大文件分发 |',
  '| 数据库 | ¥640.00 | ¥640.00 | 0% | 包年包月 |', '',
  '查询脚本（按天汇总）：', '',
  '```python',
  'import sqlite3',
  '',
  'def daily_cost(db_path: str, days: int = 7) -> list[tuple[str, float]]:',
  '    with sqlite3.connect(db_path) as db:',
  '        rows = db.execute(',
  '            "SELECT date(created_at), SUM(amount) FROM billing WHERE created_at >= date(\'now\', ?) GROUP BY 1 ORDER BY 1",',
  '            (f"-{days} days",),',
  '        ).fetchall()',
  '    return [(day, round(total, 2)) for day, total in rows]',
  '```', '',
  '> 提示：带宽按 95 计费，突发流量会被平滑。', '',
  '- 下周建议把冷数据迁到归档存储',
  '- 继续观察周三的分发任务',
].join('\n')

const history = [
  { role: 'user', text: '帮我看看昨晚的部署有没有问题', origin_channel: 'telegram', created_at: iso(60 * 26) },
  { role: 'assistant', text: '昨晚 23:40 的部署已经完成，所有健康检查都通过了。', origin_channel: 'telegram', created_at: iso(60 * 26 - 1) },
  { role: 'assistant', text: '（较早的记录，没有保存来自哪个入口）' },
  { role: 'user', text: '把这周的服务器开销整理成表格，再给一段查询脚本', origin_channel: 'web', created_at: iso(12) },
  { role: 'assistant', text: markdownReply, origin_channel: 'web', created_at: iso(11) },
]

const inbox = [
  { id: 'm1', from_agent: researcher.id, to_agent: conveyor.id, operator: 'web-console', kind: 'message',
    body: '我整理好了三家云厂商的价格对比，放在共享资料里了。\n需要的话我可以继续细化到每个套餐。', status: 'done', created_at: epoch(35), untrusted: true },
  { id: 'm2', from_agent: finance.id, to_agent: conveyor.id, operator: 'web-console', kind: 'task_result',
    body: '十月账单核对完成：共 3 笔异常扣费，已在表格里标红。', status: 'queued', created_at: epoch(60 * 5), untrusted: true },
]
const tasks = [
  { id: 'c'.repeat(32), from_agent: conveyor.id, to_agent: coder.id, status: 'leased', attempts: 1, created_at: epoch(8), deadline: epoch(-60) },
  { id: 'd'.repeat(32), from_agent: conveyor.id, to_agent: researcher.id, status: 'done', attempts: 1, created_at: epoch(60 * 3), deadline: epoch(0) },
  { id: 'e'.repeat(32), from_agent: finance.id, to_agent: conveyor.id, status: 'dead', attempts: 3, created_at: epoch(60 * 30), deadline: epoch(60 * 20) },
]

type Options = { chat?: (route: Route) => Promise<void> | void; emptyHistory?: boolean; agentsOff?: boolean }

async function fixture(page: Page, options: Options = {}) {
  // Skip the unlock form (its labels differ between branches): a remembered token unlocks on load.
  await page.addInitScript(() => { localStorage.setItem('conveyor-token', 'ux-chat-e2e-token') })
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
    if (path === '/api/chat' && options.chat) return options.chat(route)
    if (path === '/api/agents') return json(options.agentsOff ? { enabled: false, agents: [] } : { enabled: true, agents: [conveyor, finance, researcher, coder] })
    if (path === '/api/agent-screens/config') return json({ enabled: true, inbox: true, tasks: true })
    if (path === '/api/agent-screens') return json({ agent_id: url.searchParams.get('agent_id'), items: url.searchParams.get('view') === 'inbox' ? inbox : tasks })
    if (path === '/api/agent-conversations') {
      const agentId = url.searchParams.get('agent_id')
      const agent = [conveyor, finance, researcher, coder].find(item => item.id === agentId) || conveyor
      return json({ conversations: [
        { id: `${agent.id}-web`, title: 'Web 会话（原有记录）', origin_channel: 'web', session_id: agent.session_id },
        ...(agent === conveyor ? [
          { id: 'c-tg', title: 'Telegram 独立会话', origin_channel: 'telegram', session_id: telegramTopic },
          { id: 'c-plan', title: '发布计划', origin_channel: 'web', session_id: planTopic },
        ] : []),
      ] })
    }
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'),
      messages: !options.emptyHistory && url.searchParams.get('session_id') === conveyor.session_id ? history : [] })
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
  page.screenshot({ path: `test-results/ux-chat/${view}-${width}.png`, fullPage: true })

async function noHorizontalScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
}

async function openChat(page: Page, width: number, options: Options = {}) {
  await page.setViewportSize({ width, height: width > 600 ? 900 : 844 })
  await fixture(page, options)
  await page.goto('/')
  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await expect(composer).toBeVisible()
  // Markdown renders from a lazily loaded chunk; wait so screenshots show the final look.
  if (!options.emptyHistory) await expect(page.getByRole('heading', { name: '本周服务器开销' })).toBeVisible()
  return composer
}

// The agent list is a column on desktop but may live in a drawer on phones:
// click it when it is on screen, otherwise select the agent the way a reload would.
async function switchAgent(page: Page, agent: { id: string; name: string }) {
  const item = page.getByRole('button', { name: new RegExp(`^${agent.name}`) }).first()
  if (await item.isVisible()) return item.click()
  await page.evaluate(id => localStorage.setItem('conveyor-agent', id), agent.id)
  await page.reload()
}

// Primary navigation label differs between branches (Chat / 对话) and element types.
async function openChatView(page: Page) {
  const name = /^(💬\s*)?(Chat|对话)$/
  const nav = page.getByRole('button', { name }).or(page.getByRole('tab', { name })).or(page.getByRole('link', { name }))
  if (!(await page.locator('.chat-panel-container textarea').first().isVisible())) await nav.first().click()
}

// 助手消息 / 委派记录: two text toggles in the conversation bar; on phones (≤480px) behind its "⋯" button.
async function openRecords(page: Page, name: '助手消息' | '委派记录') {
  await expect(page.locator('.chat-secondary-views, .chat-secondary-menu')).toBeVisible()
  const more = page.getByRole('button', { name: /^其他记录/ })
  if (!(await more.isVisible())) return page.getByRole('button', { name, exact: true }).click()
  await more.click()
  await page.getByRole('menu', { name: '其他记录' }).getByRole('menuitemcheckbox', { name }).click()
}

// Leftover English that used to be in this screen.
const ENGLISH = /\b(Send|Message Conveyor|Inbox|Tasks|Memory|Copy|Streaming|Untrusted|Delegated|Refresh|Approve|Deny|Enter to send|Token rejected|CONVEYOR_)\b/

for (const width of [1440, 390]) {
  test.describe(`chat at ${width}px`, () => {
    test('long markdown reply with table and code block', async ({ page }) => {
      const composer = await openChat(page, width)
      await expect(composer).toHaveAttribute('placeholder', '给 Conveyor 发消息…')
      await expect(page.locator('.stream-panel .composer textarea').first()).toHaveAttribute('aria-label', '给 Conveyor 发消息')
      await expect(page.getByRole('heading', { name: '本周服务器开销' })).toBeVisible()
      await expect(page.getByRole('table')).toBeVisible()
      await expect(page.locator('.code-block-pre')).toBeVisible()
      await expect(page.locator('.code-lang-tag')).toHaveText('python')
      // Origins: Telegram/网页 labelled, unknown legacy origin not guessed.
      await expect(page.locator('.transcript-message.role-user .transcript-source')).toHaveText(['Telegram', '网页'])
      await expect(page.locator('.chat-panel-container')).not.toContainText('来源未记录')
      await expect(page.locator('.chat-panel-container time').first()).toHaveText(/^(昨天 )?\d{2}:\d{2}$|月/)
      // One compact bar: no agent-level tab row, no explanation rows.
      await expect(page.locator('.agent-screen-tabs')).toHaveCount(0)
      await expect(page.getByText('各入口的历史独立')).toHaveCount(0)
      await expect(page.locator('.chat-context-strip')).toHaveCount(0)
      await expect(page.getByRole('button', { name: '当前会话：网页 · 主会话' })).toBeVisible()
      await expect(page.getByRole('button', { name: '新会话', exact: true })).toBeVisible()
      await expect(page.getByText('Enter 发送 · Shift + Enter 换行')).toBeVisible()
      // The hint is never clipped by the chat column (the column's own height comes from the shell layout).
      const hint = await page.getByText('Enter 发送 · Shift + Enter 换行').boundingBox()
      const column = await page.locator('.chat-panel-container').boundingBox()
      expect(hint!.y + hint!.height).toBeLessThanOrEqual(column!.y + column!.height)
      expect(await page.locator('.agent-screen').innerText()).not.toMatch(ENGLISH)
      // Copy on replies and on code.
      await expect(page.locator('.chat-copy-button').last()).toHaveText('复制')
      await expect(page.locator('.code-copy-btn')).toHaveText('复制')
      await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
      await page.locator('.code-copy-btn').click()
      await expect(page.locator('.code-copy-btn')).toHaveText('已复制')
      // Wide table and code scroll inside the bubble; the page itself never scrolls sideways.
      if (width < 600) {
        expect(await page.locator('.formatted-table-scroll').evaluate(node => node.scrollWidth > node.clientWidth)).toBe(true)
        expect(await page.locator('.code-block-pre').evaluate(node => node.scrollWidth > node.clientWidth)).toBe(true)
      }
      await noHorizontalScroll(page)
      await shot(page, 'chat-markdown', width)
    })

    test('conversation dropdown open', async ({ page }) => {
      await openChat(page, width)
      await page.getByRole('button', { name: /^当前会话/ }).click()
      const list = page.getByRole('listbox', { name: '选择会话' })
      await expect(list).toBeVisible()
      await expect(list.getByRole('option')).toHaveCount(3)
      await expect(list.getByRole('option', { name: /网页.*主会话/ })).toHaveAttribute('aria-selected', 'true')
      await expect(list.getByRole('option', { name: /Telegram.*主会话/ })).toBeVisible()
      await expect(list.getByRole('option', { name: /发布计划/ })).toBeVisible()
      await noHorizontalScroll(page)
      await shot(page, 'dropdown-open', width)
      await page.keyboard.press('Escape')
      await expect(list).toBeHidden()
      await expect(page.getByRole('button', { name: /^当前会话/ })).toBeFocused()
    })

    test('draft in the composer survives switching agent and conversation', async ({ page }) => {
      const composer = await openChat(page, width)
      await composer.click()
      await composer.pressSequentially('明天上午十点发布 v2.3：')
      await composer.press('Shift+Enter')
      await composer.pressSequentially('1. 先在预览环境跑一遍冒烟测试')
      await composer.press('Shift+Enter')
      await composer.pressSequentially('2. 通过后再切流量，失败就回滚')
      const height = await composer.evaluate(node => node.getBoundingClientRect().height)
      expect(height).toBeGreaterThan(60)
      // Growing the box keeps the latest message in view (no stray "回到最新").
      await expect(page.locator('.chat-jump-latest')).toHaveCount(0)
      const stream = page.locator('.chat-panel-container .event-stream')
      expect(await stream.evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThan(4)
      await noHorizontalScroll(page)
      await shot(page, 'composer-draft', width)
      // Switch to another agent and back: the draft is still there.
      await switchAgent(page, finance)
      const financeBox = page.getByRole('textbox', { name: '给 Finance 发消息', exact: true })
      await expect(financeBox).toHaveValue('')
      await switchAgent(page, conveyor)
      await expect(composer).toHaveValue(/先在预览环境跑一遍冒烟测试/)
      // Another conversation has its own draft.
      await page.getByRole('button', { name: /^当前会话/ }).click()
      await page.getByRole('option', { name: /发布计划/ }).click()
      await expect(composer).toHaveValue('')
      await page.getByRole('button', { name: /^当前会话/ }).click()
      await page.getByRole('option', { name: /网页.*主会话/ }).click()
      await expect(composer).toHaveValue(/失败就回滚/)
    })

    test('new-message pill while reading history', async ({ page }) => {
      let reply: Route | undefined
      const composer = await openChat(page, width, { chat: route => { reply = route } })
      await composer.fill('顺便查一下今天的告警')
      await composer.press('Enter')
      await expect(page.getByRole('status').filter({ hasText: '正在思考…' })).toBeVisible()
      await expect.poll(() => Boolean(reply)).toBe(true)
      const stream = page.locator('.chat-panel-container .event-stream')
      await stream.evaluate(node => { node.scrollTo({ top: 0, behavior: 'instant' }); node.dispatchEvent(new Event('scroll')) })
      await expect(page.getByRole('button', { name: '回到最新' })).toBeVisible()
      await reply!.fulfill({ status: 200, contentType: 'text/event-stream', body: [
        `event: session\ndata: ${JSON.stringify({ session_id: conveyor.session_id })}\n\n`,
        `event: message\ndata: ${JSON.stringify({ text: '今天有 2 条告警，都已自动恢复：\n\n- 09:12 磁盘使用率 91%\n- 13:40 接口延迟升高' })}\n\n`,
        'event: done\ndata: {"outcome":"answered"}\n\n',
      ].join('') })
      const pill = page.getByRole('button', { name: '有新消息' })
      await expect(pill).toBeVisible()
      expect(await stream.evaluate(node => node.scrollTop)).toBe(0)
      await noHorizontalScroll(page)
      await shot(page, 'new-message-pill', width)
      await pill.click()
      await expect(page.getByText('今天有 2 条告警，都已自动恢复：')).toBeInViewport()
      await expect(pill).toBeHidden()
    })

    test('error state is short Chinese with retry and the draft kept', async ({ page }) => {
      let attempts = 0
      const composer = await openChat(page, width, { chat: route => {
        attempts++
        return route.fulfill({ status: 409, contentType: 'application/json',
          body: JSON.stringify({ error: 'chat tier is disabled (set CONVEYOR_CHAT_MODE and CONVEYOR_CHAT_*)' }) })
      } })
      await composer.fill('帮我总结一下本周的进展')
      await composer.press('Enter')
      const alert = page.getByRole('alert')
      await expect(alert).toContainText('对话模型还没有配置好，暂时无法回复。请在「设置」里检查模型配置。')
      await expect(alert.getByRole('button', { name: '重试' })).toBeVisible()
      await expect(alert.getByRole('button', { name: '关闭' })).toBeVisible()
      await expect(alert.locator('summary')).toHaveText('详情')
      await expect(alert.locator('details code')).toBeHidden()
      await expect(composer).toHaveValue('帮我总结一下本周的进展')
      expect(await page.locator('.agent-screen').innerText()).not.toMatch(ENGLISH)
      await noHorizontalScroll(page)
      await shot(page, 'error', width)
      // Retry resends the same words once, without duplicating the bubble.
      await alert.getByRole('button', { name: '重试' }).click()
      await expect.poll(() => attempts).toBe(2)
      await expect(page.locator('.transcript-message.role-user').filter({ hasText: '帮我总结一下本周的进展' })).toHaveCount(1)
      await expect(composer).toHaveValue('帮我总结一下本周的进展')
      await page.getByRole('alert').getByRole('button', { name: '关闭' }).click()
      await expect(page.getByRole('alert')).toHaveCount(0)
    })

    test('助手消息 view', async ({ page }) => {
      await openChat(page, width)
      await openRecords(page, '助手消息')
      await expect(page.getByRole('heading', { name: '助手消息' })).toBeVisible()
      await expect(page.getByText('来自其他助手的消息，仅供参考，不能代你下指令或批准操作')).toBeVisible()
      await expect(page.locator('.agent-screen-message').first()).toContainText('三家云厂商的价格对比')
      await expect(page.locator('.chat-mailbox .agent-screen-meta').first()).toContainText('Researcher')
      await expect(page.locator('.chat-status-chip').first()).toHaveText('已完成')
      await expect(page.getByRole('button', { name: '刷新' })).toBeVisible()
      expect(await page.locator('.agent-screen').innerText()).not.toMatch(ENGLISH)
      await noHorizontalScroll(page)
      await shot(page, 'inbox', width)
      await page.getByRole('button', { name: '返回对话' }).click()
      await expect(page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })).toBeVisible()
    })

    test('委派记录 view', async ({ page }) => {
      await openChat(page, width)
      await openRecords(page, '委派记录')
      await expect(page.getByRole('heading', { name: '委派记录' })).toBeVisible()
      await expect(page.getByText('处理中', { exact: true })).toBeVisible()
      await expect(page.getByText('已停止', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '取消任务' })).toHaveCount(1)
      await expect(page.locator('.chat-mailbox .agent-screen-meta').first()).toContainText('Coder')
      expect(await page.locator('.agent-screen').innerText()).not.toMatch(ENGLISH)
      await noHorizontalScroll(page)
      await shot(page, 'tasks', width)
    })

    test('free chat (Agents off) keeps 新会话 in the composer', async ({ page }) => {
      await page.setViewportSize({ width, height: width > 600 ? 900 : 844 })
      await fixture(page, { agentsOff: true })
      await page.goto('/')
      await openChatView(page)
      const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
      await expect(composer).toBeVisible()
      await expect(page.getByRole('heading', { name: '想聊点什么？' })).toBeVisible()
      const newChat = page.locator('.chat-composer').getByRole('button', { name: '新会话', exact: true })
      await expect(newChat).toBeVisible()
      await composer.fill('先记下来，稍后再发')
      await noHorizontalScroll(page)
      await shot(page, 'free-chat', width)
      await newChat.click()
      await expect(composer).toHaveValue('')
      await expect(composer).toBeFocused()
    })

    test('welcome state of a fresh conversation', async ({ page }) => {
      await openChat(page, width, { emptyHistory: true })
      await expect(page.getByRole('heading', { name: '和 Conveyor 聊点什么？' })).toBeVisible()
      await expect(page.getByText('夜间任务怎么用？')).toBeVisible()
      await noHorizontalScroll(page)
      await shot(page, 'welcome', width)
    })
  })
}

// The owner's phone (375px) with the agent mailbox and delegation records on: the conversation
// title gets the bar, 新会话 shrinks to its "＋", and the two record views fold into "⋯".
test('phone (375px): the conversation bar keeps the title whole', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await fixture(page)
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '本周服务器开销' })).toBeVisible()
  const bar = page.locator('.chat-conversation-bar')
  const more = bar.getByRole('button', { name: '其他记录' })
  await expect(more).toBeVisible()
  await expect(bar.getByRole('button', { name: '助手消息' })).toHaveCount(0)
  const title = bar.locator('.chat-picker-title')
  await expect(title).toHaveText('主会话')
  expect(await title.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  const newChat = bar.getByRole('button', { name: '新会话', exact: true })
  await expect(newChat).toBeVisible()
  expect((await newChat.boundingBox())!.width).toBeLessThanOrEqual(44)
  // One row: picker, ＋ and ⋯ side by side.
  const [picker, plus, menuButton] = await Promise.all([bar.locator('.chat-picker-trigger'), newChat, more].map(item => item.boundingBox()))
  expect(Math.abs(picker!.y - plus!.y)).toBeLessThanOrEqual(4)
  expect(Math.abs(plus!.y - menuButton!.y)).toBeLessThanOrEqual(4)
  expect(picker!.width).toBeGreaterThan(200)
  await noHorizontalScroll(page)
  await shot(page, 'conversation-bar', 375)

  await more.click()
  const menu = page.getByRole('menu', { name: '其他记录' })
  await expect(menu.getByRole('menuitemcheckbox')).toHaveText(['助手消息', '委派记录'])
  await expect(menu.getByRole('menuitemcheckbox', { name: '助手消息' })).toBeFocused()
  await noHorizontalScroll(page)
  await shot(page, 'conversation-bar-menu', 375)
  await page.keyboard.press('Escape')
  await expect(menu).toBeHidden()
  await expect(more).toBeFocused()
  await more.click()
  await menu.getByRole('menuitemcheckbox', { name: '委派记录' }).click()
  await expect(page.getByRole('heading', { name: '委派记录' })).toBeVisible()
  await expect(more).toHaveAccessibleName('其他记录（正在看委派记录）')
  await page.getByRole('button', { name: '返回对话' }).click()
  await expect(page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })).toBeVisible()

  // Desktop keeps the two text toggles and the labelled 新会话 button.
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(bar.getByRole('button', { name: '助手消息', exact: true })).toBeVisible()
  await expect(bar.getByRole('button', { name: '其他记录' })).toHaveCount(0)
  await expect(bar.locator('.chat-new-label')).toBeVisible()
})
