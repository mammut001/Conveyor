import { expect, test, type Page, type Route } from '@playwright/test'

// Visual + behaviour check for the "manage" area: the left agent list, the agent dialog,
// the model settings dialog and the unlock screen, at desktop and phone widths.

const WIDTHS = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
] as const

const ago = (ms: number) => new Date(Date.now() - ms).toISOString()
const MINUTE = 60_000
const HOUR = 60 * MINUTE
/** Always "2 天前", whatever the time of day the test runs. */
function twoDaysAgoNoon() {
  const date = new Date()
  date.setDate(date.getDate() - 2)
  date.setHours(12, 0, 0, 0)
  return date.toISOString()
}

function makeAgents() {
  return [
    {
      id: 'default', name: 'Conveyor', color: '#f97316', is_default: true, session_id: 'web:web-console:agent-default',
      instructions: '你是我的私人助理，帮我处理日常事务。', workspace_path: '', workspace_status: '', status: 'idle',
      last_message: '✅ **登录已确认成功**，已经为你保存了 GitHub 的会话。下一步可以直接让我查看 [PR 列表](https://github.com/pulls)。',
      last_message_role: 'assistant', last_activity: ago(2 * MINUTE), message_count: 42,
    },
    {
      id: 'finance', name: 'Finance', color: '#10b981', is_default: false, session_id: 'web:web-console:agent-finance',
      instructions: '负责整理每月账单和报销。回答先给结论，再列明细。', workspace_path: '/srv/finance-reports', workspace_status: 'ok',
      status: 'working', last_message: '# 本月账单 - 信用卡：¥3,240 - 订阅：`¥168`', last_message_role: 'assistant',
      last_activity: ago(65 * MINUTE), message_count: 12,
    },
    {
      id: 'researcher', name: 'Researcher', color: '#8b5cf6', is_default: false, session_id: 'web:web-console:agent-researcher',
      instructions: '查资料并写摘要。', workspace_path: '', workspace_status: '', status: 'waiting',
      last_message: '帮我看看 arxiv 上 **多智能体协作** 的最新论文', last_message_role: 'user',
      last_activity: twoDaysAgoNoon(), message_count: 5,
    },
    {
      id: 'coder', name: 'Coder', color: '#2f7df6', is_default: false, session_id: 'web:web-console:agent-coder',
      instructions: '负责 Conveyor 仓库的代码修改与测试。', workspace_path: '/home/conveyor/Conveyor', workspace_status: 'ok',
      status: 'idle', last_message: '', last_message_role: null, last_activity: null, message_count: 0,
    },
  ]
}

const provider = {
  provider_id: 'deepseek', provider_name: 'DeepSeek', model: 'deepseek-chat', reasoning_effort: 'minimal',
  base_url: 'https://api.deepseek.com/v1', wire_api: 'chat', env_key: 'DEEPSEEK_API_KEY',
  api_key_configured: true, api_key_hint: '••••8a34', config_path: '/home/conveyor/.codex/config.toml',
}

type Options = { agentsOn?: boolean; queue?: Record<string, number>; providerFailures?: number }
const pageErrors: string[] = []
test.afterEach(() => { expect(pageErrors).toEqual([]) })

