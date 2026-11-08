import { expect, test, type Page, type Route } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import path from 'node:path'

// Visual + behaviour check for the 任务 view and the right-hand context panel
// (agent card, runs, job, approvals, changes/diff, computer, system).
// Everything is mocked; the data mirrors the owner's deployment: agents on,
// live screen on, Telegram configured, Feishu not.

const TOKEN = 'ux-tasks-e2e-token-not-a-secret-0001'
const NOW = Date.now()
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString()
const CODER_SESSION = 'web:web-console:agent-coder'
const WIDTHS = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
] as const

type JobKind = 'running' | 'changes' | 'clean' | 'none'
type Scenario = {
  job: JobKind
  approvals?: boolean
  takeover?: 'disabled' | 'idle' | 'active'
  liveScreen?: boolean
  hostPreview?: boolean
  refinement?: boolean
}

const instructions = [
  '你负责 focus-mint 项目的前端代码。',
  '1. 改代码前先读相关文件，说明你打算怎么改。',
  '2. 每次只做一件事，改动尽量小，保持现有命名。',
  '3. 改完必须运行 npm run build 和 npm test，失败就先修好再汇报。',
  '4. 不要修改 .env、密钥文件和部署脚本。',
  '5. 汇报时用中文，列出改了哪些文件、为什么改。',
  '6. 遇到需要登录、付款或验证码的步骤，暂停并请我接手。',
  '7. 不确定需求时先提问，不要猜。',
].join('\n')

const agent = (id: string, name: string, color: string, extra: Record<string, unknown> = {}) => ({
  id, name, color, is_default: id === 'default', session_id: `web:web-console:agent-${id}`,
  instructions: '', workspace_path: '', workspace_status: '', status: 'idle',
  last_message: '', message_count: 3, ...extra,
})
const agents = [
  agent('default', 'Conveyor', '#bd5c40', { instructions: '你是我的个人常驻助手。', last_message: '今天的待办已经整理好了。' }),
  agent('finance', 'Finance', '#10b981', { last_message: '本月账单已核对。' }),
  agent('researcher', 'Researcher', '#8b5cf6', { last_message: '资料已汇总。' }),
  agent('coder', 'Coder', '#2f7df6', {
    instructions, workspace_path: '/home/conveyor/projects/focus-mint', workspace_status: 'ok',
    status: 'working', last_message: '正在修复构建。',
  }),
]

const changedFiles = [
  { status: 'M', path: 'web/src/App.tsx' },
  { status: '??', path: 'web/src/ux/tasks.css' },
  { status: 'D', path: 'web/src/legacy/old-changes-panel.css' },
]

const diff = `diff --git a/web/src/App.tsx b/web/src/App.tsx
index 3f2a1c4..8b9d0e2 100644
--- a/web/src/App.tsx
+++ b/web/src/App.tsx
@@ -1063,9 +1063,10 @@ export default function App() {
-        <ContextSection title="Changes" className="context-section--changes" collapsible>
-          <div className="file-list">{files.map(renderFile)}</div>
+        <ContextSection title="改动" className="context-section--changes" collapsible>
+          {files.length === 0
+            ? <Empty text="暂无改动" />
+            : <ChangedFiles files={files} onApply={() => action(\`/api/jobs/\${job.id}/apply\`)} onDiscard={() => action(\`/api/jobs/\${job.id}/discard\`)} />}
         </ContextSection>
diff --git a/web/src/ux/tasks.css b/web/src/ux/tasks.css
new file mode 100644
index 0000000..5c1e2d7
--- /dev/null
+++ b/web/src/ux/tasks.css
@@ -0,0 +1,4 @@
+.file-chip { border-radius: 6px; font-size: 12px; }
+.file-chip--added { color: #15803d; background: #ecfdf3; }
+.diff-line--add { background: #ecfdf3; }
+.diff-line--del { background: #fef3f2; }
diff --git a/web/src/legacy/old-changes-panel.css b/web/src/legacy/old-changes-panel.css
deleted file mode 100644
index 7d1e0aa..0000000
--- a/web/src/legacy/old-changes-panel.css
+++ /dev/null
@@ -1,3 +0,0 @@
-.changes-button { font: 10px ui-monospace, monospace; }
-.changes-button.apply { background: #2563eb; }
-.changes-button.discard { color: #ff9a9f; }
`

