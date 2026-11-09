import { expect, test, type Page, type Route } from '@playwright/test'

// Visual + behaviour check for the "guard" pages: 密钥 (Secrets), 网站授权 (Browser sites)
// and 值守 (Teammate). All API traffic is mocked with realistic Chinese data.
// Screenshots land in web/test-results/ux-guard/<view>-<width>.png for review.

const NOW = new Date('2026-11-03T14:30:00+08:00')
const SHOTS = 'test-results/ux-guard'
const SIZES = [{ width: 1440, height: 900 }, { width: 390, height: 844 }] as const
type Size = typeof SIZES[number]

const ago = (minutes: number) => new Date(NOW.getTime() - minutes * 60_000).toISOString()
const epoch = (minutesFromNow: number) => NOW.getTime() / 1000 + minutesFromNow * 60

const agent = (id: string, name: string, color: string, isDefault = false) => ({
  id, name, color, is_default: isDefault, session_id: `web:web-console:agent-${id}`,
  instructions: '', workspace_path: '', status: 'idle', last_message: '', message_count: 0,
})
const AGENTS = [
  agent('default', 'Conveyor', '#bd5c40', true), agent('finance', 'Finance', '#0a8578'),
  agent('researcher', 'Researcher', '#315fba'), agent('coder', 'Coder', '#7a5af8'),
]

type Secret = { name: string; set: boolean; updated_at: string | null }
const SECRETS: Secret[] = [
  { name: 'DEEPSEEK_API_KEY', set: true, updated_at: ago(60 * 26) },
  { name: 'OPENAI_API_KEY', set: true, updated_at: ago(60 * 5 + 25) },
  { name: 'ANTHROPIC_API_KEY', set: false, updated_at: ago(60 * 24 * 3) },
  { name: 'BROWSER_LOGIN_9F3C2A7E51D04B8C6E2F1A3D5B7C9E0F2A4C6E8B1D3F5A7C', set: true, updated_at: ago(60 * 24 * 9) },
]

const GRANTS = [
  { id: 'a'.repeat(32), kind: 'path', host: 'docs.python.org', port: 443, path: '/3/library/asyncio-task.html',
    query_field: null, expires_at: epoch(180), approver: 'operator:7f3a9c21b8e4', action_types: [] },
  { id: 'b'.repeat(32), kind: 'path', host: 'www.zhihu.com', port: 443, path: '/search',
    query_field: null, expires_at: epoch(45), approver: 'operator:7f3a9c21b8e4', action_types: [] },
  { id: 'c'.repeat(32), kind: 'query', host: 'www.zhihu.com', port: 443, path: '/search',
    query_field: 'q', expires_at: epoch(45), approver: 'operator:7f3a9c21b8e4', action_types: ['search_get'] },
  { id: 'd'.repeat(32), kind: 'actions', host: 'news.ycombinator.com', port: 443, path: '/news',
    query_field: null, expires_at: epoch(60 * 26), approver: 'operator:7f3a9c21b8e4', action_types: ['follow_link', 'paginate'] },
  { id: 'e'.repeat(32), kind: 'path', host: 'status.parlemiao.cn', port: 8443, path: '/dashboard/services/very/long/path/that/keeps/going/for/a/while',
    query_field: null, expires_at: epoch(60 * 24 * 6), approver: 'operator:0b51e6d2aa90', action_types: [] },
]

const EGRESS = [
  { timestamp: epoch(-2), host: 'docs.python.org', method: 'GET', outcome: 'allowed', reason: 'approved_readonly' },
  { timestamp: epoch(-3), host: 'news.ycombinator.com', method: 'OTHER', outcome: 'allowed', reason: 'g2_step_follow_link' },
  { timestamp: epoch(-9), host: 'news.ycombinator.com', method: 'GET', outcome: 'denied', reason: 'redirect_denied' },
  { timestamp: epoch(-31), host: 'unknown', method: 'GET', outcome: 'denied', reason: 'approval_missing' },
  { timestamp: epoch(-60 * 20), host: 'intranet.local', method: 'GET', outcome: 'denied', reason: 'dns_private_address_denied' },
  { timestamp: epoch(-60 * 40), host: 'www.zhihu.com', method: 'GET', outcome: 'denied', reason: 'g5_domain_not_certified' },
]

