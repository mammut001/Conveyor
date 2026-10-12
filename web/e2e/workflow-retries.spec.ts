import { expect, test, type Page, type Route } from '@playwright/test'

type Step = { ordinal: number; kind: string; label: string; state: string; attempts: number }
type Workflow = { id: string; title: string; state: string; revision: number; updated_at: number; steps: Step[] }
type RequestBody = { title: string; request_id: string; steps: Array<{ kind: string; label: string }> }

async function installWorkflowApi(page: Page) {
  const workflows: Workflow[] = []
  const createdByRequest = new Map<string, Workflow>()
  const actionsByRequest = new Map<string, Workflow>()
  const seenCreateKeys: string[] = []
  const seenActionKeys: string[] = []
  let loseCreateResponse = true
  let loseActionResponse = true

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/health') return json({ ok: true })
    if (path === '/api/system/status') return json({
      uptime_seconds: 120, load_average: [0.1, 0.1, 0.1], cpu_count: 2,
      memory: { total: null, available: null },
      disk: { total: 1024, used: 0, free: 1024 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: false }, feishu: { configured: false } },
      nodes: [], features: { workflows: true, mobile_ui: true },
    })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [] })
    if (path === '/api/events/stream') return route.fulfill({
      status: 200, headers: { 'content-type': 'text/event-stream' }, body: '',
    })

    if (path === '/api/workflows' && request.method() === 'GET') return json({ items: workflows })
    if (path === '/api/workflows' && request.method() === 'POST') {
      const body = request.postDataJSON() as RequestBody
      seenCreateKeys.push(body.request_id)
      let item = createdByRequest.get(body.request_id)
      if (!item) {
        item = {
          id: 'f'.repeat(32), title: body.title, state: 'active', revision: 0, updated_at: Date.now() / 1000,
          steps: body.steps.map((step, ordinal) => ({
            ordinal, ...step, state: ordinal === 0 ? 'ready' : 'pending', attempts: 0,
          })),
        }
        createdByRequest.set(body.request_id, item)
        workflows.push(item)
      }
      if (loseCreateResponse) {
        loseCreateResponse = false
        // Server committed but connection dropped before the browser saw 201.
        return route.abort('failed')
      }
      return json(item, 201)
    }

    const match = path.match(/^\/api\/workflows\/([a-f0-9]{32})\/action$/)
    if (match && request.method() === 'POST') {
      const body = request.postDataJSON() as { action: string; expected_revision: number; request_id: string }
      seenActionKeys.push(body.request_id)
      const old = actionsByRequest.get(body.request_id)
      if (old) return json({ ...old, replayed: true })
      const item = workflows.find(value => value.id === match[1])
      if (!item) return json({ error: 'not found' }, 404)
      if (body.expected_revision !== item.revision) return json({ error: 'stale revision' }, 409)
      if (body.action === 'begin' && item.steps[0].state === 'ready') {
        item.steps[0].state = 'running'
        item.steps[0].attempts = 1
        item.revision += 1
        actionsByRequest.set(body.request_id, structuredClone(item))
        if (loseActionResponse) {
          loseActionResponse = false
          return route.abort('failed')
        }
        return json(item)
      }
      return json({ error: 'invalid mock action' }, 409)
    }

    return json({ error: 'not implemented in workflow fixture' }, 404)
  })

  return { workflows, seenCreateKeys, seenActionKeys }
}

async function openWorkflows(page: Page) {
  await page.goto('/')
  await page.getByLabel('Console token').fill('pr107-browser-test-token-not-a-secret-001')
  await page.getByRole('button', { name: 'Unlock console' }).click()
  await page.getByRole('button', { name: 'Workflows', exact: true }).click()
  await expect(page.getByText('Durable Workflows · checkpoint preview')).toBeVisible()
}

test('lost create response retries with same ID instead of creating duplicate', async ({ page }) => {
  const fixture = await installWorkflowApi(page)
  await openWorkflows(page)

  await page.getByRole('button', { name: 'Create safe visa demo' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  expect(fixture.workflows).toHaveLength(1)

  await page.getByRole('button', { name: 'Create safe visa demo' }).click()
  await expect(page.getByRole('heading', { name: 'Australia Visitor 600 — simulated workflow' })).toBeVisible()
  expect(fixture.workflows).toHaveLength(1)
  expect(fixture.seenCreateKeys).toHaveLength(2)
  expect(fixture.seenCreateKeys[0]).toBe(fixture.seenCreateKeys[1])
})

test('lost action response persists pending ID across reload and never advances twice', async ({ page }) => {
  const fixture = await installWorkflowApi(page)
  await openWorkflows(page)

  await page.getByRole('button', { name: 'Create safe visa demo' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('button', { name: 'Create safe visa demo' }).click()
  await expect(page.getByRole('button', { name: 'Mark work started' })).toBeVisible()

  await page.getByRole('button', { name: 'Mark work started' }).click()
  await expect(page.getByRole('button', { name: 'Retry exactly the same request' })).toBeVisible()
  expect(fixture.workflows[0].revision).toBe(1)

  await page.reload()
  await page.getByRole('button', { name: 'Workflows', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Retry exactly the same request' })).toBeVisible()
  await page.getByRole('button', { name: 'Retry exactly the same request' }).click()
  await expect(page.getByRole('button', { name: 'Mark step complete' })).toBeVisible()

  expect(fixture.workflows[0].revision).toBe(1)
  expect(fixture.seenActionKeys).toHaveLength(2)
  expect(fixture.seenActionKeys[0]).toBe(fixture.seenActionKeys[1])
})
