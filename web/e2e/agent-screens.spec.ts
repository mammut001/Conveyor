import { expect, test, type Page, type Route } from '@playwright/test'

const main = { id: 'default', name: 'Main', color: '#0a8578', is_default: true,
  session_id: 'web:web-console:agent-default', instructions: '', workspace_path: '',
  status: 'idle', last_message: '', message_count: 0 }
const beta = { ...main, id: 'beta', name: 'Beta', is_default: false,
  session_id: 'web:web-console:agent-beta' }
const attack = '<img src=x onerror="document.body.dataset.agentXss=1"><script>alert(1)</script>'
let cancelCount = 0

async function fixture(page: Page, enabled = true, mobileUI = false) {
  cancelCount = 0
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const json = (value: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json',
        body: JSON.stringify(value) })
    if (path === '/api/health') return json({ ok: true })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: true, agents: [main, beta] })
    if (path === '/api/agent-screens/config' && url.searchParams.get('agent_id') === beta.id) return enabled
      ? json({ enabled: true, inbox: true, tasks: true }) : json({ error: 'not found' }, 404)
    if (path === '/api/agent-screens' && url.searchParams.get('view') === 'inbox') return json({
      agent_id: beta.id, items: [{
        id: 'malicious', from_agent: 'default', to_agent: beta.id,
        operator: 'web-console', kind: 'message', body: attack,
        status: 'queued', created_at: 0, untrusted: true,
      }],
    })
    if (path === '/api/agent-screens' && url.searchParams.get('view') === 'tasks') return json({
      agent_id: beta.id, items: [{
        id: 'a'.repeat(32), from_agent: 'default', to_agent: beta.id,
        status: cancelCount ? 'dead' : 'leased', attempts: 1,
        created_at: 0, deadline: 999999,
      }],
    })
    if (path === '/api/agent-screens/cancel' && route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { agent_id: string; task_id: string }
      expect(body.agent_id).toBe(beta.id)
      expect(body.task_id).toBe('a'.repeat(32))
      expect(route.request().headers()['x-requested-with']).toBe('XMLHttpRequest')
      cancelCount++
      return json({ cancelled: true })
    }
    if (path === '/api/chat/history') return json({
      session_id: url.searchParams.get('session_id'), messages: [],
    })
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0, 0, 0], cpu_count: 1,
      memory: { total: null, available: null },
      disk: { total: 1000, used: 0, free: 1000 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: false }, feishu: { configured: false } },
      nodes: [], features: { mobile_ui: mobileUI, agents: true },
    })
    if (path === '/api/computer/status') return json({
      armed: false, arm_remaining_seconds: 0, screenshots: [],
      screen_preview_enabled: false,
    })
    if (path === '/api/events/stream') return route.fulfill({
      status: 200, headers: { 'content-type': 'text/event-stream' }, body: '',
    })
    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })
  await page.goto('/')
  await page.getByLabel('Console token').fill('agent-screen-e2e-token')
  await page.getByRole('button', { name: 'Unlock console' }).click()
  await page.locator('.agent-list button.agent-item').filter({ hasText: 'Beta' }).click()
}

