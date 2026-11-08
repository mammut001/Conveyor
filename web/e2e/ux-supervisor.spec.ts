import { expect, test, type Page, type Route } from '@playwright/test'
import type {
  SupervisorCapabilities,
  SupervisorDeployment,
  SupervisorRepo,
  SupervisorTask,
  SupervisorTaskDetail,
} from '../src/components/SupervisorWorkspace'
import type { SupervisorDiff } from '../src/components/SupervisorDiff'
import type { SupervisorChecksCandidate } from '../src/components/SupervisorChecks'

// NOTE: All tests in this spec use mocked API fixtures to verify frontend contract wiring,
// state isolation, idempotency, accessibility, and honest status rendering.
// Mocked UI tests are frontend evidence only and do NOT represent live end-to-end backend execution.

const TOKEN = 'ux-supervisor-mock-fixture-token-0001'
const DESKTOP = { width: 1440, height: 900 }
const PHONE = { width: 390, height: 844 }

// Server-detected 自动检查 candidates for the mocked Conveyor repository.
const CHECKS_CANDIDATES: SupervisorChecksCandidate[] = [
  {
    id: 'npm-test',
    label: '运行测试（npm test）',
    argv: ['npm', 'test', '--silent'],
    reason: 'package.json 的 scripts.test',
    available: true,
    unavailable_reason: '',
  },
  {
    id: 'npm-build',
    label: '构建项目（npm run build）',
    argv: ['npm', 'run', 'build'],
    reason: 'package.json 的 scripts.build',
    available: true,
    unavailable_reason: '',
  },
]

function createMockCapabilities(overrides: Partial<SupervisorCapabilities> = {}): SupervisorCapabilities {
  return {
    enabled: true,
    coordinator: { executor: 'stepfun', model: 'step-2-16k' },
    roles: {
      analyst: { executor: 'claude', model: 'claude-3-7-sonnet', available: true },
      implementer: { executor: 'antigravity', model: null, available: true },
      reviewer: { executor: 'codex', model: 'o3-mini', available: true },
    },
    executors: [
      {
        name: 'claude',
        label: 'Claude Code',
        available: true,
        models: [
          { id: 'claude-3-7-sonnet', name: 'Claude 3.7 Sonnet' },
          { id: 'claude-3-5-haiku', name: 'Claude 3.5 Haiku' },
        ],
      },
      {
        name: 'antigravity',
        label: 'Antigravity',
        available: true,
        // No models list provided: UI must offer editable text input rather than fabricating models
      },
      {
        name: 'codex',
        label: 'Codex CLI',
        available: true,
      },
      {
        name: 'cursor_unavailable',
        label: 'Cursor Agent',
        available: false,
        reason: '本地未安装 cursor-agent 可执行文件',
      },
    ],
    deployment: {
      configured: false,
      can_start: false,
      reason: '尚未配置生产部署运行器（需在服务端配置 deploy runner）',
    },
    source_machine: 'dev-workstation',
    ...overrides,
  }
}

function createInitialRepos(): SupervisorRepo[] {
  return [
    {
      id: 'repo-conveyor',
      name: 'Conveyor',
      path: '/srv/repos/conveyor',
      source_machine: 'dev-workstation',
      validation_source: 'none',
      worktrees: [
        {
          id: 'wt-main',
          path: '/srv/repos/conveyor',
          branch: 'main',
          head: '8352f46a9c01',
          is_main: true,
          managed: false,
          locked: false,
        },
        {
          id: 'wt-fix-console',
          path: '/srv/repos/conveyor-wt/fix-console',
          branch: 'fix/console',
          head: 'a1b2c3d4e5f6',
          is_main: false,
          managed: true,
          locked: false,
          chat_session_id: 'web:web-console:wt-fix-console',
        },
        {
          id: 'wt-no-chat',
          path: '/srv/repos/conveyor-wt/no-chat',
          branch: 'feat/no-chat-binding',
          head: '998877665544',
          is_main: false,
          managed: true,
          locked: false,
          // Intentionally no chat_session_id
        },
      ],
    },
    {
      id: 'repo-french',
      name: 'reussir-en-francais',
      path: '/srv/repos/reussir-en-francais',
      worktrees: [
        {
          id: 'wt-fr-main',
          path: '/srv/repos/reussir-en-francais',
          branch: 'main',
          head: '112233445566',
          is_main: true,
          managed: false,
          locked: false,
        },
      ],
    },
  ]
}

