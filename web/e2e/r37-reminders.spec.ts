import { expect, test, type Page, type Route } from '@playwright/test';

const NOW = new Date('2026-11-19T09:00:00+08:00');

test.use({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });

type Reminder = {
  id: number;
  text: string;
  due_at: string;
  channel: string;
};

const agents = [
  {
    id: 'default', name: 'Conveyor', color: '#f97316', is_default: true,
    session_id: 's-default', instructions: '', workspace_path: '',
    status: 'idle', last_message: '', last_activity: null, message_count: 0,
  },
];

const provider = {
  provider_id: 'deepseek', provider_name: 'DeepSeek', model: 'deepseek-chat', reasoning_effort: 'minimal',
  base_url: 'https://api.deepseek.com/v1', wire_api: 'chat', env_key: 'DEEPSEEK_API_KEY',
  api_key_configured: true, api_key_hint: '••••8a34',
};

const pageErrors: string[] = [];

async function install(
  page: Page,
  initialReminders: Reminder[],
  options?: { routines?: boolean; systemDelayMs?: number; createError?: string },
) {
  pageErrors.length = 0;
  page.on('pageerror', error => pageErrors.push(error.message));

  let reminders = [...initialReminders];
  const cancelCalls: string[] = [];
  const createCalls: Array<{ text?: unknown }> = [];

  await page.clock.setFixedTime(NOW);
  await page.addInitScript(() => {
    localStorage.setItem('conveyor-token', 't');
  });

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    if (path === '/api/system/status') {
      if (options?.systemDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.systemDelayMs));
      }
      return json({
        uptime_seconds: 10, load_average: [0.2, 0.1, 0.1], cpu_count: 2,
        memory: { total: null, available: null }, disk: { total: 1000, used: 0, free: 1000 },
        queue: { depth: 0, paused: false, states: {} },
        channels: { telegram: { configured: true }, feishu: { configured: false } },
        nodes: [],
        features: {
          agents: true, long_term_memory: true, routines: options?.routines ?? true, teammate: true,
          approval_inbox: true, skills: true, workflows: true, mcp: true, webhooks: false,
        },
      });
    }
    if (path === '/api/agents') return json({ enabled: true, agents });
    if (path === '/api/config/provider') return json(provider);
    if (path === '/api/events/stream') {
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
    }
    if (path === '/api/inbox') return json({ unread: 0, items: [] });
    if (path === '/api/routines') return json({ routines: [] });
    if (path === '/api/reminders' && method === 'GET') return json({ reminders });
    if (path === '/api/reminders' && method === 'POST') {
      const payload = (req.postDataJSON() ?? {}) as { text?: unknown };
      createCalls.push(payload);
      if (options?.createError) {
        return json({ error: options.createError }, 400);
      }
      const created: Reminder = {
        id: 100 + reminders.length,
        text: '开会',
        due_at: '2026-11-20T00:00:00+00:00',
        channel: 'telegram',
      };
      reminders = [...reminders, created];
      return json({ reminder: created }, 201);
    }

    const cancelMatch = /^\/api\/reminders\/(\d+)\/cancel$/.exec(path);
    if (cancelMatch && method === 'POST') {
      cancelCalls.push(path);
      const id = Number(cancelMatch[1]);
      reminders = reminders.filter(r => r.id !== id);
      return json({ ok: true });
    }

    if (path === '/api/computer/status') {
      return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false });
    }
    if (path === '/api/agent-screens/config') return json({ error: 'not found' }, 404);
    if (path.startsWith('/api/sessions/')) {
      return json({ id: 'x', channel: 'web', created_at: '', last_activity: '', job_count: 0, messages: [], runs: [], jobs: [] });
    }
    return json({ items: [], approvals: [], sessions: [], jobs: [], skills: [], workflows: [], messages: [] });
  });

  await page.goto('/#inbox');
  await expect(page.locator('.kn-inbox')).toBeVisible();
  return { cancelCalls, createCalls };
}

