import { expect, test, type Page, type Route } from '@playwright/test'

const webSessionId = 'web:web-console:collision-123'
const telegramSessionId = 'telegram:alice:collision-123'
const createdAt = '2026-10-09T18:00:00.000Z'

type Run = {
  id: string
  state: string
  mode: string
  created_at: string
  updated_at: string
  prompt_preview: string
  refinement_intent: boolean
  refinement_turn: number
}

function fixtureJob(id: string, channel: string, chatId: string, prompt: string, turn: number) {
  return {
    id,
    state: 'completed',
    mode: 'fix',
    channel,
    chat_id: chatId,
    operator_id: 'alice',
    created_at: createdAt,
    updated_at: createdAt,
    prompt_preview: prompt,
    metadata: { refinement_turn: turn, runtime_job_id: `runtime-${id}` },
    changed_files: [{ status: 'M', path: 'index.html' }, { status: 'M', path: 'style.css' }],
  }
}

function fixtureRun(id: string, prompt: string, turn: number): Run {
  return {
    id,
    state: 'completed',
    mode: 'fix',
    created_at: createdAt,
    updated_at: createdAt,
    prompt_preview: prompt,
    refinement_intent: turn > 1,
    refinement_turn: turn,
  }
}

function fixtureMessages(sessionId: string, label: string) {
  return [
    { id: `${label}-user-1`, session_id: sessionId, role: 'user', content: `${label} turn one`, created_at: createdAt },
    { id: `${label}-assistant-1`, session_id: sessionId, role: 'assistant', content: `${label} transcript preserved`, created_at: createdAt },
    { id: `${label}-user-2`, session_id: sessionId, role: 'user', content: `${label} turn two`, created_at: createdAt },
  ]
}

