import { expect, test, type Page, type Route } from '@playwright/test'

const main = { id: 'default', name: 'Main', color: '#0a8578', is_default: true,
  session_id: 'web:web-console:agent-default', instructions: '', workspace_path: '',
  status: 'idle', last_message: '', message_count: 0 }
const beta = { ...main, id: 'beta', name: 'Beta', is_default: false,
  session_id: 'web:web-console:agent-beta' }
const attack = '<img src=x onerror="document.body.dataset.agentXss=1"><script>alert(1)</script>'
let cancelCount = 0

async function fixture(page: Page, enabled = true) {
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
      nodes: [], features: { mobile_ui: false, agents: true },
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
  await page.locator('.agent-list').getByRole('button', { name: /Beta/ }).click()
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