test('agent Inbox is literal text; task cancel affects only selected task', async ({ page }) => {
  await fixture(page)
  const tabs = page.locator('.agent-screen-tabs')
  await expect(tabs).toBeVisible()
  await tabs.getByRole('button', { name: 'Inbox' }).click()
  const content = page.locator('.agent-screen-message')
  await expect(content).toHaveText(attack)
  await expect(content.locator('img, script')).toHaveCount(0)
  expect(await page.locator('body').getAttribute('data-agent-xss')).toBeNull()
  await tabs.getByRole('button', { name: 'Tasks' }).click()
  await expect(page.getByText('leased', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Cancel task' }).click()
  await expect(page.getByText('dead', { exact: true })).toBeVisible()
  expect(cancelCount).toBe(1)
})

test('feature off leaves the old agent chat without extra screen tabs', async ({ page }) => {
  await fixture(page, false)
  await expect(page.locator('.agent-screen-tabs')).toHaveCount(0)
  await expect(page.getByText('Beta', { exact: true }).first()).toBeVisible()
})

test('agent chat retains a pending reply and next draft while viewing its Inbox', async ({ page }) => {
  await fixture(page, true, true)
  let reply: Route | undefined
  await page.route('**/api/chat', async route => { reply = route })
  const input = page.getByRole('textbox', { name: 'Message Beta', exact: true })
  await input.fill('Please keep working while I inspect Inbox')
  await input.press('Enter')
  await expect.poll(() => Boolean(reply)).toBe(true)
  await input.fill('My next draft')
  const tabs = page.locator('.agent-screen-tabs')
  await tabs.getByRole('button', { name: 'Inbox', exact: true }).click()
  await expect(page.locator('.agent-screen-message')).toBeVisible()
  await reply!.fulfill({ status: 200, contentType: 'text/event-stream', body: [
    `event: session\ndata: ${JSON.stringify({ session_id: beta.session_id })}\n\n`,
    `event: message\ndata: ${JSON.stringify({ text: 'Reply completed while Inbox was open\n\n```text\n' + 'x'.repeat(200) + '\n```' })}\n\n`,
    'event: done\ndata: {"outcome":"answered"}\n\n',
  ].join('') })
  await tabs.getByRole('button', { name: 'Chat', exact: true }).click()
  await expect(page.getByText('Reply completed while Inbox was open', { exact: true })).toBeVisible()
  await expect(input).toHaveValue('My next draft')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(input).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('voice submission preserves a typed draft and displays the transcription', async ({ page }) => {
  await fixture(page)
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }),
    } })
    Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: class {
      static isTypeSupported() { return true }
      state = 'inactive'
      ondataavailable?: (event: { data: Blob }) => void
      onstop?: () => void
      start() { this.state = 'recording' }
      stop() {
        this.state = 'inactive'
        this.ondataavailable?.({ data: new Blob(['fake test audio']) })
        this.onstop?.()
      }
    } })
  })
  await page.route('**/api/chat/voice**', async route => {
    expect(route.request().headers()['content-type']).toContain('audio/webm')
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: [
      `event: session\ndata: ${JSON.stringify({ session_id: beta.session_id })}\n\n`,
      'event: heard\ndata: {"text":"Voice request transcribed"}\n\n',
      'event: message\ndata: {"text":"Voice request answered"}\n\n',
      'event: done\ndata: {"outcome":"answered"}\n\n',
    ].join('') })
  })
  const input = page.getByRole('textbox', { name: 'Message Beta', exact: true })
  await input.fill('Keep this typed draft')
  await page.getByRole('button', { name: 'Record voice message', exact: true }).click()
  await page.getByRole('button', { name: 'Stop recording', exact: true }).click()
  await expect(page.getByText('Voice request transcribed', { exact: true })).toBeVisible()
  await expect(page.getByText('Voice request answered', { exact: true })).toBeVisible()
  await expect(input).toHaveValue('Keep this typed draft')
})


test('agent conversation identifies message origins without guessing legacy history', async ({ page }) => {
  await fixture(page)
  await page.route('**/api/chat/history?**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({
      session_id: beta.session_id,
      messages: [
        { role: 'user', text: 'Telegram question', origin_channel: 'telegram' },
        { role: 'assistant', text: 'Feishu answer', origin_channel: 'feishu' },
        { role: 'assistant', text: 'Legacy answer' },
      ],
    }),
  }))
  await page.reload()
  const context = page.getByLabel('当前会话', { exact: true })
  await expect(context).toContainText('Beta')
  await expect(page.locator('.transcript-message.role-user .transcript-source')).toHaveText('Telegram')
  await expect(page.locator('.transcript-message.role-assistant .transcript-source')).toHaveText(['飞书', '来源未记录'])
  await page.screenshot({ path: test.info().outputPath('agent-history-origins.png') })
})

