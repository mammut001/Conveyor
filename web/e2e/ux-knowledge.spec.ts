import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

// Visual + behaviour check for the knowledge pages: 记忆, 收件箱 (定时任务) and the agent 资料 tab.
// Every API call is mocked with realistic Chinese data; screenshots land in test-results/ux-knowledge/.

const OUT = 'test-results/ux-knowledge'
const TOKEN = 'ux-knowledge-e2e-token-not-a-real-secret-0001'
const NOW = new Date('2026-11-03T14:05:00+08:00')

test.use({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai' })

const agentBase = {
  color: '#bd5c40', instructions: '', workspace_path: '', workspace_status: 'ok', status: 'idle',
  last_message: '', message_count: 0,
}
const agents = [
  { ...agentBase, id: 'default', name: 'Conveyor', is_default: true, session_id: 'web:web-console:agent-default',
    instructions: '你的常驻助手，负责日常事务和提醒。' },
  { ...agentBase, id: 'finance', name: 'Finance', color: '#10b981', is_default: false, session_id: 'web:web-console:agent-finance',
    instructions: '负责账单、记账和每月对账。' },
  { ...agentBase, id: 'researcher', name: 'Researcher', color: '#8b5cf6', is_default: false, session_id: 'web:web-console:agent-researcher' },
  { ...agentBase, id: 'coder', name: 'Coder', color: '#2f7df6', is_default: false, session_id: 'web:web-console:agent-coder' },
]

type MemoryRow = { id: number; kind: 'profile' | 'log'; text: string; created_at: string; source_channel: string }
const sixFacts: MemoryRow[] = [
  { id: 12, kind: 'profile', text: '我习惯用中文交流，回复尽量简洁，先说结论。', created_at: '2026-10-20T01:12:00Z', source_channel: 'web' },
  { id: 13, kind: 'profile', text: '我在上海，工作时间是工作日 9:00 到 18:00。', created_at: '2026-10-28T12:30:00Z', source_channel: 'telegram' },
  { id: 15, kind: 'profile', text: '周末不安排会议，紧急的事情发 Telegram 给我。', created_at: '2026-11-01T03:00:00Z', source_channel: 'telegram' },
  { id: 18, kind: 'log', text: 'Finance 每月 1 号要整理上个月的信用卡账单，按类别汇总。', created_at: '2026-11-02T13:15:00Z', source_channel: 'web' },
  { id: 21, kind: 'log', text: '博客网站改完以后，要先在本地预览，确认没问题再发布。', created_at: '2026-11-03T02:20:00Z', source_channel: 'web' },
  { id: 24, kind: 'log', text: '下周三下午 3 点和房东看房，记得带合同复印件。', created_at: '2026-11-03T05:40:00Z', source_channel: 'telegram' },
]

type Routine = Record<string, unknown> & { id: number; name: string; enabled: boolean }
type InboxRow = Record<string, unknown> & { id: number; read_at: string | null; status: string }

function seedRoutines(): Routine[] {
  return [
    { id: 1, name: '每日邮件摘要', schedule: '0 9 * * *', schedule_cron: '0 9 * * *',
      prompt: '看看我今天新收到的邮件，挑出需要回复的，用几句话总结给我。', deliver: ['web', 'telegram'], enabled: true,
      created_at: '2026-10-25T08:00:00Z', updated_at: '2026-11-03T01:00:40Z', last_run_at: '2026-11-03T01:00:40Z',
      next_run_at: '2026-11-04T01:00:00Z', last_run_status: 'ok', consecutive_failures: 0, hook: null, agent_id: null },
    { id: 2, name: '每周服务器巡检', schedule: '30 8 * * 1', schedule_cron: '30 8 * * 1',
      prompt: '检查服务器的磁盘、内存和任务队列，有异常就列出来，并给出处理建议。', deliver: ['web'], enabled: false,
      created_at: '2026-10-12T08:00:00Z', updated_at: '2026-11-02T00:31:10Z', last_run_at: '2026-11-02T00:31:10Z',
      next_run_at: null, last_run_status: 'approval_pending', consecutive_failures: 0, hook: null, agent_id: null },
  ]
}

function seedInbox(): InboxRow[] {
  return [
    { id: 103, routine_id: 1, routine_name: '每日邮件摘要', started_at: '2026-11-03T01:00:00Z', finished_at: '2026-11-03T01:00:40Z',
      status: 'ok', trigger: 'schedule', read_at: null, approval_id: null, approval: null,
      delivery: { web: 'ok', telegram: 'ok' },
      output: '**今天有 3 封邮件需要回复：**\n\n1. 房东：确认下周三下午 3 点看房。\n2. 招商银行：信用卡账单已出，11 月 20 日前还款。\n3. 同事小王：周报模板有更新，请在周五前提交。\n\n其余 12 封是通知和广告，可以不看。' },
    { id: 102, routine_id: 2, routine_name: '每周服务器巡检', started_at: '2026-11-02T00:30:00Z', finished_at: '2026-11-02T00:31:10Z',
      status: 'approval_pending', trigger: 'schedule', read_at: null, approval_id: 'tok-disk-cleanup',
      approval: { id: 'tok-disk-cleanup', status: 'pending', expires_at: '2026-11-03T08:30:00Z' },
      delivery: { web: 'ok' },
      output: '磁盘使用率 91%，/var/log 占用 18 GB。建议清理 30 天前的日志，需要你批准后执行。' },
    { id: 101, routine_id: 1, routine_name: '每日邮件摘要', started_at: '2026-11-01T01:00:00Z', finished_at: '2026-11-01T01:02:00Z',
      status: 'error', trigger: 'manual', read_at: '2026-11-01T03:00:00Z', approval_id: null, approval: null,
      delivery: { web: 'ok', telegram: 'ok' },
      output: 'Routine execution timed out after 120 seconds.' },
  ]
}

const libraries: Record<string, unknown> = {
  default: { agent_id: 'default', jobs: [], routines: [], memory: { profile: 3, log: 3 }, screenshots: [] },
  finance: {
    agent_id: 'finance',
    memory: { profile: 2, log: 5 },
    routines: [
      { id: 4, name: '每周账单汇总', schedule: '0 9 * * 1', enabled: true, next_run_at: '2026-11-09T01:00:00Z', last_run_at: '2026-11-02T01:00:00Z' },
      { id: 5, name: '月初对账提醒', schedule: '30 8 1 * *', enabled: false, next_run_at: null, last_run_at: '2026-11-01T00:30:00Z' },
    ],
    jobs: [
      { id: 'job-7f3a', state: 'completed', prompt_preview: '把 10 月的信用卡账单整理成表格，按餐饮、交通、购物等类别汇总支出，并标出比上个月多的项目',
        updated_at: '2026-11-02T07:20:00Z', changed_files: [
          { status: 'A', path: 'finance/2026-10/summary.csv' },
          { status: 'M', path: 'finance/README.md' },
          { status: 'M', path: 'scripts/categorize_transactions.py' },
        ] },
      { id: 'job-6c21', state: 'failed', prompt_preview: '删除重复的交易记录', updated_at: '2026-10-30T11:05:00Z',
        changed_files: [{ status: 'D', path: 'finance/2026-09/duplicates.csv' }] },
    ],
    screenshots: [
      { id: 'shot-finance-1', created_at: '2026-11-03T05:10:00Z', width: 1440, height: 900 },
      { id: 'shot-finance-2', created_at: '2026-11-02T09:45:00Z', width: 1440, height: 900 },
    ],
  },
  researcher: { agent_id: 'researcher', jobs: [], routines: [], memory: { profile: 0, log: 0 }, screenshots: [] },
}

const PNG = [
  'iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA/0lEQVR42u3XsRVAMBSG0YyjUJrFLClUJjGJQmUSR2EClc4Gcl7i/uebwC1epK4f1HDJJwAswAIswPoAeMyLGg4wYAEWYAEWYAEWYMACLMCqHfg4LxUIMGDAgAEDBgxYgAVYMYBvKzLAgAEDBgwYMGADbICtBuA8zdUFGDBgwIABe2QZYMCAAQMGDBiw/RF43Xa9AQYMGLAb7AYDBgwYMGDA5j/YOxkwYMCA3WA3GDBgwIANsAE2wIABAwYMGDBgA2yADTBgwIABAwYMGDBgA2yADTBgwIABAwYM2AAbYAMMGDBgwIABAwbcOrDiBxiwAAuwAAuwAAswYAEWYAXoARwfzfpqP/rAAAAAAElFTkSuQmCC',
  'iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA/klEQVR42u3XoQ2AMBRF0Y6DQDIQY1Sg0MyEQDEJQTAAhoBjA5q2nORO0CPeb2jaThUXPAFgARZgAdYHwLGfVHGAAQuwAAuwAAuwAAMWYAFW6cDbfihBgAEDBgwYMGDAAizAygP4vk4lCDBgwIABAwYMWIAFWCUAx2EsLsCAAQMGDNiRJcCAAQMGDBgwYP0ReF5WvQEGDBiwDbbBgAEDBgwYsPyD3cmAAQMGbINtMGDAgAELsAALMGDAgAEDBgxYgAVYgAEDBgwYMGDAgAELsAALMGDAgAEDBgxYgAVYgAEDBgwYMGDAgGsHVv4BBizAAizAAizAAgxYgAVYGfQA4tdnZpnj02oAAAAASUVORK5CYII=',
]

type Options = { memory?: MemoryRow[]; agentId?: string; hash?: string; memoryOff?: boolean; routinesOff?: boolean; agentScreens?: boolean; webhooks?: boolean }

type Api = {
  memoryQueries: string[]
  memoryPosts: unknown[]
  memoryDeletes: number[]
  routinePosts: Record<string, unknown>[]
  routines: Routine[]
  inbox: InboxRow[]
  decisions: string[]
  reads: number[]
}

async function openConsole(page: Page, options: Options = {}): Promise<Api> {
  const memory: Record<string, MemoryRow[]> = { default: [...(options.memory ?? sixFacts)], researcher: [], coder: [],
    finance: [
      { id: 40, kind: 'profile', text: '信用卡账单每月 5 号出，20 号前还款。', created_at: '2026-10-05T02:00:00Z', source_channel: 'web' },
      { id: 41, kind: 'log', text: '10 月餐饮支出比 9 月多了 18%，主要是外卖。', created_at: '2026-11-02T07:20:00Z', source_channel: 'web' },
    ] }
  const api: Api = {
    memoryQueries: [], memoryPosts: [], memoryDeletes: [], routinePosts: [],
    routines: seedRoutines(), inbox: seedInbox(), decisions: [], reads: [],
  }
  let nextMemoryId = 30

  await page.clock.setFixedTime(NOW)
  await page.addInitScript(([token, agent]) => {
    sessionStorage.setItem('conveyor-token', token)
    if (!localStorage.getItem('conveyor-agent')) localStorage.setItem('conveyor-agent', agent)
  }, [TOKEN, options.agentId ?? 'default'])

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    const disabledRoutines = () => json({ error: 'routines are disabled (set CONVEYOR_ROUTINES_ENABLED=true)' }, 409)

    if (path === '/api/health') return json({ ok: true, service: 'conveyor-web', schema_version: 1 })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path.startsWith('/api/sessions/')) return json({
      id: path.slice('/api/sessions/'.length), channel: 'web', title: '', operator_id: 'web-console',
      created_at: '2026-10-01T00:00:00Z', last_activity: '2026-11-03T05:00:00Z', job_count: 0, message_count: 0,
      messages: [], runs: [], jobs: [],
    })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals' && method === 'GET') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: true, agents })
    if (path === '/api/system/status') return json({
      uptime_seconds: 3600, load_average: [0.2, 0.2, 0.1], cpu_count: 4,
      memory: { total: null, available: null }, disk: { total: 1000, used: 400, free: 600 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: true }, feishu: { configured: false } },
      nodes: [],
      features: { agents: true, long_term_memory: true, routines: true, teammate: true, live_screen: true,
        mobile_ui: false, pwa: false, approval_inbox: false, skills: false, workflows: false, mcp: false, webhooks: Boolean(options.webhooks) },
    })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/takeover/status') return json({ available: false, enabled: false })
    if (path === '/api/agent-screens/config') return options.agentScreens
      ? json({ enabled: true, inbox: false, tasks: false }) : json({ error: 'not found' }, 404)
    if (path === '/api/agent-conversations') {
      const agent = agents.find(item => item.id === url.searchParams.get('agent_id')) ?? agents[0]
      return json({ conversations: [{ id: agent.id, title: 'Web 原有会话', origin_channel: 'web', session_id: agent.session_id }] })
    }
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })

    // ---- 记忆
    if (path === '/api/memory' || path.startsWith('/api/memory/')) {
      if (options.memoryOff) return json({ error: 'memory is disabled (set CONVEYOR_MEMORY_V2=1 or CONVEYOR_LONG_TERM_MEMORY=true)' }, 409)
      const owner = url.searchParams.get('agent') || 'default'
      const rows = memory[owner] ?? []
      if (path === '/api/memory' && method === 'GET') {
        api.memoryQueries.push(url.search)
        const kind = url.searchParams.get('kind')
        const q = (url.searchParams.get('q') || '').toLowerCase()
        const terms = q.split(/\s+/).filter(Boolean)
        const items = rows
          .filter(row => !kind || row.kind === kind)
          .filter(row => !terms.length || terms.some(term => row.text.toLowerCase().includes(term)))
          .sort((a, b) => b.created_at.localeCompare(a.created_at))
          .map(row => ({ ...row, day: row.created_at.slice(0, 10), updated_at: row.created_at }))
        return json({
          items,
          counts: { profile: rows.filter(row => row.kind === 'profile').length, log: rows.filter(row => row.kind === 'log').length },
          shared: true, profile_cap: 8,
        })
      }
      if (path === '/api/memory' && method === 'POST') {
        const body = request.postDataJSON() as { text: string; agent?: string }
        api.memoryPosts.push(body)
        const target = memory[body.agent || 'default']
        const row: MemoryRow = { id: nextMemoryId++, kind: target.filter(item => item.kind === 'profile').length < 8 ? 'profile' : 'log',
          text: body.text, created_at: NOW.toISOString(), source_channel: 'web' }
        target.push(row)
        return json({ ok: true, ...row, day: '2026-11-03', updated_at: row.created_at }, 201)
      }
      const match = /^\/api\/memory\/(\d+)$/.exec(path)
      if (match && method === 'DELETE') {
        const id = Number(match[1])
        api.memoryDeletes.push(id)
        const index = rows.findIndex(row => row.id === id)
        if (index < 0) return json({ error: 'memory not found' }, 404)
        const [gone] = rows.splice(index, 1)
        return json({ ok: true, deleted: gone, purge_pending: false })
      }
    }

    // ---- 定时任务 + 收件箱
    if (path === '/api/routines' || path.startsWith('/api/routines/')) {
      if (options.routinesOff) return disabledRoutines()
      if (path === '/api/routines' && method === 'GET') return json({ routines: api.routines })
      if (path === '/api/routines' && method === 'POST') {
        const body = request.postDataJSON() as Record<string, unknown>
        api.routinePosts.push(body)
        if (!String(body.name || '').trim()) return json({ error: 'name must be 1-80 characters' }, 400)
        if (String(body.schedule || '').trim().split(/\s+/).length !== 5) {
          return json({ error: 'cron expression must have exactly 5 fields (minute hour day-of-month month day-of-week), got 3' }, 400)
        }
        const routine: Routine = { id: 9, name: String(body.name), schedule: String(body.schedule), schedule_cron: String(body.schedule),
          prompt: String(body.prompt), deliver: body.deliver as string[], enabled: true, created_at: NOW.toISOString(), updated_at: NOW.toISOString(),
          last_run_at: null, next_run_at: '2026-11-09T00:30:00Z', last_run_status: null, consecutive_failures: 0, hook: null, agent_id: null }
        api.routines.push(routine)
        return json(routine, 201)
      }
      const action = /^\/api\/routines\/(\d+)\/(pause|resume|run)$/.exec(path)
      if (action && method === 'POST') {
        const routine = api.routines.find(item => item.id === Number(action[1]))
        if (!routine) return json({ error: 'routine not found' }, 404)
        if (action[2] !== 'run') {
          routine.enabled = action[2] === 'resume'
          routine.next_run_at = routine.enabled ? '2026-11-09T00:30:00Z' : routine.next_run_at
          return json({ ok: true, routine })
        }
        const run: InboxRow = { id: 200 + api.inbox.length, routine_id: routine.id, routine_name: routine.name,
          started_at: NOW.toISOString(), finished_at: NOW.toISOString(), status: 'ok', trigger: 'manual', read_at: null,
          approval_id: null, approval: null, delivery: { web: 'ok' }, output: '手动运行完成：今天没有新的待回复邮件。' }
        api.inbox.unshift(run)
        routine.last_run_at = NOW.toISOString()
        routine.last_run_status = 'ok'
        return json(run)
      }
      const hook = /^\/api\/routines\/(\d+)\/hook$/.exec(path)
      if (hook && method === 'POST') {
        const routine = api.routines.find(item => item.id === Number(hook[1]))
        if (!routine) return json({ error: 'routine not found' }, 404)
        routine.hook = { hook_id: 'hk_new', created_at: NOW.toISOString(), last_fired_at: null, fire_count: 0 }
        return json({ hook_id: 'hk_new', secret: 'whsec_test_only_not_real_0000', path: '/api/hooks/hk_new' }, 201)
      }
      const remove = /^\/api\/routines\/(\d+)$/.exec(path)
      if (remove && method === 'DELETE') {
        api.routines = api.routines.filter(item => item.id !== Number(remove[1]))
        return json({ ok: true })
      }
    }
    if (path === '/api/inbox' || path.startsWith('/api/inbox/')) {
      if (options.routinesOff) return disabledRoutines()
      if (path === '/api/inbox' && method === 'GET') {
        return json({ items: api.inbox, unread: api.inbox.filter(item => !item.read_at).length })
      }
      if (path === '/api/inbox/read-all' && method === 'POST') {
        api.inbox.forEach(item => { item.read_at = item.read_at || NOW.toISOString() })
        return json({ ok: true })
      }
      const read = /^\/api\/inbox\/(\d+)\/read$/.exec(path)
      if (read && method === 'POST') {
        api.reads.push(Number(read[1]))
        const item = api.inbox.find(row => row.id === Number(read[1]))
        if (item) item.read_at = NOW.toISOString()
        return json({ ok: Boolean(item) })
      }
    }
    const decision = /^\/api\/approvals\/([^/]+)\/(approve|reject)$/.exec(path)
    if (decision && method === 'POST') {
      api.decisions.push(`${decision[1]}:${decision[2]}`)
      const item = api.inbox.find(row => (row.approval as { id?: string } | null)?.id === decision[1])
      if (item) {
        const approved = decision[2] === 'approve'
        item.approval = { id: decision[1], status: approved ? 'approved' : 'denied' }
        item.status = approved ? 'executed' : 'denied'
      }
      return json({ ok: true })
    }

    // ---- 资料
    const library = /^\/api\/agents\/([^/]+)\/library$/.exec(path)
    if (library) return libraries[library[1]] ? json(libraries[library[1]]) : json({ error: 'not found' }, 404)
    const shot = /^\/api\/agents\/([^/]+)\/screenshots\/([^/]+)$/.exec(path)
    if (shot) return route.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from(PNG[shot[2].endsWith('2') ? 1 : 0], 'base64') })

    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })

  await page.goto(`/${options.hash ?? ''}`)
  return api
}