async function installSupervisorMockFixture(
  page: Page,
  options: {
    featureEnabled?: boolean
    capabilities?: SupervisorCapabilities
    initialTasksByWt?: Record<string, SupervisorTaskDetail[]>
    initialDeployments?: SupervisorDeployment[]
    delayWtTasksMs?: Record<string, number>
    diffsByTask?: Record<string, SupervisorDiff>
    diffsByGoal?: Record<string, SupervisorDiff>
    diffMissingFor?: string[]
  } = {},
) {
  const featureEnabled = options.featureEnabled ?? true
  let capabilities = options.capabilities || createMockCapabilities()
  const repos = createInitialRepos()
  const tasksByWt: Record<string, SupervisorTaskDetail[]> = {
    'wt-fix-console': [],
    'wt-no-chat': [],
    ...(options.initialTasksByWt || {}),
  }
  const deployments: SupervisorDeployment[] = [...(options.initialDeployments || [])]

  const createCalls: Array<Record<string, unknown>> = []
  const dispatchCalls: Array<{ taskId: string; body: Record<string, unknown> }> = []
  const cancelCalls: Array<{ taskId: string; body: Record<string, unknown> }> = []
  const deployStartCalls: Array<Record<string, unknown>> = []
  // 「改动」懒加载证据：只有选中一个任务 / 点开「查看改动」时才应该出现请求。
  const diffRequests: string[] = []
  const createdByRequestId = new Map<string, SupervisorTaskDetail>()

  let loseNextCreateResponse = false
  let failNextDispatchResponse = false
  let cancelReturnState: 'cancelling' | 'cancelled' = 'cancelling'
  let chatSuggestionPrompt = ''

  await page.addInitScript(token => {
    sessionStorage.setItem('conveyor-token', token)
  }, TOKEN)

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const method = request.method()
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 3600,
        load_average: [0.2, 0.2, 0.2],
        cpu_count: 4,
        memory: { total: 8_000_000_000, available: 4_000_000_000 },
        disk: { total: 100_000_000_000, used: 40_000_000_000, free: 60_000_000_000 },
        queue: { depth: 0, paused: false, states: { running: 0, completed: 3 } },
        channels: { telegram: { configured: true }, feishu: { configured: false } },
        nodes: [],
        features: {
          agents: true,
          long_term_memory: true,
          routines: true,
          teammate: true,
          agent_supervisor: featureEnabled,
        },
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

    // Chat endpoints for ChatPanel inside Code workspace
    if (path === '/api/chat/history' && method === 'GET') {
      return json({ session_id: url.searchParams.get('session_id'), messages: [] })
    }
    if (path === '/api/chat' && method === 'POST') {
      const body = request.postDataJSON() as { message: string; session_id?: string }
      const sid = body.session_id || 'web:web-console:wt-fix-console'
      const msgPayload: Record<string, unknown> = {
        text: `已收到关于「${body.message}」的讨论，我们可以先梳理方案。`,
      }
      if (chatSuggestionPrompt) {
        msgPayload.task_suggestion = {
          prompt: chatSuggestionPrompt,
          session_id: sid,
        }
      }
      const sse = [
        `event: session\ndata: ${JSON.stringify({ session_id: sid })}\n\n`,
        `event: message\ndata: ${JSON.stringify(msgPayload)}\n\n`,
        `event: done\ndata: {}\n\n`,
      ].join('')
      return route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
        body: sse,
      })
    }

    // Supervisor contract endpoints
    if (path === '/api/supervisor/capabilities' && method === 'GET') {
      return json(capabilities)
    }
    if (path === '/api/supervisor/repositories' && method === 'GET') {
      return json({ repositories: repos })
    }
    if (path === '/api/supervisor/repositories' && method === 'POST') {
      const body = request.postDataJSON() as { path: string; name?: string }
      const newRepo: SupervisorRepo = {
        id: `repo-${repos.length + 1}`,
        name: body.name || body.path.split('/').pop() || 'new-repo',
        path: body.path,
        source_machine: 'dev-workstation',
        worktrees: [
          {
            id: `wt-new-main-${repos.length + 1}`,
            path: body.path,
            branch: 'main',
            head: 'abcdef123456',
            is_main: true,
            managed: false,
            locked: false,
          },
        ],
      }
      repos.push(newRepo)
      return json(newRepo, 201)
    }

    const createWtMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/worktrees$/)
    if (createWtMatch && method === 'POST') {
      const repoId = createWtMatch[1]
      const body = request.postDataJSON() as { branch?: string; base_ref?: string }
      const repo = repos.find(r => r.id === repoId)
      if (!repo) return json({ error: 'repository not found' }, 404)
      const branch = body.branch || `worktree-${repo.worktrees.length + 1}`
      const createdWt = {
        id: `wt-managed-${Date.now()}`,
        path: `${repo.path}-wt/${branch.replace(/\//g, '-')}`,
        branch,
        head: 'a1b2c3d4e5f6',
        is_main: false,
        managed: true,
        locked: false,
        chat_session_id: `web:web-console:${branch}`,
      }
      repo.worktrees.push(createdWt)
      tasksByWt[createdWt.id] = []
      return json(createdWt, 201)
    }

    // 自动检查 contract: per-repository validation candidates (collapsed card above the dispatch card)
    const validationMatch = path.match(/^\/api\/supervisor\/repositories\/([^/]+)\/validation$/)
    if (validationMatch && method === 'GET') {
      const repoId = validationMatch[1]
      const known = repos.some(r => r.id === repoId)
      return json({
        repo_id: repoId,
        candidates: known ? CHECKS_CANDIDATES : [],
        selected: [],
        source: 'none',
        env_commands: [],
        effective: [],
      })
    }

    if (path === '/api/supervisor/tasks' && method === 'GET') {
      const wtId = url.searchParams.get('worktree_id') || ''
      const delayMs = options.delayWtTasksMs?.[wtId] || 0
      if (delayMs > 0) {
        await new Promise(resolve => setTimeout(resolve, delayMs))
      }
      const list = wtId ? (tasksByWt[wtId] || []) : Object.values(tasksByWt).flat()
      return json({ tasks: list, count: list.length })
    }

    if (path === '/api/supervisor/tasks' && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      createCalls.push(body)
      const reqId = String(body.request_id || '')
      const wtId = String(body.worktree_id || 'wt-fix-console')
      let task = reqId ? createdByRequestId.get(reqId) : undefined
      if (!task) {
        task = {
          id: `sup-task-${createCalls.length}`,
          goal: String(body.goal || ''),
          repo_path: '/srv/repos/conveyor',
          worktree_path: '/srv/repos/conveyor-wt/fix-console',
          branch: 'fix/console',
          base_sha: '8352f46',
          current_sha: 'a1b2c3d',
          state: 'created',
          stage: 'created',
          repair_round: 0,
          max_repair_rounds: 2,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          metadata: {
            repo_id: String(body.repo_id || ''),
            worktree_id: wtId,
            acceptance_criteria: body.acceptance_criteria ? String(body.acceptance_criteria) : undefined,
            session_id: body.session_id ? String(body.session_id) : undefined,
          },
          handoffs: [],
          executions: [],
          acceptance: null,
          summary: '',
        }
        if (reqId) createdByRequestId.set(reqId, task)
        tasksByWt[wtId] = [task, ...(tasksByWt[wtId] || [])]
      }
      if (loseNextCreateResponse) {
        loseNextCreateResponse = false
        return route.abort('failed')
      }
      return json(task, 201)
    }

    const taskDetailMatch = path.match(/^\/api\/supervisor\/tasks\/([^/]+)$/)
    if (taskDetailMatch && method === 'GET') {
      const taskId = taskDetailMatch[1]
      const found = Object.values(tasksByWt).flat().find(t => t.id === taskId)
      return found ? json(found) : json({ error: 'task not found' }, 404)
    }

    // 「改动」：GET /api/supervisor/tasks/<id>/diff（并行任务提供的契约，这里按同样的形状 mock）
    const taskDiffMatch = path.match(/^\/api\/supervisor\/tasks\/([^/]+)\/diff$/)
    if (taskDiffMatch && method === 'GET') {
      const taskId = taskDiffMatch[1]
      diffRequests.push(taskId)
      if ((options.diffMissingFor || []).includes(taskId)) {
        return json({ error: 'worktree is gone', code: 'worktree_missing' }, 404)
      }
      const diff = options.diffsByTask?.[taskId]
      return diff ? json(diff) : json({ error: 'task not found' }, 404)
    }

    const goalDiffMatch = path.match(/^\/api\/supervisor\/goals\/([^/]+)\/diff$/)
    if (goalDiffMatch && method === 'GET') {
      const goalId = goalDiffMatch[1]
      diffRequests.push(goalId)
      if ((options.diffMissingFor || []).includes(goalId)) {
        return json({ error: 'worktree is gone', code: 'worktree_missing' }, 404)
      }
      const diff = options.diffsByGoal?.[goalId]
      return diff ? json(diff) : json({ error: 'goal not found' }, 404)
    }

    const taskActionMatch = path.match(/^\/api\/supervisor\/tasks\/([^/]+)\/(dispatch|cancel)$/)
    if (taskActionMatch && method === 'POST') {
      const [, taskId, action] = taskActionMatch
      const body = request.postDataJSON() as Record<string, unknown>
      const found = Object.values(tasksByWt).flat().find(t => t.id === taskId)
      if (!found) return json({ error: 'task not found' }, 404)

      if (action === 'dispatch') {
        dispatchCalls.push({ taskId, body })
        if (failNextDispatchResponse) {
          failNextDispatchResponse = false
          return json({ error: '构建队列繁忙，请稍后重试派发' }, 503)
        }
        found.state = 'running'
        found.stage = 'implementation'
        found.metadata = {
          ...(found.metadata || {}),
          roles: (body.roles as Record<string, { executor?: string; model?: string | null }>) || undefined,
        }
        return json(found, 202)
      }

      if (action === 'cancel') {
        cancelCalls.push({ taskId, body })
        found.state = cancelReturnState
        found.stage = cancelReturnState === 'cancelled' ? 'cancelled' : found.stage
        return json(found, 200)
      }
    }

    if (path === '/api/supervisor/deployments' && method === 'GET') {
      const taskId = url.searchParams.get('task_id')
      const filtered = taskId ? deployments.filter(d => !d.task_id || d.task_id === taskId) : deployments
      return json({ deployments: filtered, count: filtered.length })
    }

    if (path === '/api/supervisor/deployments' && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      deployStartCalls.push(body)
      const createdDeploy: SupervisorDeployment = {
        id: `deploy-${deployments.length + 1}`,
        task_id: String(body.task_id || ''),
        source_machine: capabilities.deployment.source_machine || 'dev-workstation',
        target_machine: capabilities.deployment.target_machine || 'conveyor-vps',
        build_version: 'a1b2c3d',
        live_version: '8352f46',
        stage: 'local_ci',
        status: 'running',
        started_at: new Date().toISOString(),
        stages: [
          { stage: 'local_ci', status: 'running', detail: '正在执行 npm run build 与 pytest…', started_at: new Date().toISOString() },
        ],
      }
      deployments.unshift(createdDeploy)
      return json(createdDeploy, 202)
    }

    return json({ error: `unhandled ${method} ${path}` }, 404)
  })

  return {
    createCalls,
    dispatchCalls,
    cancelCalls,
    deployStartCalls,
    diffRequests,
    tasksByWt,
    deployments,
    setCapabilities(next: SupervisorCapabilities) {
      capabilities = next
    },
    setLoseNextCreateResponse(v: boolean) {
      loseNextCreateResponse = v
    },
    setFailNextDispatchResponse(v: boolean) {
      failNextDispatchResponse = v
    },
    setCancelReturnState(s: 'cancelling' | 'cancelled') {
      cancelReturnState = s
    },
    setChatSuggestionPrompt(p: string) {
      chatSuggestionPrompt = p
    },
  }
}

test('mock fixture: Code nav is gated by features.agent_supervisor; repo search, read-only vs managed worktree, context isolation & race protection', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, { featureEnabled: false })
  await page.goto('/')
  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('button', { name: '对话' })).toBeVisible()
  await expect(nav.getByRole('button', { name: '项目' })).toHaveCount(0)
})

test('mock fixture: repo/worktree tree selection, register repo, create managed worktree, and rapid selection race protection', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    featureEnabled: true,
    delayWtTasksMs: { 'wt-no-chat': 700 },
    initialTasksByWt: {
      'wt-no-chat': [
        {
          id: 'task-stale-wt',
          goal: '只属于 no-chat 分支的延迟任务',
          state: 'running',
          stage: 'analysis',
          metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-no-chat' },
        },
      ],
      'wt-fix-console': [],
    },
  })

  await page.goto('/#code')
  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('button', { name: '项目' })).toHaveAttribute('aria-current', 'page')

  // Default selected worktree is the managed unlocked wt-fix-console
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  // Search filters repositories
  const search = page.getByRole('searchbox', { name: '搜索仓库' })
  await search.fill('francais')
  await expect(page.locator('.sup-repo-tree').getByText('reussir-en-francais', { exact: true })).toBeVisible()
  await expect(page.locator('.sup-repo-tree').getByText('fix/console')).toHaveCount(0)
  await search.fill('')

  // Selecting main worktree shows read-only notice and disables dispatch
  await page.locator('.sup-worktree-item').filter({ hasText: 'main' }).first().click()
  await expect(page.getByRole('heading', { name: 'Conveyor / main' })).toBeVisible()
  await expect(page.getByText(/当前为只读工作目录/)).toBeVisible()
  await expect(page.getByRole('button', { name: '开始执行' })).toBeDisabled()

  // Create a new managed worktree from the read-only CTA
  await page.getByRole('button', { name: '+ 新建托管 Worktree' }).click()
  const createWtForm = page.getByRole('form', { name: '为 Conveyor 新建 Worktree' })
  await createWtForm.getByLabel('分支名称（可选）').fill('feat/supervisor-e2e')
  await createWtForm.getByRole('button', { name: '创建 Worktree' }).click()
  await expect(page.getByRole('heading', { name: 'Conveyor / feat/supervisor-e2e' })).toBeVisible()
  await expect(page.getByText(/当前为只读工作目录/)).toHaveCount(0)

  // Enter a draft goal in feat/supervisor-e2e, then rapidly click wt-no-chat (delayed 700ms) -> wt-fix-console
  const goalBox = page.getByRole('textbox', { name: '任务目标' })
  await goalBox.fill('只保存在 feat/supervisor-e2e 的草稿目标')

  await page.locator('.sup-worktree-item').filter({ hasText: 'feat/no-chat-binding' }).click()
  await page.locator('.sup-worktree-item').filter({ hasText: 'fix/console' }).click()
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()
  await expect(goalBox).toHaveValue('')

  // Wait past the 700ms delayed response from feat/no-chat-binding: it must NOT paint into fix/console
  await page.waitForTimeout(850)
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()
  await expect(page.getByText('只属于 no-chat 分支的延迟任务')).toHaveCount(0)

  // Switching back to feat/supervisor-e2e restores its isolated draft
  await page.locator('.sup-worktree-item').filter({ hasText: 'feat/supervisor-e2e' }).click()
  await expect(goalBox).toHaveValue('只保存在 feat/supervisor-e2e 的草稿目标')

  // Register a new repository via POST /api/supervisor/repositories
  await page.getByRole('button', { name: '注册仓库' }).click()
  const regForm = page.getByRole('form', { name: '注册仓库表单' })
  await regForm.getByLabel('仓库路径').fill('/srv/repos/lifemint')
  await regForm.getByLabel('显示名称（可选）').fill('LifeMint')
  await regForm.getByRole('button', { name: '确认添加' }).click()
  await expect(page.getByRole('heading', { name: 'LifeMint / main' })).toBeVisible()
})