const DISK_ALERT = {
  source: 'host.disk', severity: 'warning', title: '磁盘空间偏低 (/srv)',
  summary: '挂载路径 `/srv` 可用空间仅剩 2.4 GB (6.1% 可用，总空间 39.3 GB，已用 93.9%)。',
  fingerprint: 'host.disk./srv', created_at: ago(14),
}
const LOG_ALERT = {
  source: 'host.logs', severity: 'warning', title: '日志错误突增预警',
  summary: '最近 10 分钟检测到 23 条错误日志，出现异常峰值。\n最新错误样例：\n'
    + '• Nov 03 14:12:09 parlemiao-dev conveyor-telegram-bot[812345]: ERROR handlers.chat: upstream model call failed after 3 retries: '
    + 'httpx.ReadTimeout: timed out while reading response from https://api.deepseek.com/chat/completions (request_id=6f1c0a9e-2b47-4c1e-9d3a-5e8f7b6a4c21)\n'
    + '• Nov 03 14:12:10 parlemiao-dev conveyor-scheduler[901234]: ERROR routines: routine morning-digest failed: TimeoutError\n'
    + '• Nov 03 14:12:41 parlemiao-dev kernel: [8812.441] Out of memory: Killed process 77123 (chromium) total-vm:4302156kB, anon-rss:1203448kB',
  fingerprint: 'host.logs.error_burst', created_at: ago(18),
}
const SERVICE_ALERT = {
  source: 'host.service', severity: 'critical', title: '系统服务异常: conveyor-feishu-bot',
  summary: '服务状态为 `failed` (Result: exit-code)，未处于正常运行状态。',
  fingerprint: 'host.service.conveyor-feishu-bot', created_at: ago(60 * 3 + 5),
}
const CI_ALERT = {
  source: 'repo.ci', severity: 'critical', title: '自动构建没有通过',
  summary: '仓库「mammut001/Conveyor」默认分支（main）最新一次自动构建失败。\n✗ web-console / e2e (pull_request) failed in 4m12s\n✗ python / 运行测试 failed in 2m03s',
  fingerprint: 'repo.ci.main', created_at: ago(60 * 30),
}
const GIT_ALERT = {
  source: 'repo.git', severity: 'info', title: '工作区积累了较多未提交变更',
  summary: '主仓库 `Conveyor` 当前有 37 个未提交或未跟踪文件。', fingerprint: 'repo.git.Conveyor', created_at: ago(60 * 50),
}
const CPU_ALERT = {
  source: 'host.cpu', severity: 'warning', title: '系统 CPU 负载过高',
  summary: '1分钟负载达到 9.84 (4 核 CPU，负载比例 2.5x)。\n高耗能进程排名前列：\n77123 93.1 chromium --headless\n812345 41.0 python bot.py',
  fingerprint: 'host.cpu.high_load', created_at: NOW.toISOString(),
  suggested_actions: [
    { label: '查看实时进程快照', command: '/ps' },
    { label: '运行主机系统诊断', command: '/diagnose' },
  ],
}

function teammateStatus(overrides: Record<string, unknown> = {}) {
  return {
    enabled: true, is_paused: false, paused_until: null, muted_sources: ['repo.git'],
    interval_seconds: 300, cooldown_seconds: 7200, last_patrol_at: ago(3), total_alerts_count: 12,
    recent_alerts: [DISK_ALERT, LOG_ALERT, SERVICE_ALERT, CI_ALERT],
    monitored_services: ['conveyor-telegram-bot', 'conveyor-feishu-bot', 'conveyor-vps-computer', 'conveyor-scheduler'],
    thresholds: { disk_pct: 90, disk_gb: 3, load_ratio: 2, error_burst: 5 },
    status_text: '',
    ...overrides,
  }
}