async function openInbox(page: Page) {
  // The nav belongs to the shell; accept its old English or new Chinese label.
  await page.locator('button, a, [role="tab"]').filter({ hasText: /^\s*(Inbox|收件箱)/ }).first().click()
  await expect(page.locator('.kn-inbox')).toBeVisible()
}

async function noHorizontalScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
}

const ALLOWED_LATIN = new Set(['conveyor', 'telegram', 'webhook', 'cron', 'curl', 'github', 'finance', 'researcher', 'coder', 'var', 'log'])

/** Latin words in the UI text that are not product names or mock data (code, paths and raw logs excluded). */
async function englishLeftovers(locator: Locator) {
  const text = await locator.evaluate(element => {
    const copy = element.cloneNode(true) as HTMLElement
    copy.querySelectorAll('code, pre, input, textarea, select, script, style').forEach(node => node.remove())
    const walker = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT)
    const parts: string[] = []
    while (walker.nextNode()) parts.push(walker.currentNode.textContent || '')
    return parts.join(' ')
  })
  return (text.match(/[A-Za-z][A-Za-z_-]{2,}/g) || []).filter(word => !ALLOWED_LATIN.has(word.toLowerCase()))
}

async function shoot(page: Page, view: string, width: number, content?: Locator) {
  await page.setViewportSize({ width, height: width > 600 ? 900 : 844 })
  await page.waitForTimeout(250)
  await noHorizontalScroll(page)
  await page.screenshot({ path: `${OUT}/${view}-${width}.png`, fullPage: true })
  if (content) {
    // A tall window shows the whole panel (it scrolls inside the page) for review.
    await page.setViewportSize({ width, height: 2200 })
    await page.waitForTimeout(150)
    await content.screenshot({ path: `${OUT}/${view}-${width}-content.png` })
  }
}

