import { expect, test, type Page, type Route } from '@playwright/test'

async function openConsole(page: Page, pwa: boolean) {
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/system/status') data = {
      uptime_seconds: 10, load_average: [0, 0, 0], cpu_count: 1,
      memory: { total: null, available: null },
      disk: { total: 1, used: 0, free: 1 },
      queue: { depth: 0, paused: false, states: {} },
      channels: {}, nodes: [], features: { pwa },
    }
    else if (path === '/api/sessions') data = { sessions: [] }
    else if (path === '/api/jobs') data = { jobs: [] }
    else if (path === '/api/approvals') data = { approvals: [] }
    else if (path === '/api/nodes') data = { nodes: [] }
    else if (path === '/api/agents') data = { enabled: false, agents: [] }
    else if (path === '/api/computer/status') data = { armed: false, arm_remaining_seconds: 0, screenshots: [] }
    else if (path === '/api/approval-inbox') data = { items: [] }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  await page.goto('/')
  const unlock = page.getByLabel('Console token')
  if (await unlock.isVisible()) {
    await unlock.fill('pwa-test-token-0123456789abcdef0123456789abcdef')
    await page.getByRole('button', { name: 'Unlock console' }).click()
  }
  await expect(page.locator('main.app-shell')).toBeVisible()
}

test('PWA manifest link and notification opt-in appear only with PWA flag', async ({ page }) => {
  await openConsole(page, false)
  await expect(page.locator('link[rel="manifest"]')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Enable alerts/i })).toHaveCount(0)
  // Re-load with server feature bit flipped, not a frontend env variable.
  await page.unroute('**/api/**')
  await openConsole(page, true)
  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute('href', '/manifest.webmanifest')
  await expect(page.getByRole('button', { name: /Enable alerts/i })).toBeVisible()
  const manifest = await page.request.get('/manifest.webmanifest')
  expect(manifest.ok()).toBeTruthy()
  expect((await manifest.json()).name).toBe('Conveyor')
})

test('notification deep link lands on authenticated approval inbox', async ({ page }) => {
  await openConsole(page, true)
  await page.goto('/?approval=approval123#approvals')
  await expect(page.locator('main.app-shell')).toBeVisible()
  await expect(page.getByRole('button', { name: /Approvals/i }).first()).toBeVisible()
})
