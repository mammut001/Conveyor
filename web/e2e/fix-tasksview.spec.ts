import { expect, test, type Page, type Route } from '@playwright/test'
import { mkdirSync } from 'node:fs'

// 任务 view fixes: failed-run presentation, stale badge, touch composer, 40px mode toggle.
const SESSION = 'web:web-console:agent-coder'
const NOW = Date.now()
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString()
const SHOTS = 'test-results/fix-tasksview'
const RAW = "[Errno 2] No such file or directory: 'codex'"

const job = (id: string, state: string, mode: string, prompt: string, minutesAgo: number, extra: Record<string, unknown> = {}) => ({
  id, state, mode, channel: 'web', chat_id: 'agent-coder', operator_id: 'web-console',
  created_at: iso(minutesAgo), updated_at: iso(minutesAgo), started_at: iso(minutesAgo), finished_at: iso(minutesAgo),
  prompt_preview: prompt, metadata: {}, changed_files: [], ...extra,
})

async function install(page: Page, jobs: ReturnType<typeof job>[], opts: { messages?: unknown[]; sessionTitle?: string; submitted?: unknown[] } = {}) {
  await page.addInitScript(() => { localStorage.setItem('conveyor-token', 't'); sessionStorage.setItem('conveyor-token', 't'); localStorage.setItem('conveyor-agent', 'coder') })
  const sorted = [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at))
  const session = {
    id: SESSION, channel: 'web', title: opts.sessionTitle ?? '新会话', created_at: iso(300), last_activity: iso(1),
    operator_id: 'web-console', source_chat_id: 'agent-coder', job_count: jobs.length, message_count: (opts.messages || []).length,
    latest_job: sorted[0], active_refinement: null,
  }
  const runs = sorted.map(j => ({ id: j.id, state: j.state, mode: j.mode, created_at: j.created_at, prompt_preview: j.prompt_preview, refinement_intent: false }))
  await page.route('**/api/**', async (route: Route) => {
    const p = decodeURIComponent(new URL(route.request().url()).pathname)
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (p === '/api/events/stream') return
    if (p === '/api/sessions') return json({ sessions: [session] })
    if (p === `/api/sessions/${SESSION}`) return json({ ...session, messages: opts.messages || [], runs, jobs: sorted })
    if (p === '/api/jobs') return json({ jobs: sorted })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(p)) return json({ events: [] })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(p)) return json({ diff: '', stats: {}, files: [] })
    if (p === '/api/approvals') return json({ approvals: [] })
    if (p === '/api/nodes') return json({ nodes: [] })
    if (p === '/api/agents') return json({ enabled: true, agents: [{ id: 'coder', name: 'Coder', color: '#2f7df6', is_default: false, session_id: SESSION, instructions: '', workspace_path: '', workspace_status: '', status: 'idle', last_message: '', message_count: 0 }] })
    if (p === '/api/system/status') return json({ uptime_seconds: 1, load_average: [0, 0, 0], cpu_count: 1, memory: { total: 1, available: 1 }, disk: { total: 1, used: 0, free: 1 }, queue: { depth: 0, paused: false, states: {} }, channels: {}, nodes: [], features: { agents: true } })
    if (p === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, active_task: null, screen_preview_enabled: false, screenshots: [], screen_request: null })
    if (p === '/api/screen/status') return json({ enabled: false, available: false, reason: '', control_enabled: false, controlling: false, agent_paused: false, blocked_by: null, width: 0, height: 0 })
    if (p === '/api/takeover/status') return json({ enabled: false, takeover: null, privacy_mode: false, closing: null, transport_allowed: false, transport: { phase: 'idle', running: false, ready: false } })
    if (p === '/api/tasks') { opts.submitted?.push(route.request().postDataJSON()); return json({ job_id: 'q-new', session_id: SESSION }) }
    return json({})
  })
}

async function openTasks(page: Page) {
  await page.goto('/')
  await expect(page.locator('main.app-shell')).toBeVisible()
  if (await page.locator('.workbench-modebar').isVisible()) {
    await page.addStyleTag({ content: '.workbench-modebar { display: none !important; } .primary-interface .app-shell { height: 100dvh !important; }' })
  }
  const tasks = page.locator('button, a, [role="tab"]').filter({ hasText: /^[^\p{L}\p{N}]*(任务|Tasks)\s*$/u }).filter({ visible: true })
  await tasks.first().click()
  await expect(page.locator('.stream-panel > .event-stream')).toBeVisible()
}

async function shoot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true })
  for (const [w, h] of [[1440, 900], [390, 844]] as const) {
    await page.setViewportSize({ width: w, height: h })
    await page.waitForTimeout(250)
    await page.screenshot({ path: `${SHOTS}/${name}-${w}.png` })
  }
}