test('记忆: empty state teaches how to add, per-agent wording', async ({ page }) => {
  await openConsole(page, { memory: [], hash: '#memory' })
  const panel = page.locator('.kn-memory')
  // One title per page: the page header says 记忆; the panel does not repeat it.
  await expect(page.locator('.stream-header h2')).toHaveText('记忆')
  await expect(panel.getByRole('heading')).toHaveCount(0)
  await expect(panel.getByText('网页、Telegram 和飞书共用这份记忆。')).toBeVisible()
  await expect(panel.getByText(/在对话里说“记住……”也能添加，需要你确认；不会保存密码和密钥/)).toBeVisible()
  await expect(panel.getByText('还没有记忆。')).toBeVisible()
  await expect(panel.getByRole('button', { name: '记住' })).toBeDisabled()
  await expect(panel.getByText('一次一句话 · 0 / 280')).toBeVisible()
  // Nothing to filter or search yet, so the toolbar stays out of the way.
  await expect(panel.getByRole('searchbox')).toHaveCount(0)
  expect(await englishLeftovers(panel)).toEqual([])
  await shoot(page, 'memory-empty', 1440)
  await shoot(page, 'memory-empty', 390)

  // A non-default agent's own store says so.
  await page.evaluate(() => localStorage.setItem('conveyor-agent', 'researcher'))
  await page.reload()
  await expect(page.locator('.stream-header .shell-subtitle')).toHaveText('Researcher 长期记住的信息')
  await expect(panel.getByText('这是 Researcher 自己的记忆，其他助手看不到。')).toBeVisible()
})

