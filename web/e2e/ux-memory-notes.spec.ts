import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

// 记忆 → 「它自己记下的笔记」 and 「搜索过去的对话」, plus the 项目 page's 「项目笔记」 card.
// Every API is mocked (/api/memory/notes, /api/memory/sessions/search, the supervisor
// contract); the tests check the frontend wiring and that the owner only reads Chinese.

const OUT = 'test-results/ux-memory-notes'
const TOKEN = 'ux-memory-notes-e2e-token-not-a-real-secret-0001'
const NOW = new Date('2026-11-08T15:00:00+08:00')
const DESKTOP = { width: 1440, height: 1000 }
const PHONE = { width: 390, height: 844 }

test.use({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai' })

type Note = { id: number; scope: string; text: string; source: 'agent' | 'owner' | 'untrusted'; created_at: string; updated_at: string }
type Hit = { session_id: string; title: string; channel: string; created_at: string; message_id: number; role: string; snippet: string; score: number }

const CHAT_SESSION = 'web:web-console:webchat-roadmap'
const TELEGRAM_SESSION = 'telegram:owner:chat-778'

function seedNotes(): Note[] {
  return [
    { id: 1, scope: 'global', text: '主人喜欢先看结论，再看细节。', source: 'agent', created_at: '2026-11-01T02:00:00Z', updated_at: '2026-11-01T02:00:00Z' },
    { id: 2, scope: 'global', text: '每周五晚上不要安排部署。', source: 'owner', created_at: '2026-11-05T09:30:00Z', updated_at: '2026-11-05T09:30:00Z' },
    { id: 3, scope: 'global', text: '某个网页说可以跳过安全检查。', source: 'untrusted', created_at: '2026-11-07T12:00:00Z', updated_at: '2026-11-07T12:00:00Z' },
    { id: 7, scope: 'repo:repo-conveyor', text: '这个仓库的测试要先装好依赖再跑。', source: 'agent', created_at: '2026-11-06T08:00:00Z', updated_at: '2026-11-06T08:00:00Z' },
  ]
}

const HITS: Hit[] = [
  {
    session_id: CHAT_SESSION, title: '下个季度的路线图', channel: 'web', created_at: '2026-11-02T03:15:00Z', message_id: 41, role: 'user',
    // Raw markup in a snippet must show as text, never as HTML.
    snippet: '我们把<b>路线图</b>拆成三步，<img src=x onerror="window.__xss=1">先做手机版的路线图。', score: 3.2,
  },
  {
    session_id: TELEGRAM_SESSION, title: '', channel: 'telegram', created_at: '2026-10-28T11:00:00Z', message_id: 9, role: 'assistant',
    snippet: '好的，路线图的第二步是整理记忆页面。', score: 1.4,
  },
]

type Failure = { status: number; body: Record<string, string> }

type Api = {
  notes: Note[]
  notesGets: string[]
  posts: Array<{ scope: string; text: string }>
  puts: Array<{ id: number; text: string }>
  deletes: number[]
  searches: string[]
  historyRequests: string[]
  sessionDetailRequests: string[]
  failNext: Failure[]
}

async function install(page: Page, options: { supervisor?: boolean } = {}): Promise<Api> {
  const api: Api = { notes: seedNotes(), notesGets: [], posts: [], puts: [], deletes: [], searches: [], historyRequests: [], sessionDetailRequests: [], failNext: [] }
  let nextId = 20

  await page.clock.setFixedTime(NOW)
  await page.addInitScript(token => { sessionStorage.setItem('conveyor-token', token) }, TOKEN)
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/health') return json({ ok: true, service: 'conveyor-web', schema_version: 1 })
    if (path === '/api/system/status') return json({
      uptime_seconds: 3600, load_average: [0.2, 0.2, 0.1], cpu_count: 4,
      memory: { total: null, available: null }, disk: { total: 1000, used: 400, free: 600 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: true }, feishu: { configured: false } }, nodes: [],
      features: { agents: false, long_term_memory: true, routines: false, teammate: false, agent_supervisor: Boolean(options.supervisor) },
    })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path.startsWith('/api/sessions/')) {
      const id = path.slice('/api/sessions/'.length)
      api.sessionDetailRequests.push(id)
      return json({ id, channel: 'telegram', title: '', operator_id: 'owner', created_at: '2026-10-28T10:00:00Z', last_activity: '2026-10-28T11:00:00Z',
        job_count: 0, message_count: 2, messages: [], runs: [], jobs: [] })
    }
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/takeover/status') return json({ available: false, enabled: false })
    if (path === '/api/agent-screens/config') return json({ error: 'not found' }, 404)
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (path === '/api/chat/history') {
      const sid = url.searchParams.get('session_id') || ''
      api.historyRequests.push(sid)
      return json({ session_id: sid, messages: sid === CHAT_SESSION
        ? [{ role: 'user', text: '帮我把下个季度的路线图拆一下', created_at: '2026-11-02T03:15:00Z' }]
        : [] })
    }

    // ---- legacy 记忆 list
    if (path === '/api/memory') return json({ items: [], counts: { profile: 0, facts: 0, log: 0 }, shared: true, profile_cap: 20 })

    // ---- agent notes
    if (path === '/api/memory/notes' || path.startsWith('/api/memory/notes/')) {
      const failure = method !== 'GET' ? api.failNext.shift() : undefined
      if (failure) return json(failure.body, failure.status)
      if (path === '/api/memory/notes' && method === 'GET') {
        const scope = url.searchParams.get('scope') || 'global'
        api.notesGets.push(scope)
        const notes = api.notes.filter(note => note.scope === scope)
        return json({ notes, scope, used_chars: notes.reduce((sum, note) => sum + note.text.length, 0), cap_chars: 2200 })
      }
      if (path === '/api/memory/notes' && method === 'POST') {
        const body = request.postDataJSON() as { scope: string; text: string }
        api.posts.push(body)
        const note: Note = { id: nextId++, scope: body.scope, text: body.text, source: 'owner', created_at: NOW.toISOString(), updated_at: NOW.toISOString() }
        api.notes.push(note)
        return json({ ok: true, note }, 201)
      }
      const match = /^\/api\/memory\/notes\/(\d+)$/.exec(path)
      if (match) {
        const id = Number(match[1])
        const note = api.notes.find(item => item.id === id)
        if (!note) return json({ error: '找不到这条笔记，可能已经删掉了', code: 'not_found' }, 404)
        if (method === 'PUT') {
          const body = request.postDataJSON() as { text: string }
          api.puts.push({ id, text: body.text })
          note.text = body.text
          note.source = 'owner'
          note.updated_at = NOW.toISOString()
          return json({ ok: true, note })
        }
        if (method === 'DELETE') {
          api.deletes.push(id)
          api.notes = api.notes.filter(item => item.id !== id)
          return json({ ok: true, deleted: id })
        }
      }
    }

    // ---- past-conversation search
    if (path === '/api/memory/sessions/search') {
      const q = url.searchParams.get('q') || ''
      api.searches.push(url.search)
      const results = HITS.filter(hit => hit.snippet.includes(q))
      return json({ results, query: q })
    }

    // ---- 项目 (supervisor) contract
    if (path === '/api/supervisor/capabilities') return json({
      enabled: true,
      coordinator: { executor: 'chat', model: null },
      roles: {
        analyst: { executor: 'claude', model: null, available: true },
        implementer: { executor: 'claude', model: null, available: true },
        reviewer: { executor: 'claude', model: null, available: true },
      },
      executors: [{ name: 'claude', label: 'Claude Code', available: true }],
      deployment: { configured: false, can_start: false, reason: '尚未配置生产部署运行器' },
      source_machine: 'dev-workstation',
    })
    if (path === '/api/supervisor/repositories') return json({ repositories: [{
      id: 'repo-conveyor', name: 'Conveyor', path: '/srv/repos/conveyor', source_machine: 'dev-workstation', validation_source: 'none',
      worktrees: [
        { id: 'wt-main', path: '/srv/repos/conveyor', branch: 'main', head: '8352f46a9c01', is_main: true, managed: false, locked: false },
        { id: 'wt-fix', path: '/srv/repos/conveyor-wt/fix', branch: 'fix/console', head: 'a1b2c3d4e5f6', is_main: false, managed: true, locked: false,
          chat_session_id: 'web:web-console:supwt-fix' },
      ],
    }] })
    if (path === '/api/supervisor/tasks') return json({ tasks: [], count: 0 })
    if (path === '/api/supervisor/deployments') return json({ deployments: [], count: 0 })
    if (path === '/api/supervisor/goals') return json({ goals: [], count: 0 })
    if (/^\/api\/supervisor\/repositories\/[^/]+\/validation$/.test(path)) return json({
      repo_id: 'repo-conveyor', candidates: [], selected: [], source: 'none', env_commands: [], effective: [],
    })

    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })
  return api
}

