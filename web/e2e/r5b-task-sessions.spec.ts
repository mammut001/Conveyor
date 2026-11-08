import { expect, test, type Page, type Route } from '@playwright/test'

// QA r5 N3/N4: a finished 改代码 run must read as finished (no in-progress
// refinement state, calm 0-file line, request and reply visible), and task
// sessions must stay in the sidebar after a reload.

const createdAt = '2026-11-08T03:00:00.000Z'
const finishedAt = '2026-11-08T03:02:00.000Z'
const taskSessionId = 'web:web-console:readme-task'
const taskTitle = '在 README 末尾加一行说明'
const chatSessionId = 'web:web-console:status-chat'
const chatTitle = '请检查系统状态'
const reply = '已检查 README，末尾已经有说明，这次没有需要改的地方。'

type Mode = { withTranscript: boolean }

function taskJob() {
  return {
    id: 'q2', state: 'completed', mode: 'fix', channel: 'web', chat_id: 'readme-task', operator_id: 'web-console',
    created_at: createdAt, started_at: createdAt, updated_at: finishedAt, finished_at: finishedAt,
    prompt_preview: taskTitle, refinement_intent: false, metadata: { refinement_turn: 1, runtime_job_id: 'runtime-q2' },
    changed_files: [],
  }
}

function taskChain() {
  return {
    chain_id: 'chain-readme', state: 'active', turn_count: 1, root_queue_job_id: 'q2',
    latest_queue_job_id: 'q2', latest_runtime_job_id: 'runtime-q2', updated_at: finishedAt,
  }
}

function taskSession(messageCount: number) {
  return {
    id: taskSessionId, channel: 'web', operator_id: 'web-console', source_chat_id: 'readme-task',
    title: taskTitle, created_at: createdAt, last_activity: finishedAt,
    job_count: 1, message_count: messageCount, latest_job: taskJob(), active_refinement: taskChain(),
  }
}

function chatSession() {
  return {
    id: chatSessionId, channel: 'web', operator_id: 'web-console', source_chat_id: 'status-chat',
    title: chatTitle, created_at: createdAt, last_activity: createdAt,
    job_count: 0, message_count: 2, latest_job: null, active_refinement: null,
  }
}

function transcriptRows() {
  return [
    { id: 'row-user', session_id: taskSessionId, job_id: 'q2', role: 'user', kind: 'codex', content: taskTitle, created_at: createdAt },
    { id: 'row-reply', session_id: taskSessionId, job_id: 'q2', role: 'assistant', kind: 'codex', content: reply, created_at: finishedAt },
  ]
}

async function installTaskApi(page: Page, mode: Mode) {
  const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  // Sidebar: the chat session has a transcript; the task session has only its run unless the transcript was written.
  const sessions = () => [taskSession(mode.withTranscript ? 2 : 0), chatSession()]
  const detail = (id: string) => {
    if (id === taskSessionId) {
      return {
        ...taskSession(mode.withTranscript ? 2 : 0),
        messages: mode.withTranscript ? transcriptRows() : [],
        runs: [{
          id: 'q2', state: 'completed', mode: 'fix', created_at: createdAt, updated_at: finishedAt, finished_at: finishedAt,
          prompt_preview: taskTitle, refinement_intent: false, refinement_turn: 1,
        }],
        jobs: [taskJob()],
      }
    }
    if (id === chatSessionId) {
      return {
        ...chatSession(),
        messages: [
          { id: 'chat-user', session_id: chatSessionId, role: 'user', kind: 'chat', content: chatTitle, created_at: createdAt },
          { id: 'chat-reply', session_id: chatSessionId, role: 'assistant', kind: 'chat', content: '系统正常。', created_at: createdAt },
        ],
        runs: [], jobs: [],
      }
    }
    return null
  }
  const events = {
    events: [
      { schema_version: 1, event_id: 'e1', sequence: 1, timestamp: createdAt, kind: 'assistant.started', job_id: 'q2', payload: {} },
      { schema_version: 1, event_id: 'e2', sequence: 2, timestamp: finishedAt, kind: 'assistant.completed', job_id: 'q2', payload: { text: reply, runtime_job_id: 'runtime-q2' } },
    ],
  }

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)

    if (path === '/api/health') return json(route, { ok: true, service: 'conveyor-web', schema_version: 1 })
    if (path === '/api/sessions' && request.method() === 'GET') return json(route, { sessions: sessions() })
    if (path.startsWith('/api/sessions/') && request.method() === 'GET') {
      const id = path.slice('/api/sessions/'.length)
      const value = detail(id)
      return json(route, value || { error: 'not found' }, value ? 200 : 404)
    }
    if (path === '/api/jobs' && request.method() === 'GET') return json(route, { jobs: [taskJob()] })
    if (path === '/api/approvals' && request.method() === 'GET') return json(route, { approvals: [] })
    if (path === '/api/nodes') return json(route, { nodes: [] })
    if (path === '/api/agents') return json(route, { enabled: false, agents: [] })
    if (path === '/api/system/status') return json(route, {
      uptime_seconds: 10, load_average: [0.1, 0.1, 0.1], cpu_count: 2,
      memory: { total: null, available: null }, disk: { total: 1024, used: 0, free: 1024 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: false }, feishu: { configured: false } },
      nodes: [], features: { mobile_ui: false },
    })
    if (path === '/api/computer/status') return json(route, { armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json(route, events)
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) return json(route, { job_id: 'q2', diff: '' })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    return json(route, { error: `unhandled fixture request: ${request.method()} ${path}` }, 404)
  })
}