function event(sequence: number, kind: string, payload: Record<string, unknown>, toolCallId?: string, minutesAgo = 10) {
  return {
    schema_version: 1, event_id: `ev-${sequence}`, sequence, timestamp: iso(minutesAgo - sequence * 0.2),
    kind, job_id: 'q128', payload, ...(toolCallId ? { tool_call_id: toolCallId } : {}),
  }
}

function jobEvents(kind: JobKind) {
  const base = [
    event(1, 'task.started', {}),
    event(2, 'tool.started', { name: 'bash', status: 'running' }, 'call-1'),
    event(3, 'tool.completed', { status: 'completed' }, 'call-1'),
    event(4, 'tool.started', { name: 'read', status: 'running' }, 'call-2'),
    event(5, 'tool.completed', { status: 'completed' }, 'call-2'),
    event(6, 'tool.started', { name: 'edit', status: 'running' }, 'call-3'),
    event(7, 'tool.failed', { status: 'failed', error: 'old_string not found in web/src/App.tsx' }, 'call-3'),
    event(8, 'tool.started', { name: 'edit', status: 'running' }, 'call-4'),
    event(9, 'tool.completed', { status: 'completed' }, 'call-4'),
    event(10, 'tool.started', { name: 'shell', status: 'in_progress' }, 'call-5'),
    event(11, 'tool.output', {
      name: 'shell', output: '> focus-mint@1.0.0 test\n> vitest run\n\n ✓ src/App.test.tsx (12 tests) 412ms\n ✓ src/changes.test.tsx (5 tests) 96ms\n',
    }, 'call-5'),
    event(12, 'assistant.delta', { text: '构建失败是因为 `App.tsx` 还在引用已经删除的 `legacy.css`。' }),
    event(13, 'assistant.delta', { text: '我已经去掉这个引用，正在重新运行测试…' }),
  ]
  if (kind === 'running') return base
  return [
    ...base,
    event(14, 'tool.completed', { name: 'shell', status: 'completed', output: 'Test Files  2 passed (2)\n     Tests  17 passed (17)' }, 'call-5'),
    event(15, 'task.completed', {}),
  ]
}

function fixture(scenario: Scenario) {
  const running = scenario.job === 'running'
  const files = scenario.job === 'changes' ? changedFiles : []
  const current = {
    id: 'q128', state: running ? 'running' : 'completed', mode: scenario.job === 'clean' ? 'run' : 'fix',
    channel: 'web', chat_id: 'agent-coder', operator_id: 'web-console',
    created_at: iso(12), updated_at: iso(1), started_at: iso(11), finished_at: running ? undefined : iso(2),
    prompt_preview: running
      ? '检查 focus-mint 的构建为什么失败，并修复。'
      : scenario.job === 'changes'
        ? '把任务页右侧的改动面板整理一下：没有改动时不要显示按钮，文件列表加上状态标签。'
        : '解释一下 apply_policy 为什么拒绝修改 .env 文件。',
    metadata: { execution_owner_id: 'web:pid-4121', execution_owner_bound_at: iso(11) },
    changed_files: files,
  }
  const previous = {
    ...current, id: 'q127', state: 'completed', mode: 'run', created_at: iso(80), started_at: iso(80), finished_at: iso(78),
    prompt_preview: '列出 focus-mint 里还在用的旧样式文件。', changed_files: [],
  }
  const runs = [current, previous].map(job => ({
    id: job.id, state: job.state, mode: job.mode, created_at: job.created_at, updated_at: job.updated_at,
    finished_at: job.finished_at, prompt_preview: job.prompt_preview, refinement_intent: false,
  }))
  const messages = running ? [
    { id: 'm1', session_id: CODER_SESSION, role: 'user', content: '列出 focus-mint 里还在用的旧样式文件。', created_at: iso(80) },
    { id: 'm2', session_id: CODER_SESSION, role: 'assistant', content: '还在用的旧样式文件有两个：`legacy.css` 和 `old-changes-panel.css`。', created_at: iso(78) },
  ] : [
    { id: 'm1', session_id: CODER_SESSION, role: 'user', content: current.prompt_preview, created_at: iso(12), job_id: 'q128' },
    {
      id: 'm2', session_id: CODER_SESSION, role: 'assistant', job_id: 'q128', created_at: iso(2),
      content: scenario.job === 'changes'
        ? '已完成修改：\n\n1. 没有改动时只显示「暂无改动」；\n2. 文件列表增加了新增、修改、删除标签；\n3. 差异按行着色。\n\n请在右侧查看差异，确认后点「应用改动」。'
        : '`apply_policy` 会拒绝高风险路径，`.env` 里有密钥，所以不会被自动应用。这次没有修改任何文件。',
    },
  ]
  const none = scenario.job === 'none'
  const session = {
    id: CODER_SESSION, channel: 'web', title: 'Coder', created_at: iso(200), last_activity: iso(1),
    operator_id: 'web-console', source_chat_id: 'agent-coder', job_count: none ? 0 : 2, message_count: none ? 0 : messages.length,
    latest_job: none ? null : current,
    active_refinement: scenario.refinement ? {
      chain_id: 'chain-1', state: 'active', turn_count: 2, root_queue_job_id: 'q127',
      latest_queue_job_id: 'q128', latest_runtime_job_id: 'runtime-q128', updated_at: iso(1),
    } : null,
  }
  const approvals = scenario.approvals ? [
    { id: 'ap-1', kind: 'job', job_id: 'q128', action: 'apply', status: 'pending', created_at: iso(1), expires_at: NOW / 1000 + 280 },
    {
      id: 'tool-1', kind: 'tool', tool_name: 'bash', arg: 'git push origin feature/changes-panel',
      summary: 'Run a shell command on the host', session_id: CODER_SESSION, status: 'pending', expires_at: NOW / 1000 + 170,
    },
  ] : []
  const jobs = none ? [] : [current, previous]
  return { jobs, runs: none ? [] : runs, messages: none ? [] : messages, session, approvals, files }
}

