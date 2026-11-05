import { expect, test, type Page, type Route } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'

// Second-pass polish: every page at the owner's laptop sizes and at phone widths, so the whole
// console can be compared side by side. Screenshots land in web/test-results/ux-polish/<view>-<width>.png.
// Set UX_STYLE_DUMP=<dir> to also write each page's computed styles (used to prove that removing
// dead legacy CSS changes nothing on screen).
const SHOTS = 'test-results/ux-polish'
const TOKEN = 'ux-polish-e2e-token-not-a-secret-0001'
const STYLE_DUMP = process.env.UX_STYLE_DUMP || ''
const SIZES = [
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
  ...(STYLE_DUMP ? [{ width: 800, height: 1000 }] : []),
  { width: 390, height: 844 },
  { width: 360, height: 740 },
]
// A fixed clock keeps relative times (and so every layout) identical between runs.
const NOW = new Date('2026-11-03T14:05:00+08:00').getTime()
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString()

const agents = [
  { id: 'default', name: 'Conveyor', color: '#bd5c40', is_default: true, session_id: 'web:web-console:agent-default',
    instructions: '你是我的常驻助手：维护服务器、修改代码、提醒我该处理的事。', workspace_path: '', status: 'idle',
    last_message: '登录已确认成功，会话已保存。', last_message_role: 'assistant', last_activity: ago(12), message_count: 24 },
  { id: 'finance', name: 'Finance', color: '#10b981', is_default: false, session_id: 'web:web-console:agent-finance',
    instructions: '记账、对账，每月 1 号整理报表。', workspace_path: '/srv/finance-reports', workspace_status: 'ok', status: 'waiting',
    last_message: '10 月报表已生成，有两笔支出需要你确认。', last_message_role: 'assistant', last_activity: ago(95), message_count: 8 },
  { id: 'researcher', name: 'Researcher', color: '#8b5cf6', is_default: false, session_id: 'web:web-console:agent-researcher',
    instructions: '查资料并写成简短的中文摘要。', workspace_path: '', status: 'working',
    last_message: '正在对比三篇向量数据库的评测文章…', last_message_role: 'assistant', last_activity: ago(3), message_count: 15 },
  { id: 'coder', name: 'Coder', color: '#2f7df6', is_default: false, session_id: 'web:web-console:agent-coder',
    instructions: '负责 Conveyor 仓库的代码修改和测试。', workspace_path: '/srv/apps/conveyor', workspace_status: 'ok', status: 'idle',
    last_message: '已提交修复：登录页在手机上不再溢出。', last_message_role: 'assistant', last_activity: ago(60 * 26), message_count: 41 },
]

const job = {
  id: 'q-7f3a91', state: 'running', mode: 'fix', channel: 'web', chat_id: 'agent-default', operator_id: 'web-console',
  created_at: ago(6), started_at: ago(5), updated_at: ago(1),
  prompt_preview: '把登录页的错误提示改成中文，并在手机上检查一遍',
  changed_files: [{ status: 'M', path: 'web/src/LoginPage.tsx' }, { status: 'A', path: 'web/src/i18n/zh-CN.json' }],
}
const session = {
  id: agents[0].session_id, channel: 'web', title: '登录页错误提示改成中文', created_at: ago(300), last_activity: ago(1),
  job_count: 3, message_count: 6, latest_job: job, active_refinement: null,
}
const transcript = [
  { id: 'm1', session_id: session.id, role: 'user', content: '把登录页的错误提示改成中文，并在手机上检查一遍', created_at: ago(6) },
  { id: 'm2', session_id: session.id, role: 'assistant', content: '好的，我先找到登录页的提示文案，改完后用 390 像素宽度检查布局。', created_at: ago(5) },
]
const chatHistory = [
  { role: 'user', text: '今天服务器状态怎么样？', origin_channel: 'telegram', created_at: ago(50) },
  { role: 'assistant', text: '一切正常：\n\n- 磁盘使用 62%\n- 内存可用 5.8 GB\n- 过去 24 小时没有失败的任务', origin_channel: 'telegram', created_at: ago(49) },
  { role: 'user', text: '帮我把明早 9 点的巡检结果发到 Telegram。', origin_channel: 'web', created_at: ago(14) },
  { role: 'assistant', text: '已设置：每天 9:00 巡检一次，结果发到 Telegram，并放进收件箱。', origin_channel: 'web', created_at: ago(13) },
]