const EVIDENCE_DIR = '/tmp/conveyor-supervisor-evidence/ui-final'

test('mock fixture: ordinary conversation never auto-dispatches; task suggestion fills reviewable goal; missing chat_session_id shows honest unbound state', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installSupervisorMockFixture(page)
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  // Worktree-bound ChatPanel uses discussionOnly copy (no execution promises or nightly commands)
  const chatPane = page.locator('.sup-chat-pane')
  await expect(chatPane.getByText('此处仅用于需求梳理、方案讨论与任务拆解，不会直接执行命令或修改代码。')).toBeVisible()
  await expect(chatPane.getByText('可以帮你查资料、跑命令、改代码，删除等危险操作会先问你。')).toHaveCount(0)
  await expect(chatPane.getByText('高级：夜间任务（不用也能正常使用）')).toHaveCount(0)

  // Ordinary chat message in the Worktree ChatPanel must NOT create or dispatch a supervisor task
  const chatInput = page.locator('.sup-chat-pane .chat-composer textarea')
  await chatInput.fill('帮我看看当前分支应该怎么重构导航栏')
  await page.locator('.sup-chat-pane .chat-send-button').click()
  await expect(page.locator('.sup-chat-pane').getByText(/已收到关于「帮我看看当前分支应该怎么重构导航栏」的讨论/)).toBeVisible()
  expect(fixture.createCalls).toHaveLength(0)
  expect(fixture.dispatchCalls).toHaveLength(0)

  // Now enable task_suggestion on the next chat reply
  fixture.setChatSuggestionPrompt('为 Code 工作台实现仓库搜索与只读分支保护')
  await chatInput.fill('请生成可执行任务建议')
  await page.locator('.sup-chat-pane .chat-send-button').click()
  const prepareBtn = page.locator('.sup-chat-pane').getByRole('button', { name: '准备执行任务 →' })
  await expect(prepareBtn).toBeVisible()

  // Clicking 准备执行任务 → populates the reviewable goal input without auto-dispatching
  await prepareBtn.click()
  const goalBox = page.getByRole('textbox', { name: '任务目标' })
  await expect(goalBox).toHaveValue('为 Code 工作台实现仓库搜索与只读分支保护')
  await expect(page.getByText('已从右侧对话填入建议目标，请检查或修改后点击「开始执行」。')).toBeVisible()
  expect(fixture.createCalls).toHaveLength(0)
  expect(fixture.dispatchCalls).toHaveLength(0)

  // Switch to wt-no-chat (no chat_session_id): must NOT fabricate a phantom chat session
  await page.locator('.sup-worktree-item').filter({ hasText: 'feat/no-chat-binding' }).click()
  await expect(page.getByText('当前工作目录尚未绑定对话会话')).toBeVisible()
  await expect(page.locator('.sup-chat-pane .chat-composer')).toHaveCount(0)

  // Ordinary top-level 对话 view remains completely unaffected (retains standard welcome & nightly help)
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '对话' }).click()
  await expect(page.getByText('可以帮你查资料、跑命令、改代码，删除等危险操作会先问你。')).toBeVisible()
  await expect(page.getByText('高级：夜间任务（不用也能正常使用）')).toBeVisible()
})

test('mock fixture: role overrides, unavailable executor blocker, explicit create+dispatch with scoped IDs, and idempotent retry', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installSupervisorMockFixture(page)
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const goalForm = page.locator('.sup-goal-composer')
  const goalBox = page.getByRole('textbox', { name: '任务目标' })
  const criteriaBox = page.getByRole('textbox', { name: '验收标准' })
  await goalBox.fill('修复 supervisor 任务取消时的状态同步')
  await criteriaBox.fill('取消接口返回 cancelling 时界面不提前显示已完成或已取消')

  // Verify goal textarea and acceptance input stretch across available form width at 1440px
  const formBoxDesktop = await goalForm.boundingBox()
  const goalBoxDesktop = await goalBox.boundingBox()
  const criteriaBoxDesktop = await criteriaBox.boundingBox()
  expect(formBoxDesktop).not.toBeNull()
  expect(goalBoxDesktop).not.toBeNull()
  expect(criteriaBoxDesktop).not.toBeNull()
  expect(goalBoxDesktop!.width).toBeGreaterThan(500)
  expect(goalBoxDesktop!.width).toBeGreaterThanOrEqual(formBoxDesktop!.width * 0.9)
  expect(criteriaBoxDesktop!.width).toBeGreaterThanOrEqual(formBoxDesktop!.width * 0.9)
  await page.screenshot({ path: `${EVIDENCE_DIR}/code-desktop-1440.png` })

  // Also verify full-width stretch at 390px mobile viewport
  await page.setViewportSize(PHONE)
  const formBoxPhone = await goalForm.boundingBox()
  const goalBoxPhone = await goalBox.boundingBox()
  const criteriaBoxPhone = await criteriaBox.boundingBox()
  expect(formBoxPhone).not.toBeNull()
  expect(goalBoxPhone).not.toBeNull()
  expect(criteriaBoxPhone).not.toBeNull()
  expect(goalBoxPhone!.width).toBeGreaterThan(280)
  expect(goalBoxPhone!.width).toBeGreaterThanOrEqual(formBoxPhone!.width * 0.9)
  expect(criteriaBoxPhone!.width).toBeGreaterThanOrEqual(formBoxPhone!.width * 0.9)
  await page.screenshot({ path: `${EVIDENCE_DIR}/code-mobile-390.png` })
  await page.setViewportSize(DESKTOP)

  // Unavailable executors that the role does not use are not offered (see the dropdown tests below)
  const implSelect = page.getByLabel('实现角色执行器')
  await expect(implSelect.locator('option', { hasText: 'Cursor Agent' })).toHaveCount(0)
  await expect(page.getByText(/本地未安装 cursor-agent 可执行文件/)).toHaveCount(0)

  // Switch back to Antigravity and configure optional model overrides
  await implSelect.selectOption('antigravity')
  await page.locator('.sup-role-advanced > summary').click()
  // Analyst (claude) has fetched models -> <select>; Implementer (antigravity) has no models -> <input>
  await page.getByLabel('分析角色模型').selectOption('claude-3-5-haiku')
  await page.getByLabel('实现角色模型').fill('gemini-3.8-flash-high')

  // 1) Simulate lost create response: server records task, browser sees network drop, retry reuses request_id & task.id
  fixture.setLoseNextCreateResponse(true)
  await page.getByRole('button', { name: '开始执行' }).click()
  await expect(page.getByText('派发未完成')).toBeVisible()
  expect(fixture.createCalls).toHaveLength(1)
  const firstRequestId = fixture.createCalls[0].request_id
  expect(typeof firstRequestId).toBe('string')
  expect((firstRequestId as string).length).toBeGreaterThan(8)

  // 2) Next retry: create succeeds with same request_id, but simulate dispatch 503 failure
  fixture.setFailNextDispatchResponse(true)
  await page.getByRole('button', { name: '重试派发' }).click()
  await expect(page.getByText('构建队列繁忙，请稍后重试派发')).toBeVisible()
  expect(fixture.createCalls).toHaveLength(2)
  expect(fixture.createCalls[1].request_id).toBe(firstRequestId)
  expect(fixture.dispatchCalls).toHaveLength(1)

  // 3) Third click on 重试派发: skips POST /api/supervisor/tasks entirely and dispatches the existing task ID!
  await page.getByRole('button', { name: '重试派发' }).click()
  expect(fixture.createCalls).toHaveLength(2) // No 3rd create call!
  expect(fixture.dispatchCalls).toHaveLength(2)
  expect(fixture.dispatchCalls[1].taskId).toBe('sup-task-1')
  expect(fixture.dispatchCalls[1].body).toMatchObject({
    acceptance_criteria: '取消接口返回 cancelling 时界面不提前显示已完成或已取消',
    roles: {
      analyst: { executor: 'claude', model: 'claude-3-5-haiku' },
      implementer: { executor: 'antigravity', model: 'gemini-3.8-flash-high' },
      reviewer: { executor: 'codex', model: 'o3-mini' },
    },
  })

  // Active task card now displays running state and stage
  const activeCard = page.getByRole('region', { name: '当前任务进度与结果' })
  await expect(activeCard).toContainText('执行中')
  await expect(activeCard).toContainText('代码实现')

  // Cancel task: backend returns 'cancelling' first -> UI shows '取消中', NOT optimistic '已取消'
  fixture.setCancelReturnState('cancelling')
  await activeCard.getByRole('button', { name: '取消任务' }).click()
  expect(fixture.cancelCalls).toHaveLength(1)
  await expect(activeCard).toContainText('取消中')
  await expect(activeCard).not.toContainText('已取消')
})