async function installApi(page: Page, scenario: Scenario, frame: Buffer, mobileUI = false, rememberedOnly = false) {
  const data = fixture(scenario)
  const decisions: string[] = []
  let approvals = data.approvals
  // Seeding the token skips the unlock form (its labels belong to another pass).
  await page.addInitScript(([token, localOnly]) => {
    if (!localOnly) sessionStorage.setItem('conveyor-token', token)
    localStorage.setItem('conveyor-token', token)
    localStorage.setItem('conveyor-agent', 'coder')
  }, [TOKEN, rememberedOnly] as const)
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const p = decodeURIComponent(url.pathname)
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (p === '/api/events/stream') return // stays open: the job is "live"
    if (p === '/api/sessions') return json({ sessions: [data.session] })
    if (p === `/api/sessions/${CODER_SESSION}`) return json({ ...data.session, messages: data.messages, runs: data.runs, jobs: data.jobs })
    if (p === '/api/jobs') return json({ jobs: data.jobs })
    if (p === '/api/jobs/q128/events') return json({ events: jobEvents(scenario.job) })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(p)) return json({ events: [] })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(p)) {
      if (!data.files.length) return json({ job_id: 'q128', diff: '', stats: { files: 0, additions: 0, deletions: 0 }, files: [] })
      return json({
        job_id: 'q128', diff, stats: { files: 3, additions: 7, deletions: 5 },
        files: [
          { path: 'web/src/App.tsx', additions: 3, deletions: 2 },
          { path: 'web/src/ux/tasks.css', additions: 4, deletions: 0 },
          { path: 'web/src/legacy/old-changes-panel.css', additions: 0, deletions: 3 },
        ],
      })
    }
    if (p === '/api/approvals') return json({ approvals })
    const decided = p.match(/^\/api\/approvals\/([^/]+)\/(approve|reject)$/)
    if (decided) {
      const [, id, verdict] = decided
      decisions.push(`${id}:${verdict}`)
      const approval = approvals.find(item => item.id === id)
      approvals = approvals.filter(item => item.id !== id)
      if (approval?.kind !== 'job') return json({ ok: true })
      return json({
        id, job_id: approval.job_id, action: approval.action, status: verdict === 'approve' ? 'accepted' : 'rejected',
        result: verdict === 'approve'
          ? 'Applied q128. Copied 1 new files. Review main repo before committing.\n\nWorkspace status:\n M web/src/App.tsx'
          : 'Rejected by operator.',
      })
    }
    if (p === '/api/nodes') return json({ nodes: [
      { id: 'mac-studio', name: 'Mac Studio', type: 'desktop', status: 'online', last_seen_at: iso(1), capabilities: [] },
      { id: 'vps-main', name: 'vps-main', type: 'vps', status: 'online', last_seen_at: iso(0), capabilities: [] },
    ] })
    if (p === '/api/agents') return json({ enabled: true, agents })
    if (p === '/api/system/status') return json({
      uptime_seconds: 86_400, load_average: [0.42, 0.37, 0.3], cpu_count: 4,
      memory: { total: 8 * 1024 ** 3, available: 1.6 * 1024 ** 3 },
      disk: { total: 80 * 1024 ** 3, used: 57 * 1024 ** 3, free: 23.4 * 1024 ** 3 },
      queue: { depth: scenario.job === 'running' ? 1 : 0, paused: false, states: { running: scenario.job === 'running' ? 1 : 0, completed: 41 } },
      channels: { telegram: { configured: true }, feishu: { configured: false } }, nodes: [],
      features: { agents: true, long_term_memory: true, routines: true, teammate: true, live_screen: true, mobile_ui: mobileUI },
    })
    if (p === '/api/computer/status') return json({
      armed: true, arm_remaining_seconds: 0, active_task: null, screen_preview_enabled: Boolean(scenario.hostPreview),
      screenshots: scenario.hostPreview ? [{ artifact_id: 'shot-1', created_at: iso(3), width: 1280, height: 800, node_id: 'mac-studio' }] : [],
      screen_request: scenario.hostPreview ? { request_id: 'r-1', status: 'completed', upload_status: 'completed', created_at: iso(3) } : null,
    })
    if (p === '/api/artifacts/shot-1') return route.fulfill({ status: 200, contentType: 'image/jpeg', body: frame })
    if (p === '/api/screen/status') return json(scenario.liveScreen ? {
      enabled: true, available: true, reason: '', control_enabled: true, controlling: false, agent_paused: false,
      blocked_by: null, dedicated: true, width: 1280, height: 800,
    } : { enabled: false, available: false, reason: 'Live screen is disabled (set CONVEYOR_LIVE_SCREEN_ENABLED=true)', control_enabled: false, controlling: false, agent_paused: false, blocked_by: null, width: 0, height: 0 })
    if (p === '/api/screen/frame') {
      if (url.searchParams.get('since') !== '0') return route.fulfill({ status: 204, body: '' })
      return route.fulfill({ status: 200, contentType: 'image/jpeg', body: frame, headers: { 'X-Frame-Seq': '1', 'X-Screen-Width': '1280', 'X-Screen-Height': '800', 'X-Pointer-X': '640', 'X-Pointer-Y': '300' } })
    }
    if (p === '/api/takeover/status') return json(scenario.takeover === 'idle'
      ? { enabled: true, takeover: null, privacy_mode: false, closing: null, transport_allowed: false, transport: { phase: 'idle', running: false, ready: false } }
      : scenario.takeover === 'active'
        ? {
          enabled: true, privacy_mode: true, closing: null, transport_allowed: true,
          takeover: { id: 'tk-1', state: 'waiting_for_human', reason: 'login', remaining_seconds: 245 },
          transport: { phase: 'ready', running: true, ready: true, url: 'https://desktop.example.test/vnc' },
        }
        : { enabled: false, takeover: null, privacy_mode: false, closing: null, transport_allowed: false, transport: null, message: 'Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)' })
    if (p === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    if (p === '/api/agent-conversations') return json({ conversations: [{ id: 'coder', title: 'Web 原有会话', origin_channel: 'web', session_id: CODER_SESSION }] })
    return json({ items: [], approvals: [], sessions: [], jobs: [], conversations: [] })
  })
  return { decisions }
}