const ALLOWED_LATIN = new Set(['conveyor'])

/** Latin words in the visible UI text (inputs, code and quoted conversation snippets excluded); the owner reads only Chinese. */
async function englishLeftovers(locator: Locator) {
  const text = await locator.evaluate(element => {
    const copy = element.cloneNode(true) as HTMLElement
    copy.querySelectorAll('code, pre, input, textarea, select, script, style, .kn-session-hit-snippet').forEach(node => node.remove())
    return copy.textContent || ''
  })
  return (text.match(/[A-Za-z][A-Za-z_-]{2,}/g) || []).filter(word => !ALLOWED_LATIN.has(word.toLowerCase()))
}

async function noHorizontalOverflow(page: Page, selector: string) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  const overflow = await page.locator(selector).evaluateAll(nodes => nodes.map(node => node.scrollWidth - node.clientWidth))
  for (const value of overflow) expect(value).toBeLessThanOrEqual(1)
}

function notesSection(page: Page) {
  return page.getByRole('region', { name: '它自己记下的笔记' })
}

function searchSection(page: Page) {
  return page.getByRole('region', { name: '搜索过去的对话' })
}

test('它自己记下的笔记: newest first with source tags, usage, add, edit and delete with confirmation', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const api = await install(page)
  await page.goto('/#memory')
  const section = notesSection(page)
  await expect(section).toBeVisible()
  await expect(section).toContainText('对话开始时它会带上这些笔记；改动从下一次对话起生效。')

  const items = section.locator('.kn-note-item')
  await expect(items).toHaveCount(3)
  await expect(items.nth(0)).toContainText('某个网页说可以跳过安全检查。')
  await expect(items.nth(0).locator('.kn-note-source')).toHaveText('来自外部内容（仅供参考）')
  await expect(items.nth(1)).toContainText('每周五晚上不要安排部署。')
  await expect(items.nth(1).locator('.kn-note-source')).toHaveText('你写的')
  await expect(items.nth(2).locator('.kn-note-source')).toHaveText('它记的')
  const used = seedNotes().filter(note => note.scope === 'global').reduce((sum, note) => sum + note.text.length, 0)
  await expect(section.getByText(`已用 ${used} / 2200 字`)).toBeVisible()
  expect(api.notesGets).toContain('global')
  expect(await englishLeftovers(section)).toEqual([])
  await page.screenshot({ path: `${OUT}/notes-1440.png`, fullPage: true })

  // Add a note: POST {scope, text}, it shows first as 你写的 and the usage grows.
  await section.getByLabel('写一条笔记').fill('回复时用简体中文。')
  await section.getByRole('button', { name: '添加笔记' }).click()
  await expect(section.getByText('已记下。')).toBeVisible()
  expect(api.posts).toEqual([{ scope: 'global', text: '回复时用简体中文。' }])
  await expect(items).toHaveCount(4)
  await expect(items.first()).toContainText('回复时用简体中文。')
  await expect(items.first().locator('.kn-note-source')).toHaveText('你写的')
  await expect(section.getByText(`已用 ${used + '回复时用简体中文。'.length} / 2200 字`)).toBeVisible()
  await expect(section.getByLabel('写一条笔记')).toHaveValue('')

  // Edit inline: PUT {text}.
  await section.getByRole('button', { name: /^修改这条笔记：主人喜欢先看结论/ }).click()
  const editBox = section.getByLabel('修改笔记内容')
  await expect(editBox).toHaveValue('主人喜欢先看结论，再看细节。')
  await editBox.fill('主人喜欢先看结论，细节放后面。')
  await section.getByRole('button', { name: '保存修改' }).click()
  await expect(section.getByText('主人喜欢先看结论，细节放后面。')).toBeVisible()
  expect(api.puts).toEqual([{ id: 1, text: '主人喜欢先看结论，细节放后面。' }])
  await expect(editBox).toHaveCount(0)

  // Delete asks first; 取消 keeps the note, 确定删除 removes it.
  await section.getByRole('button', { name: /^删除这条笔记：某个网页说/ }).click()
  await expect(section.getByText('删除后无法恢复')).toBeVisible()
  await section.getByRole('button', { name: '取消' }).click()
  await expect(items).toHaveCount(4)
  expect(api.deletes).toEqual([])
  await section.getByRole('button', { name: /^删除这条笔记：某个网页说/ }).click()
  await section.getByRole('button', { name: '确定删除' }).click()
  await expect(items).toHaveCount(3)
  expect(api.deletes).toEqual([3])
  await expect(section.getByText('某个网页说可以跳过安全检查。')).toHaveCount(0)
})