function systemStatus(mobileUI: boolean, moreFeatures: boolean) {
  return {
    uptime_seconds: 86_400, load_average: [0.42, 0.38, 0.35], cpu_count: 4,
    memory: { total: 8_589_934_592, available: 6_227_702_579 },
    disk: { total: 107_374_182_400, used: 66_571_993_088, free: 40_802_189_312 },
    queue: { depth: 1, paused: false, states: { running: 1, queued: 1, completed: 42, failed: 2 } },
    channels: { telegram: { configured: true }, feishu: { configured: false } },
    nodes: [],
    features: {
      agents: true, long_term_memory: true, routines: true, teammate: true, live_screen: true,
      mobile_ui: mobileUI, pwa: false, approval_inbox: moreFeatures, skills: moreFeatures, workflows: moreFeatures, mcp: moreFeatures, webhooks: false,
    },
  }
}

type Fixture = { failJobs: boolean; failStatus: boolean }

async function installShellApi(page: Page, options: { mobileUI?: boolean; moreFeatures?: boolean } = {}): Promise<Fixture> {
  const fixture: Fixture = { failJobs: false, failStatus: false }
  await page.clock.setFixedTime(NOW)
  await page.addInitScript(token => {
    sessionStorage.setItem('conveyor-token', token)
    // A job's live progress stream stays open, as it does on the real server.
    const original = window.fetch.bind(window)
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.includes('/api/events/stream')) {
        return Promise.resolve(new Response(new ReadableStream({ start() {} }), { status: 200, headers: { 'content-type': 'text/event-stream' } }))
      }
      return original(input, init)
    }
  }, TOKEN)
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/system/status') return fixture.failStatus ? json({}, 503) : json(systemStatus(Boolean(options.mobileUI), Boolean(options.moreFeatures)))
    if (path === '/api/agents') return json({ enabled: true, agents })
    if (path === '/api/sessions') return json({ sessions: [session] })
    // Finance has not run anything yet: its 任务 page shows the welcome state.
    if (path.startsWith('/api/sessions/') && path.includes('agent-finance')) return json({ id: agents[1].session_id, channel: 'web', title: '',
      created_at: ago(300), last_activity: ago(95), job_count: 0, message_count: 0, latest_job: null, active_refinement: null, messages: [], jobs: [], runs: [] })
    if (path.startsWith('/api/sessions/')) return json({ ...session, messages: transcript, jobs: [job],
      runs: [{ id: job.id, state: job.state, mode: job.mode, created_at: job.created_at, prompt_preview: job.prompt_preview, refinement_intent: false }] })
    if (path === '/api/jobs') return fixture.failJobs ? json({}, 503) : json({ jobs: [job] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [{ id: 'mac-mini', name: 'Mac mini', type: 'desktop', status: 'online', last_seen_at: ago(1), capabilities: ['screen'] }] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json({ events: [] })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) return json({ diff: 'diff --git a/web/src/LoginPage.tsx b/web/src/LoginPage.tsx\n-  Invalid password\n+  密码不正确，请重新输入' })
    if (path === '/api/agent-screens/config') return json({ error: 'not found' }, 404)
    if (path === '/api/agent-conversations') {
      const agent = agents.find(item => item.id === url.searchParams.get('agent_id')) || agents[0]
      return json({ conversations: [{ id: agent.id, title: 'Web 会话（原有记录）', origin_channel: 'web', session_id: agent.session_id }] })
    }
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'), messages: chatHistory })
    if (path === '/api/memory') return json({
      items: [
        { id: 1, kind: 'profile', text: '我习惯用中文交流，回答尽量简短。', day: '2026-10-02', created_at: ago(60 * 24 * 30), updated_at: ago(60 * 24 * 30), source_channel: 'web' },
        { id: 2, kind: 'facts', text: '生产服务器在 VPS 上，部署目录 /opt/conveyor。', day: '2026-10-15', created_at: ago(60 * 24 * 18), updated_at: ago(60 * 24 * 18), source_channel: 'telegram' },
        { id: 3, kind: 'facts', text: '每周五晚上做一次数据库备份。', day: '2026-10-20', created_at: ago(60 * 24 * 13), updated_at: ago(60 * 24 * 13), source_channel: 'web' },
        { id: 4, kind: 'log', text: '11 月 1 日：登录已确认成功，保存了 12306 的会话。', day: '2026-11-01', created_at: ago(60 * 40), updated_at: ago(60 * 40), source_channel: 'web' },
      ],
      counts: { profile: 1, facts: 2, log: 1 }, shared: false, profile_cap: 20,
    })
    if (path === '/api/inbox') return json({ unread: 2, items: [
      { id: 11, routine_id: 1, routine_name: '每日服务器巡检', started_at: ago(70), finished_at: ago(69), status: 'ok', output: '磁盘 62%，内存正常，没有失败的任务。', read_at: null },
      { id: 12, routine_id: 2, routine_name: '月度对账', started_at: ago(60 * 20), finished_at: ago(60 * 20 - 2), status: 'ok', output: '10 月有两笔支出需要确认。', read_at: null },
    ] })
    if (path === '/api/routines') return json({ routines: [
      { id: 1, name: '每日服务器巡检', schedule: '每天 09:00', prompt: '检查服务器状态', deliver: ['telegram'], enabled: true, created_at: ago(60 * 24 * 20), updated_at: ago(60 * 24 * 20), next_run_at: ago(-60 * 10) },
    ] })
    if (path === '/api/teammate/status') return json({
      enabled: true, is_paused: false, paused_until: null, muted_sources: [], interval_seconds: 300, cooldown_seconds: 1800,
      last_patrol_at: ago(4), total_alerts_count: 1,
      recent_alerts: [{ source: 'host.disk', severity: 'warning', title: '/var 分区使用率 86%', summary: '旧日志占用较多空间，建议清理。', fingerprint: 'disk-var', suggested_actions: [{ label: '清理旧日志', command: '/clean_old_jobs' }], created_at: ago(40) }],
      monitored_services: ['conveyor', 'nginx', 'postgresql'], thresholds: { disk_pct: 85, disk_gb: 5, load_ratio: 1.5, error_burst: 20 },
      status_text: '值守中，最近一次巡检 4 分钟前',
    })
    if (path === '/api/secrets') return json({ items: [
      { name: 'GITHUB_TOKEN', set: true, updated_at: ago(60 * 24 * 3) },
      { name: 'DEEPSEEK_API_KEY', set: true, updated_at: ago(60 * 24 * 12) },
      { name: 'SMTP_PASSWORD', set: false, updated_at: null },
    ] })
    if (path === '/api/browser/approvals') return json({ kill_switch: false, approvals: [
      { id: 'g1', kind: 'path', host: 'kyfw.12306.cn', port: 443, path: '/otn/', query_field: null, expires_at: NOW / 1000 + 86_400, approver: 'web-console', action_types: [] },
    ] })
    if (path === '/api/browser/egress') return json({ requests: [
      { timestamp: NOW / 1000 - 300, host: 'kyfw.12306.cn', method: 'GET', outcome: 'allowed', reason: 'approved path' },
    ] })
    if (path === '/api/screen/status') return json({ enabled: false, available: false })
    if (path === '/api/takeover/status') return json({ available: false })
    if (/^\/api\/agents\/[^/]+\/library$/.test(path)) return json({ jobs: [], routines: [], memory: { profile: 1, log: 4 }, screenshots: [] })
    if (path === '/api/approval-inbox') return json({ items: [], counts: { total: 0 } })
    if (path === '/api/skills') return json({ skills: [] })
    if (path === '/api/workflows') return json({ workflows: [] })
    if (path === '/api/mcp/servers') return json({ servers: [] })
    if (path === '/api/config/provider') return json({ provider_id: 'deepseek', provider_name: 'DeepSeek', model: 'deepseek-chat', reasoning_effort: 'minimal',
      base_url: 'https://api.deepseek.com/v1', wire_api: 'chat', env_key: 'DEEPSEEK_API_KEY', api_key_configured: true, api_key_hint: '…k3x9', config_path: '/opt/conveyor/config.toml' })
    return json({ error: `unhandled ${request.method()} ${path}` }, 404)
  })
  return fixture
}

