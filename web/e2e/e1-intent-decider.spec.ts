import { expect, test, type Page, type Route } from '@playwright/test'

const TOKEN = 'e1-decider-e2e-token'
const SESSION_ID = 'web:web-console:default'

async function setupCommonMocks(page: Page) {
  const apiCalls: Array<{ method: string; path: string; body: unknown }> = []
  let deciderHealth: Record<string, unknown> = {
    provider: 'off',
    consecutive_failures: 0,
    last_error_kind: null,
    last_error_at: null,
    last_ok_at: null,
    last_error_message: '',
    unhealthy: false,
  }
  let deciderConfig = {
    provider: 'off',
    configured_provider: 'off',
    auto_selected: false,
    endpoint: '',
    model: '',
    secret_name: '',
    secret_configured: false,
    available_secrets: ['OPENAI_API_KEY'],
    ready: false,
    error: '',
    health: deciderHealth,
  }
  let secrets: Array<{ name: string; set: boolean; updated_at: string | null; hint?: string }> = [
    { name: 'OPENAI_API_KEY', set: true, updated_at: '2026-11-15T10:00:00Z', hint: '…9999' },
  ]
  const clarifyCard = {
    id: 'card-clarify-1',
    title: '确认你的意思',
    text: '你是想查看项目进度吗？',
    used: false,
    outcome: '',
    buttons: [
      { id: 'act-clarify-yes', label: '是', action: 'confirm_clarified_intent', style: 'primary', payload: {} },
      { id: 'act-clarify-no', label: '不是', action: 'reject_clarified_intent', style: 'secondary', payload: {} },
    ],
  }
  const stopCard = {
    id: 'card-stop-1',
    title: '确认停止目标',
    text: '要停止 todo-app 的目标吗？已经做好的改动会保留。',
    used: false,
    outcome: '',
    buttons: [
      { id: 'act-stop-yes', label: '停止', action: 'confirm_stop_goal', style: 'primary', payload: {} },
      { id: 'act-stop-no', label: '先不停', action: 'cancel_action', style: 'secondary', payload: {} },
    ],
  }
  let clarifyRejected = false
  let stopUsed = false
  const chatRequests: string[] = []

  const setDeciderHealth = (health: Record<string, unknown>) => {
    Object.assign(deciderHealth, health)
    if (health.provider) {
      deciderConfig.provider = String(health.provider)
      deciderConfig.ready = health.provider !== 'off'
    }
  }

  await page.addInitScript(() => {
    localStorage.setItem('conveyor-token', 'e1-decider-e2e-token')
  })

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const json = (value: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })

    let body: unknown = null
    if (method === 'POST' || method === 'PUT') {
      try {
        body = req.postDataJSON()
      } catch {
        body = null
      }
    }
    apiCalls.push({ method, path, body })

    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 120,
        load_average: [0.1, 0.1, 0.1],
        cpu_count: 4,
        memory: { total: 8_000_000_000, available: 4_000_000_000 },
        disk: { total: 100_000_000_000, used: 20_000_000_000, free: 80_000_000_000 },
        queue: { depth: 0, paused: false, states: {} },
        channels: { telegram: { configured: false }, feishu: { configured: false } },
        nodes: [],
        features: { agents: false, long_term_memory: true, routines: true, teammate: true, agent_supervisor: true, chat_model_configured: true },
      })
    }
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/sessions') return json({ sessions: [] })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [] })
    if (path === '/api/inbox') return json({ unread: 0, items: [] })
    if (path === '/api/takeover/status') return json({ available: false })
    if (path === '/api/screen/status') return json({ enabled: false, available: false })
    if (path === '/api/events/stream') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    }
    if (path === '/api/config/provider') {
      return json({
        provider_id: 'deepseek',
        provider_name: 'DeepSeek',
        model: 'deepseek-chat',
        reasoning_effort: 'minimal',
        base_url: 'https://api.deepseek.com/v1',
        wire_api: 'chat',
        env_key: 'DEEPSEEK_API_KEY',
        api_key_configured: true,
        api_key_hint: '••••1234',
        config_path: '/tmp/config.toml',
        chat_model_configured: true,
      })
    }
    if (path === '/api/config/intent-decider' && method === 'GET') {
      return json(deciderConfig)
    }
    if (path === '/api/config/intent-decider' && method === 'POST') {
      const b = (body || {}) as Record<string, string>
      const p = b.provider || 'off'
      const secretName = p === 'gemini' ? 'GEMINI_API_KEY' : p === 'jev' ? 'TYPESAFE_API_KEY' : (b.secret_name || '')
      deciderConfig = {
        ...deciderConfig,
        provider: p,
        configured_provider: p,
        endpoint: b.endpoint || '',
        model: p === 'gemini' ? 'gemini-decision-1' : p === 'jev' ? (b.model || 'jev-latest') : (b.model || ''),
        secret_name: secretName,
        secret_configured: deciderConfig.available_secrets.includes(secretName),
      }
      return json({ ok: true, config: deciderConfig })
    }
    if (path === '/api/config/intent-decider/test' && method === 'POST') {
      return json({
        ok: true,
        choice: 'progress',
        choice_label: '查看进度',
        confidence: 0.98,
        latency_ms: 264,
        provider: 'gemini',
      })
    }
    if (path === '/api/secrets' && method === 'GET') {
      return json({ items: secrets })
    }
    if (path === '/api/secrets' && method === 'POST') {
      const b = (body || {}) as { name: string; value: string }
      const hint = b.value.length > 4 ? `…${b.value.slice(-4)}` : '…'
      const item = { name: b.name, set: true, updated_at: '2026-11-16T01:00:00Z', hint }
      secrets = [...secrets.filter(s => s.name !== b.name), item]
      return json({ name: b.name, set: true, updated_at: item.updated_at, hint }, 201)
    }
    if (path === '/api/chat/history' && method === 'GET') {
      return json({ session_id: SESSION_ID, messages: [] })
    }
    if (path === '/api/chat' && method === 'POST') {
      const b = (body || {}) as { message?: string }
      const text = String(b.message || '')
      chatRequests.push(text)
      if (text.includes('先停一下')) {
        const sse = [
          `event: session\ndata: ${JSON.stringify({ session_id: SESSION_ID })}\n\n`,
          `event: message\ndata: ${JSON.stringify({ text: stopCard.text, kind: 'action_card', action_card: stopCard })}\n\n`,
          'event: done\ndata: {"outcome":"answered"}\n\n',
        ].join('')
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
      }
      if (text === '进度怎么样了？' && !clarifyRejected) {
        const sse = [
          `event: session\ndata: ${JSON.stringify({ session_id: SESSION_ID })}\n\n`,
          `event: message\ndata: ${JSON.stringify({ text: clarifyCard.text, kind: 'action_card', action_card: clarifyCard })}\n\n`,
          'event: done\ndata: {"outcome":"answered"}\n\n',
        ].join('')
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
      }
      const sse = [
        `event: session\ndata: ${JSON.stringify({ session_id: SESSION_ID })}\n\n`,
        `event: message\ndata: ${JSON.stringify({ text: '这是普通对话的正常回复。' })}\n\n`,
        'event: done\ndata: {"outcome":"answered"}\n\n',
      ].join('')
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse })
    }
    if (path === '/api/chat/actions' && method === 'POST') {
      const b = (body || {}) as { action_id?: string }
      if (b.action_id === 'act-clarify-no') {
        clarifyRejected = true
        const updated = { ...clarifyCard, used: true, outcome: '好的，按普通对话回答' }
        return json({
          ok: true,
          card_id: updated.id,
          outcome: updated.outcome,
          card: updated,
          client_action: { type: 'normal_chat', text: '进度怎么样了？' },
        })
      }
      if (b.action_id === 'act-stop-yes') {
        if (stopUsed) return json({ error: '这个操作已经处理过了，不能重复使用' }, 409)
        stopUsed = true
        const updated = { ...stopCard, used: true, outcome: '已停止「todo-app」的目标' }
        return json({
          ok: true,
          card_id: updated.id,
          outcome: updated.outcome,
          card: updated,
          follow_up_message: {
            role: 'assistant',
            text: '已停止 todo-app 的目标，已经做好的改动会保留。',
            created_at: '2026-11-16T01:00:00Z',
          },
        })
      }
      return json({ error: 'unknown action' }, 404)
    }
    return json({})
  })

  return { apiCalls, chatRequests, setDeciderHealth }
}