async function installApi(page: Page, options: Options = {}) {
  const agents = makeAgents()
  const posts: { path: string; method: string; body: unknown }[] = []
  let providerFailures = options.providerFailures ?? 0
  let config = { ...provider }
  pageErrors.length = 0
  page.on('pageerror', error => pageErrors.push(error.message))
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
    if ((request.headers().authorization || '') === 'Bearer wrong-token') return json({ error: 'unauthorized' }, 401)
    if (method !== 'GET') posts.push({ path, method, body: request.postDataJSON() })
    if (path === '/api/sessions') return json({ sessions: options.agentsOn === false ? [
      { id: 'web:web-console:s1', channel: 'web', title: '整理十月账单', created_at: ago(3 * HOUR), last_activity: ago(40 * MINUTE),
        job_count: 2, message_count: 6, latest_job: { id: 'j1', state: 'completed', mode: 'run', channel: 'web', chat_id: 's1', created_at: ago(HOUR), prompt_preview: '整理十月账单' } },
      { id: 'web:web-console:s2', channel: 'web', title: '', created_at: ago(5 * HOUR), last_activity: ago(4 * HOUR),
        job_count: 1, message_count: 2, latest_job: { id: 'j2', state: 'failed', mode: 'fix', channel: 'web', chat_id: 's2', created_at: ago(4 * HOUR), prompt_preview: '修复登录页' } },
    ] : [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents' && method === 'GET') return json(options.agentsOn === false ? { enabled: false, agents: [] } : { enabled: true, agents })
    if (path === '/api/agents' && method === 'POST') {
      const body = request.postDataJSON() as { name: string; color: string; instructions: string; workspace_path: string }
      const created = { ...agents[3], ...body, id: 'newagent', session_id: 'web:web-console:agent-newagent', is_default: false, status: 'idle', last_message: '', last_activity: null }
      agents.push(created)
      return json(created, 201)
    }
    const agentPath = path.match(/^\/api\/agents\/([^/]+)$/)
    if (agentPath && method === 'PUT') {
      const index = agents.findIndex(agent => agent.id === agentPath[1])
      agents[index] = { ...agents[index], ...(request.postDataJSON() as object) }
      return json(agents[index])
    }
    if (agentPath && method === 'DELETE') {
      agents.splice(agents.findIndex(agent => agent.id === agentPath[1]), 1)
      return json({ ok: true })
    }
    if (path === '/api/agent-conversations') {
      const agent = agents.find(item => item.id === url.searchParams.get('agent_id')) || agents[0]
      return json({ conversations: [{ id: agent.id, title: 'Web 原有会话', origin_channel: 'web', session_id: agent.session_id }] })
    }
    if (path === '/api/chat/history') return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    if (path === '/api/config/provider' && method === 'GET') return json(config)
    if (path === '/api/config/provider' && method === 'POST') {
      if (providerFailures > 0) { providerFailures--; return json({ error: 'internal error' }, 500) }
      const body = request.postDataJSON() as typeof provider & { api_key: string }
      config = { ...config, ...body, api_key_hint: body.api_key ? `••••${body.api_key.slice(-4)}` : config.api_key_hint }
      return json({ ok: true, config })
    }
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0.2, 0.1, 0.1], cpu_count: 2,
      memory: { total: null, available: null }, disk: { total: 1000, used: 0, free: 1000 },
      queue: { depth: 0, paused: false, states: options.queue || { completed: 31, failed: 2 } },
      channels: { telegram: { configured: true }, feishu: { configured: false } },
      nodes: [], features: { agents: options.agentsOn !== false, long_term_memory: true, routines: true, teammate: true },
    })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    if (path === '/api/agent-screens/config') return json({ error: 'not found' }, 404)
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json({ events: [] })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) return json({ diff: '' })
    if (path.startsWith('/api/sessions/')) {
      const id = decodeURIComponent(path.slice('/api/sessions/'.length))
      return json({ id, channel: 'web', created_at: ago(HOUR), last_activity: ago(HOUR), job_count: 0, messages: [], runs: [], jobs: [] })
    }
    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })
  return { posts }
}

async function unlock(page: Page) {
  await page.goto('/')
  await page.getByLabel('访问令牌').fill('ux-manage-e2e-token')
  await page.getByRole('button', { name: '解锁', exact: true }).click()
}

async function shot(page: Page, name: string, width: number) {
  await page.waitForTimeout(300) // let border/background transitions settle
  await page.screenshot({ path: `test-results/ux-manage/${name}-${width}.png`, fullPage: true })
}

async function noSidewaysScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
}

/** 设置 sits in the top bar on desktop and in the bottom bar's 更多 sheet on phones. */
async function openSettings(page: Page) {
  await expect(page.locator('main.app-shell')).toBeVisible()
  const more = page.getByRole('navigation', { name: '底部导航' }).getByRole('button', { name: '更多' })
  if (!(await more.isVisible())) return page.locator('header.topbar').getByRole('button', { name: '设置' }).click()
  await more.click()
  await page.getByRole('dialog', { name: '更多' }).getByRole('button', { name: '设置' }).click()
}

/** The agent / session list is a column on desktop; on phones it is the drawer opened from the page header. */
async function showList(page: Page) {
  await expect(page.locator('main.app-shell')).toBeVisible()
  const opener = page.getByRole('button', { name: /^(切换助手|会话列表)/ })
  if (!(await opener.isVisible()) || (await opener.getAttribute('aria-expanded')) === 'true') return
  await opener.click()
  await expect(page.getByRole('dialog', { name: /^(助手|会话)$/ })).toBeVisible()
  // Let the slide-in finish so hover checks and screenshots see the final position.
  await expect.poll(() => page.locator('aside.sessions-panel').evaluate(el => Math.round(el.getBoundingClientRect().left))).toBe(0)
}