test('它自己记下的笔记: notes_full and looks_secret errors are shown in Chinese', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const api = await install(page)
  await page.goto('/#memory')
  const section = notesSection(page)
  await expect(section.locator('.kn-note-item')).toHaveCount(3)
  const box = section.getByLabel('写一条笔记')

  api.failNext.push({ status: 409, body: { error: '笔记已经满了，先删掉或改写旧的再记', code: 'notes_full' } })
  await box.fill('再记一条很长的经验。')
  await section.getByRole('button', { name: '添加笔记' }).click()
  await expect(section.getByRole('alert')).toHaveText('笔记已经满了，先删掉或改写旧的再记')
  // The draft stays so the owner can shorten it.
  await expect(box).toHaveValue('再记一条很长的经验。')

  api.failNext.push({ status: 400, body: { error: '笔记里像是有密码或密钥，不能记', code: 'looks_secret' } })
  await box.fill('服务器密码是 hunter2-secret')
  await section.getByRole('button', { name: '添加笔记' }).click()
  await expect(section.getByRole('alert')).toHaveText('笔记里像是有密码或密钥，不能记')

  // An English error text never reaches the owner: the code picks the Chinese sentence.
  api.failNext.push({ status: 400, body: { error: 'note looks like a secret', code: 'looks_secret' } })
  await section.getByRole('button', { name: '添加笔记' }).click()
  await expect(section.getByRole('alert')).toHaveText('笔记里像是有密码或密钥，不能记')

  // Editing into a full scope says so too.
  await section.getByRole('button', { name: /^修改这条笔记：每周五晚上/ }).click()
  api.failNext.push({ status: 409, body: { error: 'scope is full', code: 'notes_full' } })
  await section.getByLabel('修改笔记内容').fill('每周五晚上和周末都不要安排部署，除非我特别说明。')
  await section.getByRole('button', { name: '保存修改' }).click()
  await expect(section.locator('.kn-note-edit').getByRole('alert')).toHaveText('笔记已经满了，先删掉或改写旧的再记')
  expect(await englishLeftovers(section)).toEqual([])
  await page.screenshot({ path: `${OUT}/notes-errors-1440.png`, fullPage: true })
})