async function openTasks(page: Page) {
  await page.goto('/')
  await expect(page.locator('main.app-shell')).toBeVisible()
  // Screenshots only: the legacy 32px strip above the app pushes the workspace 32px below the
  // viewport (cutting off every composer). The shell pass removes it; emulate that if it is still here.
  if (await page.locator('.workbench-modebar').isVisible()) {
    await page.addStyleTag({ content: '.workbench-modebar { display: none !important; } .primary-interface .app-shell { height: 100dvh !important; }' })
  }
  // The section switcher may be renamed by the shell (and carries an icon on phones); accept either label.
  const tasks = page.locator('button, a, [role="tab"]').filter({ hasText: /^[^\p{L}\p{N}]*(任务|Tasks)\s*$/u }).filter({ visible: true })
  await tasks.first().click()
  await expect(page.locator('.stream-panel > .event-stream')).toBeVisible()
}

/** The panel is a drawer on phones (and collapsed below 1200px) after the shell pass: open it if it is off screen. */
async function openPanel(page: Page) {
  const panel = page.locator('aside.context-panel')
  const onScreen = () => panel.evaluate(el => {
    const style = getComputedStyle(el)
    const box = el.getBoundingClientRect()
    return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.right > 1 && box.left < innerWidth - 1
  })
  if (await onScreen()) return
  const toggle = page.getByRole('button', { name: /详情|Context/ }).filter({ visible: true })
  if (await toggle.count()) await toggle.first().click()
  else {
    await page.locator('.mobile-bottom-nav button').last().click()
    await page.locator('.mobile-sheet-item').filter({ hasText: /详情|上下文|Context/ }).first().click()
  }
  await expect.poll(onScreen).toBe(true)
}