type Calls = { method: string; path: string; body: unknown }[]
type Fixture = {
  secrets?: Secret[]
  secretsError?: boolean
  grants?: typeof GRANTS
  egress?: typeof EGRESS
  killed?: boolean
  approvalsError?: string
  killError?: string
  teammate?: ReturnType<typeof teammateStatus>
  mobileUI?: boolean
}

async function installApi(page: Page, fixture: Fixture = {}) {
  const calls: Calls = []
  let secrets = [...(fixture.secrets ?? SECRETS)]
  let grants = [...(fixture.grants ?? GRANTS)]
  let killed = Boolean(fixture.killed)
  let teammate = fixture.teammate ?? teammateStatus()
  await page.clock.setFixedTime(NOW)
  await page.addInitScript(() => sessionStorage.setItem('conveyor-token', 'ux-guard-e2e-token-not-a-secret'))
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    const body = method === 'GET' ? null : request.postDataJSON()
    if (method !== 'GET') calls.push({ method, path, body })
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })

    if (path === '/api/health') return json({ ok: true })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: true, agents: AGENTS })
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0, 0, 0], cpu_count: 4,
      memory: { total: null, available: null }, disk: { total: 1000, used: 0, free: 1000 },
      queue: { depth: 0, paused: false, states: {} },
      channels: { telegram: { configured: true }, feishu: { configured: false } }, nodes: [],
      features: { agents: true, long_term_memory: true, routines: true, teammate: true, live_screen: true, mobile_ui: Boolean(fixture.mobileUI) },
    })

    // 密钥
    if (path === '/api/secrets' && method === 'GET') {
      if (fixture.secretsError) return json({ error: 'secret store unavailable' }, 500)
      return json({ items: secrets })
    }
    if (path === '/api/secrets' && method === 'POST') {
      const { name } = body as { name: string; value: string }
      const item = { name, set: true, updated_at: NOW.toISOString() }
      secrets = [...secrets.filter(s => s.name !== name), item].sort((a, b) => a.name.localeCompare(b.name))
      return json(item, 201)
    }
    if (path.startsWith('/api/secrets/') && method === 'DELETE') {
      const name = decodeURIComponent(path.slice('/api/secrets/'.length))
      secrets = secrets.map(s => s.name === name ? { name, set: false, updated_at: NOW.toISOString() } : s)
      return json({ ok: true })
    }

    // 网站授权
    if (path === '/api/browser/approvals' && method === 'GET') {
      if (fixture.approvalsError) return json({ error: fixture.approvalsError }, 409)
      return json({ approvals: grants, kill_switch: killed })
    }
    if (path === '/api/browser/egress') {
      const limit = Number(url.searchParams.get('limit') || 50)
      return json({ requests: (fixture.egress ?? EGRESS).slice(0, limit) })
    }
    if (path === '/api/browser/kill' && method === 'POST') {
      if (fixture.killError) return json({ error: fixture.killError }, 409)
      killed = (body as { enabled: boolean }).enabled
      return json({ kill_switch: killed })
    }
    if (path.startsWith('/api/browser/approvals/') && method === 'DELETE') {
      const id = path.split('/').pop()
      grants = grants.filter(g => g.id !== id)
      return json({ ok: true })
    }

    // 值守
    if (path === '/api/teammate/status') return json(teammate)
    if (path === '/api/teammate/patrol') return json({
      ok: true, is_healthy: false, alerts: [CPU_ALERT], suppressed: [GIT_ALERT], timestamp: NOW.toISOString(),
    })
    if (path === '/api/teammate/action') {
      const { action, value } = body as { action: string; value?: unknown }
      if (action === 'pause') teammate = { ...teammate, is_paused: true, paused_until: new Date(NOW.getTime() + Number(value) * 3600_000).toISOString() }
      if (action === 'resume') teammate = { ...teammate, is_paused: false, paused_until: null }
      if (action === 'mute') teammate = { ...teammate, muted_sources: [...teammate.muted_sources, String(value)] }
      if (action === 'unmute') teammate = { ...teammate, muted_sources: teammate.muted_sources.filter(s => s !== value) }
      return json({ ok: true, action })
    }
    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })
  return calls
}