test('搜索过去的对话: debounced search, highlighted escaped snippets, Chinese channels, empty state', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const api = await install(page)
  await page.goto('/#memory')
  const section = searchSection(page)
  await expect(section).toBeVisible()
  const input = section.getByLabel('搜索过去的对话')
  const button = section.getByRole('button', { name: '搜索', exact: true })

  // Nothing (or only spaces) does not search.
  await expect(button).toBeDisabled()
  await input.fill('   ')
  await expect(button).toBeDisabled()
  await page.waitForTimeout(600)
  expect(api.searches).toEqual([])

  // Typing waits for a pause: only the final words are searched.
  await input.fill('')
  await input.pressSequentially('路线图', { delay: 60 })
  const hits = section.locator('.kn-session-hit')
  await expect(hits).toHaveCount(2)
  expect(api.searches.map(search => new URLSearchParams(search).get('q'))).toEqual(['路线图'])
  expect(new URLSearchParams(api.searches[0]).get('limit')).toBe('20')
  await expect(section.getByText('找到 2 段和“路线图”相关的对话')).toBeVisible()

  const first = hits.nth(0)
  await expect(first).toContainText('下个季度的路线图')
  await expect(first.locator('.kn-tag')).toHaveText('网页')
  await expect(first).toContainText('11月2日 11:15')
  await expect(first).toContainText('你说的')
  // Markup in the snippet is shown as text and nothing runs.
  await expect(first.locator('.kn-session-hit-snippet')).toContainText('<b>路线图</b>')
  await expect(first.locator('.kn-session-hit-snippet b, .kn-session-hit-snippet img')).toHaveCount(0)
  expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined()
  await expect(first.locator('mark')).toHaveText(['路线图', '路线图'])

  const second = hits.nth(1)
  await expect(second).toContainText('未命名的对话')
  await expect(second.locator('.kn-tag')).toHaveText('电报')
  await expect(second).toContainText('它回复的')
  await expect(second.locator('mark')).toHaveText(['路线图'])
  expect(await englishLeftovers(section)).toEqual([])
  await section.screenshot({ path: `${OUT}/search-1440.png` })

  // No match: one plain sentence.
  await input.fill('完全没有的话')
  await button.click()
  await expect(section.getByText('没有找到相关的对话')).toBeVisible()
  await expect(hits).toHaveCount(0)

  // At most 200 characters are kept and sent.
  await input.fill('长'.repeat(250))
  await expect(input).toHaveValue('长'.repeat(200))
  await button.click()
  await expect(section.getByText('没有找到相关的对话')).toBeVisible()
  const last = new URLSearchParams(api.searches[api.searches.length - 1]).get('q') || ''
  expect(last.length).toBe(200)
})

