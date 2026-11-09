import { expect, test, type Page, type Route } from '@playwright/test';

const SHOTS = 'test-results/r2-forms';
const NOW = Date.now();

const agents = [
  { id: 'default', name: 'Conveyor', color: '#f97316', is_default: true, session_id: 's-default', instructions: '', workspace_path: '', status: 'idle', last_message: '', last_activity: null, message_count: 0 },
  { id: 'finance', name: 'Finance', color: '#10b981', is_default: false, session_id: 's-fin', instructions: '记账', workspace_path: '/srv/f', workspace_status: 'ok', status: 'idle', last_message: '', last_activity: null, message_count: 0 },
];

const provider = {
  provider_id: 'deepseek', provider_name: 'DeepSeek', model: 'deepseek-chat', reasoning_effort: 'minimal',
  base_url: 'https://api.deepseek.com/v1', wire_api: 'chat', env_key: 'DEEPSEEK_API_KEY', api_key_configured: true, api_key_hint: '••••8a34',
};

const GOOD: Record<string, unknown> = {
  '/api/memory': { items: [{ id: 1, kind: 'facts', text: '我习惯用中文', day: '2026-11-01', created_at: new Date(NOW).toISOString(), updated_at: '', source_channel: 'web' }], counts: { profile: 0, facts: 1, log: 0 }, shared: false, profile_cap: 20 },
  '/api/inbox': { unread: 0, items: [] },
  '/api/routines': { routines: [] },
  '/api/secrets': { items: [{ name: 'GITHUB_TOKEN', set: true, updated_at: null }] },
  '/api/browser/approvals': { approvals: [], kill_switch: false },
  '/api/browser/egress': { requests: [] },
  '/api/teammate/status': { enabled: true, is_paused: false, paused_until: null, muted_sources: [], interval_seconds: 300, cooldown_seconds: 1800, last_patrol_at: null, total_alerts_count: 0, recent_alerts: [], monitored_services: [], thresholds: { disk_pct: 85, disk_gb: 5, load_ratio: 1.5, error_burst: 20 }, status_text: '值守中' },
};

type Override = { status?: number; body?: unknown; raw?: string; abort?: boolean; hang?: boolean };
const pageErrors: string[] = [];

async function install(page: Page, overrides: Record<string, Override> = {}) {
  pageErrors.length = 0;
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.addInitScript(() => { localStorage.setItem('conveyor-token', 't'); });
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    const over = overrides[path];
    if (over) {
      if (over.abort) return route.abort('failed');
      if (over.hang) return new Promise<void>(() => {});
      if (over.raw !== undefined) return route.fulfill({ status: over.status || 200, contentType: 'application/json', body: over.raw });
      return json(over.body, over.status || 200);
    }
    if (path === '/api/system/status') return json({
      uptime_seconds: 10, load_average: [0.2, 0.1, 0.1], cpu_count: 2, memory: { total: null, available: null }, disk: { total: 1000, used: 0, free: 1000 },
      queue: { depth: 0, paused: false, states: {} }, channels: { telegram: { configured: true }, feishu: { configured: false } }, nodes: [],
      features: { agents: true, long_term_memory: true, routines: true, teammate: true, approval_inbox: true, skills: true, workflows: true, mcp: true, webhooks: false },
    });
    if (path === '/api/agents') return json({ enabled: true, agents });
    if (path === '/api/config/provider') return json(provider);
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
    if (path in GOOD) return json(GOOD[path]);
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [], screen_preview_enabled: false });
    if (path === '/api/agent-screens/config') return json({ error: 'not found' }, 404);
    if (path.startsWith('/api/sessions/')) return json({ id: 'x', channel: 'web', created_at: '', last_activity: '', job_count: 0, messages: [], runs: [], jobs: [] });
    return json({ items: [], approvals: [], sessions: [], jobs: [], skills: [], workflows: [], messages: [] });
  });
}

async function goto(page: Page, view: string) {
  await page.goto('/#' + view);
  await expect(page.locator('main.app-shell')).toBeVisible();
}