for (const size of WIDTHS) {
  test.describe(`manage area at ${size.width}px`, () => {
    test.use({ viewport: size })

    test('agent list: Chinese heading, plain previews, status and queue line', async ({ page }) => {
      await installApi(page, { queue: { running: 1, queued: 2, completed: 31, failed: 2 } })
      await unlock(page)
      await showList(page)
      const list = page.locator('.agent-list')
      await expect(list.locator('.agent-item')).toHaveCount(4)
      await expect(page.getByRole('heading', { name: '助手', exact: true })).toBeVisible()
      // One title, also in the phone drawer (its header is the list heading).
      await expect(page.locator('aside.sessions-panel').getByText('助手', { exact: true })).toHaveCount(1)
      await expect(page.getByRole('button', { name: '新建助手' })).toBeVisible()

      const conveyor = list.locator('.agent-item').filter({ hasText: 'Conveyor' })
      await expect(conveyor).toHaveAttribute('aria-pressed', 'true')
      const preview = conveyor.locator('.agent-item-preview')
      await expect(preview).toContainText('✅ 登录已确认成功，已经为你保存了 GitHub 的会话')
      await expect(preview).toContainText('PR 列表')
      for (const leftover of ['**', '](', 'https://']) expect(await preview.textContent()).not.toContain(leftover)
      await expect(conveyor.locator('time')).toHaveText('2 分钟前')

      const finance = list.locator('.agent-item').filter({ hasText: 'Finance' })
      await expect(finance.locator('.agent-item-state')).toHaveText('运行中')
      await expect(finance.locator('.agent-item-preview')).toContainText('本月账单 · 信用卡：¥3,240 · 订阅：¥168')
      await expect(finance.locator('time')).toHaveText('1 小时前')
      const researcher = list.locator('.agent-item').filter({ hasText: 'Researcher' })
      await expect(researcher.locator('.agent-item-state')).toHaveText('待你确认')
      await expect(researcher.locator('.agent-item-preview')).toContainText('你：帮我看看 arxiv 上 多智能体协作 的最新论文')
      await expect(researcher.locator('time')).toHaveText('2 天前')
      await expect(list.locator('.agent-item').filter({ hasText: 'Coder' }).locator('.agent-item-preview')).toHaveText('还没有对话')

      await expect(page.locator('.queue-summary')).toHaveText(/运行中\s*1\s*·\s*排队\s*2/)
      await expect(page.getByText('ACTIVE QUEUE')).toHaveCount(0)
      await expect(page.getByText('YOUR TEAM')).toHaveCount(0)

      // Selecting another agent moves the highlight (and closes the phone drawer); hovering a row reveals its edit button.
      await finance.click()
      await expect(finance).toHaveAttribute('aria-pressed', 'true')
      await expect(conveyor).toHaveAttribute('aria-pressed', 'false')
      await showList(page)
      await researcher.hover()
      await expect(page.getByRole('button', { name: '编辑 Researcher' })).toHaveCSS('opacity', '1')
      await expect(page.getByRole('button', { name: '编辑 Finance' })).toHaveCSS('opacity', '0')
      await conveyor.click()
      await showList(page)
      await page.mouse.move(size.width - 5, size.height - 5)
      await noSidewaysScroll(page)
      await shot(page, 'sidebar', size.width)

      if (size.width < 600) {
        // On phones the list lives in the shell's drawer (at most 320px); it must also fit 280–340px.
        expect((await page.locator('aside.sessions-panel').boundingBox())!.width).toBeLessThanOrEqual(340)
        for (const drawer of [280, 340]) {
          await page.addStyleTag({ content: `.primary-interface .app-shell.mobile-ui .sessions-panel.drawer-open { width: ${drawer}px !important; }` })
          const box = await list.boundingBox()
          expect(box?.width).toBeLessThanOrEqual(drawer)
          for (const row of await list.locator('.agent-row').all()) {
            const rowBox = await row.boundingBox()
            expect((rowBox?.x || 0) + (rowBox?.width || 0)).toBeLessThanOrEqual(drawer)
          }
          await page.screenshot({ path: `test-results/ux-manage/sidebar-drawer${drawer}-${size.width}.png` })
        }
      }
    })

    test('new agent dialog validates in Chinese and saves with Enter', async ({ page }) => {
      const api = await installApi(page)
      await unlock(page)
      await showList(page)
      await page.getByRole('button', { name: '新建助手' }).click()
      const dialog = page.getByRole('dialog', { name: '新建助手' })
      await expect(dialog).toBeVisible()
      await expect(dialog.getByLabel('名称')).toBeFocused()
      for (const label of ['职责说明', '项目文件夹']) await expect(dialog.getByLabel(label)).toBeVisible()
      await expect(dialog.getByRole('radiogroup', { name: '头像颜色' })).toBeVisible()
      await expect(dialog.getByText('显示在助手列表和对话顶部。')).toBeVisible()

      await dialog.getByLabel('项目文件夹').fill('projects/finance')
      await dialog.getByRole('button', { name: '创建助手' }).click()
      await expect(dialog.getByText('请填写名称', { exact: true })).toBeVisible()
      await expect(dialog.getByText('请填写以 / 开头的完整路径，例如 /home/me/project')).toBeVisible()
      await expect(dialog.getByLabel('名称')).toBeFocused()
      await expect(dialog.getByLabel('名称')).toHaveAttribute('aria-invalid', 'true')
      expect(api.posts).toHaveLength(0)
      await noSidewaysScroll(page)
      await shot(page, 'new-agent-error', size.width)

      await dialog.getByLabel('名称').fill('财务助理')
      await expect(dialog.getByText('请填写名称', { exact: true })).toHaveCount(0)
      await dialog.getByLabel('项目文件夹').fill('/srv/finance')
      await dialog.getByRole('radio', { name: '紫色' }).click()
      await dialog.getByLabel('名称').press('Enter')
      await expect(dialog).toHaveCount(0)
      expect(api.posts[0]).toMatchObject({ path: '/api/agents', method: 'POST', body: { name: '财务助理', workspace_path: '/srv/finance', color: '#8b5cf6' } })
      await showList(page)
      await expect(page.locator('.agent-list .agent-item').filter({ hasText: '财务助理' })).toBeVisible()
    })

    test('edit dialog, archive confirmation and Esc', async ({ page }) => {
      const api = await installApi(page)
      await unlock(page)
      await showList(page)
      await page.locator('.agent-list .agent-item').filter({ hasText: 'Finance' }).click()
      await showList(page)
      await page.getByRole('button', { name: '编辑 Finance' }).click()
      const dialog = page.getByRole('dialog', { name: '编辑助手' })
      await expect(dialog).toBeVisible()
      await expect(dialog.getByLabel('名称')).toHaveValue('Finance')
      await expect(dialog.getByLabel('项目文件夹')).toHaveValue('/srv/finance-reports')
      await expect(dialog.getByRole('radio', { name: '绿色' })).toHaveAttribute('aria-checked', 'true')
      await noSidewaysScroll(page)
      await shot(page, 'edit-agent', size.width)

      await dialog.getByRole('button', { name: '归档', exact: true }).click()
      await expect(dialog.getByText('归档「Finance」？')).toBeVisible()
      await expect(dialog.getByText(/对话记录和文件仍保留在服务器上/)).toBeVisible()
      await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeFocused()
      await shot(page, 'archive-confirm', size.width)
      await page.keyboard.press('Escape')
      await expect(dialog.getByText('归档「Finance」？')).toHaveCount(0)
      await expect(dialog).toBeVisible()
      await page.keyboard.press('Escape')
      await expect(dialog).toHaveCount(0)
      expect(api.posts).toHaveLength(0)

      // Editing and saving sends a PUT; archiving after confirming sends a DELETE.
      await showList(page)
      await page.getByRole('button', { name: '编辑 Finance' }).click()
      await dialog.getByLabel('职责说明').fill('负责报销。')
      await dialog.getByRole('button', { name: '保存', exact: true }).click()
      await expect(dialog).toHaveCount(0)
      expect(api.posts.at(-1)).toMatchObject({ path: '/api/agents/finance', method: 'PUT', body: { instructions: '负责报销。' } })
      await showList(page)
      await page.getByRole('button', { name: '编辑 Finance' }).click()
      await dialog.getByRole('button', { name: '归档', exact: true }).click()
      await dialog.getByRole('button', { name: '确认归档' }).click()
      await expect(dialog).toHaveCount(0)
      expect(api.posts.at(-1)).toMatchObject({ path: '/api/agents/finance', method: 'DELETE' })
      await expect(page.locator('.agent-list .agent-item')).toHaveCount(3)
    })

    test('model settings: Chinese labels, validation, save states, Esc', async ({ page }) => {
      const api = await installApi(page, { providerFailures: 1 })
      await unlock(page)
      await openSettings(page)
      const dialog = page.getByRole('dialog', { name: '模型设置' })
      await expect(dialog).toBeVisible()
      for (const label of ['显示名称', '接口地址', '模型', '接口类型', 'API 密钥']) {
        await expect(dialog.getByLabel(label, { exact: true })).toBeVisible()
      }
      // 服务商代号, 密钥名称 and 推理强度 sit under the collapsed 高级 section (QA F7, F9);
      // the two machine names are one level deeper, inside 技术设置 (QA N26).
      await expect(dialog.getByLabel('服务商代号', { exact: true })).toBeHidden()
      await expect(dialog.getByLabel('推理强度', { exact: true })).toBeHidden()
      await dialog.locator('.provider-advanced > summary').click()
      await expect(dialog.getByLabel('推理强度', { exact: true })).toBeVisible()
      await expect(dialog.getByLabel('服务商代号', { exact: true })).toBeHidden()
      await dialog.locator('.provider-tech > summary').click()
      for (const label of ['服务商代号', '密钥名称']) {
        await expect(dialog.getByLabel(label, { exact: true })).toBeVisible()
      }
      await expect(dialog.getByLabel('API 密钥', { exact: true })).toHaveAttribute('placeholder', '已配置 ••••8a34，留空则保持不变')
      await expect(dialog.getByText('密钥只保存在服务器上')).toBeVisible()
      await expect(dialog.getByLabel('推理强度')).toHaveValue('minimal')
      await expect(dialog.getByLabel('推理强度').locator('option:checked')).toHaveText('最低')
      if (size.width < 600) {
        const box = await dialog.boundingBox()
        expect(box?.width).toBe(size.width)
        await expect(dialog.getByRole('button', { name: '保存', exact: true })).toBeInViewport()
      }
      await noSidewaysScroll(page)
      await shot(page, 'settings', size.width)
      if (size.width < 600) {
        await dialog.locator('.sheet-body').evaluate(element => element.scrollTo(0, element.scrollHeight))
        await page.screenshot({ path: `test-results/ux-manage/settings-bottom-${size.width}.png` })
      }

      await dialog.getByLabel('接口地址').fill('http://api.example.com/v1')
      await dialog.getByRole('button', { name: '保存', exact: true }).click()
      await expect(dialog.getByText('远程服务必须使用 https')).toBeVisible()
      await expect(dialog.getByLabel('接口地址')).toBeFocused()
      await dialog.getByLabel('接口地址').fill('https://api.example.com/v1')
      await dialog.getByRole('button', { name: '保存', exact: true }).click()
      await expect(dialog.getByRole('alert')).toContainText('服务器暂时出了问题，请稍后再试。')
      await expect(dialog.getByRole('alert')).toBeInViewport()
      await shot(page, 'settings-error', size.width)
      await dialog.getByLabel('API 密钥', { exact: true }).fill('sk-new-secret-1234')
      await dialog.getByRole('button', { name: '保存', exact: true }).click()
      await expect(dialog.getByText('✓ 已保存，新任务会使用这个配置。')).toBeVisible()
      await expect(dialog.getByLabel('API 密钥', { exact: true })).toHaveValue('')
      await expect(dialog.getByLabel('API 密钥', { exact: true })).toHaveAttribute('placeholder', '已配置 ••••1234，留空则保持不变')
      await expect(dialog.getByRole('button', { name: '完成', exact: true })).toBeVisible()
      expect(api.posts.at(-1)).toMatchObject({ path: '/api/config/provider', body: { base_url: 'https://api.example.com/v1', api_key: 'sk-new-secret-1234' } })
      await page.keyboard.press('Escape')
      await expect(dialog).toHaveCount(0)
    })

    test('unlock screen: Chinese copy, show/hide, friendly errors', async ({ page }) => {
      await installApi(page)
      await page.goto('/')
      await expect(page.getByRole('heading', { name: '解锁 Conveyor 控制台' })).toBeVisible()
      await expect(page.getByLabel('在这台设备上记住')).not.toBeChecked()
      const token = page.getByLabel('访问令牌')
      await expect(token).toHaveAttribute('placeholder', '输入访问令牌')
      await page.getByRole('button', { name: '解锁', exact: true }).click()
      await expect(page.getByRole('alert')).toHaveText('请先输入访问令牌。')

      await token.fill('wrong-token')
      await expect(token).toHaveAttribute('type', 'password')
      await page.getByRole('button', { name: '显示令牌' }).click()
      await expect(token).toHaveAttribute('type', 'text')
      await expect(page.getByRole('button', { name: '隐藏令牌' })).toBeVisible()
      await page.getByRole('button', { name: '解锁', exact: true }).click()
      await expect(page.getByRole('alert')).toHaveText('访问令牌不正确，请检查后重试。')
      await expect(token).toHaveAttribute('aria-invalid', 'true')
      await expect(page.getByText('Token rejected')).toHaveCount(0)
      await noSidewaysScroll(page)
      await shot(page, 'unlock-error', size.width)

      await token.fill('ux-manage-e2e-token')
      await expect(page.getByRole('alert')).toHaveCount(0)
      await page.getByRole('button', { name: '解锁', exact: true }).click()
      await expect(page.locator('.agent-list .agent-item')).toHaveCount(4)
    })

    test('sessions list when agents are off', async ({ page }) => {
      await installApi(page, { agentsOn: false })
      await unlock(page)
      await showList(page)
      await expect(page.getByRole('heading', { name: '会话', exact: true })).toBeVisible()
      await expect(page.locator('aside.sessions-panel').getByText('会话', { exact: true })).toHaveCount(1)
      await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible()
      const rows = page.locator('.sessions-panel .session-item')
      await expect(rows).toHaveCount(2)
      await expect(rows.first()).toContainText('整理十月账单')
      await expect(rows.first()).toContainText('6 条消息 · 2 次运行')
      await expect(rows.first().locator('time')).toHaveText('40 分钟前')
      await expect(rows.last()).toContainText('未命名会话')
      await expect(page.getByRole('button', { name: '归档会话' })).toHaveCount(2)
      if (size.width > 820) await expect(page.locator('.queue-summary')).toHaveText('队列空闲')
      await noSidewaysScroll(page)
      await shot(page, 'sessions', size.width)
    })
  })
}