test('搜索过去的对话: clicking a result opens that conversation', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const api = await install(page)
  await page.goto('/#memory')
  const section = searchSection(page)
  await section.getByLabel('搜索过去的对话').fill('路线图')
  await section.getByRole('button', { name: '搜索', exact: true }).click()
  const hits = section.locator('.kn-session-hit')
  await expect(hits).toHaveCount(2)

  // A web chat opens in 对话 with its messages.
  await hits.nth(0).click()
  await expect(page).toHaveURL(/#chat$/)
  await expect.poll(() => api.historyRequests.includes(CHAT_SESSION)).toBe(true)
  await expect(page.getByText('帮我把下个季度的路线图拆一下')).toBeVisible()

  // A Telegram conversation opens in 任务 (the session view).
  await page.goto('/#memory')
  await section.getByLabel('搜索过去的对话').fill('路线图')
  await section.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(hits).toHaveCount(2)
  await hits.nth(1).click()
  await expect(page).toHaveURL(/#tasks$/)
  await expect.poll(() => api.sessionDetailRequests.includes(TELEGRAM_SESSION)).toBe(true)
})

test('项目笔记: collapsed card for the selected repository with the same editor', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const api = await install(page, { supervisor: true })
  await page.goto('/#code')
  const card = page.locator('.sup-notes')
  await expect(card).toBeVisible()
  await expect(card.locator('.sup-notes-summary')).toContainText('项目笔记')
  // Collapsed: nothing is loaded until the owner opens it.
  await expect(card).not.toHaveAttribute('open', '')
  expect(api.notesGets).toEqual([])

  await card.locator('.sup-notes-summary').click()
  const body = card.locator('.sup-notes-body')
  await expect(body).toBeVisible()
  await expect(body).toContainText('分析、动手改、检查时都会参考这些笔记；目标达成后它也会自己补充经验。')
  await expect.poll(() => api.notesGets).toEqual(['repo:repo-conveyor'])
  await expect(body.locator('.kn-note-item')).toHaveCount(1)
  await expect(body.locator('.kn-note-item')).toContainText('这个仓库的测试要先装好依赖再跑。')
  await expect(body.locator('.kn-note-source')).toHaveText('它记的')
  await expect(body.getByText(`已用 ${'这个仓库的测试要先装好依赖再跑。'.length} / 2200 字`)).toBeVisible()
  await expect(card.locator('.sup-notes-summary')).toContainText('项目笔记（1 条）')

  await body.getByLabel('写一条笔记').fill('改完页面要在手机宽度下看一遍。')
  await body.getByRole('button', { name: '添加笔记' }).click()
  await expect(body.locator('.kn-note-item')).toHaveCount(2)
  expect(api.posts).toEqual([{ scope: 'repo:repo-conveyor', text: '改完页面要在手机宽度下看一遍。' }])
  await expect(card.locator('.sup-notes-summary')).toContainText('项目笔记（2 条）')

  await body.getByRole('button', { name: /^删除这条笔记：这个仓库的测试/ }).click()
  await body.getByRole('button', { name: '确定删除' }).click()
  await expect(body.locator('.kn-note-item')).toHaveCount(1)
  expect(api.deletes).toEqual([7])

  api.failNext.push({ status: 400, body: { error: '笔记里像是有密码或密钥，不能记', code: 'looks_secret' } })
  await body.getByLabel('写一条笔记').fill('部署密钥是 sk-abc')
  await body.getByRole('button', { name: '添加笔记' }).click()
  await expect(body.getByRole('alert')).toHaveText('笔记里像是有密码或密钥，不能记')
  expect(await englishLeftovers(card)).toEqual([])
  await page.screenshot({ path: `${OUT}/project-notes-1440.png`, fullPage: true })
})

