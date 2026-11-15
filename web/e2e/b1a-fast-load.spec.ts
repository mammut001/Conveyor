import { expect, test, type Page, type Route } from '@playwright/test'

// 冷服务器上最慢的请求（/api/workers 要探一遍安装好的 CLI）不该拖住解锁：
//  1. 提交令牌后，essentials（sessions/jobs/approvals/agents）一回来就能进应用，
//     输入框立即可用，「正在解锁…」立刻消失；
//  2. extras 在后台自己落状态，失败也不影响已经解锁的界面；
//  3. extras 还在飞时，轮询加入同一组，不再打一次 /api/workers。
// 界面上看到的字都是简体中文。

const TOKEN = 'b1a-fast-load-e2e-token-not-a-secret-0001'
// 冷服务器探一遍 CLI 的时间：远长于一次解锁可以等的时间。
const WORKERS_DELAY_MS = 5000
// 解锁可以等的最长时间：essentials 都是本地 mock，本该瞬间回来。
const UNLOCK_BUDGET_MS = 1500

const systemStatus = () => ({
  uptime_seconds: 60,
  load_average: [0.1, 0.1, 0.1],
  cpu_count: 4,
  memory: { total: null, available: null },
  disk: { total: 1024, used: 128, free: 896 },
  queue: { depth: 0, paused: false, states: {} },
  channels: { telegram: { configured: false }, feishu: { configured: false } },
  nodes: [],
  features: { agents: false, long_term_memory: false, routines: false, teammate: true, workers: true },
})

type Trackers = { workersRequests: number; workersAnswered: boolean }

async function install(page: Page): Promise<Trackers> {
  const trackers: Trackers = { workersRequests: 0, workersAnswered: false }
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/health') return json({ ok: true, service: 'conveyor-web', schema_version: 1 })
    if (path === '/api/events/stream') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    }
    // Essentials：解锁和每一次轮询都等它们，所以这里都快。
    if (path === '/api/sessions' && method === 'GET') return json({ sessions: [] })
    if (path === '/api/jobs' && method === 'GET') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    // Extras：冷服务器上最慢的一个，解锁绝不等它。
    if (path === '/api/workers' && method === 'GET') {
      trackers.workersRequests += 1
      await new Promise(resolve => setTimeout(resolve, WORKERS_DELAY_MS))
      trackers.workersAnswered = true
      return json({ enabled: true, workers: [], active: null })
    }
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/system/status') return json(systemStatus())
    if (path === '/api/computer/status') {
      return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false })
    }
    if (path === '/api/config/provider' && method === 'GET') {
      return json({
        config: {
          provider_id: '', provider_name: '', model: '', reasoning_effort: 'minimal',
          base_url: '', wire_api: 'chat', env_key: 'OPENAI_API_KEY', api_key_configured: false,
          api_key_hint: '', config_path: '/tmp/config.toml',
        },
      })
    }
    return json({ items: [], approvals: [], sessions: [], jobs: [] })
  })
  return trackers
}

// 对话 的输入框：placeholder 以「给 」开头（给 Conveyor 发消息…）。
const composer = (page: Page) => page.locator('textarea[placeholder^="给 "]')
const unlockingButton = (page: Page) => page.getByRole('button', { name: '正在解锁…' })

async function submitToken(page: Page) {
  // 没有记住的令牌：解锁表单是第一个画面，令牌靠手打。
  await expect(page.getByRole('heading', { name: '解锁 Conveyor 控制台' })).toBeVisible()
  await page.locator('#unlock-token').fill(TOKEN)
  const submittedAt = Date.now()
  await page.getByRole('button', { name: '解锁', exact: true }).click()
  return submittedAt
}

test('提交令牌后不等最慢的 /api/workers：输入框立刻能用，「正在解锁…」立刻消失', async ({ page }) => {
  const trackers = await install(page)
  await page.goto('/#chat')

  const submittedAt = await submitToken(page)

  // 最慢的 extras 还在飞（5s），essentials 已经放行：应用已经在手上。
  await expect(composer(page)).toBeVisible({ timeout: UNLOCK_BUDGET_MS })
  await expect(unlockingButton(page)).toHaveCount(0, { timeout: UNLOCK_BUDGET_MS })
  expect(Date.now() - submittedAt).toBeLessThan(UNLOCK_BUDGET_MS)
  // 这一刻 /api/workers 还没回答：解锁确实没有等它。
  expect(trackers.workersAnswered).toBe(false)
  expect(trackers.workersRequests).toBe(1)
  // 首览清单靠 extras 说话：数据没到之前它不闪出来（没有「没有可用方式」的错状态）。
  await expect(page.locator('.setup-checklist-card')).toHaveCount(0)

  // 慢答案最后自己到了，界面一直好用（解锁没有等它，也没有闪过错状态）。
  await expect.poll(() => trackers.workersAnswered, { timeout: WORKERS_DELAY_MS + 2000 }).toBe(true)
  await expect(composer(page)).toBeVisible()
})

test('extras 还在飞时，轮询加入同一组，不再打一次 /api/workers', async ({ page }) => {
  const trackers = await install(page)
  await page.goto('/#chat')

  await submitToken(page)
  await expect(composer(page)).toBeVisible({ timeout: UNLOCK_BUDGET_MS })

  // 轮询每 3s 一次：等到第二次轮询之后（/api/workers 的 5s 还没到），
  // 它仍然只发起过一次——慢的那一组被复用，没有被重启。
  await page.waitForTimeout(4000)
  expect(trackers.workersRequests).toBe(1)

  // 慢答案落地后清单才拿到数据：此前「没有可用方式」绝不闪出来。
  await expect.poll(() => trackers.workersAnswered, { timeout: WORKERS_DELAY_MS + 2000 }).toBe(true)
  await expect(composer(page)).toBeVisible()
})
