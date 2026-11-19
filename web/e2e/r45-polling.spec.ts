import { expect, test, type Page, type Route } from '@playwright/test'

const TOKEN = 'r45-polling-e2e-token'
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

type Trackers = {
  jobsRequests: number
}

async function install(page: Page): Promise<Trackers> {
  const trackers: Trackers = { jobsRequests: 0 }

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
      return json({ session_id: SESSION_ID, messages: [] })
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
    if (path === '/api/workers' && method === 'GET') {
      return json({ enabled: true, workers: [], active: null })
    }
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path.startsWith('/api/sessions/')) {
      return json({
        id: SESSION_ID,
        channel: 'web',
        created_at: '',
        last_activity: '',
        job_count: 0,
        messages: [],
        runs: [],
        jobs: [],
      })
    }
    if (path === '/api/jobs') {
      trackers.jobsRequests += 1
      return json({ jobs: [] })
    }
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/computer/status') {
      return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    }
    if (path === '/api/inbox') return json({ unread: 0, items: [] })
    if (path === '/api/approval-inbox') return json({ counts: { total: 0 }, items: [] })
    if (path === '/api/supervisor/goals') return json({ goals: [], count: 0 })
    if (path === '/api/supervisor/repositories') return json({ repositories: [] })
    if (path === '/api/takeover/status') return json({ available: false })
    if (path === '/api/screen/status') return json({ enabled: false, available: false })
    return json({})
  })

  return trackers
}

test('startup does not burst /api/jobs, pauses while hidden, and refreshes on visible', async ({ page }) => {
  test.setTimeout(30_000)
  const trackers = await install(page)

  await page.goto('/')
  await expect(page.locator('main.app-shell')).toBeVisible()

  await page.waitForTimeout(2000)
  expect(trackers.jobsRequests).toBeGreaterThanOrEqual(1)
  expect(trackers.jobsRequests).toBeLessThanOrEqual(2)

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  trackers.jobsRequests = 0
  await page.waitForTimeout(7000)
  expect(trackers.jobsRequests).toBe(0)

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect.poll(() => trackers.jobsRequests, { timeout: 1500 }).toBeGreaterThan(0)
})