test('mock fixture: technical details & run history collapse by default, auto-expand once on failure or waiting_user, and manual collapse sticks across polling', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install()
  const fixture = await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [
        {
          id: 'sup-task-ok',
          goal: '整理 Code 页面构建脚本',
          state: 'completed',
          stage: 'completed',
          summary: '构建产物已输出到 web/dist。',
          metadata: {
            repo_id: 'repo-conveyor',
            worktree_id: 'wt-fix-console',
            changed_files: [{ status: 'M', path: 'web/src/App.tsx' }],
          },
          handoffs: [{ id: 'h1', from_role: 'implementer', to_role: 'reviewer', summary: '完成自测' }],
          executions: [{ id: 'e1', role: 'implementer', executor: 'antigravity', status: 'completed', output: 'vite build ok' }],
        },
        // Unrelated worktree failure returned in raw payload must be filtered out and never trigger auto-expand
        {
          id: 'sup-task-unrelated-fail',
          goal: '其他分支的失败任务不应干扰当前目录',
          state: 'failed',
          stage: 'implementation',
          error: 'unrelated failure',
          worktree_path: '/srv/repos/other-worktree',
          metadata: {
            repo_id: 'repo-francais',
            worktree_id: 'wt-fr-main',
          },
        },
      ],
    },
  })

  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  // Technical details <details> is collapsed by default
  const techDetails = page.locator('details.sup-tech-details').first()
  await expect(techDetails).not.toHaveAttribute('open', '')

  // Run history is collapsed into one row by default when scoped tasks are completed (unrelated failure ignored)
  const historyToggle = page.locator('.sup-run-history-bar')
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(historyToggle).toContainText('运行记录 · 1 条')
  await expect(page.getByRole('group', { name: '当前工作目录运行记录列表' })).toHaveCount(0)

  // Verify core non-attention states (pending_dispatch, dispatching, repair_pending) render Chinese labels and stay collapsed
  fixture.tasksByWt['wt-fix-console'].unshift({
    id: 'sup-task-2',
    goal: '跟进修复轮次状态与人工确认',
    state: 'pending_dispatch',
    metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
  })
  await page.clock.fastForward(4_500)
  const activeCard = page.getByRole('region', { name: '当前任务进度与结果' })
  await expect(activeCard).toContainText('待派发')
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'false')

  fixture.tasksByWt['wt-fix-console'][0].state = 'dispatching'
  await page.clock.fastForward(4_500)
  await expect(activeCard).toContainText('派发中')

  fixture.tasksByWt['wt-fix-console'][0].state = 'repair_pending'
  await page.clock.fastForward(4_500)
  await expect(activeCard).toContainText('待修复')
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'false')

  // Inject a first failed task on next poll: run history auto-expands once
  fixture.tasksByWt['wt-fix-console'].push({
    id: 'sup-task-fail-1',
    goal: '修复类型检查报错',
    state: 'failed',
    stage: 'verification',
    error: 'tsc --noEmit 退出码 2',
    updated_at: '2026-11-07T10:00:00Z',
    metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
  })
  await page.clock.fastForward(4_500)
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('group', { name: '当前工作目录运行记录列表' })).toContainText('tsc --noEmit 退出码 2')

  // User manually collapses run history: subsequent polls with the same failure (even if updated_at changes) must NOT re-open it
  await historyToggle.click()
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'false')
  fixture.tasksByWt['wt-fix-console'].find(t => t.id === 'sup-task-fail-1')!.updated_at = '2026-11-07T10:05:00Z'
  await page.clock.fastForward(4_500)
  await page.clock.fastForward(4_500)
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'false')

  // Now the second task transitions to waiting_user while the first failed task still persists:
  // Run history must auto-expand once for the newly attention-needing second task!
  fixture.tasksByWt['wt-fix-console'][0].state = 'waiting_user'
  fixture.tasksByWt['wt-fix-console'][0].stage = 'waiting_user'
  await page.clock.fastForward(4_500)
  await expect(activeCard).toContainText('等待确认')
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('group', { name: '当前工作目录运行记录列表' })).toContainText('等待确认')

  // User manually collapses again: stays collapsed across subsequent polls
  await historyToggle.click()
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'false')
  await page.clock.fastForward(4_500)
  await expect(historyToggle).toHaveAttribute('aria-expanded', 'false')

  // Transition active task to terminal interrupted state: shows 已中断 and hides 取消任务 button
  fixture.tasksByWt['wt-fix-console'][0].state = 'interrupted'
  await page.clock.fastForward(4_500)
  await expect(activeCard).toContainText('已中断')
  await expect(activeCard.getByRole('button', { name: '取消任务' })).toHaveCount(0)
})

// Regression: the live backend returns metadata.changed_files as a list of plain git path
// strings (supervisor_service._enrich_task_dict), NOT {path,status} objects. The 项目 page must
// normalize them and never fall through to the RootErrorBoundary crash screen.
const STRING_CHANGED_FILES_TASK: SupervisorTaskDetail = {
  id: 'sup-task-string-changed-files',
  goal: '改动文件按纯字符串返回时页面不应崩溃',
  repo_path: '/srv/repos/conveyor',
  worktree_path: '/srv/repos/conveyor-wt/fix-console',
  branch: 'fix/console',
  state: 'completed',
  stage: 'completed',
  summary: '已完成。',
  metadata: {
    repo_id: 'repo-conveyor',
    worktree_id: 'wt-fix-console',
    changed_files: ['calc.py', 'tests/test_calc.py'],
  },
  handoffs: [],
  executions: [],
}

test('mock fixture: metadata.changed_files as plain strings renders the 项目 page without the error screen and lists both files in 技术细节', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, { initialTasksByWt: { 'wt-fix-console': [STRING_CHANGED_FILES_TASK] } })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  // The auto-selected completed task renders: no RootErrorBoundary crash screen anywhere.
  await expect(page.locator('.root-error')).toHaveCount(0)
  await expect(page.getByText('页面出了点问题')).toHaveCount(0)

  // The collapsed summary already counts both normalized files.
  const techDetails = page.locator('details.sup-tech-details').first()
  await expect(techDetails.locator('.sup-tech-meta')).toContainText('2 个改动文件')

  // Opening 技术细节 lists both files (strings normalized to {path, status:'M'} → 修改 chip).
  await techDetails.locator(':scope > summary').click()
  await expect(techDetails).toHaveAttribute('open', '')
  const fileList = techDetails.locator('.changed-files')
  await expect(fileList.locator('.changed-file')).toHaveCount(2)
  expect(await fileList.locator('.changed-file-name').allTextContents()).toEqual(['calc.py', 'test_calc.py'])
  expect(await fileList.locator('.changed-file-dir').allTextContents()).toEqual(['tests'])
  await expect(fileList.locator('.file-chip').first()).toHaveText('修改')
})

