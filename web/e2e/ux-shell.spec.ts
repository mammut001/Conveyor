import { expect, test, type Page, type Route } from '@playwright/test'

// Shell area: frame, primary navigation, URL routing, layout, phone layout, keyboard.
// Screenshots land in web/test-results/ux-shell/<view>-<width>.png.
const SHOTS = 'test-results/ux-shell'
const TOKEN = 'ux-shell-e2e-token-not-a-secret-000001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

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
      { id: 'g1', kind: 'path', host: 'kyfw.12306.cn', port: 443, path: '/otn/', query_field: null, expires_at: Date.now() / 1000 + 86_400, approver: 'web-console', action_types: [] },
    ] })
    if (path === '/api/browser/egress') return json({ requests: [
      { timestamp: Date.now() / 1000 - 300, host: 'kyfw.12306.cn', method: 'GET', outcome: 'allowed', reason: 'approved path' },
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

const primaryNav = (page: Page) => page.getByRole('navigation', { name: '主导航' })
const heading = (page: Page) => page.locator('.stream-header h2')
const noSidewaysScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
// One scroll area per page: the document never scrolls and no scrolling region sits inside another.
async function expectSingleScrollArea(page: Page, label: string) {
  const report = await page.evaluate(() => {
    const scrolls = (el: HTMLElement) => /(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1 && el.clientHeight > 120
    const areas = Array.from(document.querySelectorAll<HTMLElement>('.stream-panel, .stream-panel *'))
      .filter(el => el.getClientRects().length > 0 && !['TEXTAREA', 'PRE', 'CODE', 'TABLE'].includes(el.tagName) && scrolls(el))
    return {
      page: document.documentElement.scrollHeight - window.innerHeight,
      nested: areas.filter(el => areas.some(other => other !== el && other.contains(el))).map(el => el.className || el.tagName),
    }
  })
  expect(report.page, `${label}: the page itself scrolls`).toBeLessThanOrEqual(0)
  expect(report.nested, `${label}: nested scroll areas`).toEqual([])
}
async function shot(page: Page, name: string, fullPage = true) {
  await page.waitForTimeout(350)
  await page.mouse.move(0, 0)
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage, animations: 'disabled' })
}

test('desktop: Chinese primary nav that never moves, short header, 详情 panel only on 对话/任务', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installShellApi(page)
  await page.goto('/')
  const nav = primaryNav(page)
  await expect(nav.getByRole('button')).toHaveText(['对话', '任务', /^收件箱\s*2$/, '记忆', '值守', '网站授权', '密钥'])
  await expect(nav.getByRole('button', { name: '对话', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page).toHaveTitle('对话 · Conveyor 控制台')
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN')
  await expect(page.locator('.workbench-modebar')).toHaveCount(0)

  const topbar = page.locator('header.topbar')
  await expect(topbar).toContainText('控制台')
  await expect(topbar.locator('.top-status')).toContainText('在线')
  await expect(topbar.locator('.top-status')).toContainText('运行中 1')
  await expect(topbar.locator('.top-status')).toContainText('队列 1')
  await expect(topbar).not.toContainText(/Settings|Lock|Online|Queue|CONTROL CONSOLE|Workbench/)
  await expect(topbar.getByRole('button', { name: '设置' })).toBeVisible()
  await expect(topbar.getByRole('button', { name: '锁定' })).toBeVisible()

  const anchor = await nav.getByRole('button', { name: '对话', exact: true }).boundingBox()
  const views = [
    { slug: 'chat', label: '对话', title: 'Conveyor', subtitle: '默认助手' },
    { slug: 'tasks', label: '任务', title: '登录页错误提示改成中文', subtitle: '任务' },
    { slug: 'inbox', label: '收件箱', title: '收件箱', subtitle: '定时任务的结果和通知' },
    { slug: 'memory', label: '记忆', title: '记忆', subtitle: '助手长期记住的信息' },
    { slug: 'teammate', label: '值守', title: '值守', subtitle: '全天候值守，发现问题及时提醒' },
    { slug: 'browser', label: '网站授权', title: '网站授权', subtitle: '允许助手访问的网站' },
    { slug: 'secrets', label: '密钥', title: '密钥', subtitle: '只供助手使用的私密凭据' },
  ]
  for (const view of views) {
    const button = nav.getByRole('button', { name: view.slug === 'inbox' ? /^收件箱/ : view.label, exact: view.slug !== 'inbox' })
    await button.click()
    await expect(page).toHaveURL(new RegExp(`/#${view.slug}$`))
    await expect(button).toHaveAttribute('aria-current', 'page')
    await expect(heading(page)).toHaveText(view.title)
    await expect(page.locator('.stream-header .shell-subtitle')).toHaveText(view.subtitle)
    await expect(page.locator('.stream-header .eyebrow')).toHaveCount(0)
    expect(await nav.getByRole('button', { name: '对话', exact: true }).boundingBox()).toEqual(anchor)
    const panel = page.locator('aside.context-panel')
    if (view.slug === 'chat' || view.slug === 'tasks') {
      await expect(panel).toBeVisible()
      await expect(page.getByRole('button', { name: '详情' })).toHaveAttribute('aria-expanded', 'true')
    } else {
      await expect(panel).toBeHidden()
      await expect(page.getByRole('button', { name: '详情' })).toHaveCount(0)
    }
    if (view.slug === 'tasks') await expect(page.locator('.stream-header .shell-heading .status-badge')).toHaveText('运行中')
    expect(await page.locator('.stream-header').evaluate(node => node.getBoundingClientRect().height)).toBeLessThanOrEqual(52)
    expect(await noSidewaysScroll(page)).toBe(true)
    await expectSingleScrollArea(page, `${view.slug} 1440`)
    await shot(page, `${view.slug}-1440`)
  }
})

// The frame is exactly one viewport tall: nothing (composer, its hint, the bottom of 详情) is cut off,
// also with the error banner shown, at the owner's laptop sizes.
for (const size of [DESKTOP, { width: 1366, height: 768 }, { width: 1519, height: 784 }]) {
  test(`desktop ${size.width}×${size.height}: the workspace fits the viewport, with and without an error banner`, async ({ page }) => {
    await page.setViewportSize(size)
    await page.clock.install()
    const fixture = await installShellApi(page)
    const fits = async (label: string) => {
      const report = await page.evaluate(() => {
        const bottom = (selector: string) => {
          const node = Array.from(document.querySelectorAll<HTMLElement>(selector)).find(item => item.getClientRects().length > 0)
          return node ? Math.round(node.getBoundingClientRect().bottom) : -1
        }
        return {
          viewport: window.innerHeight,
          page: document.documentElement.scrollHeight,
          workspace: bottom('.workspace'),
          composer: bottom('.stream-panel .composer'),
          panel: bottom('aside.context-panel'),
          panelScrolls: (() => { const p = document.querySelector<HTMLElement>('aside.context-panel'); return p ? getComputedStyle(p).overflowY : '' })(),
          lastSection: (() => {
            const p = document.querySelector<HTMLElement>('aside.context-panel')
            if (!p) return -1
            p.scrollTop = p.scrollHeight
            const last = Array.from(p.children).filter(child => child.getClientRects().length > 0).pop() as HTMLElement | undefined
            return last ? Math.round(last.getBoundingClientRect().bottom) : -1
          })(),
        }
      })
      expect(report.page, `${label}: page height`).toBeLessThanOrEqual(report.viewport)
      expect(report.workspace, `${label}: workspace bottom`).toBeLessThanOrEqual(report.viewport)
      expect(report.composer, `${label}: composer bottom`).toBeGreaterThan(0)
      expect(report.composer, `${label}: composer bottom`).toBeLessThanOrEqual(report.viewport)
      expect(report.panel, `${label}: 详情 bottom`).toBeLessThanOrEqual(report.viewport)
      expect(report.panelScrolls, `${label}: 详情 scrolls inside`).toMatch(/auto|scroll/)
      expect(report.lastSection, `${label}: last 详情 section`).toBeLessThanOrEqual(report.viewport)
    }
    await page.goto('/#chat')
    await expect(page.locator('.stream-panel .composer textarea:visible')).toBeVisible()
    await page.evaluate(() => localStorage.setItem('conveyor-context-panel', 'open'))
    await page.reload()
    await expect(page.locator('aside.context-panel')).toBeVisible()
    await expect(page.locator('.stream-panel .composer textarea:visible')).toBeVisible()
    await fits('chat')
    await shot(page, `fit-chat-${size.width}x${size.height}`, false)
    await page.getByRole('button', { name: '任务' }).first().click()
    await expect(heading(page)).toHaveText('登录页错误提示改成中文')
    await fits('tasks')
    fixture.failJobs = true
    await page.clock.fastForward(3_500)
    await expect(page.locator('.app-shell > .error-banner.global')).toBeVisible()
    await fits('tasks + error banner')
    await page.getByRole('button', { name: '对话' }).first().click()
    await fits('chat + error banner')
    await shot(page, `fit-chat-banner-${size.width}x${size.height}`, false)
  })
}

test('desktop: 详情 toggle hides the right panel, remembers it, and stays collapsed by default below 1200px', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installShellApi(page)
  await page.goto('/#tasks')
  const panel = page.locator('aside.context-panel')
  const toggle = page.getByRole('button', { name: '详情' })
  await expect(panel).toBeVisible()
  await toggle.click()
  await expect(panel).toBeHidden()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await page.reload()
  await expect(heading(page)).toHaveText('登录页错误提示改成中文')
  await expect(panel).toBeHidden()
  await toggle.click()
  await expect(panel).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('conveyor-context-panel'))).toBe('open')

  await page.evaluate(() => localStorage.removeItem('conveyor-context-panel'))
  await page.setViewportSize({ width: 1100, height: 800 })
  await page.reload()
  await expect(heading(page)).toHaveText('登录页错误提示改成中文')
  await expect(panel).toBeHidden()
  await toggle.click()
  await expect(panel).toBeVisible()
  expect(await noSidewaysScroll(page)).toBe(true)

  // Narrow desktop with the deployed flags: 网站授权 and 密钥 stay one click away.
  const nav = primaryNav(page)
  await expect(nav.getByRole('button')).toHaveText(['对话', '任务', /^收件箱/, '记忆', '值守', '网站授权', '密钥'])
  await nav.getByRole('button', { name: '网站授权' }).click()
  await expect(page).toHaveURL(/#browser$/)
  await expect(page.locator('aside.context-panel')).toBeHidden()
  await shot(page, `browser-1100`, false)
})

test('narrow desktop with every optional page on: the rarely used pages fold into 更多', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 })
  await installShellApi(page, { moreFeatures: true })
  await page.goto('/#tasks')
  const nav = primaryNav(page)
  await expect(nav.getByRole('button')).toHaveText(['对话', '任务', /^收件箱/, '审批', '记忆', '值守', '更多'])
  await expect(nav.getByRole('button', { name: '密钥' })).toHaveCount(0)
  await nav.getByRole('button', { name: '更多' }).click()
  const menu = page.getByRole('group', { name: '更多页面' })
  await expect(menu.getByRole('button')).toHaveText([/^网站授权/, /^密钥/, /^技能/, /^工作流/, /^连接器/])
  await expect(menu.getByRole('button', { name: '密钥', exact: true })).toHaveAttribute('title', /密钥（(Alt\+|⌥)8）/)
  expect(await noSidewaysScroll(page)).toBe(true)
  await shot(page, `more-menu-1024`, false)
  await page.keyboard.press('Escape')
  await expect(menu).toBeHidden()
  await expect(nav.getByRole('button', { name: '更多' })).toBeFocused()
  await nav.getByRole('button', { name: '更多' }).click()
  await menu.getByRole('button', { name: '密钥' }).click()
  await expect(page).toHaveURL(/#secrets$/)
  await expect(menu).toBeHidden()
  await expect(nav.getByRole('button', { name: '更多' })).toHaveClass(/active/)
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(nav.getByRole('button')).toHaveText(['对话', '任务', /^收件箱/, '审批', '记忆', '值守', '网站授权', '密钥', '技能', '工作流', '连接器'])
  expect(await noSidewaysScroll(page)).toBe(true)
  await shot(page, `all-pages-1440`, false)
})