test.describe('Round 2 forms fixes', () => {
  test.afterEach(() => {
    expect(pageErrors).toEqual([]);
  });

  test('F1: routine form validates cron fields inline and maps server error to concise Chinese', async ({ page }) => {
    await install(page);
    await goto(page, 'inbox');

    await page.getByRole('button', { name: '新建定时任务' }).click();
    const form = page.getByRole('form', { name: '新建定时任务' });
    await expect(form).toBeVisible();

    const nameInput = page.getByLabel('名称');
    const taskInput = page.getByLabel('要做的事');
    const cronInput = page.getByLabel(/cron 表达式/);
    const reading = page.locator('#kn-cron-reading');

    await nameInput.fill('每日整理');
    await taskInput.fill('总结今日更新');

    // 1. Minute out of range: "99 * * * *"
    await cronInput.fill('99 * * * *');
    await expect(reading).toHaveText('分钟只能是 0–59');
    await expect(reading).toHaveClass(/is-bad/);

    // 2. Hour out of range: "0 25 * * *"
    await cronInput.fill('0 25 * * *');
    await expect(reading).toHaveText('小时只能是 0–23');
    await expect(reading).toHaveClass(/is-bad/);

    // 3. Day of month out of range: "0 9 32 * *"
    await cronInput.fill('0 9 32 * *');
    await expect(reading).toHaveText('日期只能是 1–31');
    await expect(reading).toHaveClass(/is-bad/);

    // 4. Month out of range: "0 9 1 13 *"
    await cronInput.fill('0 9 1 13 *');
    await expect(reading).toHaveText('月份只能是 1–12');
    await expect(reading).toHaveClass(/is-bad/);

    // 5. Weekday out of range: "0 9 1 1 8"
    await cronInput.fill('0 9 1 1 8');
    await expect(reading).toHaveText('星期只能是 0–7');
    await expect(reading).toHaveClass(/is-bad/);

    // 6. Valid weekday 0-7: "0 9 * * 0-7"
    await cronInput.fill('0 9 * * 0-7');
    await expect(reading).not.toHaveClass(/is-bad/);

    // 7. Test client validation stops submit and displays error on invalid cron
    await cronInput.fill('99 * * * *');
    await form.getByRole('button', { name: '创建定时任务' }).click();
    await expect(form.getByRole('alert')).toContainText('分钟只能是 0–59');

    // 8. Test server cron error mapping (no raw English like "value out of range (0-59) in minute: '99'")
    await page.route('**/api/routines', route => {
      if (route.request().method() === 'POST') {
        return route.fulfill({
          status: 400,
          contentType: 'application/json',
          body: JSON.stringify({ error: "value out of range (0-59) in minute: '99'" }),
        });
      }
      return route.fallback();
    });

    // Make cron pass client check to test server error mapping
    await cronInput.fill('0 9 * * *');
    await form.getByRole('button', { name: '创建定时任务' }).click();
    const alert = form.getByRole('alert');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('分钟只能是 0–59');
    const alertText = await alert.innerText();
    expect(alertText).not.toContain('value out of range');
    expect(alertText).not.toContain('in minute');

    await page.screenshot({ path: `${SHOTS}/f1-cron-validation.png` });
  });

  test('F2: routine form button is enabled by default; empty submit shows inline errors and focuses', async ({ page }) => {
    await install(page);
    await goto(page, 'inbox');

    await page.getByRole('button', { name: '新建定时任务' }).click();
    const form = page.getByRole('form', { name: '新建定时任务' });
    await expect(form).toBeVisible();

    const createBtn = form.getByRole('button', { name: '创建定时任务' });
    // Button is enabled even when fields are empty
    await expect(createBtn).toBeEnabled();

    // 1. Submit empty -> shows "请填写名称" and focuses name field
    await createBtn.click();
    const nameInput = page.getByLabel('名称');
    await expect(page.locator('#kn-routine-name-error')).toHaveText('请填写名称');
    await expect(nameInput).toBeFocused();
    await page.screenshot({ path: `${SHOTS}/f2-routine-empty-fields.png` });

    // 2. Fill name, submit -> shows "请写下要做的事" and focuses prompt field
    await nameInput.fill('每日报表');
    // Error clears on change
    await expect(page.locator('#kn-routine-name-error')).toHaveCount(0);

    await createBtn.click();
    const taskInput = page.getByLabel('要做的事');
    await expect(page.locator('#kn-routine-task-error')).toHaveText('请写下要做的事');
    await expect(taskInput).toBeFocused();

    await taskInput.fill('汇总昨天的销售额并生成图表');
    await expect(page.locator('#kn-routine-task-error')).toHaveCount(0);
  });

  test('F3: secrets panel extra tap after successful save does not validate untouched empty form', async ({ page }) => {
    let savedSecret: unknown = null;
    await install(page);
    await page.route('**/api/secrets', async route => {
      if (route.request().method() === 'POST') {
        savedSecret = route.request().postDataJSON();
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ok: true }),
        });
      }
      return route.fallback();
    });

    await goto(page, 'secrets');
    const nameInput = page.getByLabel('名称', { exact: true });
    const valueInput = page.locator('#secret-value');
    const saveBtn = page.getByRole('button', { name: '保存', exact: true });

    await nameInput.fill('QA_TEST_KEY');
    await valueInput.fill('super-secret-token-12345');
    await saveBtn.click();

    // Verify success feedback
    const successFeedback = page.locator('.guard-feedback-ok');
    await expect(successFeedback).toBeVisible();
    await expect(successFeedback).toContainText('已保存 QA_TEST_KEY');

    // Form fields are cleared
    await expect(nameInput).toHaveValue('');
    await expect(valueInput).toHaveValue('');

    // Second tap on untouched empty form
    await saveBtn.click();

    // Errors should NOT appear next to success notice
    await expect(page.locator('.guard-field-error')).toHaveCount(0);
    await expect(successFeedback).toBeVisible();
    await expect(successFeedback).toContainText('已保存 QA_TEST_KEY');

    await page.screenshot({ path: `${SHOTS}/f3-secrets-second-tap.png` });
  });

  test('F6: memory text over 280 chars is not truncated, shows red counter and disables 记住 with reason', async ({ page }) => {
    await install(page);
    await goto(page, 'memory');

    const textarea = page.getByLabel('添加一条记忆');
    const submitBtn = page.getByRole('button', { name: '记住', exact: true });
    const counter = page.locator('#kn-memory-count-me');

    // Verify textarea has NO maxLength attribute
    await expect(textarea).not.toHaveAttribute('maxlength');

    // Paste 300 characters
    const text300 = '一'.repeat(300);
    await textarea.fill(text300);

    // Verify text is NOT truncated
    await expect(textarea).toHaveValue(text300);
    expect(await textarea.inputValue()).toHaveLength(300);

    // Verify counter shows 300 / 280, has .is-over, and indicates overflow count
    await expect(counter).toContainText('300 / 280');
    await expect(counter).toContainText('超出 20 字');
    await expect(counter).toHaveClass(/is-over/);

    // Verify "记住" button is disabled and has title with reason
    await expect(submitBtn).toBeDisabled();
    await expect(submitBtn).toHaveAttribute('title', /字数超出限制（最多 280 字）/);

    // Verify Enter key does not submit
    await textarea.press('Enter');
    await expect(page.locator('.kn-saved')).toHaveCount(0);

    await page.screenshot({ path: `${SHOTS}/f6-memory-overflow.png` });
  });

  test('env F6: agent edit dialog replaces native confirm with in-dialog confirm; Esc returns to editing', async ({ page }) => {
    await install(page);
    await goto(page, 'chat');

    let nativeConfirmCalled = false;
    page.on('dialog', async dialog => {
      nativeConfirmCalled = true;
      await dialog.dismiss();
    });

    // Open edit dialog for Finance agent
    await page.getByRole('button', { name: '编辑 Finance' }).click();
    const dialog = page.getByRole('dialog', { name: '编辑助手' });
    await expect(dialog).toBeVisible();

    const nameInput = dialog.getByLabel('名称');
    await expect(nameInput).toHaveValue('Finance');

    // Modify name to make form dirty
    await nameInput.fill('Finance Updated');

    // Press Escape
    await page.keyboard.press('Escape');

    // Confirm that native window.confirm was NOT invoked
    expect(nativeConfirmCalled).toBe(false);

    // Verify inline in-dialog confirm appeared
    const discardConfirm = dialog.locator('footer.agent-archive-confirm');
    await expect(discardConfirm).toBeVisible();
    await expect(discardConfirm.getByText('放弃还没保存的修改？')).toBeVisible();

    const keepEditingBtn = discardConfirm.getByRole('button', { name: '继续编辑' });
    const discardBtn = discardConfirm.getByRole('button', { name: '放弃修改' });
    await expect(keepEditingBtn).toBeVisible();
    await expect(discardBtn).toBeVisible();

    // Verify "继续编辑" is focused by default
    await expect(keepEditingBtn).toBeFocused();

    await page.screenshot({ path: `${SHOTS}/f5-agent-discard-confirm.png` });

    // Esc on the confirm returns to editing
    await page.keyboard.press('Escape');
    await expect(discardConfirm).toHaveCount(0);
    await expect(dialog).toBeVisible();
    await expect(nameInput).toHaveValue('Finance Updated');

    // Clicking "取消" button in main footer also triggers discard confirm
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await expect(discardConfirm).toBeVisible();

    // Clicking "放弃修改" closes the dialog
    await discardBtn.click();
    await expect(dialog).toHaveCount(0);
  });
});
