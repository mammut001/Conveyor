import { expect, test, type Page, type Route } from '@playwright/test'
import { openPage } from './helpers'

const TOKEN = 'b1-calm-shell-e2e-token-not-a-secret-01'
const DESKTOP = { width: 1400, height: 900 }
const PHONE = { width: 390, height: 844 }

type FixtureOptions = {
  mobileUI?: boolean
  approvalCount?: number
}

async function installCalmShellApi(page: Page, options: FixtureOptions = {}) {
  const approvalCount = options.approvalCount ?? 0
  await page.addInitScript(token => {
    sessionStorage.setItem('conveyor-token', token)
  }, TOKEN)

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    const path = decodeURIComponent(url.pathname)
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 86_400,
        load_average: [0.42, 0.37, 0.31],
        cpu_count: 4,
        memory: { total: 8_589_934_592, available: 4_294_967_296 },
        disk: { total: 107_374_182_400, used: 64_424_509_440, free: 42_949_672_960 },
        queue: { depth: 0, paused: false, states: { running: 0, queued: 0, completed: 12, failed: 0 } },
        channels: { telegram: { configured: true }, feishu: { configured: false } },
        nodes: [],
        features: {
          agents: true,
          agent_supervisor: true,
          long_term_memory: true,
          routines: true,
          teammate: true,
          workers: true,
          live_screen: true,
          mobile_ui: Boolean(options.mobileUI),
          approval_inbox: approvalCount > 0,
          skills: false,
          workflows: false,
          mcp: false,
        },
      })
    }
    if (path === '/api/agents') {
      return json({
        enabled: true,
        agents: [
          {
            id: 'default',
            name: 'Conveyor',
            color: '#bd5c40',
            is_default: true,
            session_id: 'web:web-console:agent-default',
            instructions: '默认助手',
            workspace_path: '',
            status: 'idle',
            last_message: '你好！',
            last_message_role: 'assistant',
            last_activity: new Date().toISOString(),
            message_count: 2,
          },
        ],
      })
    }
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, active_task: null, screenshots: [] })
    if (path === '/api/agent-screens/config') return json({ error: 'not found' }, 404)
    if (path === '/api/agent-conversations') return json({ conversations: [{ id: 'default', title: '默认会话', origin_channel: 'web', session_id: 'web:web-console:agent-default' }] })
    if (/^\/api\/agents\/[^/]+\/library$/.test(path)) return json({ jobs: [], routines: [], memory: { profile: 0, log: 0 }, screenshots: [] })
    if (path === '/api/inbox') return json({ unread: 3, items: [] })
    if (path === '/api/supervisor/goals') return json({ goals: [] })
    if (path === '/api/supervisor/repositories') return json({ repositories: [] })
    if (path === '/api/supervisor/workers/summary') return json({ workers: [] })
    if (path === '/api/approval-inbox') return json({ items: [], counts: { total: approvalCount } })
    if (path === '/api/workers') return json({ enabled: true, active: 'codex', install_enabled: false, workers: [], switch_log: [] })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/memory') return json({ items: [], counts: { profile: 0, facts: 0, log: 0 }, shared: false, profile_cap: 20 })
    if (path === '/api/routines') return json({ routines: [] })
    if (path === '/api/teammate/status') {
      return json({
        enabled: true, is_paused: false, paused_until: null, muted_sources: [],
        interval_seconds: 300, cooldown_seconds: 1800, last_patrol_at: null,
        total_alerts_count: 0, recent_alerts: [], monitored_services: [],
        thresholds: { disk_pct: 85, disk_gb: 5, load_ratio: 1.5, error_burst: 20 },
        status_text: '值守中',
      })
    }
    if (path === '/api/secrets') return json({ items: [] })
    if (path === '/api/browser/approvals') return json({ kill_switch: false, approvals: [] })
    if (path === '/api/browser/egress') return json({ requests: [] })
    if (path === '/api/screen/status') return json({ enabled: false, available: false })
    if (path === '/api/takeover/status') return json({ available: false })
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    if (path === '/api/config/provider') {
      return json({
        config: {
          provider_id: 'deepseek',
          provider_name: 'DeepSeek',
          model: 'deepseek-chat',
          reasoning_effort: 'minimal',
          base_url: 'https://api.deepseek.com/v1',
          wire_api: 'chat',
          env_key: 'DEEPSEEK_API_KEY',
          api_key_configured: true,
          api_key_hint: '…1234',
        },
      })
    }
    return json({})
  })
}

const EXPECTED_MORE = [
  { label: '任务', desc: '提问、改代码和单次运行记录', hash: '#tasks' },
  { label: '记忆', desc: '助手长期记住你的偏好和背景', hash: '#memory' },
  { label: '值守', desc: '全天候值守，发现问题及时提醒', hash: '#teammate' },
  { label: '网站授权', desc: '允许助手访问和登录的网站', hash: '#browser' },
  { label: '密钥', desc: '只供助手使用的私密凭据', hash: '#secrets' },
  { label: '执行者', desc: '写代码的 AI 工具，以及它们能不能用', hash: '#workers' },
] as const