test('理解指令 settings: provider dropdown, secret link, and 测试 output', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  const { apiCalls } = await setupCommonMocks(page)
  await page.goto('/')

  await page.getByRole('button', { name: '设置' }).click()
  const dialog = page.getByRole('dialog', { name: '模型设置' })
  await expect(dialog).toBeVisible()

  const section = dialog.getByRole('region', { name: '理解指令' })
  await expect(section).toBeVisible()
  const providerSelect = section.getByLabel('服务商')
  await expect(providerSelect).toHaveValue('off')

  // Switch to Gemini Decisions -> shows GEMINI_API_KEY + 去密钥页面添加
  await providerSelect.selectOption('gemini')
  await expect(section.locator('code', { hasText: 'GEMINI_API_KEY' })).toBeVisible()
  await expect(section.getByRole('link', { name: '去密钥页面添加' })).toBeVisible()

  // Click 测试 -> shows 「判断为：查看进度（把握 98%，264 毫秒）」
  await section.getByRole('button', { name: '测试', exact: true }).click()
  await expect(section.getByRole('status')).toContainText('判断为：查看进度（把握 98%，264 毫秒）')

  // Switch to Jev -> shows 接口地址 + 模型 (jev-latest) + TYPESAFE_API_KEY
  await providerSelect.selectOption('jev')
  await expect(section.getByLabel('接口地址')).toBeVisible()
  await expect(section.getByLabel('模型')).toHaveValue('jev-latest')
  await expect(section.locator('code', { hasText: 'TYPESAFE_API_KEY' })).toBeVisible()

  await section.getByLabel('接口地址').fill('https://jev.example.com/v1/decisions')
  await section.getByRole('button', { name: '保存理解指令' }).click()
  await expect(section.getByText('✓ 已保存理解指令设置')).toBeVisible()
  expect(apiCalls.some(c => c.method === 'POST' && c.path === '/api/config/intent-decider')).toBe(true)

  // Clicking 去密钥页面添加 closes dialog and opens 密钥 page
  await section.getByRole('link', { name: '去密钥页面添加' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.locator('.guard-page.guard-secrets')).toBeVisible()
})