test('记忆: list, add with a counter, search as you type, delete with confirmation', async ({ page }) => {
  const api = await openConsole(page, { hash: '#memory' })
  const panel = page.locator('.kn-memory')
  const items = panel.locator('.kn-memory-item')
  await expect(items).toHaveCount(6)
  await expect(items.first()).toContainText('下周三下午 3 点和房东看房')
  await expect(items.first()).toContainText('25 分钟前')
  await expect(items.first()).toContainText('来自 Telegram')
  await expect(items.nth(1)).toContainText('今天 10:20')
  await expect(items.last()).toContainText('10月20日 09:12')
  await expect(panel.getByRole('button', { name: /^全部/ })).toContainText('6')
  await expect(panel.getByRole('button', { name: /^全部/ })).toHaveAttribute('aria-pressed', 'true')
  await expect(panel.getByRole('button', { name: /^常用档案/ })).toContainText('3')
  await expect(panel.getByText(/最先记住的 8 条是常用档案/)).toBeVisible()
  expect(await englishLeftovers(panel)).toEqual([])
  await shoot(page, 'memory-list', 1440, panel)
  await shoot(page, 'memory-list', 390, panel)
  await page.setViewportSize({ width: 1440, height: 900 })

  // Add one fact: counter, Enter to save, confirmation.
  const box = panel.getByLabel('添加一条记忆')
  await box.fill('我喜欢喝无糖的冰美式。')
  await expect(panel.getByText('一次一句话 · 11 / 280')).toBeVisible()
  await box.press('Enter')
  await expect(panel.getByText('已记住。')).toBeVisible()
  expect(api.memoryPosts).toEqual([{ text: '我喜欢喝无糖的冰美式。' }])
  await expect(items).toHaveCount(7)
  await expect(box).toHaveValue('')

  // Search as you type (no button needed).
  await panel.getByRole('searchbox', { name: '搜索记忆' }).fill('看房')
  await expect(panel.getByText('找到 1 条和“看房”相关的记忆')).toBeVisible()
  await expect(items).toHaveCount(1)
  expect(api.memoryQueries.some(query => new URLSearchParams(query).get('q') === '看房')).toBe(true)
  await shoot(page, 'memory-search', 1440)
  await shoot(page, 'memory-search', 390)
  await page.setViewportSize({ width: 1440, height: 900 })
  await panel.getByRole('button', { name: '清除搜索' }).click()
  await expect(items).toHaveCount(7)

  // Filters are Chinese and real buttons.
  await panel.getByRole('button', { name: /^记忆日志/ }).click()
  await expect(items).toHaveCount(3)
  await panel.getByRole('button', { name: /^全部/ }).click()

  // Delete asks first and says what it does.
  await panel.getByRole('button', { name: /^删除这条记忆：博客网站/ }).click()
  await expect(panel.getByText('删除后无法恢复')).toBeVisible()
  await panel.getByRole('button', { name: '确定删除' }).click()
  await expect(items).toHaveCount(6)
  expect(api.memoryDeletes).toEqual([21])
})