test.use({ timezoneId: 'Asia/Shanghai', locale: 'zh-CN' })

const PAGES = [
  { slug: 'chat', ready: '.chat-panel-container .composer textarea' },
  { slug: 'tasks', ready: '.task-composer textarea' },
  { slug: 'inbox', ready: '.stream-panel :text("每日服务器巡检")' },
  { slug: 'memory', ready: '.stream-panel :text("每周五晚上做一次数据库备份。")' },
  { slug: 'teammate', ready: '.stream-panel :text("/var 分区使用率 86%")' },
  { slug: 'browser', ready: '.stream-panel :text("kyfw.12306.cn") >> nth=0' },
  { slug: 'secrets', ready: '.stream-panel :text("DEEPSEEK_API_KEY")' },
]

const noSidewaysScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)

/** Computed styles of every rendered element, keyed by its DOM path (only with UX_STYLE_DUMP). */
async function dumpStyles(page: Page, name: string) {
  if (!STYLE_DUMP) return
  const styles = await page.evaluate(() => {
    // Freeze running animations at their start so pulsing dots never show up as differences.
    for (const animation of document.getAnimations()) {
      try { animation.finish() } catch { animation.pause(); animation.currentTime = 0 }
    }
    const props = ['display', 'position', 'top', 'right', 'bottom', 'left', 'z-index', 'box-sizing',
      'margin-top', 'margin-right', 'margin-bottom', 'margin-left', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
      'border-top-width', 'border-right-width', 'border-bottom-width', 'border-left-width', 'border-top-color', 'border-left-color',
      'border-top-style', 'border-top-left-radius', 'border-bottom-right-radius', 'outline-style', 'outline-color',
      'background-color', 'background-image', 'color', 'opacity', 'visibility', 'box-shadow', 'transform', 'filter', 'backdrop-filter',
      'font-family', 'font-size', 'font-weight', 'line-height', 'letter-spacing', 'text-align', 'text-transform', 'white-space', 'text-overflow',
      'overflow-x', 'overflow-y', 'overflow-wrap', 'word-break', 'gap', 'row-gap', 'column-gap', 'grid-template-columns', 'grid-template-rows',
      'grid-column-start', 'grid-column-end', 'grid-row-start', 'flex-direction', 'flex-wrap', 'flex-grow', 'flex-shrink', 'flex-basis', 'order',
      'align-items', 'align-self', 'justify-content', 'justify-self', 'width', 'height', 'min-width', 'min-height', 'max-width', 'max-height',
      'cursor', 'pointer-events', 'touch-action', 'transition-property', 'transition-duration', 'animation-name', 'content', 'list-style-type',
      'object-fit', 'vertical-align', 'table-layout', 'border-collapse', 'scrollbar-width', 'overscroll-behavior-y', 'user-select']
    const out: Record<string, Record<string, string>> = {}
    const path = (el: Element): string => {
      const parts: string[] = []
      for (let node: Element | null = el; node && node !== document.documentElement; node = node.parentElement) {
        const parent = node.parentElement
        const index = parent ? Array.from(parent.children).indexOf(node) : 0
        parts.unshift(`${node.tagName.toLowerCase()}${typeof node.className === 'string' && node.className.trim() ? '.' + node.className.trim().split(/\s+/).sort().join('.') : ''}:${index}`)
      }
      return parts.join(' > ')
    }
    const record = (el: Element, pseudo: string | null, key: string) => {
      const cs = getComputedStyle(el, pseudo)
      if (pseudo && (cs.content === 'none' || cs.content === 'normal')) return
      const entry: Record<string, string> = {}
      for (const prop of props) entry[prop] = cs.getPropertyValue(prop)
      if (!pseudo) {
        const rect = el.getBoundingClientRect()
        entry.rect = [rect.x, rect.y, rect.width, rect.height].map(n => Math.round(n * 2) / 2).join(',')
      }
      out[key] = entry
    }
    for (const el of Array.from(document.querySelectorAll('body *'))) {
      if (el.getClientRects().length === 0 && getComputedStyle(el).display === 'none') continue
      const key = path(el)
      record(el, null, key)
      record(el, '::before', key + '::before')
      record(el, '::after', key + '::after')
    }
    return out
  })
  mkdirSync(STYLE_DUMP, { recursive: true })
  writeFileSync(`${STYLE_DUMP}/${name}.json`, JSON.stringify(styles))
}