test('mock fixture: honest unconfigured deployment vs staged deployment progress, plus mobile layout without horizontal overflow', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installSupervisorMockFixture(page, {
    capabilities: createMockCapabilities({
      source_machine: undefined,
      deployment: {
        configured: false,
        can_start: false,
        reason: '未配置 VPS 部署密钥与运行器',
      },
    }),
    initialTasksByWt: {
      'wt-fix-console': [
        {
          id: 'sup-task-ready',
          goal: '准备发布版本',
          state: 'completed',
          stage: 'completed',
          current_sha: 'a1b2c3d',
          metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
        },
      ],
    },
  })

  await page.goto('/#code')
  await page.getByRole('tab', { name: '部署' }).click()

  const deployView = page.getByRole('region', { name: '部署拓扑与传输路径' })
  await expect(deployView).toBeVisible()
  // Honest unconfigured state: disable deploy button, clear Chinese unconfigured banner, backend reason preserved in collapsed details
  await expect(page.getByRole('button', { name: '部署不可用' })).toBeDisabled()
  await expect(page.getByText('尚未配置部署目标和传输流程')).toBeVisible()
  const techDetails = page.locator('.sup-deploy-tech-details')
  await expect(techDetails).not.toHaveAttribute('open', '')
  await techDetails.locator('summary').click()
  await expect(page.getByText('未配置 VPS 部署密钥与运行器')).toBeVisible()

  await expect(deployView).toContainText('未指定目标服务器')
  await expect(deployView).toContainText('私有网络连接与传输')
  await expect(deployView).not.toContainText('网络连接与产物传输（非执行器）')
  await expect(page.getByText('任务完成 ≠ PR 已合并 ≠ 已部署上线')).toBeVisible()

  // Missing PR metadata displays '未核验' and not falsely asserted '未合并'
  const prRow = page.locator('.sup-kv-list div', { hasText: 'PR 合并状态' })
  await expect(prRow).toContainText('未核验')
  await expect(prRow).not.toContainText('未合并')

  // Version labels are plain Chinese without backend field names in primary UI
  await expect(page.locator('.sup-kv-list div', { hasText: '构建版本' })).toBeVisible()
  await expect(page.locator('.sup-kv-list div', { hasText: '线上版本' })).toBeVisible()
  await expect(page.getByText('构建版本 (build_version)')).toHaveCount(0)
  await expect(page.getByText('线上版本 (live_version)')).toHaveCount(0)

  // Primary stage cards show plain Chinese labels, not raw backend stage codes
  await expect(page.locator('.sup-stage-step-code')).toHaveCount(0)

  // All 5 stages are unknown when no deployment has run
  for (const stageKey of ['local_ci', 'tailscale_transfer', 'vps_verify', 'service_switch', 'health_check']) {
    await expect(page.locator(`.sup-stage-step[data-stage="${stageKey}"]`)).toHaveAttribute('data-status', 'unknown')
  }

  // Now configure deployment runner and provide a multi-stage deployment fixture where later stages are still unrecorded
  fixture.setCapabilities(
    createMockCapabilities({
      deployment: {
        configured: true,
        can_start: true,
        source_machine: 'mac-builder',
        target_machine: 'conveyor-prod-vps',
      },
    }),
  )
  fixture.deployments.push({
    id: 'deploy-staged-1',
    task_id: 'sup-task-ready',
    source_machine: 'mac-builder',
    target_machine: 'conveyor-prod-vps',
    build_version: 'a1b2c3d',
    live_version: '8352f46',
    stage: 'tailscale_transfer',
    status: 'running',
    stages: [
      { stage: 'local_ci', status: 'completed', detail: '本地构建与测试通过' },
      { stage: 'tailscale_transfer', status: 'running', detail: '正在通过 Tailscale 传输归档包' },
      // vps_verify, service_switch, health_check intentionally omitted: must remain unknown!
    ],
  })

  await page.reload()
  await page.getByRole('tab', { name: '部署' }).click()
  await expect(deployView).toContainText('mac-builder')
  await expect(deployView).toContainText('conveyor-prod-vps')
  await expect(page.locator('.sup-stage-step[data-stage="local_ci"]')).toHaveAttribute('data-status', 'completed')
  await expect(page.locator('.sup-stage-step[data-stage="tailscale_transfer"]')).toHaveAttribute('data-status', 'running')
  await expect(page.locator('.sup-stage-step[data-stage="vps_verify"]')).toHaveAttribute('data-status', 'unknown')
  await expect(page.locator('.sup-stage-step[data-stage="service_switch"]')).toHaveAttribute('data-status', 'unknown')
  await expect(page.locator('.sup-stage-step[data-stage="health_check"]')).toHaveAttribute('data-status', 'unknown')

  // Optional stage logs disclosure is collapsed by default, shows real stage detail when opened
  const logsDrawer = page.locator('details.sup-deploy-logs')
  await expect(logsDrawer).not.toHaveAttribute('open', '')
  await logsDrawer.locator('summary').click()
  await expect(logsDrawer).toContainText('正在通过 Tailscale 传输归档包')
  await page.screenshot({ path: `${EVIDENCE_DIR}/deploy-desktop-1440.png` })

  // Verify no horizontal overflow on desktop and mobile (390px)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.setViewportSize(PHONE)
  await expect(page.getByRole('button', { name: '选择仓库与 Worktree' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('mock fixture: App right context 运行记录 section collapses by default, auto-expands on failure, and respects manual collapse across polling', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await page.clock.install()
  let runState: 'completed' | 'failed' = 'completed'

  await page.addInitScript(token => {
    sessionStorage.setItem('conveyor-token', token)
    localStorage.setItem('conveyor-context-panel', 'open')
  }, TOKEN)

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    const path = decodeURIComponent(url.pathname)
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    const currentJob = {
      id: 'q-run-1',
      state: runState,
      mode: 'fix',
      channel: 'web',
      chat_id: 's-1',
      operator_id: 'op-1',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      prompt_preview: '检查任务右侧运行记录默认收起',
      changed_files: [],
    }
    const sessionObj = {
      id: 's-1',
      channel: 'web',
      operator_id: 'op-1',
      source_chat_id: 's-1',
      title: '测试运行记录收起会话',
      created_at: new Date().toISOString(),
      last_activity: new Date().toISOString(),
      job_count: 1,
      message_count: 1,
      latest_job: currentJob,
      active_refinement: null,
    }

    if (path === '/api/system/status') {
      return json({
        uptime_seconds: 100, load_average: [0.1, 0.1, 0.1], cpu_count: 4,
        memory: { total: 1000, available: 500 }, disk: { total: 1000, used: 200, free: 800 },
        queue: { depth: 0, paused: false, states: {} },
        channels: { telegram: { configured: false }, feishu: { configured: false } },
        nodes: [], features: { agent_supervisor: true },
      })
    }
    if (path === '/api/sessions') return json({ sessions: [sessionObj] })
    if (path === '/api/sessions/s-1') {
      return json({
        ...sessionObj,
        messages: [],
        jobs: [currentJob],
        runs: [{ id: 'q-run-1', state: runState, mode: 'fix', created_at: currentJob.created_at, updated_at: currentJob.updated_at, prompt_preview: currentJob.prompt_preview, refinement_intent: false }],
      })
    }
    if (path === '/api/jobs') return json({ jobs: [currentJob] })
    if (path === '/api/approvals') return json({ approvals: [] })
    if (path === '/api/nodes') return json({ nodes: [] })
    if (path === '/api/agents') return json({ enabled: false, agents: [] })
    if (path === '/api/computer/status') return json({ armed: false, arm_remaining_seconds: 0, screenshots: [] })
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path)) return json({ events: [] })
    if (/^\/api\/jobs\/[^/]+\/diff$/.test(path)) return json({ diff: '' })
    if (path === '/api/events/stream') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' })
    return json({})
  })

  await page.goto('/#tasks')
  const runsSection = page.locator('aside.context-panel .context-section--runs')
  const toggleBtn = runsSection.locator('.context-section-toggle')
  await expect(toggleBtn).toBeVisible()
  // Collapsed by default when run is completed and no action is needed
  await expect(toggleBtn).toHaveAttribute('aria-expanded', 'false')
  await expect(runsSection.getByRole('group', { name: '本会话的运行记录' })).toHaveCount(0)

  // Transition run to failed: next poll auto-expands once
  runState = 'failed'
  await page.clock.fastForward(3_500)
  await expect(toggleBtn).toHaveAttribute('aria-expanded', 'true')
  await expect(runsSection.getByRole('group', { name: '本会话的运行记录' })).toBeVisible()

  // User manually collapses: stays collapsed across subsequent polls with the same failure
  await toggleBtn.click()
  await expect(toggleBtn).toHaveAttribute('aria-expanded', 'false')
  await page.clock.fastForward(3_500)
  await page.clock.fastForward(3_500)
  await expect(toggleBtn).toHaveAttribute('aria-expanded', 'false')
})

// ---------------------------------------------------------------------------
// 执行者池（自动轮换）: executor list, which worker ran each step, and quota switch notes.
// ---------------------------------------------------------------------------

const POOL_REASON_UNAVAILABLE = '执行者池里没有可用的 Claude Code / Antigravity / Command Code'

function createPoolCapabilities(poolAvailable: boolean): SupervisorCapabilities {
  return createMockCapabilities({
    executors: [
      poolAvailable
        ? { name: 'pool', label: '执行者池（自动轮换）', available: true }
        : { name: 'pool', label: '执行者池（自动轮换）', available: false, reason: POOL_REASON_UNAVAILABLE },
      { name: 'claude', label: 'Claude Code', available: true },
      { name: 'antigravity', label: 'Antigravity', available: true },
    ],
  })
}

const SWITCH_TASK: SupervisorTaskDetail = {
  id: 'sup-task-pool-switch',
  goal: '补充执行者池切换说明',
  state: 'completed',
  stage: 'completed',
  summary: '已完成，执行者池切换记录可在技术细节中查看。',
  metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
  handoffs: [],
  executions: [
    {
      id: 'exec-claude-quota',
      role: 'implementer',
      executor: 'claude',
      status: 'failed',
      error: 'Claude usage limit reached, resets in 2h',
      started_at: '2026-11-08T10:00:00Z',
      finished_at: '2026-11-08T10:01:00Z',
    },
    {
      id: 'exec-antigravity-ok',
      role: 'implementer',
      executor: 'antigravity',
      status: 'completed',
      output: 'build ok',
      started_at: '2026-11-08T10:02:00Z',
      finished_at: '2026-11-08T10:05:00Z',
    },
  ],
}

test('mock fixture: 执行者池 is first in the executor list, marked 推荐 when available, and shows a Chinese reason when unavailable', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, { capabilities: createPoolCapabilities(true) })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const analystSelect = page.getByLabel('分析角色执行器')
  const optionTexts = await analystSelect.locator('option').allTextContents()
  expect(optionTexts[0]).toBe('执行者池（自动轮换）（推荐）')
  expect(optionTexts).toContain('Claude Code')
  await analystSelect.selectOption('pool')
  await expect(page.locator('.sup-role-reason')).toHaveCount(0)
})

test('mock fixture: unavailable 执行者池 explains the reason in Chinese on the role card', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  // Pool is the analyst's current selection, so it stays visible (marked 不可用) with its reason.
  const capabilities = createPoolCapabilities(false)
  capabilities.roles.analyst = { executor: 'pool', model: null, available: false, reason: POOL_REASON_UNAVAILABLE }
  await installSupervisorMockFixture(page, { capabilities })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const analystSelect = page.getByLabel('分析角色执行器')
  await expect(analystSelect.locator('option').first()).toHaveText('执行者池（自动轮换）（不可用）')
  await expect(page.locator('.sup-role-reason').first()).toHaveText(POOL_REASON_UNAVAILABLE)
})