async function installWorkbenchApi(page: Page, options: { delayWebDetailOnce?: boolean; mobileUI?: boolean } = {}) {
  const webRuns = [
    fixtureRun('q2', 'Turn two: Focus Mint Demo and rounded Start button', 2),
    fixtureRun('q1', 'Turn one: pale green card and Start button', 1),
  ]
  const telegramRuns = [
    fixtureRun('q4', 'Telegram turn two', 2),
    fixtureRun('q3', 'Telegram turn one', 1),
  ]
  const jobs = [
    fixtureJob('q2', 'web', 'collision-123', webRuns[0].prompt_preview, 2),
    fixtureJob('q1', 'web', 'collision-123', webRuns[1].prompt_preview, 1),
    fixtureJob('q4', 'telegram', 'collision-123', telegramRuns[0].prompt_preview, 2),
    fixtureJob('q3', 'telegram', 'collision-123', telegramRuns[1].prompt_preview, 1),
  ]
  let webChainActive = true
  let telegramChainActive = true
  let webDetailStarted = false
  let delayedWebDetail = false
  let approvalSequence = 0
  const approvals: Array<{ id: string; kind: 'job'; job_id: string; action: string; status: string; created_at: string; expires_at: number }> = []
  const decisions: Array<{ job_id: string; action: string }> = []

  const webSummary = () => webChainActive ? {
    chain_id: 'chain-web', state: 'active', turn_count: 2,
    root_queue_job_id: 'q1', latest_queue_job_id: 'q2', latest_runtime_job_id: 'runtime-q2', updated_at: createdAt,
  } : null
  const telegramSummary = () => telegramChainActive ? {
    chain_id: 'chain-telegram', state: 'active', turn_count: 2,
    root_queue_job_id: 'q3', latest_queue_job_id: 'q4', latest_runtime_job_id: 'runtime-q4', updated_at: createdAt,
  } : null
  const webSession = () => ({
    id: webSessionId, channel: 'web', operator_id: 'alice', source_chat_id: 'collision-123',
    title: 'Web refinement session', created_at: createdAt, last_activity: createdAt,
    job_count: 2, message_count: 3, latest_job: jobs[0], active_refinement: webSummary(),
  })
  const telegramSession = () => ({
    id: telegramSessionId, channel: 'telegram', operator_id: 'alice', source_chat_id: 'collision-123',
    title: 'Telegram refinement session', created_at: createdAt, last_activity: createdAt,
    job_count: 2, message_count: 3, latest_job: jobs[2], active_refinement: telegramSummary(),
  })
  const sessionDetail = (id: string) => id === webSessionId ? ({
    ...webSession(), messages: fixtureMessages(webSessionId, 'Web'), runs: webRuns, jobs: jobs.slice(0, 2),
  }) : id === telegramSessionId ? ({
    ...telegramSession(), messages: fixtureMessages(telegramSessionId, 'Telegram'), runs: telegramRuns, jobs: jobs.slice(2),
  }) : null

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/health') return json({ ok: true, service: 'conveyor-web', schema_version: 1 })
    if (path === '/api/sessions' && request.method() === 'GET') return json({ sessions: [webSession(), telegramSession()] })
    if (path.startsWith('/api/sessions/') && request.method() === 'GET') {
      const id = path.slice('/api/sessions/'.length)
      if (id === webSessionId) {
        webDetailStarted = true
        if (options.delayWebDetailOnce && !delayedWebDetail) {
          delayedWebDetail = true
          await new Promise(resolve => setTimeout(resolve, 900))
        }
      }
      const value = sessionDetail(id)
      return json(value || { error: 'not found' }, value ? 200 : 404)
    }
    if (path === '/api/jobs' && request.method() === 'GET') return json({ jobs })
    if (path === '/api/approvals' && request.method() === 'GET') return json({ approvals })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0.1, 0.1, 0.1], cpu_count: 2,
      memory: { total: null, available: null }, disk: { total: 1024, used: 0, free: 1024 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: false }, feishu: { configured: false } },
      nodes: [], features: { mobile_ui: Boolean(options.mobileUI) },
    })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json({ events: [] })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) {
      return json({ job_id: path.split('/')[3], diff: 'diff --git a/index.html b/index.html\n+<h1>Focus Mint Demo</h1>\n+<button>Start</button>\n+background: #d8f3dc;\n+border-radius: 8px;' })
    }
    const operation = path.match(/^\/api\/jobs\/([^/]+)\/(apply|discard)$/)
    if (operation && request.method() === 'POST') {
      const [, jobId, action] = operation
      const approval = {
        id: `approval-${++approvalSequence}`, kind: 'job' as const, job_id: jobId,
        action, status: 'pending', created_at: createdAt, expires_at: Date.now() / 1000 + 300,
      }
      approvals.unshift(approval)
      return json({ approval }, 202)
    }
    const decision = path.match(/^\/api\/approvals\/([^/]+)\/(approve|reject)$/)
    if (decision && request.method() === 'POST') {
      const [, approvalId, result] = decision
      const index = approvals.findIndex(item => item.id === approvalId)
      if (index < 0) return json({ error: 'not found' }, 404)
      const [approval] = approvals.splice(index, 1)
      decisions.push({ job_id: approval.job_id, action: `${approval.action}:${result}` })
      if (result === 'approve') {
        if (approval.job_id === 'q2') webChainActive = false
        if (approval.job_id === 'q4') telegramChainActive = false
      }
      return json({ id: approvalId, job_id: approval.job_id, status: result === 'approve' ? 'accepted' : 'rejected' })
    }
    return json({ error: `unhandled fixture request: ${request.method()} ${path}` }, 404)
  })

  return { decisions, get webDetailStarted() { return webDetailStarted }, get delayedWebDetail() { return delayedWebDetail } }
}

async function unlock(page: Page) {
  await page.goto('/')
  await page.getByLabel('Console token').fill('pr103-e2e-test-token-not-a-secret-0001')
  await page.getByRole('button', { name: 'Unlock console' }).click()
  await expect(page.getByRole('heading', { name: 'Web refinement session' })).toBeVisible()
}