async function selectTab(page: Page, label: string) {
  await openPanel(page)
  await page.locator('.agent-tabs').getByRole('tab', { name: label }).click()
}

async function shoot(page: Page, name: string, width: number) {
  const dir = path.join(test.info().project.outputDir, 'ux-tasks')
  mkdirSync(dir, { recursive: true })
  await page.waitForTimeout(250)
  await page.screenshot({ path: path.join(dir, `${name}-${width}.png`), fullPage: true })
}

async function expectNoHorizontalOverflow(page: Page) {
  const result = await page.evaluate(() => {
    const doc = document.documentElement
    const panel = document.querySelector<HTMLElement>('.context-panel')
    const offenders: string[] = []
    if (panel) {
      const edge = panel.getBoundingClientRect().right + 1
      for (const el of panel.querySelectorAll<HTMLElement>('*')) {
        const box = el.getBoundingClientRect()
        if (!box.width || el.closest('pre, .diff-body, .telemetry-list')) continue
        if (box.right > edge) offenders.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ').join('.')} ${Math.round(box.right - edge)}px`)
      }
    }
    return { page: doc.scrollWidth - innerWidth, offenders: offenders.slice(0, 8) }
  })
  expect(result.page, 'page scrolls sideways').toBeLessThanOrEqual(0)
  expect(result.offenders, 'panel content wider than the panel').toEqual([])
}

let frame: Buffer

test.beforeAll(async ({ browser }) => {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
  await page.setContent(`<body style="margin:0;font:16px -apple-system,'PingFang SC',sans-serif;background:#e9edf2">
    <div style="height:38px;background:#d8dde4;display:flex;align-items:center;gap:8px;padding:0 14px">
      <i style="width:12px;height:12px;border-radius:50%;background:#f87171"></i><i style="width:12px;height:12px;border-radius:50%;background:#fbbf24"></i><i style="width:12px;height:12px;border-radius:50%;background:#34d399"></i>
      <div style="margin-left:18px;flex:1;background:#fff;border-radius:8px;padding:5px 12px;color:#475569">https://focus-mint.local/settings</div></div>
    <div style="padding:48px 64px"><h1 style="margin:0 0 18px;color:#1f2328">Focus Mint · 设置</h1>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:18px">
        ${['专注时长', '休息提醒', '主题颜色', '同步账户', '通知', '快捷键'].map(t => `<div style="background:#fff;border-radius:14px;padding:26px;height:150px;box-shadow:0 1px 2px #0001"><b>${t}</b><p style="color:#64748b">已开启 · 每 25 分钟</p></div>`).join('')}
      </div></div></body>`)
  frame = await page.screenshot({ type: 'jpeg', quality: 70 })
  await page.close()
})