async function open(page: Page, view: 'secrets' | 'browser' | 'teammate', size: Size) {
  await page.setViewportSize(size)
  await page.goto(view === 'browser' ? '/' : `/#${view}`)
  if (view === 'browser') {
    // Reached through the navigation: the top bar on desktop, the bottom bar's 更多 sheet on phones.
    await expect(page.locator('main.app-shell')).toBeVisible()
    const more = page.getByRole('navigation', { name: '底部导航' }).getByRole('button', { name: '更多' })
    if (await more.isVisible()) await more.click()
    const name = /^\W*(Browser sites|网站授权)$/
    await page.getByRole('button', { name }).or(page.getByRole('link', { name })).or(page.getByRole('tab', { name })).filter({ visible: true }).first().click()
  }
  const panel = page.locator(`.guard-page.guard-${view}`)
  await expect(panel).toBeVisible()
  return panel
}

/** Full-page screenshot, plus a "-full" one with the viewport grown until the panel's own scroll area fits. */
async function shoot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready)
  const size = page.viewportSize()!
  await page.screenshot({ path: `${SHOTS}/${name}-${size.width}.png`, fullPage: true, animations: 'disabled' })
  let height = size.height
  for (let attempt = 0; attempt < 4; attempt++) {
    const hidden = await page.locator('.guard-page').evaluate(el => el.scrollHeight - el.clientHeight)
    if (hidden <= 1) break
    height += hidden
    await page.setViewportSize({ width: size.width, height })
  }
  if (height === size.height) return
  await page.screenshot({ path: `${SHOTS}/${name}-${size.width}-full.png`, fullPage: true, animations: 'disabled' })
  await page.setViewportSize(size)
}

/** The panel itself must never scroll sideways or poke out of a phone-width viewport. */
async function expectNoSidewaysOverflow(page: Page) {
  const overflow = await page.locator('.guard-page').evaluate(el => ({
    inner: el.scrollWidth - el.clientWidth,
    right: el.getBoundingClientRect().right - window.innerWidth,
  }))
  expect(overflow.inner).toBeLessThanOrEqual(1)
  expect(overflow.right).toBeLessThanOrEqual(1)
}

/** UI copy outside code, hosts and server-provided alert text must be Chinese. */
async function expectChineseChrome(page: Page) {
  const leftovers = await page.locator('.guard-page').evaluate(root => {
    const clone = root.cloneNode(true) as HTMLElement
    clone.querySelectorAll('code, pre, .guard-tech, .guard-cell-host, .guard-host, .guard-alert-title, .guard-alert-summary, .guard-chip[title], dialog, option, input')
      .forEach(node => node.remove())
    const allowed = new Set(['CPU', 'Git', 'GitHub', 'CI', 'GB', 'KB', 'Cookie', 'IP', 'main', 'Telegram'])
    return (clone.textContent || '').match(/[A-Za-z]{2,}/g)?.filter(word => !allowed.has(word)) ?? []
  })
  expect(leftovers).toEqual([])
}

test.use({ timezoneId: 'Asia/Shanghai', locale: 'zh-CN' })