test('the view lives in the URL: back/forward, reload, hand-edited hashes and ?agent= survive', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installShellApi(page)
  const nav = primaryNav(page)
  await page.goto('/?agent=finance')
  await expect(page).toHaveURL(/\/\?agent=finance#chat$/)
  await nav.getByRole('button', { name: '记忆' }).click()
  await expect(page).toHaveURL(/\/\?agent=finance#memory$/)
  await nav.getByRole('button', { name: '密钥' }).click()
  await expect(page).toHaveURL(/\/\?agent=finance#secrets$/)

  await page.goBack()
  await expect(page).toHaveURL(/\/\?agent=finance#memory$/)
  await expect(heading(page)).toHaveText('记忆')
  await expect(nav.getByRole('button', { name: '记忆' })).toHaveAttribute('aria-current', 'page')
  await page.goBack()
  await expect(page).toHaveURL(/#chat$/)
  await expect(heading(page)).toHaveText('Conveyor')
  await page.goForward()
  await expect(heading(page)).toHaveText('记忆')

  await page.reload()
  await expect(page).toHaveURL(/\/\?agent=finance#memory$/)
  await expect(heading(page)).toHaveText('记忆')

  await page.evaluate(() => { window.location.hash = '#teammate' })
  await expect(heading(page)).toHaveText('值守')
  await page.goBack()
  await expect(heading(page)).toHaveText('记忆')

  // Every view opens directly, #tasks is honoured even with agents on, and legacy paths still work.
  for (const [slug, title] of [['chat', 'Conveyor'], ['tasks', '登录页错误提示改成中文'], ['inbox', '收件箱'], ['browser', '网站授权'], ['secrets', '密钥'], ['teammate', '值守']]) {
    await page.goto(`/#${slug}`)
    await expect(heading(page)).toHaveText(title)
    await expect(page).toHaveURL(new RegExp(`/#${slug}$`))
  }
  await page.goto('/memory')
  await expect(heading(page)).toHaveText('记忆')
  await expect(page).toHaveURL(/\/#memory$/)
})

test('keyboard: Alt+number switches views, "/" jumps to the message box, Esc closes menus', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installShellApi(page)
  await page.goto('/')
  const nav = primaryNav(page)
  await expect(nav.getByRole('button', { name: '任务' })).toHaveAttribute('title', /任务（(Alt\+|⌥)2）/)
  await expect(nav.getByRole('button', { name: '密钥' })).toHaveAttribute('title', /密钥（(Alt\+|⌥)7）/)
  await page.keyboard.press('Alt+2')
  await expect(page).toHaveURL(/#tasks$/)
  await page.keyboard.press('Alt+4')
  await expect(page).toHaveURL(/#memory$/)
  await page.keyboard.press('Alt+7')
  await expect(page).toHaveURL(/#secrets$/)
  await page.keyboard.press('Alt+1')
  await expect(page).toHaveURL(/#chat$/)

  const chatBox = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  const hiddenChatBox = page.locator('.chat-composer textarea')
  const taskBox = page.getByRole('textbox', { name: '任务内容', exact: true })
  await expect(chatBox).toBeVisible()
  await page.locator('.stream-header h2').click()
  await page.keyboard.press('/')
  await expect(chatBox).toBeFocused()
  await expect(chatBox).toHaveValue('')
  await page.keyboard.type('/plan')
  await expect(chatBox).toHaveValue('/plan')
  // 任务: the chat panel stays mounted but hidden; "/" must reach the visible 任务 box.
  await page.keyboard.press('Alt+2')
  await expect(page).toHaveURL(/#tasks$/)
  await expect(hiddenChatBox).toBeHidden()
  await page.locator('.stream-header h2').click()
  await page.keyboard.press('/')
  await expect(taskBox).toBeFocused()
  await expect(hiddenChatBox).not.toBeFocused()
  await expect(hiddenChatBox).toHaveValue('/plan')
  await expect(taskBox).toHaveValue('')
  // A page without a message box: "/" focuses nothing (never the hidden chat box).
  await page.keyboard.press('Alt+4')
  await expect(page).toHaveURL(/#memory$/)
  await page.locator('.stream-header h2').click()
  await page.keyboard.press('/')
  await expect(hiddenChatBox).not.toBeFocused()
  expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe('TEXTAREA')
  // And back on 对话 it is the chat box again, with its draft.
  await page.keyboard.press('Alt+1')
  await page.locator('.stream-header h2').click({ force: true })
  await page.keyboard.press('/')
  await expect(chatBox).toBeFocused()
  await expect(chatBox).toHaveValue('/plan')
})

test('锁定 asks for confirmation first; 取消 keeps the console open', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installShellApi(page)
  await page.goto('/#memory')
  await page.locator('header.topbar').getByRole('button', { name: '锁定' }).click()
  const dialog = page.getByRole('alertdialog', { name: '锁定控制台？' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: '取消' })).toBeFocused()
  await shot(page, `lock-confirm-1440`, false)
  await dialog.getByRole('button', { name: '取消' }).click()
  await expect(dialog).toBeHidden()
  await expect(heading(page)).toHaveText('记忆')
  await page.locator('header.topbar').getByRole('button', { name: '锁定' }).click()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await page.locator('header.topbar').getByRole('button', { name: '锁定' }).click()
  await dialog.getByRole('button', { name: '锁定' }).click()
  await expect(page.locator('main.unlock-shell')).toBeVisible()
  expect(await page.evaluate(() => sessionStorage.getItem('conveyor-token'))).toBeNull()
})

test('errors read as short Chinese sentences and the status shows a lost connection', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install()
  const fixture = await installShellApi(page)
  await page.goto('/#memory')
  await expect(page.locator('.top-status')).toContainText('在线')
  fixture.failJobs = true
  await page.clock.fastForward(3_500)
  const banner = page.locator('.app-shell > .error-banner.global')
  await expect(banner).toContainText('服务器暂时出错，请稍后重试')
  await expect(banner).toContainText('HTTP 503')
  await expect(banner).not.toContainText('Request failed')
  await shot(page, `error-banner-1440`, false)
  await banner.getByRole('button', { name: '关闭提示' }).click()
  await expect(banner).toBeHidden()
  await page.clock.fastForward(13_000)
  await expect(page.locator('.top-status')).toHaveAttribute('data-connection', 'reconnecting')
  await expect(page.locator('.top-status')).toContainText('重连中')
  fixture.failJobs = false
  await page.clock.fastForward(3_500)
  await expect(page.locator('.top-status')).toContainText('在线')
})

test('phone (390): compact bar, bottom nav, drawers, pinned message box, no sideways scroll', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installShellApi(page)
  await page.goto('/')
  const bottom = page.getByRole('navigation', { name: '底部导航' })
  await expect(bottom).toBeVisible()
  await expect(primaryNav(page)).toBeHidden()
  await expect(bottom.getByRole('button')).toHaveText(['对话', '任务', /收件箱/, '记忆', '更多'])
  await expect(page.locator('header.topbar .top-status')).toContainText('在线')
  await expect(page.getByRole('button', { name: '设置' })).toHaveCount(0)

  // 对话: the message box is visible and sits right above the bottom bar.
  const box = page.locator('.stream-panel .composer textarea:visible')
  await expect(box).toBeVisible()
  const navTop = (await bottom.boundingBox())!.y
  const composer = (await page.locator('.stream-panel .composer:visible').boundingBox())!
  expect(composer.y + composer.height).toBeLessThanOrEqual(navTop + 1)
  expect(composer.y + composer.height).toBeGreaterThan(navTop - 40)
  expect(await noSidewaysScroll(page)).toBe(true)
  await shot(page, `chat-390`)

  const views = [
    { slug: 'tasks', label: '任务', inSheet: false, title: '登录页错误提示改成中文' },
    { slug: 'memory', label: '记忆', inSheet: false, title: '记忆' },
    { slug: 'teammate', label: '值守', inSheet: true, title: '值守' },
    { slug: 'secrets', label: '密钥', inSheet: true, title: '密钥' },
  ]
  for (const view of views) {
    if (view.inSheet) {
      await bottom.getByRole('button', { name: '更多' }).click()
      await page.getByRole('dialog', { name: '更多' }).getByRole('button', { name: view.label }).click()
      await expect(page.getByRole('dialog', { name: '更多' })).toBeHidden()
      await expect(bottom.getByRole('button', { name: '更多' })).toHaveClass(/active/)
    } else {
      await bottom.getByRole('button', { name: view.label }).click()
      await expect(bottom.getByRole('button', { name: view.label })).toHaveAttribute('aria-current', 'page')
    }
    await expect(page).toHaveURL(new RegExp(`#${view.slug}$`))
    await expect(heading(page)).toHaveText(view.title)
    expect(await noSidewaysScroll(page)).toBe(true)
    await expectSingleScrollArea(page, `${view.slug} 390`)
    if (view.slug === 'tasks') {
      const tasksComposer = (await page.locator('.stream-panel .composer:visible').boundingBox())!
      expect(tasksComposer.y + tasksComposer.height).toBeLessThanOrEqual(navTop + 1)
    }
    await shot(page, `${view.slug}-390`)
  }

  // 更多 sheet: the other pages plus 设置 and 锁定 (the only settings entry on a phone).
  await bottom.getByRole('button', { name: '更多' }).click()
  const sheet = page.getByRole('dialog', { name: '更多' })
  await expect(sheet.getByRole('button')).toHaveText(['×', '值守', '网站授权', '密钥', '设置', '锁定'])
  await expect(page.getByRole('button', { name: '设置' })).toHaveCount(1)
  await shot(page, `more-390`, false)
  await page.keyboard.press('Escape')
  await expect(sheet).toBeHidden()

  // Drawers: 助手 (left) and 详情 (right), opened from the page header.
  await bottom.getByRole('button', { name: '对话' }).click()
  await page.getByRole('button', { name: /切换助手/ }).click()
  const agentsDrawer = page.getByRole('dialog', { name: '助手' })
  await expect(agentsDrawer).toBeVisible()
  await expect(agentsDrawer.getByText('Finance', { exact: true })).toBeVisible()
  // One header row: the title once, 新建 and the close button beside it.
  await expect(agentsDrawer.getByText('助手', { exact: true })).toHaveCount(1)
  const drawerHeading = agentsDrawer.locator('.panel-heading')
  await expect(drawerHeading.getByRole('button', { name: '新建助手' })).toBeVisible()
  await expect(drawerHeading.getByRole('button', { name: '关闭' })).toBeVisible()
  await page.waitForTimeout(300)
  expect(await noSidewaysScroll(page)).toBe(true)
  await shot(page, `drawer-agents-390`, false)
  await page.keyboard.press('Escape')
  await expect(agentsDrawer).toBeHidden()
  await expect(page.getByRole('button', { name: /切换助手/ })).toBeFocused()
  await page.getByRole('button', { name: /切换助手/ }).click()
  await drawerHeading.getByRole('button', { name: '关闭' }).click()
  await expect(agentsDrawer).toBeHidden()

  await page.getByRole('button', { name: '详情' }).click()
  const detailsDrawer = page.getByRole('dialog', { name: '详情' })
  await expect(detailsDrawer).toBeVisible()
  await page.waitForTimeout(300)
  // The cards keep their spacing inside the drawer (it comes from the panel's flex gap).
  const gaps = await detailsDrawer.evaluate(drawer => {
    const boxes = Array.from(drawer.children)
      .filter(child => !child.classList.contains('mobile-drawer-header') && child.getClientRects().length > 0)
      .map(child => child.getBoundingClientRect())
    return boxes.slice(1).map((box, index) => Math.round(box.top - boxes[index].bottom))
  })
  expect(gaps.length).toBeGreaterThan(2)
  for (const gap of gaps) expect(gap).toBeGreaterThanOrEqual(8)
  await shot(page, `drawer-details-390`, false)
  await page.getByRole('button', { name: '关闭详情' }).first().click()
  await expect(detailsDrawer).toBeHidden()

  // Picking another agent from the drawer switches the conversation.
  await page.getByRole('button', { name: /切换助手/ }).click()
  await agentsDrawer.locator('button.agent-item').filter({ hasText: 'Finance' }).click()
  await expect(agentsDrawer).toBeHidden()
  await expect(page.getByRole('button', { name: /切换助手，当前：Finance/ })).toBeVisible()
})

test('phone layout does not depend on features.mobile_ui', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 })
  await installShellApi(page, { mobileUI: true })
  await page.goto('/#tasks')
  await expect(page.getByRole('navigation', { name: '底部导航' })).toBeVisible()
  await expect(heading(page)).toHaveText('登录页错误提示改成中文')
  expect(await noSidewaysScroll(page)).toBe(true)
  await shot(page, `tasks-360-flag-on`)
  await page.setViewportSize({ width: 800, height: 1000 })
  await expect(page.getByRole('navigation', { name: '底部导航' })).toBeVisible()
  await expect(page.locator('aside.context-panel')).toBeHidden()
  expect(await noSidewaysScroll(page)).toBe(true)
  await shot(page, `tasks-800`)
})