test('historical run review keeps cumulative chain approvals scoped to the session', async ({ page }) => {
  const fixture = await installWorkbenchApi(page)
  await unlock(page)
  const runHistory = page.getByRole('group', { name: 'Session run history' })
  await expect(runHistory.getByRole('button')).toHaveCount(2)
  await expect(page.getByText('Active refinement · turn 2', { exact: true })).toBeVisible()

  await runHistory.getByRole('button').last().click()
  await expect(runHistory.getByRole('button').last()).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('Web transcript preserved', { exact: true })).toBeVisible()
  await expect(page.getByText('Active refinement · turn 2', { exact: true })).toBeVisible()
  await page.locator('details.diff-view summary').click()
  await expect(page.locator('details.diff-view pre')).toContainText('Focus Mint Demo')
  await expect(page.locator('details.diff-view pre')).toContainText('#d8f3dc')
  await expect(page.locator('details.diff-view pre')).toContainText('border-radius: 8px')

  await page.getByRole('button', { name: 'Apply active changes…' }).click()
  await expect(page.getByText('APPROVAL REQUIRED', { exact: true })).toBeVisible()
  await expect(page.getByText(/decision is scoped to job q2/)).toBeVisible()
  expect(fixture.decisions).toEqual([])

  await page.getByRole('button', { name: /Telegram refinement session/ }).click()
  await expect(page.getByRole('heading', { name: 'Telegram refinement session' })).toBeVisible()
  await expect(page.getByText('APPROVAL REQUIRED', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('group', { name: 'Session run history' }).getByRole('button')).toHaveCount(2)
  await page.getByRole('button', { name: 'Discard active changes…' }).click()
  await expect(page.getByText('APPROVAL REQUIRED', { exact: true })).toBeVisible()
  await expect(page.getByText(/decision is scoped to job q4/)).toBeVisible()
  await page.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByText('Active refinement · turn 2', { exact: true })).toHaveCount(0)
  expect(fixture.decisions).toContainEqual({ job_id: 'q4', action: 'discard:approve' })

  await page.getByRole('button', { name: /Web refinement session/ }).click()
  await expect(page.getByText('APPROVAL REQUIRED', { exact: true })).toBeVisible()
  await page.getByRole('group', { name: 'Session run history' }).getByRole('button').last().click()
  await page.getByRole('button', { name: 'Reject' }).click()
  await expect(page.getByText('APPROVAL REQUIRED', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Active refinement · turn 2', { exact: true })).toBeVisible()
  expect(fixture.decisions).toContainEqual({ job_id: 'q2', action: 'apply:reject' })

  await page.getByRole('button', { name: 'Apply active changes…' }).click()
  await expect(page.getByText('APPROVAL REQUIRED', { exact: true })).toBeVisible()
  await expect(page.getByText(/decision is scoped to job q2/)).toBeVisible()
  await page.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByText('Active refinement · turn 2', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('group', { name: 'Session run history' }).getByRole('button')).toHaveCount(2)
  await expect(page.getByText('Web transcript preserved', { exact: true })).toBeVisible()
  expect(fixture.decisions).toContainEqual({ job_id: 'q2', action: 'apply:approve' })
})

test('a late session response cannot replace the newly selected session', async ({ page }) => {
  const fixture = await installWorkbenchApi(page, { delayWebDetailOnce: true })
  await unlock(page)
  await expect.poll(() => fixture.webDetailStarted).toBe(true)
  await expect.poll(() => fixture.delayedWebDetail).toBe(true)
  await page.getByRole('button', { name: /Telegram refinement session/ }).click()
  await expect(page.getByRole('heading', { name: 'Telegram refinement session' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'Session run history' }).getByRole('button').first()).toContainText('Telegram turn two')
  await page.waitForTimeout(1100)
  await expect(page.getByRole('heading', { name: 'Telegram refinement session' })).toBeVisible()
  const visibleRuns = await page.getByRole('group', { name: 'Session run history' }).getByRole('button').allTextContents()
  expect(visibleRuns.join('\n')).toContain('Telegram turn one')
  expect(visibleRuns.join('\n')).not.toContain('Focus Mint Demo')
  await expect(page.getByText('Telegram transcript preserved', { exact: true })).toBeVisible()
})

test('mobile navigation and session drawer remain keyboard reachable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await installWorkbenchApi(page, { mobileUI: true })
  await unlock(page)

  const navigation = page.getByRole('navigation', { name: 'Mobile navigation' })
  await expect(navigation).toBeVisible()
  const tasks = navigation.getByRole('button', { name: /Tasks/ })
  const chat = navigation.getByRole('button', { name: /Chat/ })
  await tasks.focus()
  await page.keyboard.press('Tab')
  await expect(chat).toBeFocused()

  await navigation.getByRole('button', { name: /More/ }).click()
  const options = page.getByRole('dialog', { name: 'More options' })
  await expect(options).toBeVisible()
  await options.getByRole('button', { name: /Sessions/ }).click()
  await expect(page.getByRole('dialog', { name: 'Sessions' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Web refinement session/ })).toBeVisible()
})