test.describe('R37: inbox reminders', () => {
  test.afterEach(() => {
    expect(pageErrors).toEqual([]);
  });

  test('renders two reminders in order, cancels via confirmation, and keeps 390px layout clean', async ({ page }) => {
    const seed: Reminder[] = [
      { id: 7, text: '给妈妈打电话', due_at: '2026-11-20T02:00:00+00:00', channel: 'telegram' },
      { id: 12, text: '交房租和物业费（记得核对账单明细）', due_at: '2026-11-22T01:30:00+00:00', channel: 'telegram' },
    ];
    const { cancelCalls } = await install(page, seed);
    const panel = page.locator('.kn-inbox');

    // Section appears between 运行结果 and 定时任务
    const headings = panel.locator('section.kn-section > header h3');
    await expect(headings).toHaveCount(3);
    await expect(headings.nth(0)).toContainText('运行结果');
    await expect(headings.nth(1)).toContainText('提醒');
    await expect(headings.nth(2)).toContainText('定时任务');
    await expect(panel.getByText('在这里或在对话里说“提醒我……”都能添加，到点会发到 Telegram。')).toBeVisible();

    const list = panel.getByRole('list', { name: '提醒列表' });
    const rows = list.locator('li');
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText('明天 10:00');
    await expect(rows.nth(0)).toContainText('给妈妈打电话');
    await expect(rows.nth(1)).toContainText('11月22日 09:30');
    await expect(rows.nth(1)).toContainText('交房租和物业费（记得核对账单明细）');

    // 390px viewport has no horizontal scroll
    await page.setViewportSize({ width: 390, height: 844 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);

    // Click 取消 -> inline confirmation -> confirm -> POST /api/reminders/7/cancel -> row disappears
    await rows.nth(0).getByRole('button', { name: '取消' }).click();
    await expect(rows.nth(0)).toContainText('确定取消？');
    await expect(rows.nth(0).getByRole('button', { name: '不了' })).toBeVisible();
    await rows.nth(0).getByRole('button', { name: '取消提醒' }).click();

    await expect.poll(() => cancelCalls).toEqual(['/api/reminders/7/cancel']);
    await expect(rows).toHaveCount(1);
    await expect(rows.nth(0)).toContainText('交房租和物业费（记得核对账单明细）');
    await expect(panel.getByText('给妈妈打电话')).toHaveCount(0);
  });

  test('shows 提醒 section when empty, creates reminder via Enter, and keeps 390px layout clean (R76)', async ({ page }) => {
    const { createCalls } = await install(page, []);
    const panel = page.locator('.kn-inbox');

    await expect(panel.getByRole('heading', { name: /^提醒/ })).toBeVisible();
    await expect(panel.getByText('还没有提醒。')).toBeVisible();
    const input = panel.getByLabel('新建提醒');
    const addBtn = panel.getByRole('button', { name: '添加' });
    await expect(input).toBeVisible();
    await expect(addBtn).toBeDisabled();

    await page.setViewportSize({ width: 390, height: 844 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);

    await input.fill('明天早上8点 开会');
    await expect(addBtn).toBeEnabled();
    await input.press('Enter');

    await expect.poll(() => createCalls).toEqual([{ text: '明天早上8点 开会' }]);
    await expect(input).toHaveValue('');
    await expect(panel.getByRole('status')).toContainText('已添加：明天 08:00 开会');

    const list = panel.getByRole('list', { name: '提醒列表' });
    const rows = list.locator('li');
    await expect(rows).toHaveCount(1);
    await expect(rows.nth(0)).toContainText('明天 08:00');
    await expect(rows.nth(0)).toContainText('开会');
  });

  test('shows server 400 error in alert and keeps input value (R76)', async ({ page }) => {
    const { createCalls } = await install(page, [], { createError: '看不懂这个时间' });
    const panel = page.locator('.kn-inbox');

    const input = panel.getByLabel('新建提醒');
    await input.fill('随便写写');
    await panel.getByRole('button', { name: '添加' }).click();

    await expect.poll(() => createCalls).toEqual([{ text: '随便写写' }]);
    await expect(panel.getByRole('alert')).toContainText('看不懂这个时间');
    await expect(input).toHaveValue('随便写写');
  });

  test('reminders show even when routines are off (R38)', async ({ page }) => {
    const seed: Reminder[] = [
      { id: 19, text: '下午三点开项目会', due_at: '2026-11-20T02:00:00+00:00', channel: 'telegram' },
    ];
    const { cancelCalls } = await install(page, seed, { routines: false });
    const panel = page.locator('.kn-inbox');

    await expect(panel.getByText('定时任务功能未开启。')).toBeVisible();
    await expect(panel.getByRole('heading', { name: /^提醒/ })).toBeVisible();
    await expect(panel.getByText('下午三点开项目会')).toBeVisible();

    const list = panel.getByRole('list', { name: '提醒列表' });
    const rows = list.locator('li');
    await expect(rows).toHaveCount(1);
    await rows.nth(0).getByRole('button', { name: '取消' }).click();
    await expect(rows.nth(0)).toContainText('确定取消？');
    await rows.nth(0).getByRole('button', { name: '取消提醒' }).click();

    await expect.poll(() => cancelCalls).toEqual(['/api/reminders/19/cancel']);
    await expect(rows).toHaveCount(0);
    await expect(panel.getByText('下午三点开项目会')).toHaveCount(0);
  });

  test('收件箱 opened before system status loads still shows 定时任务 (R39)', async ({ page }) => {
    const statusPromise = page.waitForResponse((res) => new URL(res.url()).pathname === '/api/system/status');
    await install(page, [], { routines: true, systemDelayMs: 1500 });
    await statusPromise;
    const panel = page.locator('.kn-inbox');

    await expect(panel.getByRole('heading', { name: /^定时任务/ })).toBeVisible();
    await expect(panel.getByText('定时任务功能未开启。')).toHaveCount(0);
  });
});