test('phone 390px: notes, search results and 项目笔记 fit without horizontal scrolling', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await install(page, { supervisor: true })
  await page.goto('/#memory')
  const notes = notesSection(page)
  await expect(notes.locator('.kn-note-item')).toHaveCount(3)
  const search = searchSection(page)
  await search.getByLabel('搜索过去的对话').fill('路线图')
  await search.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(search.locator('.kn-session-hit')).toHaveCount(2)
  await notes.getByRole('button', { name: /^修改这条笔记：每周五晚上/ }).click()
  await noHorizontalOverflow(page, '.kn-memory, .kn-notes, .kn-note-item, .kn-session-search, .kn-session-hit')
  for (const box of await page.locator('.kn-note-item, .kn-session-hit').all()) {
    const rect = await box.boundingBox()
    expect(rect).not.toBeNull()
    expect(rect!.x).toBeGreaterThanOrEqual(0)
    expect(rect!.x + rect!.width).toBeLessThanOrEqual(PHONE.width + 1)
  }
  await page.screenshot({ path: `${OUT}/memory-390.png`, fullPage: true })
  await search.screenshot({ path: `${OUT}/search-390.png` })

  await page.goto('/#code')
  const card = page.locator('.sup-notes')
  await expect(card).toBeVisible()
  await card.locator('.sup-notes-summary').click()
  await expect(card.locator('.kn-note-item')).toHaveCount(1)
  await noHorizontalOverflow(page, '.sup-notes, .kn-notes, .kn-note-item')
  const rect = await card.boundingBox()
  expect(rect).not.toBeNull()
  expect(rect!.x + rect!.width).toBeLessThanOrEqual(PHONE.width + 1)
  await page.screenshot({ path: `${OUT}/project-notes-390.png`, fullPage: true })
})