test('理解指令 settings: red line when the provider stopped working, green when healthy', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  const { setDeciderHealth } = await setupCommonMocks(page)
  await page.goto('/')

  const openSection = async () => {
    await page.getByRole('button', { name: '设置' }).click()
    const dialog = page.getByRole('dialog', { name: '模型设置' })
    await expect(dialog).toBeVisible()
    const section = dialog.getByRole('region', { name: '理解指令' })
    await expect(section).toBeVisible()
    return { dialog, section }
  }

  // 关闭: no health line at all.
  let { dialog, section } = await openSection()
  await expect(section.locator('.intent-decider-health')).toHaveCount(0)
  await dialog.locator('button.close-button').click()

  // Healthy: green 「正常（上次成功：…）」.
  setDeciderHealth({
    provider: 'gemini',
    consecutive_failures: 0,
    last_error_kind: null,
    last_error_at: null,
    last_ok_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
    last_error_message: '',
    unhealthy: false,
  })
  await page.reload()
  ;({ dialog, section } = await openSection())
  await expect(section.locator('.intent-decider-health')).toHaveClass(/is-ok/)
  await expect(section.locator('.intent-decider-health')).toHaveText(/正常（上次成功：(刚刚|\d+ 分钟前)）/)
  await expect(section.locator('.intent-decider-health').getByRole('link')).toHaveCount(0)
  await dialog.locator('button.close-button').click()

  // Auth failure: red line plus a 去密钥页面 link.
  setDeciderHealth({
    provider: 'gemini',
    consecutive_failures: 3,
    last_error_kind: 'auth',
    last_error_at: '2026-11-17T09:00:00Z',
    last_ok_at: null,
    last_error_message: '密钥被拒绝了（谷歌说绑定的账号已删除或停用）',
    unhealthy: true,
  })
  await page.reload()
  ;({ dialog, section } = await openSection())
  await expect(section.locator('.intent-decider-health')).toHaveClass(/is-error/)
  await expect(section.locator('.intent-decider-health')).toContainText('理解指令暂时用不了：密钥被拒绝了（谷歌说绑定的账号已删除或停用）。')
  await expect(section.locator('.intent-decider-health').getByRole('link', { name: '去密钥页面' })).toBeVisible()
  // The link opens the 密钥 page instead of leaving the owner in the dialog.
  await section.locator('.intent-decider-health').getByRole('link', { name: '去密钥页面' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.locator('.guard-page.guard-secrets')).toBeVisible()

  // Three consecutive non-auth failures are red too, without the key link.
  setDeciderHealth({
    provider: 'gemini',
    consecutive_failures: 3,
    last_error_kind: 'timeout',
    last_error_at: '2026-11-17T09:00:00Z',
    last_ok_at: null,
    last_error_message: '等回复超时',
    unhealthy: true,
  })
  await page.reload()
  ;({ dialog, section } = await openSection())
  await expect(section.locator('.intent-decider-health')).toHaveClass(/is-error/)
  await expect(section.locator('.intent-decider-health')).toContainText('理解指令暂时用不了：等回复超时。')
  await expect(section.locator('.intent-decider-health').getByRole('link')).toHaveCount(0)
})

