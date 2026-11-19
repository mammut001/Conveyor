import { expect, test, type Page, type Route } from '@playwright/test'

const TOKEN = 'r43-intent-notice-e2e-token'
const SESSION_ID = 'web:web-console:agent-default'
const CONVEYOR_AGENT = {
  id: 'default',
  name: 'Conveyor',
  color: '#bd5c40',
  is_default: true,
  session_id: SESSION_ID,
  instructions: '负责日常运维和发布。',
  workspace_path: '',
  status: 'idle',
  last_message: '',
  message_count: 2,
}

type HistoryItem = {
  role: 'user' | 'assistant'
  text: string
  kind?: string
  origin_channel?: string
  created_at?: string
}

async function setupFixture(page: Page, history: HistoryItem[]) {
  await page.addInitScript((tokenValue: string) => {
    localStorage.setItem('conveyor-token', tokenValue)
  }, TOKEN)

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (value: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })

    if (path === '/api/events/stream') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    }
    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 60,
        load_average: [0.1, 0.1, 0.1],
        cpu_count: 4,
        memory: { total: 8_000_000_000, available: 4_000_000_000 },
        disk: { total: 100_000_000_000, used: 20_000_000_000, free: 80_000_000_000 },
        queue: { depth: 0, paused: false, states: {} },
        channels: { telegram: { configured: false }, feishu: { configured: false } },
        nodes: [],
        features: {
          agents: true,
          long_term_memory: true,
          routines: true,
          teammate: true,
          agent_supervisor: true,
          chat_model_configured: true,
          mobile_ui: false,
        },
      })
    }
    if (path === '/api/agents') {
      return json({ enabled: true, agents: [CONVEYOR_AGENT] })
    }
    if (path === '/api/agent-screens/config') {
      return json({ enabled: false, inbox: false, tasks: false })
    }
    if (path === '/api/agent-conversations') {
      return json({
        conversations: [
          { id: 'default-web', title: '主会话', origin_channel: 'web', session_id: SESSION_ID },
        ],
      })
    }
    if (path === '/api/chat/history' && method === 'GET') {
      return json({ session_id: SESSION_ID, messages: history })
    }
    if (path === '/api/config/provider' && method === 'GET') {
      return json({
        provider_id: 'deepseek',
        provider_name: 'DeepSeek',
        model: 'deepseek-chat',
        reasoning_effort: 'minimal',
        base_url: 'https://api.deepseek.com/v1',
        wire_api: 'chat',
        env_key: 'DEEPSEEK_API_KEY',
        api_key_configured: true,
        api_key_hint: '••••1234',
        config_path: '/tmp/config.toml',
        chat_model_configured: true,
      })
    }
    if (path === '/api/config/intent-decider' && method === 'GET') {
      return json({
        provider: 'gemini',
        configured_provider: 'gemini',
        auto_selected: false,
        endpoint: '',
        model: '',
        secret_name: 'GEMINI_API_KEY',
        secret_configured: true,
        available_secrets: ['GEMINI_API_KEY'],
        ready: true,
        error: '',
        health: {
          provider: 'gemini',
          consecutive_failures: 3,
          last_error_kind: 'auth',
          last_error_at: '2026-11-19T08:00:00Z',
          last_ok_at: null,
          last_error_message: '密钥被拒绝',
          unhealthy: true,
        },
      })
    }
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [] })
    if (path === '/api/inbox') return json({ unread: 0, items: [] })
    if (path === '/api/takeover/status') return json({ available: false })
    if (path === '/api/screen/status') return json({ enabled: false, available: false })
    return json({})
  })
}

test('phone (390x844): intent-decider notice button opens settings scrolled to 理解指令', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const noticeText = '理解指令暂时用不了（密钥被拒绝），这次按普通聊天回答。可以到 设置 › 理解指令 看看。'
  await setupFixture(page, [
    { role: 'user', text: '帮我看下现在的进度', origin_channel: 'web', created_at: '2026-11-19T08:00:00Z' },
    { role: 'assistant', kind: 'notice', text: noticeText, created_at: '2026-11-19T08:00:01Z' },
  ])

  await page.goto('/')
  const notice = page.locator('.chat-system-notice', { hasText: noticeText })
  await expect(notice).toBeVisible()

  const openBtn = notice.getByRole('button', { name: '打开理解指令设置' })
  await expect(openBtn).toBeVisible()
  await openBtn.click()

  const dialog = page.getByRole('dialog', { name: '模型设置' })
  await expect(dialog).toBeVisible()

  const intentHeading = page.locator('#intent-decider-title')
  await expect(intentHeading).toBeVisible()
  await expect(intentHeading).toBeInViewport()
  await expect(intentHeading).toBeFocused()
})

test('phone (390x844): a notice without 设置 › 理解指令 renders no intent settings button', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const plainNoticeText = '理解指令已恢复，后面的对话会继续帮你识别意图。'
  await setupFixture(page, [
    { role: 'user', text: '继续', origin_channel: 'web', created_at: '2026-11-19T08:00:00Z' },
    { role: 'assistant', kind: 'notice', text: plainNoticeText, created_at: '2026-11-19T08:00:01Z' },
  ])

  await page.goto('/')
  const notice = page.locator('.chat-system-notice', { hasText: plainNoticeText })
  await expect(notice).toBeVisible()
  await expect(notice.getByRole('button', { name: '打开理解指令设置' })).toHaveCount(0)
})