test('mock fixture: task detail shows Chinese executor names and a quota switch note between execution rows', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    capabilities: createPoolCapabilities(true),
    initialTasksByWt: { 'wt-fix-console': [SWITCH_TASK] },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const techDetails = page.locator('details.sup-tech-details').first()
  await techDetails.locator(':scope > summary').click()
  await expect(techDetails).toHaveAttribute('open', '')

  const switchNote = page.locator('.sup-exec-switch-note')
  await expect(switchNote).toHaveCount(1)
  await expect(switchNote).toBeVisible()
  await expect(switchNote).toContainText('Claude Code 额度用完，已换成 Antigravity')

  const rows = page.locator('.sup-execution-item')
  await expect(rows).toHaveCount(2)
  await expect(rows.nth(0)).toBeVisible()
  await expect(rows.nth(0).locator(':scope > summary')).toContainText('Claude Code')
  await expect(rows.nth(1).locator(':scope > summary')).toContainText('Antigravity')
  await expect(rows.nth(0).locator(':scope > summary')).not.toContainText('claude')

  // Raw English error stays behind 技术详情; the visible main text is Chinese.
  await expect(rows.nth(0)).toContainText('额度用完，本次执行未完成')
  const rawError = rows.nth(0).locator('details.sup-exec-raw-error')
  await expect(rawError).not.toHaveAttribute('open', '')
  await expect(rawError.locator('pre')).toHaveText('Claude usage limit reached, resets in 2h')
  await switchNote.scrollIntoViewIfNeeded()
  await page.screenshot({ path: test.info().outputPath('sup-pool-1440.png') })
})

test('mock fixture: task detail with execution switch note has no horizontal overflow at 390px', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installSupervisorMockFixture(page, {
    capabilities: createPoolCapabilities(true),
    initialTasksByWt: { 'wt-fix-console': [SWITCH_TASK] },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const techDetails = page.locator('details.sup-tech-details:visible').first()
  await techDetails.evaluate(el => { (el as HTMLDetailsElement).open = true })
  await expect(page.locator('.sup-exec-switch-note:visible')).toBeVisible()
  // Open the failed run row and its 技术详情 so the longest raw content is laid out at 390px.
  await page.locator('.sup-execution-item').first().evaluate(el => { (el as HTMLDetailsElement).open = true })
  await page.locator('.sup-exec-raw-error').first().evaluate(el => { (el as HTMLDetailsElement).open = true })
  await expect(page.locator('.sup-exec-raw-error pre').first()).toBeVisible()

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  await page.locator('.sup-exec-switch-note').first().scrollIntoViewIfNeeded()
  await page.screenshot({ path: test.info().outputPath('sup-pool-390.png') })
})

// ---------------------------------------------------------------------------
// Owner-facing polish: always-visible executor line, Chinese failure reasons, and
// unavailable executors hidden from role dropdowns unless the role currently uses them.
// ---------------------------------------------------------------------------

const CURSOR_REASON = '本地未安装 cursor-agent 可执行文件'

test('N6: with no usable role executor the 项目 page gives one guidance line that links to 执行者', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const unavailable = 'claude binary not found: claude'
  const capabilities = createMockCapabilities({
    roles: {
      analyst: { executor: 'claude', model: null, available: false, reason: unavailable },
      implementer: { executor: 'claude', model: null, available: false, reason: unavailable },
      reviewer: { executor: 'claude', model: null, available: false, reason: unavailable },
    },
    executors: [
      { name: 'pool', label: '执行者池（自动轮换）', available: false, reason: '没有可用的执行者' },
      { name: 'claude', label: 'Claude Code', available: false, reason: unavailable },
      { name: 'antigravity', label: 'Antigravity', available: false, reason: 'agy binary not found' },
    ],
  })
  await installSupervisorMockFixture(page, { capabilities })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const guidance = page.locator('.sup-role-guidance')
  await expect(guidance).toHaveCount(1)
  await expect(guidance).toContainText('项目需要 Claude Code、Antigravity 或 Command Code 至少一个已安装并登录')
  await expect(guidance).toContainText('Pi 只用于对话和任务')
  await expect(guidance).not.toContainText('Supervisor')
  await page.screenshot({ path: 'test-results/r5a/n6-project-no-executor.png' })
  // The 执行者 page is off in this fixture, so there is no link to a page that would be empty.
  await expect(guidance.getByRole('button', { name: '去执行者页' })).toHaveCount(0)
})

test('N6: the guidance line is absent once one role executor is usable', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page)
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()
  await expect(page.locator('.sup-role-guidance')).toHaveCount(0)
})

test('mock fixture: role executor dropdowns hide unavailable executors the role does not use', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page)
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  for (const label of ['分析角色执行器', '实现角色执行器', '审查角色执行器']) {
    const optionTexts = await page.getByLabel(label).locator('option').allTextContents()
    expect(optionTexts.join('|')).not.toContain('Cursor Agent')
    expect(optionTexts.join('|')).not.toContain('（不可用）')
  }
  await expect(page.getByText(CURSOR_REASON)).toHaveCount(0)
})

test('mock fixture: a role still using an unavailable executor keeps it visible with the reason and blocks dispatch', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const base = createMockCapabilities()
  const capabilities = createMockCapabilities({
    roles: {
      ...base.roles,
      implementer: { executor: 'cursor_unavailable', model: null, available: false, reason: CURSOR_REASON },
    },
  })
  await installSupervisorMockFixture(page, { capabilities })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const implSelect = page.getByLabel('实现角色执行器')
  await expect(implSelect).toHaveValue('cursor_unavailable')
  await expect(implSelect.locator('option', { hasText: 'Cursor Agent' })).toHaveText('Cursor Agent（不可用）')
  await expect(page.locator('.sup-role-reason')).toHaveText(CURSOR_REASON)

  await page.getByRole('textbox', { name: '任务目标' }).fill('修复 supervisor 任务取消时的状态同步')
  await expect(page.getByRole('button', { name: '开始执行' })).toBeDisabled()
})

const EXEC_SUMMARY_TASK: SupervisorTaskDetail = {
  id: 'sup-task-exec-summary',
  goal: '三角色执行者摘要与切换说明',
  state: 'completed',
  stage: 'completed',
  summary: '已完成。',
  metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
  handoffs: [],
  executions: [
    { id: 'exec-analyst', role: 'analyst', executor: 'claude', status: 'completed', started_at: '2026-11-08T09:50:00Z' },
    {
      id: 'exec-impl-quota',
      role: 'implementer',
      executor: 'claude',
      status: 'failed',
      error: 'Claude usage limit reached, resets in 2h',
      started_at: '2026-11-08T10:00:00Z',
    },
    { id: 'exec-impl-ok', role: 'implementer', executor: 'antigravity', status: 'completed', output: 'build ok', started_at: '2026-11-08T10:02:00Z' },
    { id: 'exec-reviewer', role: 'reviewer', executor: 'claude', status: 'completed', started_at: '2026-11-08T10:06:00Z' },
  ],
}

test('mock fixture: task card shows which worker ran each role and the switch note without opening 技术细节', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    capabilities: createPoolCapabilities(true),
    initialTasksByWt: { 'wt-fix-console': [EXEC_SUMMARY_TASK] },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const summary = page.locator('.sup-task-executors')
  await expect(summary).toBeVisible()
  await expect(summary.locator('.sup-task-executors-line')).toHaveText('分析：Claude Code · 实现：Antigravity · 审阅：Claude Code')
  await expect(summary.locator('.sup-task-switch-note')).toHaveCount(1)
  await expect(summary.locator('.sup-task-switch-note')).toHaveText('Claude Code 额度用完，已换成 Antigravity')
  // The full execution list remains in the collapsed 技术细节 section.
  await expect(page.locator('details.sup-tech-details').first()).not.toHaveAttribute('open', '')
  await page.screenshot({ path: test.info().outputPath('sup-polish-card-switch-1440.png') })
})

test('mock fixture: task card executor summary fits at 390px without horizontal overflow', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installSupervisorMockFixture(page, {
    capabilities: createPoolCapabilities(true),
    initialTasksByWt: { 'wt-fix-console': [EXEC_SUMMARY_TASK] },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const summary = page.locator('.sup-task-executors:visible')
  await expect(summary).toBeVisible()
  await expect(summary.locator('.sup-task-switch-note')).toBeVisible()
  await summary.scrollIntoViewIfNeeded()
  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  await page.screenshot({ path: test.info().outputPath('sup-polish-card-switch-390.png') })
})

test('mock fixture: a task without executions shows no executor summary', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [{
        id: 'sup-task-no-exec',
        goal: '尚未执行的任务',
        state: 'pending_dispatch',
        metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
      }],
    },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()
  await expect(page.locator('.sup-task-executors')).toHaveCount(0)
})

const FAILED_QUOTA_TASK: SupervisorTaskDetail = {
  id: 'sup-task-failed-quota',
  goal: '实现失败任务的中文原因展示',
  state: 'failed',
  stage: 'implementation',
  error: 'Claude usage limit reached, resets in 2h',
  metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
  handoffs: [],
  executions: [],
}

test('mock fixture: failed task shows the Chinese reason, and the raw English error stays behind 技术详情 until clicked', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, { initialTasksByWt: { 'wt-fix-console': [FAILED_QUOTA_TASK] } })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const banner = page.locator('.sup-banner--error', { hasText: '受阻原因' })
  await expect(banner).toBeVisible()
  await expect(banner.locator('span').first()).toHaveText('执行者的额度或用量已用完，任务没有完成')
  await expect(banner.locator('pre')).toBeHidden()

  await banner.getByText('技术详情', { exact: true }).click()
  await expect(banner.locator('pre')).toHaveText('Claude usage limit reached, resets in 2h')
  await page.screenshot({ path: test.info().outputPath('sup-polish-failed-1440.png') })
})