test('desktop top bar shows 对话/项目/收件箱/更多 (+设置, 锁定) and 更多 lists the six pages with descriptions', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installCalmShellApi(page)
  await page.goto('/')

  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('button')).toHaveText(['对话', '项目', /^收件箱\s*3$/, '更多'])
  const topbar = page.locator('header.topbar')
  await expect(topbar.getByRole('button', { name: '设置' })).toBeVisible()
  await expect(topbar.getByRole('button', { name: '锁定' })).toBeVisible()

  // Open 更多 popover and verify order + one-line Chinese descriptions.
  await nav.locator('.shell-nav-more > button').click()
  const menu = page.locator('#shell-nav-menu')
  await expect(menu).toBeVisible()
  await expect(menu.locator('.shell-nav-menu-title')).toHaveText(EXPECTED_MORE.map(item => item.label))
  await expect(menu.locator('.shell-nav-menu-desc')).toHaveText(EXPECTED_MORE.map(item => item.desc))

  // Clicking an item navigates there and marks 更多 as active with the current page name.
  await menu.getByRole('button', { name: '执行者', exact: true }).click()
  await expect(page).toHaveURL(/#workers$/)
  await expect(menu).toBeHidden()
  const moreBtn = nav.locator('.shell-nav-more > button')
  await expect(moreBtn).toHaveClass(/active/)
  await expect(moreBtn).toHaveText('更多 · 执行者')

  // Helper navigates cleanly to another item inside 更多.
  await openPage(page, '网站授权')
  await expect(page).toHaveURL(/#browser$/)
  await expect(moreBtn).toHaveText('更多 · 网站授权')
})

test('phone 更多 sheet lists the exact same six pages in the same order with descriptions, and bubbles badges', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installCalmShellApi(page, { mobileUI: true, approvalCount: 4 })
  await page.goto('/')

  const bottom = page.getByRole('navigation', { name: '底部导航' })
  await expect(bottom.getByRole('button')).toHaveText(['对话', '项目', '3收件箱', '4更多'])
  await bottom.getByRole('button', { name: /更多/ }).click()

  const sheet = page.getByRole('dialog', { name: '更多' })
  await expect(sheet).toBeVisible()
  await expect(sheet.locator('.mobile-sheet-items').first().locator('.mobile-sheet-title')).toHaveText([
    ...EXPECTED_MORE.map(item => item.label),
    '审批',
  ])
  await expect(sheet.locator('.mobile-sheet-desc')).toHaveText([
    ...EXPECTED_MORE.map(item => item.desc),
    '等待你确认或拒绝的重要操作',
  ])

  await page.setViewportSize(DESKTOP)
  await expect(page.getByRole('navigation', { name: '主导航' }).locator('.shell-nav-more > button .shell-nav-badge')).toHaveText('4')
})

test('#memory deep link marks 「更多 · 记忆」 active', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installCalmShellApi(page)
  await page.goto('/#memory')

  const nav = page.getByRole('navigation', { name: '主导航' })
  const moreBtn = nav.locator('.shell-nav-more > button')
  await expect(moreBtn).toHaveText('更多 · 记忆')
  await expect(moreBtn).toHaveClass(/active/)
  await expect(moreBtn).toHaveAttribute('aria-current', 'page')
  await expect(page.locator('.kn-memory')).toBeVisible()
})

test('对话 right rail is collapsed by default, remembers choice, and moves 系统 box to 设置「这台服务器」', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installCalmShellApi(page)
  await page.goto('/#chat')

  const panel = page.locator('aside.context-panel')
  const toggle = page.getByRole('button', { name: '详情' })
  await expect(panel).toBeHidden()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')

  // Expanding 对话 rail persists per browser and contains no 系统 / 负载 box.
  await toggle.click()
  await expect(panel).toBeVisible()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(panel).not.toContainText('负载')
  await expect(panel).not.toContainText('可用内存')
  await expect(panel.locator('.context-section--system')).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem('conveyor-chat-context-panel'))).toBe('open')

  await page.reload()
  await expect(panel).toBeVisible()
  await toggle.click()
  await expect(panel).toBeHidden()
  expect(await page.evaluate(() => localStorage.getItem('conveyor-chat-context-panel'))).toBe('closed')

  // 系统 information lives inside 设置 under 「这台服务器」.
  await page.locator('header.topbar').getByRole('button', { name: '设置' }).click()
  const serverSection = page.getByRole('dialog').locator('.server-status-section')
  await expect(serverSection).toBeVisible()
  await expect(serverSection.getByRole('heading', { name: '这台服务器' })).toBeVisible()
  await expect(serverSection).toContainText('负载')
  await expect(serverSection).toContainText('0.42')
  await expect(serverSection).toContainText('可用内存')
  await expect(serverSection).toContainText('可用磁盘')
  await expect(serverSection).toContainText('Telegram 已配置')
  await expect(serverSection).toContainText('飞书 未配置')
})