test('failed run shows own request, Chinese summary, raw detail folded, title from prompt', async ({ page }) => {
  const failed = job('q1', 'failed', 'fix', '把登录按钮改成蓝色', 2, { error: RAW })
  await install(page, [failed])
  await openTasks(page)
  await expect(page.locator('.pending-turn')).toContainText('把登录按钮改成蓝色')
  const notice = page.locator('.task-notice.failed')
  await expect(notice).toContainText('还没有可用的执行者，先去「执行者」页安装并登录一个')
  await expect(notice.locator('.task-notice-detail')).toBeHidden()
  await notice.getByText('详情').click()
  await expect(notice.locator('.task-notice-detail')).toContainText('codex')
  await expect(page.locator('.shell-heading h2')).toHaveText('把登录按钮改成蓝色')
  await shoot(page, 'failed')
})

test('draft is restored when the submitted run fails', async ({ page }) => {
  const submitted: unknown[] = []
  const old = job('q0', 'completed', 'run', '之前的问题', 30)
  await install(page, [old], { submitted })
  await openTasks(page)
  const box = page.getByLabel('任务内容')
  await box.fill('重新整理按钮样式')
  await page.getByRole('button', { name: '发送' }).click()
  await expect.poll(() => submitted.length).toBe(1)
  // The run now exists and has failed immediately.
  await page.unrouteAll()
  await install(page, [old, job('q-new', 'failed', 'run', '重新整理按钮样式', 0, { error: RAW })], { submitted })
  await expect(box).toHaveValue('重新整理按钮样式', { timeout: 15_000 })
})

test('older failure does not stay above a later successful run', async ({ page }) => {
  const failed = job('q1', 'failed', 'fix', '旧的构建任务', 60, { error: RAW })
  const ok = job('q2', 'completed', 'run', '成功的新提问', 5)
  const messages = [
    { id: 'm1', session_id: SESSION, role: 'user', content: '成功的新提问', created_at: iso(5), job_id: 'q2' },
    { id: 'm2', session_id: SESSION, role: 'assistant', content: '这是回答。', created_at: iso(4), job_id: 'q2' },
  ]
  await install(page, [failed, ok], { messages })
  await page.addInitScript(() => sessionStorage.setItem('conveyor-selected-session', 'x'))
  await openTasks(page)
  await expect(page.getByText('这是回答。')).toBeVisible()
  await expect(page.locator('.task-notice.failed')).toHaveCount(0)
  await expect(page.locator('.shell-heading')).not.toContainText('失败')
  await expect(page.locator('.shell-heading h2')).toHaveText('成功的新提问')
  await expect(page.locator('.stream-panel')).not.toContainText('任务执行失败')
  // The old failure is still listed among the runs; opening it does not resurrect the card above the newer chat.
  const oldRun = page.locator('.session-run', { hasText: '旧的构建任务' })
  await expect(oldRun).toContainText('失败')
  // Explicitly opening the old failed run shows its own card and badge again.
  await oldRun.evaluate(el => (el as HTMLElement).click())
  await expect(page.locator('.task-notice.failed')).toHaveCount(1)
  await expect(page.locator('.shell-heading')).toContainText('失败')
  await shoot(page, 'stale')
})

test('touch: Enter inserts newline, hint explains it, send keeps focus; mode toggle >= 40px', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
  const page = await context.newPage()
  await install(page, [job('q0', 'completed', 'run', '之前的问题', 30)])
  await openTasks(page)
  const box = page.getByLabel('任务内容')
  await box.fill('第一行')
  await box.press('Enter')
  await box.type('第二行')
  await expect(box).toHaveValue('第一行\n第二行')
  await expect(page.locator('.task-composer-hint')).toContainText('点「发送」')
  const prevented = await page.evaluate(() => {
    const btn = document.querySelector('.task-composer .send-button')!
    const e = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    btn.dispatchEvent(e)
    return e.defaultPrevented
  })
  expect(prevented).toBe(true)
  for (const b of await page.locator('.task-mode-switch button').all()) {
    expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(40)
  }
  await shoot(page, 'touch')
  await context.close()
})

test('desktop: Enter sends, hint intact', async ({ page }) => {
  const submitted: unknown[] = []
  await install(page, [job('q0', 'completed', 'run', '之前的问题', 30)], { submitted })
  await openTasks(page)
  await expect(page.locator('.task-composer-hint')).toContainText('Enter 发送')
  await page.getByLabel('任务内容').fill('你好')
  await page.getByLabel('任务内容').press('Enter')
  await expect.poll(() => submitted.length).toBe(1)
})