for (const size of SIZES) {
  test(`密钥 page at ${size.width}: populated, validation, save and delete`, async ({ page }) => {
    const calls = await installApi(page)
    const panel = await open(page, 'secrets', size)
    await expect(panel.getByText('密钥只保存在这台服务器上，保存后不会再显示。')).toBeVisible()
    await expect(panel.getByLabel('名称')).toHaveAttribute('placeholder', '例如 DEEPSEEK_API_KEY')
    await expect(panel.getByRole('heading', { name: '已保存的密钥' })).toBeVisible()
    await expect(panel.getByText('3 个')).toBeVisible()
    await expect(panel.locator('.guard-secret-row', { hasText: 'OPENAI_API_KEY' }).getByText('已保存')).toBeVisible()
    await expect(panel.locator('.guard-secret-row', { hasText: 'ANTHROPIC_API_KEY' }).getByText('已删除')).toBeVisible()
    await expect(panel.getByText('11月3日 09:05 更新')).toBeVisible()
    await expect(panel.getByText('网站登录')).toBeVisible()
    await expectNoSidewaysOverflow(page)
    await expectChineseChrome(page)
    await shoot(page, 'secrets')

    // Client-side validation explains what is wrong, in Chinese, next to the field.
    await panel.getByRole('button', { name: '保存', exact: true }).click()
    await expect(panel.getByText('请填写名称。')).toBeVisible()
    await expect(panel.getByText('请填写值。')).toBeVisible()
    await expect(panel.getByLabel('名称')).toBeFocused()
    await shoot(page, 'secrets-invalid')

    // Lower-case input is upper-cased; 显示/隐藏 toggles the password field.
    await panel.getByLabel('名称').fill('moonshot_api_key')
    await expect(panel.getByLabel('名称')).toHaveValue('MOONSHOT_API_KEY')
    const value = panel.locator('#secret-value')
    await value.fill('sk-test-0000-not-a-real-key')
    await expect(value).toHaveAttribute('type', 'password')
    await panel.getByRole('button', { name: '显示值' }).click()
    await expect(value).toHaveAttribute('type', 'text')
    await panel.getByRole('button', { name: '隐藏值' }).click()
    await expect(value).toHaveAttribute('type', 'password')
    await panel.getByRole('button', { name: '保存', exact: true }).click()
    await expect(panel.getByRole('status').filter({ hasText: '已保存 MOONSHOT_API_KEY' })).toBeVisible()
    expect(calls).toContainEqual({ method: 'POST', path: '/api/secrets', body: { name: 'MOONSHOT_API_KEY', value: 'sk-test-0000-not-a-real-key' } })
    await expect(panel.getByLabel('名称')).toHaveValue('')
    await expect(value).toHaveValue('')
    await expect(panel.getByText('4 个')).toBeVisible()
    await shoot(page, 'secrets-saved')

    // 更换 pre-fills the name and moves to the value field.
    await panel.getByRole('button', { name: '更换 DEEPSEEK_API_KEY' }).click()
    await expect(panel.getByLabel('名称')).toHaveValue('DEEPSEEK_API_KEY')
    await expect(value).toBeFocused()

    // Delete asks inline first; Esc backs out, 确认删除 calls the API.
    await panel.getByRole('button', { name: '删除 OPENAI_API_KEY' }).click()
    const confirm = panel.getByRole('group', { name: '确认删除 OPENAI_API_KEY' })
    await expect(confirm).toContainText('用到它的模型服务将无法调用')
    await expect(confirm.getByRole('button', { name: '取消' })).toBeFocused()
    await shoot(page, 'secrets-confirm-delete')
    await page.keyboard.press('Escape')
    await expect(confirm).toHaveCount(0)
    await expect(panel.getByRole('button', { name: '删除 OPENAI_API_KEY' })).toBeFocused()
    await panel.getByRole('button', { name: '删除 OPENAI_API_KEY' }).click()
    await confirm.getByRole('button', { name: '确认删除' }).click()
    await expect(panel.getByRole('status').filter({ hasText: '已删除 OPENAI_API_KEY。' })).toBeVisible()
    expect(calls).toContainEqual({ method: 'DELETE', path: '/api/secrets/OPENAI_API_KEY', body: null })
    await expect(panel.locator('.guard-secret-row', { hasText: 'OPENAI_API_KEY' }).getByText('已删除')).toBeVisible()
  })

  test(`密钥 page at ${size.width}: empty and error states`, async ({ page }) => {
    await installApi(page, { secrets: [] })
    const panel = await open(page, 'secrets', size)
    await expect(panel.getByText('还没有保存任何密钥')).toBeVisible()
    await expectNoSidewaysOverflow(page)
    await expectChineseChrome(page)
    await shoot(page, 'secrets-empty')

    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await installApi(page, { secretsError: true })
    await page.reload()
    await expect(panel.getByRole('alert')).toContainText('无法读取密钥列表，请稍后重试。')
    await expect(panel.locator('.guard-tech')).toHaveText('secret store unavailable')
    await shoot(page, 'secrets-error')
  })

  test(`网站授权 page at ${size.width}: tables, kill switch and revoke`, async ({ page }) => {
    const calls = await installApi(page)
    const panel = await open(page, 'browser', size)
    await expect(panel.getByText('助手访问外部网站前需要你批准；这里列出已批准的网站和最近的访问记录。')).toBeVisible()
    await expect(panel.getByRole('heading', { name: '外部浏览：正常' })).toBeVisible()
    await expect(panel.getByRole('heading', { name: '已批准的网站' })).toBeVisible()
    await expect(panel.getByRole('heading', { name: '最近访问记录' })).toBeVisible()
    for (const header of ['网址', '允许的操作', '到期时间', '批准人', '时间', '网站', '结果', '说明']) {
      await expect(panel.locator('th', { hasText: header }).first()).toBeAttached()
    }
    await expect(panel.getByText('11月3日 17:30').first()).toBeVisible()
    await expect(panel.getByText('还剩 3 小时')).toBeVisible()
    await expect(panel.getByText('页面操作：点击链接、翻页')).toBeVisible()
    await expect(panel.getByText('已批准的只读访问')).toBeVisible()
    await expect(panel.getByText('今天 14:28:00')).toBeVisible()
    await expect(panel.getByText('已拦截').first()).toBeVisible()
    await expect(panel.getByText('为保护隐私，这里不记录网址路径、搜索内容、Cookie、提交的数据和网页文字。')).toBeVisible()
    await expectNoSidewaysOverflow(page)
    await expectChineseChrome(page)
    await shoot(page, 'browser')

    // Revoke asks inline and says exactly what changes.
    await panel.getByRole('button', { name: '撤销 news.ycombinator.com/news 的授权' }).click()
    const revoke = panel.getByRole('group', { name: '确认撤销 news.ycombinator.com/news 的授权' })
    await expect(revoke).toContainText('助手不能再在 news.ycombinator.com/news 上点击链接、翻页，但仍可以查看这个页面。')
    await shoot(page, 'browser-confirm-revoke')
    await revoke.getByRole('button', { name: '确认撤销' }).click()
    await expect(panel.getByRole('status').filter({ hasText: '已撤销 news.ycombinator.com/news 的授权。' })).toBeVisible()
    expect(calls).toContainEqual({ method: 'DELETE', path: `/api/browser/approvals/${'d'.repeat(32)}`, body: null })

    // 紧急关闭 needs a confirmation that explains the effect; Esc closes it without acting.
    await panel.getByRole('button', { name: '紧急关闭外部浏览' }).click()
    const dialog = page.getByRole('dialog', { name: '紧急关闭外部浏览？' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('助手不能再访问任何外部网站，正在进行的网页访问会被中断。')
    await expect(dialog.getByRole('button', { name: '取消' })).toBeFocused()
    await shoot(page, 'browser-kill-confirm')
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    expect(calls.filter(call => call.path === '/api/browser/kill')).toEqual([])

    await panel.getByRole('button', { name: '紧急关闭外部浏览' }).click()
    await dialog.getByRole('button', { name: '立即关闭' }).click()
    await expect(dialog).toBeHidden()
    await expect(panel.getByRole('heading', { name: '外部浏览：已紧急关闭' })).toBeVisible()
    expect(calls).toContainEqual({ method: 'POST', path: '/api/browser/kill', body: { enabled: true } })
    await shoot(page, 'browser-killed')
    await panel.getByRole('button', { name: '恢复外部浏览' }).click()
    await expect(panel.getByRole('heading', { name: '外部浏览：正常' })).toBeVisible()
    expect(calls).toContainEqual({ method: 'POST', path: '/api/browser/kill', body: { enabled: false } })

    // The journal size picker still reloads with the chosen limit.
    const egress = page.waitForRequest(request => request.url().includes('/api/browser/egress?limit=100'))
    await panel.getByRole('combobox').selectOption('100')
    await egress
  })

  test(`网站授权 page at ${size.width}: unreadable state and refused kill switch`, async ({ page }) => {
    await installApi(page, { approvalsError: 'kill_path_unsafe' })
    const panel = await open(page, 'browser', size)
    await expect(panel.getByRole('heading', { name: '外部浏览：状态未知' })).toBeVisible()
    await expect(panel.getByRole('alert').filter({ hasText: '无法读取网站授权，请稍后重试。' })).toContainText('kill_path_unsafe')
    await expect(panel.getByText('暂时读取不到批准记录。')).toBeVisible()
    await expect(panel.getByText('还没有批准任何网站')).toHaveCount(0)
    // In an emergency the switch must stay usable even when the status cannot be read.
    await expect(panel.getByRole('button', { name: '紧急关闭外部浏览' })).toBeEnabled()
    await expectNoSidewaysOverflow(page)
    await shoot(page, 'browser-error')

    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await installApi(page, { killError: 'kill_path_unmanaged' })
    await open(page, 'browser', size)
    await expect(panel.getByRole('heading', { name: '外部浏览：正常' })).toBeVisible()
    await panel.getByRole('button', { name: '紧急关闭外部浏览' }).click()
    await page.getByRole('dialog', { name: '紧急关闭外部浏览？' }).getByRole('button', { name: '立即关闭' }).click()
    await expect(panel.getByRole('button', { name: '刷新' })).toBeEnabled()
    // The refusal must survive the reload that follows the attempt.
    await expect(panel.getByRole('alert')).toContainText('服务器使用了自定义的紧急开关文件，只能在服务器上切换。')
    await expect(panel.getByRole('heading', { name: '外部浏览：正常' })).toBeVisible()
  })

  test(`网站授权 page at ${size.width}: empty state`, async ({ page }) => {
    await installApi(page, { grants: [], egress: [] })
    const panel = await open(page, 'browser', size)
    await expect(panel.getByText('还没有批准任何网站。助手需要访问网站时，会先征求你的同意。')).toBeVisible()
    await expect(panel.getByText('还没有访问记录。')).toBeVisible()
    await expectNoSidewaysOverflow(page)
    await expectChineseChrome(page)
    await shoot(page, 'browser-empty')
  })

  test(`值守 page at ${size.width}: status, alerts, patrol, pause and mute`, async ({ page }) => {
    const calls = await installApi(page)
    const panel = await open(page, 'teammate', size)
    await expect(panel.getByRole('heading', { name: '值守中' })).toBeVisible()
    await expect(panel.getByText('每 5 分钟自动巡检一次 · 上次巡检：3 分钟前')).toBeVisible()
    for (const name of ['立即巡检', '暂停 4 小时', '暂停 24 小时', '刷新']) {
      await expect(panel.getByRole('button', { name, exact: true })).toBeVisible()
    }
    await expect(panel.getByRole('heading', { name: '最近告警' })).toBeVisible()
    await expect(panel.getByText('累计 12 次')).toBeVisible()
    await expect(panel.locator('.guard-alert.is-critical')).toHaveCount(2)
    await expect(panel.locator('.guard-alert', { hasText: '日志错误突增预警' }).getByText('14 分钟前')).toHaveCount(0)
    await expect(panel.locator('.guard-alert', { hasText: '日志错误突增预警' }).getByText('18 分钟前')).toBeVisible()
    await expect(panel.locator('.guard-alert', { hasText: '自动构建没有通过' }).getByText('11月2日 08:30')).toBeVisible()
    for (const title of ['磁盘空间', 'CPU 负载', '系统服务', '错误日志', '代码仓库与自动构建']) {
      await expect(panel.getByRole('heading', { name: title, exact: true })).toBeVisible()
    }
    await expect(panel.getByText('飞书机器人')).toBeVisible()
    await expect(panel.getByText('还没有静音任何来源。')).toHaveCount(0)
    await expect(panel.getByRole('button', { name: '取消静音 代码改动' })).toBeVisible()
    await expect(panel.getByPlaceholder('输入告警来源，例如 host.disk')).toBeVisible()
    await expectNoSidewaysOverflow(page)
    await expectChineseChrome(page)

    // Long raw log lines live in a wrapped monospace details block.
    const logAlert = panel.locator('.guard-alert', { hasText: '日志错误突增预警' })
    await logAlert.getByText('查看详情').click()
    const pre = logAlert.locator('pre')
    await expect(pre).toBeVisible()
    expect(await pre.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true)
    await shoot(page, 'teammate')

    await panel.getByRole('button', { name: '立即巡检' }).click()
    await expect(panel.getByRole('heading', { name: '巡检发现 1 个问题' })).toBeVisible()
    await expect(panel.getByText('另有 1 个问题来自已静音的来源，没有列出。')).toBeVisible()
    await expect(panel.getByRole('button', { name: '查看实时进程快照 /ps' })).toBeVisible()
    expect(calls).toContainEqual({ method: 'POST', path: '/api/teammate/patrol', body: { force: true } })
    await shoot(page, 'teammate-patrol')

    await panel.getByRole('button', { name: '暂停 4 小时' }).click()
    await expect(panel.getByRole('heading', { name: '已暂停' })).toBeVisible()
    await expect(panel.getByText('暂停到 11月3日 18:30，期间不会推送提醒')).toBeVisible()
    await expect(panel.getByRole('status')).toHaveText('已暂停 4 小时，期间不会推送提醒。')
    expect(calls).toContainEqual({ method: 'POST', path: '/api/teammate/action', body: { action: 'pause', value: 4 } })
    await shoot(page, 'teammate-paused')
    await panel.getByRole('button', { name: '恢复值守' }).click()
    await expect(panel.getByRole('heading', { name: '值守中' })).toBeVisible()

    // Mute accepts the Chinese name as well as the source code; chips can be removed.
    await panel.getByPlaceholder('输入告警来源，例如 host.disk').fill('磁盘空间')
    await panel.getByRole('button', { name: '静音', exact: true }).click()
    expect(calls).toContainEqual({ method: 'POST', path: '/api/teammate/action', body: { action: 'mute', value: 'host.disk' } })
    await expect(panel.getByRole('button', { name: '取消静音 磁盘空间' })).toBeVisible()
    await panel.getByRole('button', { name: '取消静音 代码改动' }).click()
    expect(calls).toContainEqual({ method: 'POST', path: '/api/teammate/action', body: { action: 'unmute', value: 'repo.git' } })
    await expect(panel.getByRole('button', { name: '取消静音 代码改动' })).toHaveCount(0)

    // Module quick commands keep the send-to-chat behaviour: the command lands in the chat composer.
    // The button names the check in plain words; the slash command is only under 详情.
    await expect(panel.getByRole('button', { name: '在对话中执行 /ps' })).toHaveCount(0)
    await panel.getByRole('button', { name: '查看 CPU 占用' }).click()
    await expect(panel).toHaveCount(0)
    await expect.poll(() => page.evaluate(() => [...document.querySelectorAll('textarea')].some(t => t.value === '/ps'))).toBe(true)
  })

  test(`值守 page at ${size.width}: quiet state`, async ({ page }) => {
    await installApi(page, { teammate: teammateStatus({ recent_alerts: [], muted_sources: [], total_alerts_count: 0, last_patrol_at: null }) })
    const panel = await open(page, 'teammate', size)
    await expect(panel.getByText('最近没有告警，一切正常。')).toBeVisible()
    await expect(panel.getByText('还没有静音任何来源。')).toBeVisible()
    await expect(panel.getByText('上次巡检：尚未巡检')).toBeVisible()
    await expectNoSidewaysOverflow(page)
    await expectChineseChrome(page)
    await shoot(page, 'teammate-empty')
  })
}

// The mobile_ui flag is off in production, but its stricter CSS must not break these pages.
for (const view of ['secrets', 'teammate'] as const) {
  test(`${view} page keeps working with the mobile_ui layout`, async ({ page }) => {
    await installApi(page, { mobileUI: true })
    const panel = await open(page, view, SIZES[1])
    await expect(panel.getByRole('heading', { name: view === 'secrets' ? '已保存的密钥' : '最近告警' })).toBeVisible()
    await expectNoSidewaysOverflow(page)
    await shoot(page, `${view}-mobile-ui`)
  })
}