for (const size of WIDTHS) {
  test.describe(`${size.width}px`, () => {
    test.use({ viewport: size })

    test('任务 view: running job with tool events', async ({ page }) => {
      await installApi(page, { job: 'running', takeover: 'disabled', liveScreen: true }, frame)
      await openTasks(page)
      const stream = page.locator('.stream-panel > .event-stream')
      await expect(stream.getByText('Coder 正在处理…')).toBeVisible()
      await expect(stream.getByText('运行命令').first()).toBeVisible()
      await expect(stream.getByText('读取文件')).toBeVisible()
      await expect(stream.getByText('失败', { exact: true })).toBeVisible()
      await expect(stream.getByText('运行中', { exact: true }).first()).toBeVisible()
      // Output stays collapsed until asked for.
      await expect(stream.locator('details.tool-call[open]')).toHaveCount(0)
      await expect(page.getByPlaceholder('描述要做的事…')).toBeVisible()
      await expect(page.getByRole('button', { name: '提问', exact: true })).toHaveAttribute('aria-pressed', 'true')
      await expect(page.getByRole('button', { name: '改代码', exact: true })).toHaveAttribute('aria-pressed', 'false')
      await expect(page.getByRole('button', { name: '改代码', exact: true })).toHaveAttribute('title', /隔离/)
      await expect(page.getByRole('button', { name: '发送', exact: true })).toBeVisible()
      await expect(page.locator('.task-composer-hint')).toBeVisible()
      await expectNoHorizontalOverflow(page)
      await shoot(page, 'tasks-running', size.width)
      await openPanel(page)
      await expect(page.getByRole('button', { name: '停止', exact: true })).toBeEnabled()
      await expect(page.getByText('Pi · DeepSeek')).toHaveCount(0)
      await expect(page.getByText(/TELEMETRY|Cancel|Ask Conveyor/)).toHaveCount(0)
      await expectNoHorizontalOverflow(page)
      await shoot(page, 'tasks-running-panel', size.width)
    })

    test('changes: completed job with three files and the diff open', async ({ page }) => {
      await installApi(page, { job: 'changes', takeover: 'disabled', liveScreen: true }, frame)
      await openTasks(page)
      await openPanel(page)
      const changes = page.locator('.context-section--changes')
      await expect(changes.getByText('3 个文件').first()).toBeVisible()
      await expect(changes.getByText('修改', { exact: true })).toBeVisible()
      await expect(changes.getByText('新增', { exact: true })).toBeVisible()
      await expect(changes.getByText('删除', { exact: true })).toBeVisible()
      await expect(changes.getByRole('button', { name: '应用改动' })).toBeEnabled()
      await expect(changes.getByRole('button', { name: '放弃改动' })).toBeEnabled()
      await expect(page.getByRole('button', { name: '停止', exact: true })).toHaveCount(0)
      await changes.getByText('查看差异').click()
      const body = changes.locator('details.diff-view pre')
      await expect(body).toContainText('暂无改动')
      await expect(changes.locator('.diff-line--add').first()).toBeVisible()
      await expect(changes.locator('.diff-line--del').first()).toBeVisible()
      const box = await body.evaluate(el => ({ height: el.clientHeight, scroll: el.scrollHeight, max: getComputedStyle(el).maxHeight }))
      expect(box.max).not.toBe('none')
      await expectNoHorizontalOverflow(page)
      await changes.scrollIntoViewIfNeeded()
      await shoot(page, 'changes-diff', size.width)
    })

    test('changes: nothing to apply', async ({ page }) => {
      await installApi(page, { job: 'clean', takeover: 'disabled', liveScreen: true }, frame)
      await openTasks(page)
      await openPanel(page)
      const changes = page.locator('.context-section--changes')
      await expect(changes.getByText('暂无改动')).toBeVisible()
      await expect(changes.getByRole('button')).toHaveCount(1) // only the collapse toggle
      // One line, not one word per line.
      const lines = await changes.getByText('暂无改动').evaluate(el => Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight || '20')))
      expect(lines).toBeLessThanOrEqual(1)
      await expectNoHorizontalOverflow(page)
      await shoot(page, 'no-changes', size.width)
    })

    test('approvals: apply and tool approval cards', async ({ page }) => {
      const api = await installApi(page, { job: 'changes', approvals: true, takeover: 'disabled', liveScreen: true }, frame)
      await openTasks(page)
      await openPanel(page)
      const card = page.locator('.approval-card').filter({ hasText: '需要你确认：应用这些改动？' })
      await expect(card).toBeVisible()
      await expect(card).toContainText('q128')
      await expect(card).toContainText('分钟后自动失效')
      await expect(card.getByRole('button', { name: '批准' })).toBeVisible()
      await expect(card.getByRole('button', { name: '拒绝' })).toBeVisible()
      const tool = page.locator('.approval-card').filter({ hasText: 'git push origin feature/changes-panel' })
      await expect(tool).toContainText('需要你确认')
      await expect(page.getByText('APPROVAL REQUIRED')).toHaveCount(0)
      await expectNoHorizontalOverflow(page)
      await card.scrollIntoViewIfNeeded()
      await shoot(page, 'approval', size.width)
      await card.getByRole('button', { name: '拒绝' }).click()
      await expect.poll(() => api.decisions).toContain('ap-1:reject')
    })

    test('refinement chain: notice, cumulative changes, turn labels', async ({ page }) => {
      await installApi(page, { job: 'changes', refinement: true, takeover: 'disabled', liveScreen: true }, frame)
      await openTasks(page)
      const notice = page.locator('.task-notice--sticky')
      await expect(notice).toContainText('改动还没应用')
      await expect(notice).toContainText('已连续修改 2 轮，3 个文件有改动')
      await expect(page.getByPlaceholder('继续说明要怎么改…')).toBeVisible()
      await openPanel(page)
      await expect(page.getByText('修改待应用 · 第 2 轮', { exact: true })).toBeVisible()
      await expect(page.locator('.context-section--changes')).toContainText('改动都累积在同一个隔离副本里')
      await expectNoHorizontalOverflow(page)
      await shoot(page, 'refinement', size.width)
    })

    test('电脑 tab: live screen, host screenshot and takeover', async ({ page }) => {
      await installApi(page, { job: 'clean', takeover: 'idle', liveScreen: true, hostPreview: true }, frame)
      await openTasks(page)
      await openPanel(page)
      await expect(page.locator('.agent-card').getByText('Coder', { exact: true })).toBeVisible()
      await selectTab(page, '电脑')
      const panel = page.locator('.context-panel')
      await expect(panel.getByText('实时画面').first()).toBeVisible()
      await expect(panel.getByRole('button', { name: '紧急停止' })).toBeVisible()
      await expect(panel.getByText('人工接管')).toBeVisible()
      await expect(panel.getByText(/Armed|Disarmed|Emergency stop|Host screen/)).toHaveCount(0)
      await expect(panel.locator('.live-screen-thumb canvas')).toBeVisible()
      await expectNoHorizontalOverflow(page)
      await shoot(page, 'computer', size.width)
    })

    test('任务 view: empty welcome state', async ({ page }) => {
      await installApi(page, { job: 'none', takeover: 'disabled', liveScreen: true }, frame)
      await openTasks(page)
      await expect(page.getByRole('heading', { name: '要 Coder 做什么？' })).toBeVisible()
      await expectNoHorizontalOverflow(page)
      await shoot(page, 'welcome', size.width)
      await openPanel(page)
      await expect(page.locator('.context-section--changes').getByText('暂无改动')).toBeVisible()
      await expect(page.getByText('选一次运行查看详情')).toBeVisible()
      await expect(page.getByText('这个会话还没有运行')).toBeVisible()
      await expect(page.getByText(/Conveyor Control Console|Select a job|No runs/)).toHaveCount(0)
      await expectNoHorizontalOverflow(page)
      await shoot(page, 'welcome-panel', size.width)
    })

    test('takeover disabled: nothing about env vars', async ({ page }) => {
      await installApi(page, { job: 'clean', takeover: 'disabled', liveScreen: false, hostPreview: false }, frame)
      await openTasks(page)
      await selectTab(page, '电脑')
      const panel = page.locator('.context-panel')
      await expect(panel.getByRole('button', { name: '紧急停止' })).toBeVisible()
      await page.waitForTimeout(600) // the takeover panel has polled by now
      await expect(page.getByText(/CONVEYOR_TAKEOVER_ENABLED|Human takeover/)).toHaveCount(0)
      await expect(page.locator('.takeover-context')).toHaveCount(0)
      await selectTab(page, '详情')
      await expect(page.getByText(/CONVEYOR_TAKEOVER_ENABLED/)).toHaveCount(0)
      await expect(page.locator('.agent-instructions')).toBeVisible()
      await expect(page.getByRole('button', { name: '展开' })).toBeVisible()
      await expect(page.getByRole('button', { name: '编辑助手' })).toBeVisible()
      await expectNoHorizontalOverflow(page)
      await selectTab(page, '电脑')
      await shoot(page, 'takeover-disabled', size.width)
    })
  })
}