test('explicit continuation separates drafts, restores each Agent selection, and locks picker during a reply', async ({ page }) => {
  await fixture(page)
  const phone = { id: 'a'.repeat(32), title: 'Telegram 独立会话', origin_channel: 'telegram',
    session_id: `web:web-console:topic-${'a'.repeat(32)}` }
  const fresh = { id: 'b'.repeat(32), title: '发布计划', origin_channel: 'web',
    session_id: `web:web-console:topic-${'b'.repeat(32)}` }
  let created = false
  let releaseReply!: () => void
  const waiting = new Promise<void>(resolve => { releaseReply = resolve })
  await page.route('**/api/agent-conversations?**', route => {
    const isBeta = new URL(route.request().url()).searchParams.get('agent_id') === beta.id
    const agent = isBeta ? beta : main
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      conversations: [{ id: agent.id, title: 'Web 原有会话', origin_channel: 'web', session_id: agent.session_id },
        ...(isBeta ? [phone, ...(created ? [fresh] : [])] : [])],
    }) })
  })
  await page.route('**/api/agent-conversations/new', route => {
    expect(route.request().postDataJSON()).toEqual({ agent_id: beta.id, title: '发布计划' })
    created = true
    return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ conversation: fresh }) })
  })
  await page.route('**/api/chat', async route => {
    expect(route.request().postDataJSON().session_id).toBe(fresh.session_id)
    await waiting
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body:
      `event: session\ndata: ${JSON.stringify({ session_id: fresh.session_id })}\n\n` +
      'event: delta\ndata: {"text":"New topic answer"}\n\nevent: message\ndata: {"text":"New topic answer"}\n\nevent: done\ndata: {"outcome":"answered"}\n\n' })
  })
  await page.reload()
  const picker = page.getByLabel('选择要接续的会话')
  await expect(picker).toHaveValue(beta.session_id)
  await picker.selectOption(phone.session_id)
  const input = page.getByRole('textbox', { name: 'Message Beta', exact: true })
  await input.fill('Telegram topic draft')
  await picker.selectOption(beta.session_id)
  await expect(input).toHaveValue('')
  await picker.selectOption(phone.session_id)
  await expect(input).toHaveValue('Telegram topic draft')
  await page.locator('.agent-list button.agent-item').filter({ hasText: 'Main' }).click()
  await expect(picker).toHaveValue(main.session_id)
  await page.locator('.agent-list button.agent-item').filter({ hasText: 'Beta' }).click()
  await expect(picker).toHaveValue(phone.session_id)
  await page.getByLabel('新话题标题').fill('发布计划')
  await page.getByRole('button', { name: '另开话题', exact: true }).click()
  await expect(picker).toHaveValue(fresh.session_id)
  await expect(input).toHaveValue('')
  await input.fill('Plan this release')
  await page.getByRole('button', { name: 'Send', exact: false }).click()
  await expect(picker).toBeDisabled()
  await expect(page.getByRole('button', { name: '另开话题', exact: true })).toBeDisabled()
  releaseReply()
  await expect(page.getByText('New topic answer', { exact: true })).toBeVisible()
  await expect(page.getByText('回答中断 · 已保留收到的内容', { exact: true })).toHaveCount(0)
  await expect(picker).toBeEnabled()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('conversation lookup failure offers retry without silently opening a cached history', async ({ page }) => {
  await fixture(page)
  let fail = true
  await page.route('**/api/agent-conversations?**', route => route.fulfill({
    status: fail ? 503 : 200, contentType: 'application/json', body: JSON.stringify(fail ? { error: 'offline' } : {
      conversations: [{ id: beta.id, title: 'Web 原有会话', origin_channel: 'web', session_id: beta.session_id }],
    }),
  }))
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('暂时无法读取会话')
  await expect(page.getByRole('textbox', { name: 'Message Beta', exact: true })).toHaveCount(0)
  fail = false
  await page.getByRole('button', { name: '重试读取会话', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'Message Beta', exact: true })).toBeVisible()
  await expect(page.getByLabel('选择要接续的会话')).toHaveValue(beta.session_id)
})