test('clarifying card 不是 falls back to normal chat and stop confirmation stops goal', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  const { chatRequests } = await setupCommonMocks(page)
  await page.goto('/')
  await page.getByRole('button', { name: /对话/ }).first().click()

  const composer = page.getByRole('textbox', { name: '给 Conveyor 发消息', exact: true })
  await composer.fill('进度怎么样了？')
  await composer.press('Enter')

  const clarifyCard = page.locator('.chat-action-card', { hasText: '确认你的意思' })
  await expect(clarifyCard).toBeVisible()
  await expect(page.getByText('你是想查看项目进度吗？')).toBeVisible()

  await clarifyCard.getByRole('button', { name: '不是', exact: true }).click()
  await expect(clarifyCard.getByRole('status')).toContainText('好的，按普通对话回答')
  await expect(page.getByText('这是普通对话的正常回复。')).toBeVisible()
  // User bubble is not duplicated
  await expect(page.locator('.transcript-message.role-user', { hasText: '进度怎么样了？' })).toHaveCount(1)
  expect(chatRequests).toEqual(['进度怎么样了？', '进度怎么样了？'])

  // Stop confirmation card
  await composer.fill('先停一下')
  await composer.press('Enter')
  const stopCard = page.locator('.chat-action-card', { hasText: '确认停止目标' })
  await expect(stopCard).toBeVisible()
  await expect(page.getByText('要停止 todo-app 的目标吗？已经做好的改动会保留。')).toBeVisible()

  const stopBtn = stopCard.getByRole('button', { name: '停止', exact: true })
  const keepBtn = stopCard.getByRole('button', { name: '先不停', exact: true })
  await stopBtn.click()
  await expect(stopBtn).toBeDisabled()
  await expect(keepBtn).toBeDisabled()
  await expect(stopCard.getByRole('status')).toContainText('已停止「todo-app」的目标')
  await expect(page.getByText('已停止 todo-app 的目标，已经做好的改动会保留。')).toBeVisible()
})

test('密钥 page: 名称 secret-value trap warns & disables save, saved item shows last-4 hint', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 })
  await setupCommonMocks(page)
  await page.goto('/#secrets')

  const panel = page.locator('.guard-page.guard-secrets')
  await expect(panel).toBeVisible()

  const nameInput = panel.getByLabel('名称')
  const valueInput = panel.locator('#secret-value')
  const saveBtn = panel.getByRole('button', { name: '保存', exact: true })

  // 1. Pasting lowercase into 名称 triggers warning, keeps case unchanged, and disables 保存
  await nameInput.evaluate(el => {
    const dt = new DataTransfer()
    dt.setData('text/plain', 'aizasy_lower_paste')
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true }))
  })
  await nameInput.fill('aizasy_lower_paste')
  await expect(nameInput).toHaveValue('aizasy_lower_paste')
  await expect(panel.getByText('这看起来像密钥本身，应该填到下面的“值”里')).toBeVisible()
  await expect(saveBtn).toBeDisabled()

  // 2. Typing '-' or '.' or '/' or >30 chars also triggers trap
  await nameInput.fill('sk-proj-12345')
  await expect(nameInput).toHaveValue('sk-proj-12345')
  await expect(panel.getByText('这看起来像密钥本身，应该填到下面的“值”里')).toBeVisible()
  await expect(saveBtn).toBeDisabled()

  const longVal = 'A'.repeat(31)
  await nameInput.fill(longVal)
  await expect(nameInput).toHaveValue(longVal)
  await expect(saveBtn).toBeDisabled()

  // 3. Valid name + value saves and shows last-4 hint in the list
  await nameInput.fill('GEMINI_API_KEY')
  await expect(saveBtn).toBeEnabled()
  await valueInput.fill('AIzaSyDemoSecret5678')
  await saveBtn.click()

  const geminiRow = panel.locator('.guard-secret-row', { hasText: 'GEMINI_API_KEY' })
  await expect(geminiRow).toBeVisible()
  await expect(geminiRow.locator('.guard-secret-hint')).toHaveText('…5678')
})