test('approving Apply reports the result and retires the buttons', async ({ page }) => {
  const api = await installApi(page, { job: 'changes', approvals: true, takeover: 'disabled', liveScreen: true }, frame)
  await openTasks(page)
  await openPanel(page)
  const changes = page.locator('.context-section--changes')
  await expect(changes.getByRole('button', { name: '应用改动' })).toBeDisabled()
  await expect(changes).toContainText('已发出确认，请在上方批准或拒绝。')
  await page.locator('.approval-card').filter({ hasText: '应用这些改动' }).getByRole('button', { name: '批准' }).click()
  await expect(changes.getByRole('status')).toContainText('改动已应用到项目文件夹')
  await expect(changes.getByRole('button', { name: '应用改动' })).toHaveCount(0)
  await expect(page.locator('.approval-card').filter({ hasText: '应用这些改动' })).toHaveCount(0)
  expect(api.decisions).toEqual(['ap-1:approve'])
  await shoot(page, 'applied', 1440)
})

test('open takeover shows on every tab with Chinese controls', async ({ page }) => {
  await installApi(page, { job: 'clean', takeover: 'active', liveScreen: true }, frame)
  await openTasks(page)
  await openPanel(page)
  const card = page.locator('.takeover-active-slot .takeover-context')
  await expect(card).toContainText('隐私模式 · 助手已暂停')
  await expect(card).toContainText('登录')
  await expect(card.getByRole('link', { name: '打开远程桌面 ↗' })).toBeVisible()
  await expect(card.getByRole('button', { name: '完成，交还助手' })).toBeVisible()
  await expect(page.locator('.takeover-privacy-banner')).toContainText('人工接管期间，助手暂停操作电脑。')
  await selectTab(page, '电脑')
  await expect(page.locator('.takeover-context')).toHaveCount(1)
  await shoot(page, 'takeover-active', 1440)
})