test('mock fixture: failed task banner reads well at 390px and the 技术详情 disclosure opens with a real tap', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installSupervisorMockFixture(page, { initialTasksByWt: { 'wt-fix-console': [FAILED_QUOTA_TASK] } })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const banner = page.locator('.sup-banner--error', { hasText: '受阻原因' })
  await expect(banner).toBeVisible()
  await banner.scrollIntoViewIfNeeded()
  await banner.getByText('技术详情', { exact: true }).click()
  await expect(banner.locator('pre')).toBeVisible()

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  await page.screenshot({ path: test.info().outputPath('sup-polish-failed-390.png') })
})

const RAW_ERROR_CASES: Array<{ raw: string; expected: string }> = [
  { raw: 'timed out after 1800s; process group killed', expected: '执行超时，任务没有完成' },
  { raw: 'antigravity (agy) binary not found: /usr/local/bin/agy', expected: '执行器不可用（未安装或暂时无法使用）' },
  { raw: 'invalid review evidence: malformed JSON in review verdict', expected: '审查结果无效或审查失败，任务没有通过审查' },
  { raw: 'operator validation failed: unit (exit 1): 3 tests failed', expected: '自动验收检查没有通过' },
  { raw: 'max repair rounds exceeded; remaining issues reported', expected: '自动修了几轮还是没通过检查' },
  { raw: 'git worktree add failed: fatal: already exists', expected: '代码工作区（git worktree）出现问题' },
  { raw: 'cancelled by user', expected: '任务已被取消' },
  { raw: 'supervisor restarted while task was in implementing', expected: '执行过程被意外中断' },
  { raw: 'something nobody anticipated happened', expected: '执行没有完成' },
]

for (const { raw, expected } of RAW_ERROR_CASES) {
  test(`mock fixture: raw backend error "${raw.slice(0, 40)}" maps to a Chinese reason`, async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await installSupervisorMockFixture(page, {
      initialTasksByWt: {
        'wt-fix-console': [{ ...FAILED_QUOTA_TASK, id: 'sup-task-raw-map', error: raw }],
      },
    })
    await page.goto('/#code')
    await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

    const banner = page.locator('.sup-banner--error', { hasText: '受阻原因' })
    await expect(banner.locator('span').first()).toHaveText(expected)
    await expect(banner.locator('pre')).toHaveText(raw)
  })
}

// ---------------------------------------------------------------------------
// H2 「改动」+ 任务卡片短总结 + 运行记录中文原因 + 目标轮次标题
// ---------------------------------------------------------------------------

const GOAL_TITLE = '把首页加载时间降到 1 秒以内'
const GOAL_OUTCOME = '首页加载时间降到 1 秒以内，并且所有测试通过'

// 目标循环写下的第 1 轮 / 第 2 轮任务目标文本（supervisor_goals._first_iteration_prompt /
// _continuation_prompt 的真实形状）。
const GOAL_ROUND_1_GOAL = `${GOAL_OUTCOME}\n\n这是目标的第 1 轮。`
const GOAL_ROUND_2_GOAL = `这是目标「${GOAL_TITLE}」的第 2 轮。目标：${GOAL_OUTCOME}\n\n本轮要做：复验加载时间并补上移动端验收步骤`

function diffFile(path: string, status: SupervisorDiff['files'][number]['status'], additions: number, deletions: number) {
  return { path, status, additions, deletions }
}

function fakeDiff(...paths: string[]): string {
  return paths.map(path => [
    `diff --git a/${path} b/${path}`,
    '--- a/' + path,
    '+++ b/' + path,
    '@@ -1,2 +1,2 @@',
    ' context line',
    '-removed line',
    '+added line',
  ].join('\n')).join('\n')
}

// 运行记录默认收起；有失败时会自动展开。点开之前先看一眼，别把已经展开的又收起。
async function openRunHistory(page: Page) {
  const bar = page.locator('.sup-run-history-bar')
  if ((await bar.getAttribute('aria-expanded')) !== 'true') await bar.click()
  await expect(bar).toHaveAttribute('aria-expanded', 'true')
}

const CHANGES_TASK: SupervisorTaskDetail = {
  id: 'sup-task-changes',
  goal: GOAL_ROUND_2_GOAL,
  repo_path: '/srv/repos/conveyor',
  worktree_path: '/srv/repos/conveyor-wt/fix-console',
  branch: 'fix/console',
  state: 'completed',
  stage: 'completed',
  repair_round: 1,
  max_repair_rounds: 2,
  base_sha: '8352f46a9c01',
  current_sha: 'a1b2c3d4e5f6',
  updated_at: '2026-11-14T10:00:00Z',
  summary: [
    '任务: sup-task-changes',
    '目标: 复验加载时间',
    '状态: completed / 阶段: completed',
    '仓库: /srv/repos/conveyor',
    'Worktree: /srv/repos/conveyor-wt/fix-console',
    '分支: fix/console',
    '代码版本: a1b2c3d4e5f6',
    '内容指纹: 1a2b3c4d5e6f7788…',
    '修复轮次: 1/2',
    '验收: 通过 @ a1b2c3d4e5f6',
  ].join('\n'),
  metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
  handoffs: [],
  executions: [],
  acceptance: { passed: true, summary: '构建与测试都通过了。' },
}

// 5 个文件：超过 3 个，每个文件的差异要单独收起来。
const CHANGES_DIFF: SupervisorDiff = {
  task_id: 'sup-task-changes',
  base_sha: '8352f46a9c01',
  head_sha: 'a1b2c3d4e5f6',
  files: [
    diffFile('web/src/App.tsx', 'M', 12, 3),
    diffFile('web/src/taskText.ts', 'M', 40, 1),
    diffFile('web/src/components/SupervisorDiff.tsx', 'A', 90, 0),
    diffFile('docs/supervisor.md', 'M', 5, 2),
    diffFile('old_helper.py', 'D', 0, 20),
  ],
  diff: fakeDiff(
    'web/src/App.tsx',
    'web/src/taskText.ts',
    'web/src/components/SupervisorDiff.tsx',
    'docs/supervisor.md',
    'old_helper.py',
  ),
  truncated: true,
  untracked: ['web/src/new_notes.md'],
}

test('H2: 任务卡片显示「改动」：文件列表带 +/-，超过 3 个文件按文件收起，差异懒加载', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [
        CHANGES_TASK,
        {
          id: 'sup-task-running',
          goal: '还在跑的那一轮不该读差异',
          state: 'running',
          stage: 'implementation',
          updated_at: '2026-11-14T11:00:00Z',
          metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
        },
      ],
    },
    diffsByTask: { 'sup-task-changes': CHANGES_DIFF },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  // 「改动」是任务卡片里独立的一段，不在「技术细节」里。
  const changes = page.getByRole('region', { name: '改动' })
  await expect(changes).toBeVisible()
  await expect(changes.getByRole('heading', { name: '改动' })).toBeVisible()
  await expect(page.locator('details.sup-tech-details').first()).not.toHaveAttribute('open', '')

  // 只有选中的这一轮读了差异：正在跑的那一轮没有请求。
  expect(fixture.diffRequests).toEqual(['sup-task-changes'])

  // 文件列表：5 个改动文件 + 1 个未跟踪新文件，都带 +/- 。
  const fileList = changes.locator('.changed-files')
  await expect(fileList.locator('.changed-file')).toHaveCount(6)
  await expect(fileList.locator('.changed-file-name').first()).toHaveText('App.tsx')
  await expect(fileList.locator('.diff-stat-add').first()).toHaveText('+12')
  // +/- 用 aria-label 说清楚，避免依赖减号字符。
  await expect(fileList.locator('.diff-stat').first()).toHaveAttribute('aria-label', '新增 12 行，删除 3 行')
  await expect(fileList.locator('.file-chip').first()).toHaveText('修改')
  await expect(fileList.locator('.file-chip').nth(2)).toHaveText('新增')
  await expect(fileList.locator('.file-chip').nth(4)).toHaveText('删除')
  await expect(fileList.locator('.file-chip').last()).toHaveText('新增')

  // 改动太多：一句白话提醒。
  await expect(changes.getByText('改动太多，只显示前一部分。')).toBeVisible()

  // 超过 3 个文件：每个文件一块，默认收起；点开之前一屏都没有逐行差异。
  const fileBlocks = changes.locator('.sup-changes-file')
  await expect(fileBlocks).toHaveCount(5)
  await expect(changes.locator('pre.diff-body:visible')).toHaveCount(0)
  await fileBlocks.first().locator('summary').click()
  const opened = fileBlocks.first()
  await expect(opened).toHaveAttribute('open', '')
  await expect(changes.locator('pre.diff-body:visible')).toHaveCount(1)
  await expect(opened.locator('.diff-line--add').first()).toHaveText('+added line')
  await expect(opened.locator('.diff-line--del').first()).toHaveText('-removed line')
  await expect(opened.locator('.diff-line--file').first()).toHaveText('web/src/App.tsx')

  // 切到正在跑的那一轮：没有「改动」段，也不会去读它的差异。
  await openRunHistory(page)
  await page.getByRole('group', { name: '当前工作目录运行记录列表' }).getByText('还在跑的那一轮不该读差异').click()
  await expect(page.getByRole('region', { name: '当前任务进度与结果' })).toContainText('执行中')
  await expect(page.getByRole('region', { name: '改动' })).toHaveCount(0)
  expect(fixture.diffRequests).toEqual(['sup-task-changes'])
})