test('收件箱: routines in words, unread items, approvals', async ({ page }) => {
  const api = await openConsole(page)
  await openInbox(page)
  const panel = page.locator('.kn-inbox')
  const routines = panel.locator('.kn-routine')
  await expect(routines).toHaveCount(2)
  await expect(panel.getByRole('heading', { name: /^定时任务/ })).toContainText('2')
  await expect(routines.first()).toContainText('每日邮件摘要')
  await expect(routines.first()).toContainText('每天 09:00')
  await expect(routines.first()).toContainText('下次 明天 09:00')
  await expect(routines.first()).toContainText('同时发到 Telegram')
  await expect(routines.nth(1)).toContainText('每周一 08:30')
  await expect(routines.nth(1)).toContainText('已暂停')
  await expect(routines.nth(1)).toContainText('上次 昨天 08:31 · 等你批准')
  await expect(routines.nth(1).getByRole('button', { name: '恢复' })).toBeVisible()

  const inbox = panel.locator('.kn-inbox-item')
  await expect(inbox).toHaveCount(3)
  await expect(panel.getByRole('heading', { name: /^运行结果/ })).toContainText('2 条未读')
  await expect(inbox.first()).toHaveClass(/is-unread/)
  await expect(inbox.first()).toContainText('已完成')
  await expect(inbox.first()).toContainText('今天 09:00')
  await expect(inbox.first()).toContainText('今天有 3 封邮件需要回复')
  await expect(inbox.first()).toContainText('已发到 Telegram')
  await expect(inbox.nth(1)).toContainText('等你批准')
  await expect(inbox.nth(1)).toContainText('需要你批准才能继续。')
  await expect(inbox.nth(1)).toContainText('11月3日 16:30 前有效')
  await expect(inbox.nth(2)).toContainText('失败')
  await expect(inbox.nth(2)).toContainText('运行超时，已自动停止。')
  await expect(inbox.nth(2)).toContainText('手动运行')
  await expect(inbox.nth(2)).not.toHaveClass(/is-unread/)
  expect(await englishLeftovers(panel)).toEqual([])
  await shoot(page, 'inbox', 1440, panel)
  await shoot(page, 'inbox', 390, panel)
  await page.setViewportSize({ width: 1440, height: 900 })

  // Mark one read with a real button.
  await inbox.first().getByRole('button', { name: '标为已读' }).click()
  await expect(panel.getByRole('heading', { name: /^运行结果/ })).toContainText('1 条未读')
  expect(api.reads).toEqual([103])

  // Approve: the decision paints and the run reads as executed.
  await inbox.nth(1).getByRole('button', { name: '批准', exact: true }).click()
  await expect(inbox.nth(1)).toContainText('已批准并执行')
  expect(api.decisions).toEqual(['tok-disk-cleanup:approve'])

  // Delete asks first and explains the consequence.
  await routines.nth(1).getByRole('button', { name: '删除定时任务：每周服务器巡检' }).click()
  await expect(routines.nth(1)).toContainText('删除后不再运行，没处理的批准也会作废。')
  await routines.nth(1).getByRole('button', { name: '确定删除' }).click()
  await expect(routines).toHaveCount(1)
  await expect(panel.getByText('已删除定时任务“每周服务器巡检”。')).toBeVisible()

  // Run now waits for the result and says where it went.
  await routines.first().getByRole('button', { name: '立即运行' }).click()
  await expect(panel.getByText('“每日邮件摘要”已运行完，结果已放进收件箱。')).toBeVisible()
  await expect(inbox).toHaveCount(4)
  await expect(routines.first()).toContainText('上次 刚刚 · 已完成')
  await panel.getByRole('button', { name: '查看收件箱' }).click()
  await expect(inbox.first()).toBeInViewport()
})