test('a remembered token (localStorage only) still loads 人工接管', async ({ page }) => {
  await installApi(page, { job: 'clean', takeover: 'idle', liveScreen: true }, frame, false, true)
  await openTasks(page)
  expect(await page.evaluate(() => sessionStorage.getItem('conveyor-token'))).toBeNull()
  await selectTab(page, '电脑')
  await expect(page.locator('.takeover-slot .takeover-context')).toContainText('我来接手')
})

test('detail tab: instructions clamp, expand and collapse; panel fits 300px', async ({ page }) => {
  await installApi(page, { job: 'changes', takeover: 'disabled', liveScreen: true }, frame)
  await openTasks(page)
  await openPanel(page)
  const text = page.locator('.agent-instructions')
  const clamped = await text.evaluate(el => el.clientHeight)
  await page.getByRole('button', { name: '展开' }).click()
  const open = await text.evaluate(el => el.clientHeight)
  expect(open).toBeGreaterThan(clamped)
  await page.getByRole('button', { name: '收起' }).click()
  await expect.poll(() => text.evaluate(el => el.clientHeight)).toBe(clamped)
  await page.addStyleTag({ content: '.primary-interface aside.context-panel { width: 300px !important; min-width: 300px !important; max-width: 300px !important; }' })
  expect(await page.locator('aside.context-panel').evaluate(el => Math.round(el.getBoundingClientRect().width))).toBe(300)
  await page.locator('.context-section--changes').getByText('查看差异').click()
  await expectNoHorizontalOverflow(page)
  await selectTab(page, '电脑')
  await expectNoHorizontalOverflow(page)
})

test('mobile drawer (mobile UI on) keeps the panel inside the drawer', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await installApi(page, { job: 'changes', approvals: true, takeover: 'disabled', liveScreen: true }, frame, true)
  await openTasks(page)
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeVisible()
  await expectNoHorizontalOverflow(page)
  await shoot(page, 'mobile-ui-tasks', 390)
  await openPanel(page)
  const drawer = page.locator('aside.context-panel')
  await expect(drawer).toBeVisible()
  await expect(drawer.getByText('需要你确认：应用这些改动？')).toBeVisible()
  await expectNoHorizontalOverflow(page)
  await shoot(page, 'mobile-drawer', 390)
})