test.describe('manage area on a touch phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('only the selected row shows its edit button; tapping another row selects it', async ({ page }) => {
    await installApi(page)
    await unlock(page)
    await showList(page)
    const list = page.locator('.agent-list')
    await expect(page.getByRole('button', { name: '编辑 Conveyor' })).toHaveCSS('opacity', '1')
    await expect(page.getByRole('button', { name: '编辑 Finance' })).toHaveCSS('opacity', '0')
    // Tap where Finance's hidden edit button sits: it selects Finance (closing the drawer) instead of opening the editor.
    const row = await list.locator('.agent-row').nth(1).boundingBox()
    await page.touchscreen.tap(row!.x + row!.width - 20, row!.y + row!.height / 2)
    await expect(list.locator('.agent-item').filter({ hasText: 'Finance' })).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await showList(page)
    await page.getByRole('button', { name: '编辑 Finance' }).tap()
    await expect(page.getByRole('dialog', { name: '编辑助手' })).toBeVisible()
  })
})

test('a remembered token the server now rejects asks again, in Chinese', async ({ page }) => {
  await installApi(page)
  await page.addInitScript(() => localStorage.setItem('conveyor-token', 'wrong-token'))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '解锁 Conveyor 控制台' })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveText('访问令牌不正确，请检查后重试。')
  await expect(page.getByLabel('访问令牌')).toHaveAttribute('aria-invalid', 'true')
  await expect(page.getByText(/Token rejected|Error/)).toHaveCount(0)
  // The rejected token is forgotten; the right one unlocks.
  expect(await page.evaluate(() => localStorage.getItem('conveyor-token'))).toBeNull()
  await page.getByLabel('访问令牌').fill('ux-manage-e2e-token')
  await page.getByRole('button', { name: '解锁', exact: true }).click()
  await expect(page.locator('main.app-shell')).toBeVisible()
})