async function capture(page: Page, name: string, width: number) {
  await page.mouse.move(0, 0)
  await page.waitForTimeout(450)
  expect(await noSidewaysScroll(page), `${name}-${width}: sideways scroll`).toBe(true)
  await page.screenshot({ path: `${SHOTS}/${name}-${width}.png`, fullPage: true })
  await dumpStyles(page, `${name}-${width}`)
}

for (const size of SIZES) {
  test.describe(`polish at ${size.width}×${size.height}`, () => {
    test.use({ viewport: size })
    const phone = size.width <= 820

    test('every page', async ({ page }) => {
      await installShellApi(page)
      for (const item of PAGES) {
        await page.goto(`/#${item.slug}`)
        await expect(page.locator(item.ready).first()).toBeVisible()
        await expect(page.locator('.stream-header h2')).toHaveCount(1)
        await capture(page, item.slug, size.width)
      }
    })

    test('settings and unlock', async ({ page }) => {
      await installShellApi(page)
      await page.goto('/#chat')
      await expect(page.locator('main.app-shell')).toBeVisible()
      if (phone) {
        await page.getByRole('navigation', { name: '底部导航' }).getByRole('button', { name: '更多' }).click()
        await expect(page.getByRole('dialog', { name: '更多' })).toBeVisible()
        await capture(page, 'more-sheet', size.width)
        await page.getByRole('dialog', { name: '更多' }).getByRole('button', { name: '设置' }).click()
      } else {
        await page.locator('header.topbar').getByRole('button', { name: '设置' }).click()
      }
      const dialog = page.getByRole('dialog', { name: '模型设置' })
      await expect(dialog.getByLabel('显示名称')).toHaveValue('DeepSeek')
      await capture(page, 'settings', size.width)

      await page.keyboard.press('Escape')
      await page.evaluate(() => sessionStorage.clear())
      await page.context().clearCookies()
      const fresh = await page.context().newPage()
      await fresh.clock.setFixedTime(NOW)
      await fresh.route('**/api/**', route => route.fulfill({ status: 401, contentType: 'application/json', body: '{"error":"unauthorized"}' }))
      await fresh.goto('/')
      await expect(fresh.getByRole('heading', { name: '解锁 Conveyor 控制台' })).toBeVisible()
      await fresh.getByLabel('访问令牌').fill('wrong-token')
      await fresh.getByRole('button', { name: '解锁', exact: true }).click()
      await expect(fresh.getByRole('alert')).toHaveText('访问令牌不正确，请检查后重试。')
      // The rejected token stays in the field so a typo can be fixed in place.
      await expect(fresh.getByLabel('访问令牌')).toHaveValue('wrong-token')
      await capture(fresh, 'unlock-error', size.width)
    })

    test('task welcome and agent editor', async ({ page }) => {
      await installShellApi(page)
      const showList = async () => {
        if (!phone) return
        await page.getByRole('button', { name: /^切换助手/ }).click()
        await expect.poll(() => page.locator('aside.sessions-panel').evaluate(el => Math.round(el.getBoundingClientRect().left))).toBe(0)
      }
      await page.goto('/#chat')
      await expect(page.locator('main.app-shell')).toBeVisible()
      await showList()
      await page.locator('.agent-list .agent-item').filter({ hasText: 'Finance' }).click()
      await page.evaluate(() => { location.hash = 'tasks' })
      await expect(page.getByRole('heading', { name: '要 Finance 做什么？' })).toBeVisible()
      await capture(page, 'tasks-welcome', size.width)
      await showList()
      await page.getByRole('button', { name: '编辑 Finance' }).click({ force: true })
      const dialog = page.getByRole('dialog', { name: '编辑助手' })
      await expect(dialog.getByLabel('名称')).toHaveValue('Finance')
      await capture(page, 'agent-editor', size.width)
    })

    if (phone) {
      test('phone drawers', async ({ page }) => {
        await installShellApi(page)
        await page.goto('/#chat')
        await expect(page.locator('.chat-panel-container .composer textarea')).toBeVisible()
        await page.getByRole('button', { name: /切换助手/ }).click()
        await expect.poll(() => page.locator('aside.sessions-panel').evaluate(el => Math.round(el.getBoundingClientRect().left))).toBe(0)
        await capture(page, 'drawer-agents', size.width)
        await page.keyboard.press('Escape')
        await page.getByRole('button', { name: '详情' }).click()
        await expect(page.getByRole('dialog', { name: '详情' })).toBeVisible()
        await capture(page, 'drawer-details', size.width)
      })
    }
  })
}