test('收件箱: the new-routine form explains the schedule with presets', async ({ page }) => {
  const api = await openConsole(page)
  await openInbox(page)
  const panel = page.locator('.kn-inbox')
  await panel.getByRole('button', { name: '＋ 新建定时任务' }).click()
  const form = panel.getByRole('form', { name: '新建定时任务' })
  await expect(form).toBeVisible()
  await expect(form.getByLabel('名称')).toBeFocused()
  const cron = form.getByRole('textbox', { name: /cron 表达式/ })
  await expect(cron).toHaveValue('0 9 * * *')
  await expect(form.getByText('每天 09:00 运行（服务器时间）')).toBeVisible()
  await expect(form.getByRole('button', { name: '每天', exact: true })).toHaveAttribute('aria-pressed', 'true')

  await form.getByRole('button', { name: '每周一', exact: true }).click()
  await expect(cron).toHaveValue('0 9 * * 1')
  await form.getByLabel('小时').selectOption('8')
  await form.getByLabel('分钟').selectOption('30')
  await expect(cron).toHaveValue('30 8 * * 1')
  await expect(form.getByText('每周一 08:30 运行（服务器时间）')).toBeVisible()
  await form.getByRole('button', { name: '每小时', exact: true }).click()
  await expect(cron).toHaveValue('0 * * * *')
  await expect(form.getByText('每小时整点运行（服务器时间）')).toBeVisible()
  await expect(form.getByLabel('小时')).toBeDisabled()
  // The time picked before 每小时 is kept.
  await form.getByRole('button', { name: '工作日', exact: true }).click()
  await expect(cron).toHaveValue('30 8 * * 1-5')
  await expect(form.getByText('工作日 08:30 运行（服务器时间）')).toBeVisible()

  await expect(form.getByLabel(/飞书/)).toBeDisabled()
  await expect(form.getByText('飞书（未连接）')).toBeVisible()
  await form.getByLabel('名称').fill('工作日早报')
  await form.getByLabel('要做的事').fill('总结今天的日程和待回复的邮件，提醒我最重要的三件事。')
  await form.getByLabel('Telegram').check()
  expect(await englishLeftovers(form)).toEqual([])
  await shoot(page, 'inbox-form', 1440, panel)
  await shoot(page, 'inbox-form', 390, panel)
  await page.setViewportSize({ width: 1440, height: 900 })

  // The raw field stays editable and says when it is wrong.
  await cron.fill('0 9 *')
  await expect(form.getByText('cron 需要 5 段，用空格分开：分 时 日 月 周，例如 0 9 * * *')).toBeVisible()
  await form.getByRole('button', { name: '工作日', exact: true }).click()
  await expect(cron).toHaveValue('30 8 * * 1-5')

  await form.getByRole('button', { name: '创建定时任务' }).click()
  await expect(panel.getByText('已创建定时任务“工作日早报”，下次运行：11月9日 08:30。')).toBeVisible()
  expect(api.routinePosts).toEqual([{ name: '工作日早报', schedule: '30 8 * * 1-5',
    prompt: '总结今天的日程和待回复的邮件，提醒我最重要的三件事。', deliver: ['web', 'telegram'] }])
  await expect(panel.locator('.kn-routine')).toHaveCount(3)
  await expect(panel.locator('.kn-routine').last()).toContainText('工作日 08:30')
})