async function unlock(page: Page) {
  await page.goto('/')
  await page.getByLabel('访问令牌').fill('r5b-e2e-test-token-not-a-secret-0001')
  await page.getByRole('button', { name: '解锁', exact: true }).click()
  await expect(page.getByRole('button', { name: new RegExp(chatTitle) })).toBeVisible()
}

async function openTaskSession(page: Page) {
  await page.getByRole('button', { name: new RegExp(taskTitle) }).first().click()
  await expect(page.getByRole('heading', { name: taskTitle })).toBeVisible()
}

async function expectFinishedRun(page: Page) {
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  // The request and the reply are in the transcript, each once.
  await expect(page.locator('.transcript-message.role-user')).toHaveCount(1)
  await expect(page.locator('.transcript-message.role-user')).toContainText(taskTitle)
  await expect(page.locator('.transcript-message.role-assistant')).toContainText('这次没有需要改的地方')
  // A finished run with nothing changed is calm, and never reads as in progress.
  await expect(page.getByText('这次没有改动文件', { exact: true })).toBeVisible()
  await expect(page.getByText('改动还没应用')).toHaveCount(0)
  await expect(page.getByText('正在继续修改')).toHaveCount(0)
  await expect(page.getByText('进行中')).toHaveCount(0)
  await expect(page.getByText('对话记录暂不可用')).toHaveCount(0)
}

test('N3: a finished run with no file changes shows the request, the reply and no in-progress state', async ({ page }) => {
  await installTaskApi(page, { withTranscript: false })
  await unlock(page)
  await openTaskSession(page)
  await expectFinishedRun(page)
  // The run history of this session lists the run once the session detail has loaded.
  await expect(page.locator('.context-section--runs')).toContainText('1 条')
  await page.screenshot({ path: '/Users/dogecoin/Documents/GitHub/Conveyor-worktrees/_agentpool/qa/shots/r5b-completed-run.png', fullPage: true })

  // After a reload the run is still there, with the same reading.
  await page.reload()
  await expect(page.getByRole('button', { name: new RegExp(chatTitle) })).toBeVisible()
  await openTaskSession(page)
  await expectFinishedRun(page)
})

test('N3: a run whose transcript was written reads the same after reload (no duplicated request)', async ({ page }) => {
  await installTaskApi(page, { withTranscript: true })
  await unlock(page)
  await openTaskSession(page)
  await expectFinishedRun(page)

  await page.reload()
  await expect(page.getByRole('button', { name: new RegExp(chatTitle) })).toBeVisible()
  await openTaskSession(page)
  await expectFinishedRun(page)
})

test('N4: task sessions stay in the sidebar after a reload, listed once', async ({ page }) => {
  await installTaskApi(page, { withTranscript: false })
  await unlock(page)
  // Sidebar rows only; the run history under the transcript repeats the same prompt.
  const sidebar = () => page.locator('button.session-item', { hasText: taskTitle })
  await expect(sidebar()).toHaveCount(1)
  await expect(page.getByRole('button', { name: new RegExp(chatTitle) })).toHaveCount(1)

  await page.reload()
  await expect(page.getByRole('button', { name: new RegExp(chatTitle) })).toBeVisible()
  await expect(sidebar()).toHaveCount(1)
  await page.screenshot({ path: '/Users/dogecoin/Documents/GitHub/Conveyor-worktrees/_agentpool/qa/shots/r5b-sidebar-after-reload.png', fullPage: true })
})