test('H2: 少于 4 个改动文件时差异直接展开，没有文件时说明「这一轮没有改动文件」', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const smallDiff: SupervisorDiff = {
    task_id: 'sup-task-small',
    base_sha: '8352f46',
    head_sha: 'a1b2c3d',
    files: [diffFile('calc.py', 'M', 3, 1), diffFile('tests/test_calc.py', 'M', 8, 0)],
    diff: fakeDiff('calc.py', 'tests/test_calc.py'),
    truncated: false,
    untracked: [],
  }
  const emptyDiff: SupervisorDiff = {
    task_id: 'sup-task-empty',
    base_sha: '8352f46',
    head_sha: 'a1b2c3d',
    files: [],
    diff: '',
    truncated: false,
    untracked: [],
  }
  await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [
        { ...CHANGES_TASK, id: 'sup-task-small', goal: '小改动一轮', acceptance: null },
        { ...CHANGES_TASK, id: 'sup-task-empty', goal: '什么都没改的一轮', acceptance: null },
      ],
    },
    diffsByTask: { 'sup-task-small': smallDiff, 'sup-task-empty': emptyDiff },
  })
  await page.goto('/#code')

  const changes = page.getByRole('region', { name: '改动' })
  await expect(changes).toBeVisible()
  await expect(changes.locator('.changed-file')).toHaveCount(2)
  await expect(changes.locator('.sup-changes-file')).toHaveCount(0)
  await expect(changes.locator('pre.diff-body')).toBeVisible()
  await expect(changes.locator('.diff-line--add').first()).toHaveText('+added line')
  await expect(changes.getByText('改动太多，只显示前一部分。')).toHaveCount(0)

  // 一个文件都没有：一句白话，不摆空的差异框。
  await openRunHistory(page)
  await page.getByRole('group', { name: '当前工作目录运行记录列表' }).getByText('什么都没改的一轮').click()
  await expect(changes.getByText('这一轮没有改动文件。')).toBeVisible()
  await expect(changes.locator('.changed-files')).toHaveCount(0)
})

test('H2: 工作目录已经不在了时说「这个工作目录已经不在了」，读不到时说稍后再试', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [
        { ...CHANGES_TASK, id: 'sup-task-gone-wt' },
        { ...CHANGES_TASK, id: 'sup-task-diff-error', goal: '接口暂时读不到的那一轮' },
      ],
    },
    diffMissingFor: ['sup-task-gone-wt'],
  })
  await page.goto('/#code')

  const changes = page.getByRole('region', { name: '改动' })
  await expect(changes.getByText('这个工作目录已经不在了，看不到这一轮的改动。')).toBeVisible()
  await expect(changes.locator('.changed-files')).toHaveCount(0)

  // 另一个任务：既不是 404 worktree_missing 也没有数据 → 一句「稍后再试」。
  await openRunHistory(page)
  await page.getByRole('group', { name: '当前工作目录运行记录列表' }).getByText('接口暂时读不到的那一轮').click()
  await expect(changes.getByText('暂时读不到这一轮的改动，稍后再试。')).toBeVisible()
})

test('H2: 任务卡片用一行短总结代替原始摘要，原文收在「技术细节」的「原始摘要」里', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    initialTasksByWt: { 'wt-fix-console': [CHANGES_TASK] },
    diffsByTask: { 'sup-task-changes': CHANGES_DIFF },
  })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '当前任务进度与结果' })
  const summary = card.locator('.sup-task-summary')
  await expect(summary).toBeVisible()
  await expect(summary.locator('p').first()).toHaveText('已完成 · 自动修了 1 轮 · 验收通过 · 改了 6 个文件')
  // 验收那段话照常显示，但后端那段多行原始摘要不出现在卡片上。
  await expect(card.locator('.sup-acceptance-box')).toContainText('构建与测试都通过了。')
  await expect(card).not.toContainText('内容指纹')
  await expect(card).not.toContainText('Worktree: /srv/repos')
  await expect(card).not.toContainText('验收: 通过 @')

  // 「技术细节」里保留原文，标题就叫「原始摘要」。
  const techDetails = page.locator('details.sup-tech-details').first()
  await techDetails.locator(':scope > summary').click()
  const rawBlock = techDetails.locator('.sup-tech-block', { hasText: '原始摘要' })
  await expect(rawBlock.locator('h4')).toHaveText('原始摘要')
  await expect(rawBlock.locator('pre')).toContainText('修复轮次: 1/2')
  await expect(rawBlock.locator('pre')).toContainText('验收: 通过 @ a1b2c3d4e5f6')
})

test('H2: 执行者自己写的一句结论仍然显示在任务卡片上', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [{ ...CHANGES_TASK, summary: '构建产物已输出到 web/dist，可以部署了。' }],
    },
    diffsByTask: { 'sup-task-changes': CHANGES_DIFF },
  })
  await page.goto('/#code')

  const card = page.getByRole('region', { name: '当前任务进度与结果' })
  await expect(card.locator('.sup-task-summary-note')).toHaveText('构建产物已输出到 web/dist，可以部署了。')
})

test('H2: 运行记录里的失败原因先说中文，英文原文收在「技术详情」', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  const fixture = await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [
        CHANGES_TASK,
        {
          id: 'sup-task-rounds',
          goal: '构建脚本报错',
          state: 'failed',
          stage: 'verification',
          error: 'max repair rounds exceeded; remaining issues reported',
          updated_at: '2026-11-14T09:00:00Z',
          metadata: { repo_id: 'repo-conveyor', worktree_id: 'wt-fix-console' },
        },
      ],
    },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const history = page.locator('.sup-run-history')
  await expect(history).toContainText('1 条需处理或失败')
  const row = page.getByRole('group', { name: '当前工作目录运行记录列表' }).locator('.sup-run-row', { hasText: '构建脚本报错' })
  await expect(row.locator('.sup-run-row-error > span')).toHaveText('自动修了几轮还是没通过检查')
  await expect(row.locator('.sup-run-row-raw-error pre')).toBeHidden()
  await row.locator('.sup-run-row-raw-error summary').click()
  await expect(row.locator('.sup-run-row-raw-error pre')).toBeVisible()
  await expect(row.locator('.sup-run-row-raw-error pre')).toHaveText('max repair rounds exceeded; remaining issues reported')
  expect(fixture.diffRequests).toEqual(['sup-task-changes'])
})

test('H2: 目标轮次的任务标题在运行记录和任务卡片上都显示「目标第 N 轮：…」', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [
        CHANGES_TASK,
        { ...CHANGES_TASK, id: 'sup-task-round-1', goal: GOAL_ROUND_1_GOAL, state: 'failed', error: 'boom' },
      ],
    },
    diffsByTask: { 'sup-task-changes': CHANGES_DIFF },
  })
  await page.goto('/#code')

  // 任务卡片标题：第 2 轮显示这一轮要做的事。
  const card = page.getByRole('region', { name: '当前任务进度与结果' })
  await expect(card.locator('.sup-active-task-title h4')).toHaveText('目标第 2 轮：复验加载时间并补上移动端验收步骤')

  // 第 1 轮失败会让运行记录自动展开。
  await expect(page.locator('.sup-run-history-bar')).toHaveAttribute('aria-expanded', 'true')
  const rows = page.getByRole('group', { name: '当前工作目录运行记录列表' }).locator('.sup-run-row')
  await expect(rows).toHaveCount(2)
  await expect(rows.nth(0).locator('.sup-run-row-main strong')).toHaveText('目标第 2 轮：复验加载时间并补上移动端验收步骤')
  // 第 1 轮没有「本轮要做」，用目标结果的第一行。
  await expect(rows.nth(1).locator('.sup-run-row-main strong')).toHaveText(`目标第 1 轮：${GOAL_OUTCOME}`)
  // 原始的「这是目标…」前缀绝不直接摆在老板面前。
  await expect(rows.nth(1)).not.toContainText('这是目标', { useInnerText: true })
})

test('H2: 目标里前面失败的轮次不算进「需处理或失败」', async ({ page }) => {
  await page.setViewportSize(DESKTOP)
  await installSupervisorMockFixture(page, {
    initialTasksByWt: {
      'wt-fix-console': [
        CHANGES_TASK,
        { ...CHANGES_TASK, id: 'sup-task-round-1-failed', goal: GOAL_ROUND_1_GOAL, state: 'failed', error: 'max repair rounds exceeded; remaining issues reported' },
        { ...CHANGES_TASK, id: 'sup-task-plain-failed', goal: '和这个目标无关的失败', state: 'failed', error: 'boom' },
      ],
    },
    diffsByTask: { 'sup-task-changes': CHANGES_DIFF },
  })
  await page.goto('/#code')

  const history = page.locator('.sup-run-history')
  // 3 条记录里 2 条失败，但第 1 轮失败后面还有第 2 轮 → 只剩 1 条要处理。
  await expect(history).toContainText('运行记录 · 3 条')
  await expect(history).toContainText('1 条需处理或失败')
})

test('H2: 改动视图在 390px 手机上不横向溢出', async ({ page }) => {
  await page.setViewportSize(PHONE)
  await installSupervisorMockFixture(page, {
    initialTasksByWt: { 'wt-fix-console': [CHANGES_TASK] },
    diffsByTask: { 'sup-task-changes': CHANGES_DIFF },
  })
  await page.goto('/#code')
  await expect(page.getByRole('heading', { name: 'Conveyor / fix/console' })).toBeVisible()

  const changes = page.getByRole('region', { name: '改动' })
  await expect(changes).toBeVisible()
  await changes.scrollIntoViewIfNeeded()
  // 点开第一个文件的逐行差异：最长的一行也只在差异框里自己滚。
  await changes.locator('.sup-changes-file').first().locator('summary').click()
  await expect(changes.locator('pre.diff-body').first()).toBeVisible()

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  expect(overflow.scroll).toBeLessThanOrEqual(390)
  await page.screenshot({ path: test.info().outputPath('sup-h2-changes-390.png') })
})