test('server errors become short Chinese without config hints', async ({ page }) => {
  await openConsole(page, { memoryOff: true, routinesOff: true, hash: '#memory' })
  await expect(page.locator('.kn-memory')).toContainText('记忆功能未开启。')
  await openInbox(page)
  await expect(page.locator('.kn-inbox')).toContainText('定时任务功能未开启。')
  await expect(page.getByText(/CONVEYOR_/)).toHaveCount(0)

  // A validation error from the routine API is translated.
  const api = await openConsole(page)
  await openInbox(page)
  const panel = page.locator('.kn-inbox')
  await panel.getByRole('button', { name: '＋ 新建定时任务' }).click()
  const form = panel.getByRole('form', { name: '新建定时任务' })
  await expect(form.getByRole('button', { name: '创建定时任务' })).toBeEnabled()
  await form.getByRole('button', { name: '创建定时任务' }).click()
  await expect(form.getByText('请填写名称')).toBeVisible()
  await expect(form.getByLabel('名称')).toBeFocused()
  await form.getByLabel('名称').fill('喝水提醒')
  await form.getByRole('button', { name: '创建定时任务' }).click()
  await expect(form.getByText('请写下要做的事')).toBeVisible()
  await expect(form.getByLabel('要做的事')).toBeFocused()
  await form.getByLabel('要做的事').fill('每天提醒我喝水。')
  await page.route('**/api/routines', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'prompt must be 1-2000 characters' }) })
    : route.fallback())
  await form.getByRole('button', { name: '创建定时任务' }).click()
  await expect(form.getByRole('alert')).toContainText('要做的事需要 1–2000 个字')
  expect(api.routinePosts).toEqual([])
})

test('资料 tab: Chinese sections that fit the right panel', async ({ page }) => {
  await openConsole(page, { agentId: 'finance' })
  await page.getByRole('tab', { name: /^(Library|资料)$/ }).click()
  const library = page.locator('.kn-library')
  await expect(library).toBeVisible()
  await expect(library.getByRole('heading', { name: '记忆' })).toBeVisible()
  await expect(library.getByRole('heading', { name: /^定时任务/ })).toBeVisible()
  await expect(library.getByRole('heading', { name: '改过的文件' })).toBeVisible()
  await expect(library.getByRole('heading', { name: '电脑截图' })).toBeVisible()
  await expect(library).toContainText('2 条常用档案')
  await expect(library).toContainText('每周账单汇总')
  await expect(library).toContainText('每周一 09:00 · 下次 11月9日 09:00')
  await expect(library).toContainText('月初对账提醒已暂停')
  await expect(library).toContainText('已完成 · 昨天 15:20 · 3 个文件')
  await expect(library.locator('.kn-file-status')).toHaveText(['新增', '修改', '修改', '删除'])
  await expect(library.locator('img')).toHaveCount(2)
  expect(await englishLeftovers(library)).toEqual([])
  const section = page.locator('.context-section').filter({ has: library })
  const box = await section.boundingBox()
  expect(box?.width ?? 0).toBeGreaterThan(280)
  await shoot(page, 'library', 1440)
  await section.screenshot({ path: `${OUT}/library-panel-1440.png` })
  // On phones the right panel is the 详情 drawer, opened from the page header; 资料 stays selected.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '详情', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('tab', { name: '资料' })).toHaveAttribute('aria-selected', 'true')
  await expect(library).toBeVisible()
  await expect.poll(() => drawer.evaluate(element => Math.round(element.getBoundingClientRect().right))).toBe(390)
  await shoot(page, 'library', 390)
  await section.scrollIntoViewIfNeeded()
  await section.screenshot({ path: `${OUT}/library-panel-390.png` })
  expect(await section.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1)

  // An agent with nothing yet gets one helpful sentence.
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.locator('.agent-list button.agent-item').filter({ hasText: 'Researcher' }).click()
  await page.getByRole('tab', { name: /^(Library|资料)$/ }).click()
  await expect(page.getByText('还没有资料。')).toBeVisible()
  await expect(page.getByText('这个助手改过的文件、它的定时任务、记住的内容和电脑截图，都会汇总在这里。')).toBeVisible()
  await page.locator('.context-section').filter({ has: page.locator('.kn-library-empty') }).screenshot({ path: `${OUT}/library-empty-1440.png` })
})

test('记忆 and 收件箱 for a selected agent', async ({ page }) => {
  const api = await openConsole(page, { agentId: 'finance', agentScreens: true })
  // A selected agent's memory is the top-level 记忆 page (the conversation has no second tab row).
  await expect(page.locator('.agent-screen-tabs')).toHaveCount(0)
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '记忆', exact: true }).click()
  await expect(page).toHaveURL(/#memory$/)
  const panel = page.locator('.kn-memory')
  await expect(page.locator('.stream-header .shell-subtitle')).toHaveText('Finance 长期记住的信息')
  await expect(panel.getByText('Finance 的记忆')).toHaveCount(0)
  await expect(panel.getByText('这是 Finance 自己的记忆，其他助手看不到。')).toBeVisible()
  await expect(panel.getByText(/Finance 会在之后的对话里参考这些内容/)).toBeVisible()
  await expect(panel.locator('.kn-memory-item')).toHaveCount(2)
  expect(api.memoryQueries.length).toBeGreaterThan(0)
  expect(api.memoryQueries.every(query => new URLSearchParams(query).get('agent') === 'finance')).toBe(true)
  await shoot(page, 'memory-agent', 1440)
  await shoot(page, 'memory-agent', 390)
  await page.setViewportSize({ width: 1440, height: 900 })

  // New routines made while an agent is selected belong to it.
  await openInbox(page)
  const inbox = page.locator('.kn-inbox')
  await expect(inbox.getByText('新建的定时任务由 Finance 执行，结果放进收件箱，也会发到和 Finance 的对话里。')).toBeVisible()
  await inbox.getByRole('button', { name: '＋ 新建定时任务' }).click()
  const form = inbox.getByRole('form', { name: '新建定时任务' })
  await expect(form.getByText('由 Finance 按你写的内容执行，不会自动改代码或执行命令。')).toBeVisible()
  await form.getByLabel('名称').fill('每周账单提醒')
  await form.getByLabel('要做的事').fill('列出本周要还的账单和金额。')
  await form.getByRole('button', { name: '每周一', exact: true }).click()
  await form.press('Escape')
  await expect(form).toHaveCount(0)
  await expect(inbox.getByRole('button', { name: '＋ 新建定时任务' })).toBeFocused()
  expect(api.routinePosts).toEqual([])
})

test('收件箱 webhooks (when enabled) are Chinese and confirm before rotating', async ({ page }) => {
  await openConsole(page, { webhooks: true })
  await openInbox(page)
  const panel = page.locator('.kn-inbox')
  const routine = panel.locator('.kn-routine').first()
  await routine.getByRole('button', { name: '创建 Webhook' }).click()
  const card = panel.getByRole('region', { name: 'Webhook 信息' })
  await expect(card).toContainText('“每日邮件摘要”的 Webhook 信息')
  await expect(card).toContainText('密钥只显示这一次，请现在复制保存。')
  await expect(card.getByRole('button', { name: '复制密钥' })).toBeVisible()
  await expect(routine).toContainText('Webhook 已触发 0 次')
  await routine.getByRole('button', { name: '更换密钥' }).click()
  await expect(routine).toContainText('旧密钥会立即失效')
  await expect(routine.getByRole('button', { name: '确定更换' })).toBeVisible()
  await routine.getByRole('button', { name: '取消' }).click()
  expect(await englishLeftovers(panel)).toEqual([])
})
