import { Fragment, FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChatPanel, type TaskSuggestion } from './ChatPanel'
import {
  NARROW_DISCUSSION_HINT,
  RETRY_LOCKED_NOTE,
  RETRY_MISSING_NOTE,
  RETRY_READY_NOTE,
  WIDE_DISCUSSION_HINT,
  applyTaskSuggestion,
  handoffNotice,
  prepareRetryDraft,
} from './taskDraftHandoff'
import { ChangedFileList } from './TaskParts'
import SupervisorChecks from './SupervisorChecks'
import ProjectNotesCard from './ProjectNotes'
import SupervisorGoals, { SupervisorGoalsProbe } from './SupervisorGoals'
import SupervisorMerge from './SupervisorMerge'
import { SupervisorDiffView, useSupervisorDiff } from './SupervisorDiff'
import { goalRoundTaskTitle, parseGoalRoundTask, goalRoundKey } from '../taskText'
import { useMediaQuery } from '../ux/shellNav'
import { readDispatchDraft, writeDispatchDraft } from './supDrafts'

export type SupervisorExecutorModel = { id: string; name: string }

export type SupervisorExecutorInfo = {
  name: string
  label: string
  available: boolean
  reason?: string
  models?: SupervisorExecutorModel[]
}

export type SupervisorRoleConfig = {
  executor: string
  model: string | null
  available: boolean
  reason?: string
}

export type SupervisorRoleKey = 'analyst' | 'implementer' | 'reviewer'

export type SupervisorCapabilities = {
  enabled: boolean
  coordinator: { executor: string; model: string | null }
  roles: Record<SupervisorRoleKey, SupervisorRoleConfig>
  executors: SupervisorExecutorInfo[]
  deployment: {
    configured: boolean
    can_start: boolean
    reason?: string
    source_machine?: string
    target_machine?: string
  }
  source_machine?: string
}

export type SupervisorWorktree = {
  id: string
  path: string
  branch: string
  head: string
  is_main: boolean
  managed: boolean
  locked: boolean
  active_task_id?: string
  chat_session_id?: string
}

export type SupervisorRepo = {
  id: string
  name: string
  path: string
  worktrees: SupervisorWorktree[]
  source_machine?: string
  /** "env" = server admin configured, "repo" = owner picked, "none" = nothing selected. */
  validation_source?: 'env' | 'repo' | 'none'
}

export type SupervisorTask = {
  id: string
  goal: string
  repo_path?: string
  worktree_path?: string
  branch?: string
  base_sha?: string
  current_sha?: string
  state: string
  stage?: string
  repair_round?: number
  max_repair_rounds?: number
  created_at?: string
  updated_at?: string
  error?: string | null
  metadata?: {
    repo_id?: string
    worktree_id?: string
    acceptance_criteria?: string
    roles?: Record<string, { executor?: string; model?: string | null } | string>
    session_id?: string
    result_summary?: string
    changed_files?: Array<string | { path?: string; file?: string; name?: string; status?: string }>
    pr_status?: string
    [key: string]: unknown
  } | null
}

export type SupervisorHandoff = {
  id?: string | number
  from_role?: string
  to_role?: string
  stage?: string
  summary?: string
  created_at?: string
  [key: string]: unknown
}

export type SupervisorExecution = {
  id?: string | number
  role?: string
  executor?: string
  model?: string
  stage?: string
  status?: string
  output?: string
  error?: string
  started_at?: string
  finished_at?: string
  [key: string]: unknown
}

export type SupervisorAcceptance = {
  passed?: boolean
  status?: string
  criteria?: string
  summary?: string
  checks?: Array<{ name?: string; status?: string; detail?: string }>
  [key: string]: unknown
}

export type SupervisorTaskDetail = SupervisorTask & {
  handoffs?: SupervisorHandoff[]
  acceptance?: SupervisorAcceptance | null
  executions?: SupervisorExecution[]
  summary?: string
}

export type DeploymentStageKey = 'local_ci' | 'tailscale_transfer' | 'vps_verify' | 'service_switch' | 'health_check'

export type SupervisorDeploymentStage = {
  stage: string
  status: string
  detail?: string
  started_at?: string
  finished_at?: string
}

export type SupervisorDeployment = {
  id: string
  task_id?: string | null
  source_machine?: string | null
  target_machine?: string | null
  build_version?: string | null
  live_version?: string | null
  stage?: string | null
  status: string
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  metadata?: Record<string, unknown> | null
  stages?: SupervisorDeploymentStage[]
}

export type RoleOverrideEntry = {
  executor: string
  model: string
}

export type RoleOverrides = Record<SupervisorRoleKey, RoleOverrideEntry>

type WorktreeDraft = {
  goal: string
  acceptanceCriteria: string
  preparedSessionId?: string
  fromSuggestion?: boolean
  handoffMode?: 'review' | 'read_only' | 'retry'
  roleOverrides?: Partial<RoleOverrides>
}

type PendingDispatch = {
  fingerprint: string
  requestId: string
  taskId?: string
}

type WorktreeDataSnapshot = {
  tasks: SupervisorTask[]
  selectedTaskId: string
  userSelected?: boolean
  taskDetail: SupervisorTaskDetail | null
  deployments: SupervisorDeployment[]
  loaded: boolean
  error: string
}

const CONTEXT_STORAGE_KEY = 'conveyor-supervisor-context-v1'
const PENDING_DISPATCH_PREFIX = 'conveyor-supervisor-pending-dispatch:v1:'
/** N20: the one-paragraph 项目 intro stays closed once the owner has read it. */
const PROJECT_INTRO_DISMISSED_KEY = 'conveyor-supervisor-intro-dismissed'

const ROLE_META: { key: SupervisorRoleKey; label: string; hint: string }[] = [
  { key: 'analyst', label: '分析', hint: '先想清楚要改成什么样' },
  { key: 'implementer', label: '动手改', hint: '在工作目录里改代码并自测' },
  { key: 'reviewer', label: '检查', hint: '照验收标准逐条检查' },
]

export const DEPLOY_STAGE_ORDER: { key: DeploymentStageKey; label: string }[] = [
  { key: 'local_ci', label: '构建与测试' },
  { key: 'tailscale_transfer', label: '传输产物' },
  { key: 'vps_verify', label: '校验版本' },
  { key: 'service_switch', label: '切换服务' },
  { key: 'health_check', label: '健康检查' },
]

const SUP_STATE_LABELS: Record<string, string> = {
  created: '待派发',
  pending_dispatch: '待派发',
  pending: '等待中',
  queued: '排队中',
  dispatching: '派发中',
  dispatched: '已派发',
  running: '执行中',
  analyzing: '分析中',
  implementing: '实现中',
  reviewing: '审查中',
  verifying: '验收中',
  repair_pending: '待修复',
  repairing: '修复中',
  waiting_user: '等待确认',
  waiting_for_user: '等待确认',
  waiting_for_human: '等待人工',
  needs_attention: '需要处理',
  blocked: '受阻',
  cancelling: '取消中',
  cancelled: '已取消',
  completed: '已完成',
  succeeded: '已完成',
  ok: '已通过',
  passed: '已通过',
  failed: '失败',
  error: '失败',
  interrupted: '已中断',
  skipped: '已跳过',
  unknown: '未知',
}

const SUP_STAGE_LABELS: Record<string, string> = {
  created: '准备中',
  pending_dispatch: '待派发',
  queued: '排队等待',
  dispatching: '派发中',
  analysis: '需求分析',
  analyzing: '需求分析',
  analyst: '需求分析',
  plan: '规划方案',
  planning: '规划方案',
  implementation: '代码实现',
  implementing: '代码实现',
  implementer: '代码实现',
  coding: '代码实现',
  code: '代码实现',
  review: '代码审查',
  reviewing: '代码审查',
  reviewer: '代码审查',
  acceptance: '验收检查',
  verify: '验收检查',
  verification: '验收检查',
  repair_pending: '待修复',
  repair: '问题修复',
  repairing: '问题修复',
  waiting_user: '等待确认',
  waiting_for_user: '等待确认',
  completed: '已完成',
  done: '已完成',
  cancelled: '已取消',
  failed: '执行失败',
  interrupted: '已中断',
  local_ci: '构建与测试',
  tailscale_transfer: '传输产物',
  vps_verify: '校验版本',
  service_switch: '切换服务',
  health_check: '健康检查',
}

export function supStateLabel(state?: string | null): string {
  const key = (state || 'unknown').trim().toLowerCase()
  return SUP_STATE_LABELS[key] || state || '未知'
}

const SUP_EXECUTOR_LABELS: Record<string, string> = {
  claude: 'Claude Code',
  antigravity: 'Antigravity',
  'command-code': 'Command Code',
  codex: 'Codex CLI',
  pool: '执行者池',
  // The coordinator is the chat model itself: 讨论 never spawns a CLI (QA N29).
  chat: '内置对话模型',
}

export function supExecutorLabel(name?: string | null): string {
  const raw = (name || '').trim()
  if (!raw) return '未指定执行者'
  return SUP_EXECUTOR_LABELS[raw.toLowerCase()] || raw
}

/**
 * The coordinator (讨论) is the chat model itself. The owner reads one plain Chinese line;
 * an executor we have no Chinese name for still reads 内置对话模型, and the raw code plus
 * the model id stay behind 技术详情 (QA N29).
 */
export function supCoordinatorLabel(executor?: string | null): string {
  const raw = (executor || '').trim()
  if (!raw) return '内置对话模型'
  const label = supExecutorLabel(raw)
  return /[\u4e00-\u9fff]/.test(label) ? label : '内置对话模型'
}

const SUP_QUOTA_PATTERN = /quota|usage limit|rate limit|resource_exhausted|429|credit balance|额度|配额/i

export function supIsQuotaFailure(text?: string | null): boolean {
  return SUP_QUOTA_PATTERN.test(String(text || ''))
}

function supIsFailedExecution(ex: SupervisorExecution): boolean {
  const key = (ex.status || '').trim().toLowerCase()
  return key === 'failed' || key === 'timeout'
}

// Derived from consecutive execution rows only (no extra persistence): when the row before
// row i failed for the same role on a different executor, row i gets a Chinese switch note.
export function supExecutionSwitchNotes(executions: SupervisorExecution[]): Map<number, string> {
  const notes = new Map<number, string>()
  for (let i = 1; i < executions.length; i += 1) {
    const prev = executions[i - 1]
    const cur = executions[i]
    if (!prev || !cur || typeof prev !== 'object' || typeof cur !== 'object') continue
    if (!supIsFailedExecution(prev)) continue
    if ((prev.role || '') !== (cur.role || '')) continue
    if (!prev.executor || !cur.executor || prev.executor === cur.executor) continue
    const from = supExecutorLabel(prev.executor)
    const to = supExecutorLabel(cur.executor)
    const cause = supIsQuotaFailure(prev.error) ? `${from} 额度用完` : `${from} 执行失败`
    notes.set(i, `${cause}，已换成 ${to}`)
  }
  return notes
}

// Maps raw backend failure strings (agent_supervisor.py / supervisor_service.py) to short Chinese
// causes. Order matters: the first matching rule wins. Unknown text falls back to a generic line.
export function supFriendlyTaskError(raw?: string | null): string {
  const text = String(raw || '').trim()
  if (!text) return '执行没有完成'
  if (supIsQuotaFailure(text)) return '执行者的额度或用量已用完，任务没有完成'
  if (/cancel/i.test(text)) return '任务已被取消'
  if (/timed out|timeout/i.test(text)) return '执行超时，任务没有完成'
  if (/unavailable|not found|not installed|not implemented|binary/i.test(text)) return '执行器不可用（未安装或暂时无法使用）'
  if (/verdict|invalid review|review executor|review produced|review envelope|reviewer/i.test(text)) return '审查结果无效或审查失败，任务没有通过审查'
  if (/max repair rounds/i.test(text)) return '自动修了几轮还是没通过检查'
  if (/validation/i.test(text)) return '自动验收检查没有通过'
  if (/worktree|\bgit\b|symlink|target identity|lock/i.test(text)) return '代码工作区（git worktree）出现问题'
  if (/restarted|gone|ended before|interrupted/i.test(text)) return '执行过程被意外中断'
  return '执行没有完成'
}

const SUP_ROLE_SHORT_LABELS: Record<SupervisorRoleKey, string> = {
  analyst: '分析',
  implementer: '实现',
  reviewer: '审阅',
}

export function supStageLabel(stage?: string | null, fallbackState?: string | null): string {
  if (stage && stage.trim()) {
    const key = stage.trim().toLowerCase()
    return SUP_STAGE_LABELS[key] || stage
  }
  if (fallbackState && fallbackState.trim()) {
    const stateKey = fallbackState.trim().toLowerCase()
    if (SUP_STAGE_LABELS[stateKey]) return SUP_STAGE_LABELS[stateKey]
  }
  return '未指定阶段'
}

export function isTerminalTaskState(state?: string | null): boolean {
  const key = (state || '').trim().toLowerCase()
  return ['completed', 'succeeded', 'failed', 'cancelled', 'interrupted'].includes(key)
}

/** 这些状态才显示「改动」：这一轮已经跑完（成或败）或在等你确认，改动不会再有变化。 */
const DIFF_VISIBLE_STATES = [
  'completed', 'succeeded', 'ok', 'passed',
  'failed', 'error',
  'waiting_user', 'waiting_for_user', 'waiting_for_human',
  'needs_attention', 'blocked',
]

/** 这些状态才算「这一轮做完了」：任务与结果页的「合并到主分支」入口只看这一个状态。 */
const MERGE_DONE_STATES = ['completed', 'succeeded', 'ok', 'passed']

// 后端 summary() 那段多行原始摘要（任务 id / 仓库 path / Worktree path / 分支 / SHA /
// 内容指纹 / 修复轮次 / 验收 @ sha）对老板没有意义：看得见的那一行用字段现算，原文进「技术细节」。
const RAW_SUMMARY_PATTERN = /(任务|目标|状态|仓库|Worktree|分支|代码版本|内容指纹|修复轮次|验收)[:：]/

/** 是不是后端那段多行原始摘要（不是的话就是执行者自己写的一句话，可以照常显示）。 */
export function supIsRawTaskSummary(text?: string | null): boolean {
  const raw = (text || '').trim()
  if (!raw) return false
  if (!raw.includes('\n')) return false
  return RAW_SUMMARY_PATTERN.test(raw)
}

/** 执行者自己写的结论（不是原始摘要时）——一句话，原样给老板看。 */
function taskResultNote(taskDetail: SupervisorTaskDetail | null, task: SupervisorTask): string {
  const raw = (taskDetail?.summary || task.metadata?.result_summary || '').trim()
  if (!raw || supIsRawTaskSummary(raw)) return ''
  const firstLine = raw.split('\n').map(line => line.trim()).find(line => line.length > 0) || ''
  return firstLine.length > 200 ? `${firstLine.slice(0, 200)}…` : firstLine
}

export function isAttentionTask(task: SupervisorTask): boolean {
  const key = (task.state || '').trim().toLowerCase()
  return (
    ['failed', 'blocked', 'waiting_user', 'waiting_for_user', 'waiting_for_human', 'needs_attention', 'interrupted'].includes(key)
    || Boolean(task.error && String(task.error).trim())
  )
}

export function isTaskInWorktreeContext(
  task: SupervisorTask,
  repo: SupervisorRepo,
  worktree: SupervisorWorktree,
): boolean {
  const raw = task as SupervisorTask & { repo_id?: string; worktree_id?: string }
  const taskWtId = raw.worktree_id || task.metadata?.worktree_id
  const taskRepoId = raw.repo_id || task.metadata?.repo_id
  if (taskWtId && taskWtId !== worktree.id) return false
  if (taskRepoId && taskRepoId !== repo.id) return false
  const taskWtPath = task.worktree_path || (typeof task.metadata?.worktree_path === 'string' ? task.metadata.worktree_path : '')
  if (taskWtPath && worktree.path && taskWtPath !== worktree.path) return false
  const taskRepoPath = task.repo_path || (typeof task.metadata?.repo_path === 'string' ? task.metadata.repo_path : '')
  if (taskRepoPath && repo.path && taskRepoPath !== repo.path && (!worktree.path || taskRepoPath !== worktree.path)) {
    return false
  }
  return true
}

function generateRequestId(): string {
  const bytes = new Uint8Array(16)
  window.crypto.getRandomValues(bytes)
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
}

function computeFingerprint(value: unknown): string {
  const text = JSON.stringify(value)
  let hash = 2166136261
  for (let i = 0; i < text.length; i += 1) {
    hash = Math.imul(hash ^ text.charCodeAt(i), 16777619)
  }
  return `${text.length}:${(hash >>> 0).toString(16)}`
}

function loadPendingDispatch(contextKey: string): PendingDispatch | null {
  try {
    const raw = window.sessionStorage.getItem(`${PENDING_DISPATCH_PREFIX}${contextKey}`)
    return raw ? (JSON.parse(raw) as PendingDispatch) : null
  } catch {
    return null
  }
}

function savePendingDispatch(contextKey: string, pending: PendingDispatch | null): void {
  try {
    const key = `${PENDING_DISPATCH_PREFIX}${contextKey}`
    if (!pending) window.sessionStorage.removeItem(key)
    else window.sessionStorage.setItem(key, JSON.stringify(pending))
  } catch {
    // Ignore storage quota / privacy errors; in-memory ref still protects retry.
  }
}

function loadSavedContext(): { repoId: string; worktreeId: string } {
  try {
    const raw = window.localStorage.getItem(CONTEXT_STORAGE_KEY)
    if (!raw) return { repoId: '', worktreeId: '' }
    const parsed = JSON.parse(raw) as { repoId?: string; worktreeId?: string }
    return { repoId: parsed.repoId || '', worktreeId: parsed.worktreeId || '' }
  } catch {
    return { repoId: '', worktreeId: '' }
  }
}

function saveContext(repoId: string, worktreeId: string): void {
  try {
    if (!repoId && !worktreeId) window.localStorage.removeItem(CONTEXT_STORAGE_KEY)
    else window.localStorage.setItem(CONTEXT_STORAGE_KEY, JSON.stringify({ repoId, worktreeId }))
  } catch {
    // Ignore storage errors
  }
}

function shortSha(sha?: string | null): string {
  if (!sha) return ''
  return sha.trim().slice(0, 7)
}

function formatShortTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function MachineIcon({ kind }: { kind: 'laptop' | 'network' | 'server' }) {
  if (kind === 'laptop') {
    return (
      <svg className="sup-machine-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="4" y="5" width="16" height="11" rx="2" />
        <path d="M2.5 19h19" />
        <path d="M9.5 10l-1.5 1.5 1.5 1.5M14.5 10l1.5 1.5-1.5 1.5" />
      </svg>
    )
  }
  if (kind === 'network') {
    return (
      <svg className="sup-machine-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="7" cy="12" r="2.2" />
        <circle cx="12" cy="7" r="2.2" />
        <circle cx="17" cy="12" r="2.2" />
        <circle cx="12" cy="17" r="2.2" />
        <path d="M8.8 10.5l1.6-1.8M13.6 8.7l1.6 1.8M15.2 13.5l-1.6 1.8M10.4 15.3l-1.6-1.8" />
      </svg>
    )
  }
  return (
    <svg className="sup-machine-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4" y="4" width="16" height="7" rx="1.8" />
      <rect x="4" y="13" width="16" height="7" rx="1.8" />
      <circle cx="8" cy="7.5" r="0.9" fill="currentColor" />
      <circle cx="8" cy="16.5" r="0.9" fill="currentColor" />
      <path d="M12 7.5h5M12 16.5h5" />
    </svg>
  )
}

function worktreeBadgeInfo(wt: SupervisorWorktree): { label: string; tone: 'readonly' | 'locked' | 'ready' } {
  if (wt.is_main) return { label: '主分支（只看）', tone: 'readonly' }
  if (!wt.managed) return { label: '未托管（只看）', tone: 'readonly' }
  if (wt.locked || wt.active_task_id) return { label: '执行中 · 已锁定', tone: 'locked' }
  return { label: '可以开始', tone: 'ready' }
}

function SupervisorRepoSidebar({
  repositories,
  selectedRepoId,
  selectedWorktreeId,
  expandedRepos,
  searchQuery,
  onSearchChange,
  onToggleRepo,
  onSelectWorktree,
  onRegisterRepo,
  onCreateProject,
  onCreateWorktree,
  registerBusy,
  registerError,
  createProjectBusy,
  createProjectError,
  createWorktreeBusyRepoId,
  createWorktreeError,
  openCreateForRepoId,
  onOpenCreateWorktree,
  mobileCloseButton,
}: {
  repositories: SupervisorRepo[]
  selectedRepoId: string
  selectedWorktreeId: string
  expandedRepos: Record<string, boolean>
  searchQuery: string
  onSearchChange: (q: string) => void
  onToggleRepo: (repoId: string) => void
  onSelectWorktree: (repoId: string, worktreeId: string) => void
  onRegisterRepo: (path: string, name?: string) => Promise<boolean>
  onCreateProject: (name: string) => Promise<boolean>
  onCreateWorktree: (repoId: string, branch?: string, baseRef?: string) => Promise<boolean>
  registerBusy: boolean
  registerError: string
  createProjectBusy: boolean
  createProjectError: string
  createWorktreeBusyRepoId: string
  createWorktreeError: string
  openCreateForRepoId: string
  onOpenCreateWorktree: (repoId: string) => void
  mobileCloseButton?: React.ReactNode
}) {
  const [registerOpen, setRegisterOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [projectName, setProjectName] = useState('')
  const [repoPath, setRepoPath] = useState('')
  const [repoName, setRepoName] = useState('')
  const [newBranch, setNewBranch] = useState('')
  const [newBaseRef, setNewBaseRef] = useState('')

  const filteredRepos = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return repositories
    return repositories.filter(repo =>
      repo.name.toLowerCase().includes(q)
      || repo.path.toLowerCase().includes(q)
      || (repo.worktrees || []).some(wt => wt.branch.toLowerCase().includes(q) || wt.path.toLowerCase().includes(q)),
    )
  }, [repositories, searchQuery])

  const submitCreateProject = async (event: FormEvent) => {
    event.preventDefault()
    const name = projectName.trim()
    if (!name || createProjectBusy) return
    const ok = await onCreateProject(name)
    if (ok) {
      setProjectName('')
      setCreateOpen(false)
    }
  }

  const submitRegister = async (event: FormEvent) => {
    event.preventDefault()
    if (!repoPath.trim() || registerBusy) return
    const ok = await onRegisterRepo(repoPath.trim(), repoName.trim() || undefined)
    if (ok) {
      setRepoPath('')
      setRepoName('')
      setRegisterOpen(false)
    }
  }

  const submitCreateWorktree = async (event: FormEvent, repoId: string) => {
    event.preventDefault()
    if (createWorktreeBusyRepoId) return
    const ok = await onCreateWorktree(repoId, newBranch.trim() || undefined, newBaseRef.trim() || undefined)
    if (ok) {
      setNewBranch('')
      setNewBaseRef('')
      onOpenCreateWorktree('')
    }
  }

  return (
    <div className="sup-sidebar-inner">
      <div className="sup-sidebar-header">
        <h2>仓库</h2>
        <div className="sup-sidebar-header-actions">
          <button
            type="button"
            className="agent-new-btn sup-register-toggle"
            aria-expanded={createOpen}
            aria-label="新建项目"
            onClick={() => {
              setCreateOpen(prev => !prev)
              setRegisterOpen(false)
            }}
          >
            <span aria-hidden="true">+</span> 新建项目
          </button>
          <button
            type="button"
            className="agent-new-btn sup-register-toggle"
            aria-expanded={registerOpen}
            aria-label="注册仓库"
            onClick={() => {
              setRegisterOpen(prev => !prev)
              setCreateOpen(false)
            }}
          >
            <span aria-hidden="true">+</span> 注册仓库
          </button>
          {mobileCloseButton}
        </div>
      </div>

      <div className="sup-search-box">
        <input
          type="search"
          aria-label="搜索仓库"
          placeholder="搜索仓库或分支…"
          value={searchQuery}
          onChange={e => onSearchChange(e.target.value)}
        />
      </div>

      {createOpen && (
        <form className="sup-inline-form" aria-label="新建项目表单" onSubmit={e => void submitCreateProject(e)}>
          <p className="sup-form-help">
            只需要一个名称。Conveyor 会在服务器托管的位置新建一个空的 Git 仓库，并准备一份可写的独立工作目录。不用填写服务器上的文件夹路径。
          </p>
          <div className="sup-form-field">
            <label htmlFor="sup-project-name-input">项目名称</label>
            <input
              id="sup-project-name-input"
              type="text"
              required
              maxLength={64}
              placeholder="运维门户"
              value={projectName}
              onChange={e => setProjectName(e.target.value)}
              disabled={createProjectBusy}
            />
          </div>
          {createProjectError && (
            <p className="sup-inline-error" role="alert">{createProjectError}</p>
          )}
          <div className="sup-form-actions">
            <button type="button" className="shell-button" onClick={() => setCreateOpen(false)} disabled={createProjectBusy}>
              取消
            </button>
            <button type="submit" className="shell-button primary" disabled={!projectName.trim() || createProjectBusy}>
              {createProjectBusy ? '正在创建…' : '创建项目'}
            </button>
          </div>
        </form>
      )}

      {registerOpen && (
        <form className="sup-inline-form" aria-label="注册仓库表单" onSubmit={e => void submitRegister(e)}>
          <div className="sup-form-field">
            <label htmlFor="sup-repo-path-input">仓库路径</label>
            <input
              id="sup-repo-path-input"
              type="text"
              required
              placeholder="/path/to/git/repo"
              value={repoPath}
              onChange={e => setRepoPath(e.target.value)}
              disabled={registerBusy}
            />
          </div>
          <div className="sup-form-field">
            <label htmlFor="sup-repo-name-input">显示名称（可选）</label>
            <input
              id="sup-repo-name-input"
              type="text"
              placeholder="例如：Conveyor"
              value={repoName}
              onChange={e => setRepoName(e.target.value)}
              disabled={registerBusy}
            />
          </div>
          {registerError && (
            <p className="sup-inline-error" role="alert">{registerError}</p>
          )}
          <div className="sup-form-actions">
            <button type="button" className="shell-button" onClick={() => setRegisterOpen(false)} disabled={registerBusy}>
              取消
            </button>
            <button type="submit" className="shell-button primary" disabled={!repoPath.trim() || registerBusy}>
              {registerBusy ? '正在添加…' : '确认添加'}
            </button>
          </div>
        </form>
      )}

      <div className="sup-repo-tree" role="tree" aria-label="仓库与工作目录列表">
        {filteredRepos.map(repo => {
          const isExpanded = expandedRepos[repo.id] ?? (repo.id === selectedRepoId || repositories.length <= 3)
          const isCreatingHere = openCreateForRepoId === repo.id
          return (
            <div key={repo.id} className={`sup-repo-node ${repo.id === selectedRepoId ? 'is-selected-repo' : ''}`} role="treeitem" aria-expanded={isExpanded}>
              <div className="sup-repo-row">
                <button
                  type="button"
                  className="sup-repo-toggle"
                  aria-expanded={isExpanded}
                  onClick={() => onToggleRepo(repo.id)}
                >
                  <span className="sup-tree-chevron" aria-hidden="true">{isExpanded ? '▾' : '▸'}</span>
                  <span className="sup-repo-info">
                    <strong>{repo.name}</strong>
                    <small title={repo.path}>{repo.path}</small>
                  </span>
                  <span className="sup-worktree-count">{(repo.worktrees || []).length}</span>
                </button>
              </div>

              {isExpanded && (
                <div className="sup-worktree-list" role="group" aria-label={`${repo.name} 的工作目录`}>
                  {(repo.worktrees || []).map(wt => {
                    const active = repo.id === selectedRepoId && wt.id === selectedWorktreeId
                    const badge = worktreeBadgeInfo(wt)
                    return (
                      <button
                        key={wt.id}
                        type="button"
                        className={`sup-worktree-item ${active ? 'active' : ''}`}
                        aria-current={active ? 'true' : undefined}
                        onClick={() => onSelectWorktree(repo.id, wt.id)}
                      >
                        <div className="sup-worktree-item-top">
                          <span className="sup-worktree-branch" title={wt.branch || wt.path}>
                            <span className="sup-branch-glyph" aria-hidden="true">⑂</span>
                            <strong>{wt.branch || 'detached'}</strong>
                          </span>
                          <span className={`sup-wt-badge sup-wt-badge--${badge.tone}`}>{badge.label}</span>
                        </div>
                        <div className="sup-worktree-item-meta">
                          {wt.head && <code>{shortSha(wt.head)}</code>}
                          <span title={wt.path}>{wt.path}</span>
                        </div>
                      </button>
                    )
                  })}

                  {isCreatingHere ? (
                    <form
                      className="sup-inline-form sup-worktree-create-form"
                      aria-label={`为 ${repo.name} 新建工作目录`}
                      onSubmit={e => void submitCreateWorktree(e, repo.id)}
                    >
                      <div className="sup-form-field">
                        <label htmlFor={`sup-wt-branch-${repo.id}`}>分支名称（可选）</label>
                        <input
                          id={`sup-wt-branch-${repo.id}`}
                          type="text"
                          placeholder="例如：feat/supervisor-ui"
                          value={newBranch}
                          onChange={e => setNewBranch(e.target.value)}
                          disabled={Boolean(createWorktreeBusyRepoId)}
                        />
                      </div>
                      <div className="sup-form-field">
                        <label htmlFor={`sup-wt-base-${repo.id}`}>基准分支或提交（可选）</label>
                        <input
                          id={`sup-wt-base-${repo.id}`}
                          type="text"
                          placeholder="默认：main / HEAD"
                          value={newBaseRef}
                          onChange={e => setNewBaseRef(e.target.value)}
                          disabled={Boolean(createWorktreeBusyRepoId)}
                        />
                      </div>
                      {createWorktreeError && (
                        <p className="sup-inline-error" role="alert">{createWorktreeError}</p>
                      )}
                      <div className="sup-form-actions">
                        <button
                          type="button"
                          className="shell-button"
                          onClick={() => onOpenCreateWorktree('')}
                          disabled={Boolean(createWorktreeBusyRepoId)}
                        >
                          取消
                        </button>
                        <button
                          type="submit"
                          className="shell-button primary"
                          disabled={Boolean(createWorktreeBusyRepoId)}
                        >
                          {createWorktreeBusyRepoId === repo.id ? '创建中…' : '创建工作目录'}
                        </button>
                      </div>
                    </form>
                  ) : (
                    <button
                      type="button"
                      className="sup-create-wt-btn"
                      onClick={() => {
                        setNewBranch('')
                        setNewBaseRef('')
                        onOpenCreateWorktree(repo.id)
                      }}
                    >
                      + 新建工作目录
                    </button>
                  )}
                </div>
              )}
            </div>
          )
        })}

        {!filteredRepos.length && (
          <div className="sup-empty-tree">
            {repositories.length === 0
              ? '还没有项目。点上面的「新建项目」创建一个空仓库，或点「注册仓库」登记已有的 Git 路径。'
              : '没有匹配的仓库或分支。'}
          </div>
        )}
      </div>
    </div>
  )
}

export function SupervisorRoleBar({
  capabilities,
  effectiveRoles,
  onChangeRole,
  onOpenWorkers,
  disabled,
}: {
  capabilities: SupervisorCapabilities | null
  effectiveRoles: RoleOverrides
  onChangeRole: (role: SupervisorRoleKey, patch: Partial<RoleOverrideEntry>) => void
  onOpenWorkers?: () => void
  disabled?: boolean
}) {
  const executors = capabilities?.executors || []
  // Pi (built-in) only serves 对话 and 任务, so it never appears here; say what the Supervisor needs instead.
  const noRoleExecutorReady = capabilities !== null && !executors.some(exec => exec.available)
  const coordinatorExec = capabilities?.coordinator?.executor || '当前主对话'
  const coordinatorModel = capabilities?.coordinator?.model || '默认模型'
  // The coordinator card shows the plain Chinese label; the raw executor code and model id are
  // technical detail (QA N29).
  const coordinatorLabel = supCoordinatorLabel(coordinatorExec)
  const coordinatorTech = [coordinatorExec, coordinatorModel].filter(Boolean).join(' · ')

  const getExecutorInfo = (execName: string): SupervisorExecutorInfo | undefined =>
    executors.find(item => item.name === execName)

  const getRoleAvailability = (role: SupervisorRoleKey, selectedExecutor: string): { available: boolean; reason?: string } => {
    const execInfo = getExecutorInfo(selectedExecutor)
    const defaultRole = capabilities?.roles?.[role]
    if (execInfo) {
      return {
        available: execInfo.available,
        reason: execInfo.reason || (defaultRole?.executor === selectedExecutor ? defaultRole.reason : undefined),
      }
    }
    if (defaultRole && defaultRole.executor === selectedExecutor) {
      return { available: defaultRole.available, reason: defaultRole.reason }
    }
    return { available: Boolean(capabilities), reason: capabilities ? undefined : '能力元数据未就绪' }
  }

  return (
    <div className="sup-roles-panel" aria-label="谁来干活">
      {noRoleExecutorReady && (
        <p className="sup-role-guidance" role="status">
          项目需要 Claude Code、Antigravity 或 Command Code 至少一个已安装并登录。
          {onOpenWorkers && <> <button type="button" className="sup-role-guidance-link" onClick={onOpenWorkers}>去执行者页</button></>}
          Pi 只用于对话和任务。
        </p>
      )}
      <div className="sup-roles-grid">
        <div className="sup-role-card sup-role-card--coordinator">
          <div className="sup-role-head">
            <span className="sup-role-title">讨论</span>
            <span className="sup-role-badge is-ok">先聊方案</span>
          </div>
          <div className="sup-role-Static">
            <strong>{coordinatorLabel}</strong>
            <small>用「模型设置」里的对话模型聊方案</small>
            <details className="sup-role-tech">
              <summary>技术详情</summary>
              <span>{coordinatorTech}</span>
            </details>
          </div>
        </div>

        {ROLE_META.map(({ key, label, hint }) => {
          const current = effectiveRoles[key]
          const status = getRoleAvailability(key, current.executor)
          const optionNames = new Set(executors.map(e => e.name))
          const extraOption = current.executor && !optionNames.has(current.executor) ? current.executor : null
          return (
            <div key={key} className={`sup-role-card ${!status.available ? 'is-unavailable' : ''}`}>
              <div className="sup-role-head">
                <label htmlFor={`sup-role-exec-${key}`} className="sup-role-title">{label}</label>
                <span className={`sup-role-badge ${status.available ? 'is-ok' : 'is-err'}`}>
                  {status.available ? '已就绪' : '不可用'}
                </span>
              </div>
              <small className="sup-role-hint">{hint}</small>
              <select
                id={`sup-role-exec-${key}`}
                aria-label={`${label}执行器`}
                value={current.executor}
                disabled={disabled || (!executors.length && !extraOption)}
                onChange={e => {
                  const nextExec = e.target.value
                  const defaultRole = capabilities?.roles?.[key]
                  const nextModel = defaultRole && defaultRole.executor === nextExec ? (defaultRole.model || '') : ''
                  onChangeRole(key, { executor: nextExec, model: nextModel })
                }}
              >
                {extraOption && (
                  <option value={extraOption}>{extraOption}{!status.available ? '（不可用）' : ''}</option>
                )}
                {/* Unavailable executors are hidden unless this role currently uses them (then kept with the reason). */}
                {executors
                  .filter(exec => exec.available || exec.name === current.executor)
                  .map(exec => (
                    <option key={exec.name} value={exec.name}>
                      {exec.label || exec.name}{exec.name === 'pool' && exec.available ? '（推荐）' : ''}{!exec.available ? '（不可用）' : ''}
                    </option>
                  ))}
                {!extraOption && executors.length === 0 && (
                  <option value="">未配置执行器</option>
                )}
              </select>
              {!status.available && status.reason && (
                <small className="sup-role-reason" role="status">{status.reason}</small>
              )}
            </div>
          )
        })}
      </div>

      <details className="sup-role-advanced">
        <summary>高级设置</summary>
        <div className="sup-role-advanced-body">
          <p className="sup-quota-disclaimer">
            说明：执行器显示「已就绪」仅代表本地适配器或命令已配置，不承诺远程模型配额一定可用；执行失败时不会静默回退到其他服务商。
          </p>
          <div className="sup-model-overrides-grid">
            {ROLE_META.map(({ key, label }) => {
              const current = effectiveRoles[key]
              const execInfo = getExecutorInfo(current.executor)
              const fetchedModels = execInfo?.models && execInfo.models.length > 0 ? execInfo.models : null
              return (
                <div key={key} className="sup-form-field">
                  <label htmlFor={`sup-role-model-${key}`}>{label}模型（{current.executor || '未选'}）</label>
                  {fetchedModels ? (
                    <select
                      id={`sup-role-model-${key}`}
                      aria-label={`${label}模型`}
                      value={current.model}
                      disabled={disabled}
                      onChange={e => onChangeRole(key, { model: e.target.value })}
                    >
                      <option value="">默认模型{capabilities?.roles?.[key]?.model ? `（${capabilities.roles[key].model}）` : ''}</option>
                      {fetchedModels.map(m => (
                        <option key={m.id} value={m.id}>{m.name || m.id}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      id={`sup-role-model-${key}`}
                      aria-label={`${label}模型`}
                      type="text"
                      placeholder={capabilities?.roles?.[key]?.model ? `默认：${capabilities.roles[key].model}` : '留空使用默认模型，或输入模型名称'}
                      value={current.model}
                      disabled={disabled}
                      onChange={e => onChangeRole(key, { model: e.target.value })}
                    />
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </details>
    </div>
  )
}

function SupervisorRunHistory({
  worktreeId,
  tasks,
  selectedTaskId,
  onSelectTask,
}: {
  worktreeId: string
  tasks: SupervisorTask[]
  selectedTaskId: string
  onSelectTask: (taskId: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const lastWorktreeRef = useRef(worktreeId)
  const seenAlertKeysRef = useRef<Set<string>>(new Set())

  const attentionKeys = useMemo(
    () =>
      tasks
        .filter(t => isAttentionTask(t))
        .map(t => `${worktreeId}:${t.id}:${(t.state || '').trim().toLowerCase()}:${(t.error || '').trim()}`),
    [tasks, worktreeId],
  )

  if (lastWorktreeRef.current !== worktreeId) {
    lastWorktreeRef.current = worktreeId
    const hasUnseen = attentionKeys.some(k => !seenAlertKeysRef.current.has(k))
    if (hasUnseen) {
      for (const k of attentionKeys) seenAlertKeysRef.current.add(k)
      if (!expanded) setExpanded(true)
    } else if (expanded) {
      setExpanded(false)
    }
  } else {
    const hasUnseen = attentionKeys.some(k => !seenAlertKeysRef.current.has(k))
    if (hasUnseen) {
      for (const k of attentionKeys) seenAlertKeysRef.current.add(k)
      if (!expanded) setExpanded(true)
    }
  }

  const latestTask = tasks[0]
  // 目标循环里「前面某一轮失败、后面还有轮次」是正常过程：这些失败不算「需处理」。
  const latestRoundByGoal = useMemo(() => {
    const rounds = new Map<string, number>()
    for (const task of tasks) {
      const info = parseGoalRoundTask(task.goal)
      if (!info) continue
      const key = goalRoundKey(info)
      const seen = rounds.get(key) || 0
      if (info.round > seen) rounds.set(key, info.round)
    }
    return rounds
  }, [tasks])
  const isSupersededRound = useCallback((task: SupervisorTask) => {
    const info = parseGoalRoundTask(task.goal)
    if (!info) return false
    const latest = latestRoundByGoal.get(goalRoundKey(info))
    return typeof latest === 'number' && latest > info.round
  }, [latestRoundByGoal])
  const attentionCount = tasks.filter(t => isAttentionTask(t) && !isSupersededRound(t)).length

  return (
    <section className={`sup-run-history ${expanded ? 'is-expanded' : 'is-collapsed'}`} aria-label="运行记录">
      <button
        type="button"
        className="sup-run-history-bar"
        aria-expanded={expanded}
        onClick={() => setExpanded(prev => !prev)}
      >
        <span className="sup-run-history-chevron" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
        <strong>运行记录 · {tasks.length} 条</strong>
        {latestTask && (
          <span className="sup-run-history-latest">
            最近：{supStateLabel(latestTask.state)}
          </span>
        )}
        {attentionCount > 0 ? (
          <span className="sup-run-history-alert">{attentionCount} 条需处理或失败</span>
        ) : (
          <span className="sup-run-history-hint">失败或等待确认时自动展开</span>
        )}
      </button>

      {expanded && (
        <div className="sup-run-history-list" role="group" aria-label="当前工作目录运行记录列表">
          {tasks.length === 0 ? (
            <p className="sup-muted-note">这个工作目录还没有任务运行记录。</p>
          ) : (
            tasks.map(task => {
              const isSelected = task.id === selectedTaskId
              const attention = isAttentionTask(task)
              return (
                <div
                  key={task.id}
                  className={`sup-run-row ${isSelected ? 'selected' : ''} ${attention ? 'is-attention' : ''}`}
                >
                  <button
                    type="button"
                    className="sup-run-row-button"
                    aria-pressed={isSelected}
                    onClick={() => onSelectTask(task.id)}
                  >
                    <div className="sup-run-row-main">
                      <strong>{goalRoundTaskTitle(task.goal) || task.goal || task.id}</strong>
                      <span className={`status-badge ${task.state}`}>
                        <i />
                        {supStateLabel(task.state)}
                      </span>
                    </div>
                    <div className="sup-run-row-meta">
                      <span>阶段：{supStageLabel(task.stage, task.state)}</span>
                      {typeof task.repair_round === 'number' && typeof task.max_repair_rounds === 'number' && task.max_repair_rounds > 0 && (
                        <span>修复轮次：{task.repair_round}/{task.max_repair_rounds}</span>
                      )}
                      <span>{formatShortTime(task.updated_at || task.created_at)}</span>
                      <code>{task.id}</code>
                    </div>
                  </button>
                  {task.error && (
                    <div className="sup-run-row-error">
                      <span>{supFriendlyTaskError(task.error)}</span>
                      <details className="sup-run-row-raw-error">
                        <summary>技术详情</summary>
                        <pre>{task.error}</pre>
                      </details>
                    </div>
                  )}
                </div>
              )
            })
          )}
        </div>
      )}
    </section>
  )
}

/** Drop anything that is not a plain object so downstream field access never throws. */
function onlyObjects<T>(items: T[] | undefined | null): T[] {
  return (Array.isArray(items) ? items : []).filter((item): item is T => Boolean(item) && typeof item === 'object')
}

// Always-visible summary under the task progress: which worker ran each role (latest attempt wins)
// plus any executor switch notes. The full execution list stays in 技术细节.
function SupervisorTaskExecutors({ executions }: { executions: SupervisorExecution[] }) {
  const rows = onlyObjects(executions)
  if (rows.length === 0) return null
  const latestByRole = new Map<SupervisorRoleKey, SupervisorExecution>()
  for (const ex of rows) {
    const key = ROLE_META.find(m => m.key === ex.role)?.key
    if (key) latestByRole.set(key, ex)
  }
  const roleParts = ROLE_META.filter(m => latestByRole.has(m.key))
  const switchNotes = [...supExecutionSwitchNotes(rows).values()]
  if (roleParts.length === 0 && switchNotes.length === 0) return null
  return (
    <div className="sup-task-executors" aria-label="各角色执行者">
      {roleParts.length > 0 && (
        <p className="sup-task-executors-line">
          {roleParts.map((m, idx) => (
            <Fragment key={m.key}>
              {idx > 0 && <span aria-hidden="true"> · </span>}
              <span>{SUP_ROLE_SHORT_LABELS[m.key]}：{supExecutorLabel(latestByRole.get(m.key)?.executor)}</span>
            </Fragment>
          ))}
        </p>
      )}
      {switchNotes.map((note, idx) => (
        <p key={idx} className="sup-task-switch-note" role="note">{note}</p>
      ))}
    </div>
  )
}

export type NormalizedChangedFile = { path: string; status: string }

/**
 * metadata.changed_files arrives in two shapes from the supervisor backend: the live git
 * path (supervisor_service._enrich_task_dict) returns a list of plain strings such as
 * "calc.py", while other payloads carry {path|file|name, status} objects. Normalize both to
 * {path, status} so ChangedFileList never receives a bare string, and drop any entry that
 * has no usable path. Pure + exported so it can be unit-tested.
 */
export function normalizeChangedFiles(raw: unknown): NormalizedChangedFile[] {
  if (!Array.isArray(raw)) return []
  const out: NormalizedChangedFile[] = []
  for (const entry of raw) {
    if (typeof entry === 'string') {
      const path = entry.trim()
      if (path) out.push({ path, status: 'M' })
      continue
    }
    if (entry && typeof entry === 'object') {
      const obj = entry as { path?: unknown; file?: unknown; name?: unknown; status?: unknown }
      const rawPath = obj.path ?? obj.file ?? obj.name
      const path = typeof rawPath === 'string' ? rawPath.trim() : ''
      if (path) {
        const status = typeof obj.status === 'string' && obj.status.trim() ? obj.status : 'M'
        out.push({ path, status })
      }
    }
  }
  return out
}

function SupervisorTechnicalDetails({
  task,
  taskDetail,
}: {
  task: SupervisorTask | null
  taskDetail: SupervisorTaskDetail | null
}) {
  const changedFiles = normalizeChangedFiles(taskDetail?.metadata?.changed_files ?? task?.metadata?.changed_files)
  const rawSummary = (taskDetail?.summary || task?.metadata?.result_summary || '').trim()
  const handoffs = onlyObjects(taskDetail?.handoffs)
  const executions = onlyObjects(taskDetail?.executions)
  const switchNotes = supExecutionSwitchNotes(executions)
  const countLabel = [
    changedFiles.length ? `${changedFiles.length} 个改动文件` : '',
    handoffs.length ? `${handoffs.length} 次角色交接` : '',
    executions.length ? `${executions.length} 条执行记录` : '',
  ].filter(Boolean).join(' · ') || '默认收起'

  return (
    <details className="sup-tech-details">
      <summary className="sup-tech-summary">
        <span>技术细节（可选查看：原始摘要、改动文件、交接记录与执行输出）</span>
        <span className="sup-tech-meta">{countLabel}</span>
      </summary>
      <div className="sup-tech-body">
        {task ? (
          <div className="sup-tech-kv-row">
            {task.branch && <span>分支：<code>{task.branch}</code></span>}
            {task.base_sha && <span>基准提交：<code>{shortSha(task.base_sha)}</code></span>}
            {task.current_sha && <span>当前提交：<code>{shortSha(task.current_sha)}</code></span>}
          </div>
        ) : null}

        {rawSummary && (
          <div className="sup-tech-block">
            <h4>原始摘要</h4>
            <pre className="sup-exec-output">{rawSummary}</pre>
          </div>
        )}

        <div className="sup-tech-block">
          <h4>改动文件</h4>
          {changedFiles.length > 0 ? (
            <ChangedFileList files={changedFiles} />
          ) : (
            <p className="sup-muted-note">暂无已记录的改动文件。</p>
          )}
        </div>

        {handoffs.length > 0 && (
          <div className="sup-tech-block">
            <h4>角色交接记录</h4>
            <ul className="sup-handoff-list">
              {handoffs.map((h, idx) => (
                <li key={h.id ?? idx} className="sup-handoff-item">
                  <strong>{h.from_role || '上一阶段'} → {h.to_role || h.stage || '下一阶段'}</strong>
                  {h.created_at && <time>{formatShortTime(h.created_at)}</time>}
                  {h.summary && <p>{h.summary}</p>}
                </li>
              ))}
            </ul>
          </div>
        )}

        {executions.length > 0 && (
          <div className="sup-tech-block">
            <h4>执行与终端输出</h4>
            <div className="sup-execution-list">
              {executions.map((ex, idx) => {
                const switchNote = switchNotes.get(idx)
                const roleLabel = ROLE_META.find(m => m.key === ex.role)?.label || ex.role || ex.stage || '执行'
                return (
                  <Fragment key={ex.id ?? idx}>
                    {switchNote && (
                      <p className="sup-exec-switch-note" role="note">
                        <time>{formatShortTime(ex.started_at)}</time>
                        {switchNote}
                      </p>
                    )}
                    <details className="sup-execution-item">
                      <summary>
                        <strong>{roleLabel}</strong>
                        {ex.executor && <span> · {supExecutorLabel(ex.executor)}{ex.model ? ` (${ex.model})` : ''}</span>}
                        {ex.status && <span className="sup-exec-status"> · {supStateLabel(ex.status)}</span>}
                      </summary>
                      {ex.error && (
                        <>
                          <p className="sup-inline-error">
                            {supIsQuotaFailure(ex.error) ? '额度用完，本次执行未完成' : '本次执行失败'}
                          </p>
                          <details className="sup-exec-raw-error">
                            <summary>技术详情</summary>
                            <pre>{ex.error}</pre>
                          </details>
                        </>
                      )}
                      {ex.output ? <pre className="sup-exec-output">{ex.output}</pre> : <p className="sup-muted-note">无标准输出内容。</p>}
                    </details>
                  </Fragment>
                )
              })}
            </div>
          </div>
        )}
      </div>
    </details>
  )
}

export function supPrStatusLabel(rawStatus?: string | null): string {
  if (!rawStatus) return '未核验'
  const s = String(rawStatus).trim()
  if (!s || s.toLowerCase() === 'unknown' || s.toLowerCase() === 'unverified') return '未核验'
  const lower = s.toLowerCase()
  if (lower === 'merged') return '已合并'
  if (lower === 'open') return '开放中'
  if (lower === 'closed') return '已关闭'
  if (lower === 'draft') return '草稿'
  return s
}

function resolveStageStatus(deployment: SupervisorDeployment | null, stageKey: DeploymentStageKey): {
  status: string
  detail?: string
  started_at?: string
  finished_at?: string
} {
  if (!deployment) return { status: 'unknown' }
  const matched = (deployment.stages || []).find(s => s.stage === stageKey)
  if (matched && matched.status) {
    return {
      status: matched.status,
      detail: matched.detail,
      started_at: matched.started_at,
      finished_at: matched.finished_at,
    }
  }
  // Never infer completed/success for missing or unrecorded stages.
  if (deployment.stage === stageKey && deployment.status) {
    return { status: deployment.status }
  }
  return { status: 'unknown' }
}

function stageTone(status: string): 'ok' | 'running' | 'failed' | 'pending' | 'unknown' {
  const key = (status || 'unknown').trim().toLowerCase()
  if (['completed', 'succeeded', 'ok', 'passed'].includes(key)) return 'ok'
  if (['running', 'in_progress', 'dispatched', 'claimed'].includes(key)) return 'running'
  if (['failed', 'error', 'cancelled', 'rejected'].includes(key)) return 'failed'
  if (['pending', 'queued', 'ready'].includes(key)) return 'pending'
  return 'unknown'
}

function SupervisorDeployView({
  repo,
  worktree,
  selectedTask,
  capabilities,
  deployments,
  deployBusy,
  deployError,
  onStartDeploy,
}: {
  repo: SupervisorRepo | null
  worktree: SupervisorWorktree | null
  selectedTask: SupervisorTask | null
  capabilities: SupervisorCapabilities | null
  deployments: SupervisorDeployment[]
  deployBusy: boolean
  deployError: string
  onStartDeploy: () => void
}) {
  const activeDeployment = deployments[0] || null
  const deployCaps = capabilities?.deployment
  const canStart = Boolean(deployCaps?.configured && deployCaps?.can_start)

  const isUnconfigured = !deployCaps || !deployCaps.configured
  const unconfiguredPrimaryText = '尚未配置部署目标和传输流程'

  let disableReason = ''
  let technicalDetail = ''

  if (isUnconfigured) {
    disableReason = unconfiguredPrimaryText
    if (deployCaps?.reason && deployCaps.reason.trim() !== unconfiguredPrimaryText) {
      technicalDetail = deployCaps.reason.trim()
    }
  } else if (!deployCaps.can_start) {
    disableReason = deployCaps.reason?.trim() || '部署运行器当前处于不可用状态，暂不能发起部署。'
  } else if (!selectedTask) {
    disableReason = '请先在当前工作目录中选择或完成一项任务后再发起部署。'
  }

  const sourceMachine =
    activeDeployment?.source_machine?.trim()
    || deployCaps?.source_machine?.trim()
    || repo?.source_machine?.trim()
    || capabilities?.source_machine?.trim()
    || '未指定来源主机'

  const targetMachine =
    activeDeployment?.target_machine?.trim()
    || deployCaps?.target_machine?.trim()
    || '未指定目标服务器'

  const buildVersion = activeDeployment?.build_version?.trim() || '未知'
  const liveVersion = activeDeployment?.live_version?.trim() || '未知'
  const activeStageText = activeDeployment?.stage ? supStageLabel(activeDeployment.stage) : '未知'
  const deploymentRunning = Boolean(activeDeployment && ['running', 'queued', 'pending', 'in_progress'].includes((activeDeployment.status || '').toLowerCase()))

  const stageLogItems = (activeDeployment?.stages || []).filter(
    s => Boolean(s.detail || s.started_at || s.finished_at || (s.status && s.status !== 'unknown')),
  )

  return (
    <div className="sup-deploy-view" aria-label="部署视图">
      <div className="sup-deploy-header-card">
        <div>
          <h3>部署流水线</h3>
          <p className="sup-deploy-subtitle">
            来源：{sourceMachine} → Tailscale 传输通道 → 目标：{targetMachine}
          </p>
        </div>
        <div className="sup-deploy-cta-group">
          {canStart ? (
            <button
              type="button"
              className="shell-button primary"
              disabled={deployBusy || !selectedTask || deploymentRunning}
              onClick={onStartDeploy}
            >
              {deployBusy ? '正在启动部署…' : deploymentRunning ? '部署进行中…' : '开始部署'}
            </button>
          ) : (
            <button type="button" className="shell-button" disabled aria-disabled="true">
              部署不可用
            </button>
          )}
        </div>
      </div>

      {disableReason && (
        <div className="sup-banner sup-banner--muted" role="status">
          <div className="sup-banner-text">
            <strong>当前无法启动部署</strong>
            <span>{disableReason}</span>
          </div>
          {technicalDetail && (
            <details className="sup-tech-details sup-deploy-tech-details">
              <summary className="sup-tech-summary">
                <span>技术诊断信息（运维查看）</span>
              </summary>
              <div className="sup-tech-body">
                <code>{technicalDetail}</code>
              </div>
            </details>
          )}
        </div>
      )}

      {deployError && (
        <div className="sup-banner sup-banner--error" role="alert">
          <strong>部署请求失败</strong>
          <span>{deployError}</span>
        </div>
      )}

      {activeDeployment?.error && (
        <div className="sup-banner sup-banner--error" role="alert">
          <strong>部署阶段执行失败</strong>
          <span>{activeDeployment.error}</span>
        </div>
      )}

      <div className="sup-topology-card" role="region" aria-label="部署拓扑与传输路径">
        <div className="sup-topology-node">
          <div className="sup-topology-icon-wrap">
            <MachineIcon kind="laptop" />
          </div>
          <strong>{sourceMachine}</strong>
          <small>来源构建机</small>
          <span className="sup-topology-meta">构建版本：{shortSha(buildVersion) || buildVersion}</span>
        </div>

        <div className="sup-topology-connector" aria-hidden="true">
          <span className="sup-topology-line" />
        </div>

        <div className="sup-topology-node sup-topology-node--transport">
          <div className="sup-topology-icon-wrap">
            <MachineIcon kind="network" />
          </div>
          <strong>Tailscale</strong>
          <small>私有网络连接与传输</small>
        </div>

        <div className="sup-topology-connector" aria-hidden="true">
          <span className="sup-topology-line" />
        </div>

        <div className="sup-topology-node">
          <div className="sup-topology-icon-wrap">
            <MachineIcon kind="server" />
          </div>
          <strong>{targetMachine}</strong>
          <small>目标服务器</small>
          <span className="sup-topology-meta">当前线上：{shortSha(liveVersion) || liveVersion}</span>
        </div>
      </div>

      <ol className="sup-stage-stepper" aria-label="部署五阶段状态">
        {DEPLOY_STAGE_ORDER.map((item, idx) => {
          const resolved = resolveStageStatus(activeDeployment, item.key)
          const tone = stageTone(resolved.status)
          const isCurrent = activeDeployment?.stage === item.key
          return (
            <li
              key={item.key}
              className={`sup-stage-step sup-stage-step--${tone} ${isCurrent ? 'is-active-stage' : ''}`}
              data-stage={item.key}
              data-status={resolved.status}
            >
              <div className="sup-stage-step-indicator">
                <span className="sup-stage-step-num">{idx + 1}</span>
                <span className="sup-stage-step-status">{supStateLabel(resolved.status)}</span>
              </div>
              <strong className="sup-stage-step-label">{item.label}</strong>
              {resolved.detail && <small className="sup-stage-step-detail">{resolved.detail}</small>}
            </li>
          )
        })}
      </ol>

      <div className="sup-deploy-meta-grid">
        <div className="sup-meta-card">
          <h4>本次部署上下文</h4>
          <div className="sup-kv-list">
            <div><span>仓库</span><strong>{repo?.name || '未选'}</strong></div>
            <div><span>工作目录</span><strong>{worktree?.branch || '未选'}</strong></div>
            <div><span>关联任务</span><strong>{selectedTask?.id || activeDeployment?.task_id || '无'}</strong></div>
            <div><span>当前阶段</span><strong>{activeStageText}</strong></div>
            <div><span>总体状态</span><strong>{activeDeployment ? supStateLabel(activeDeployment.status) : '未开始'}</strong></div>
          </div>
        </div>

        <div className="sup-meta-card">
          <h4>版本与状态边界</h4>
          <div className="sup-kv-list">
            <div><span>构建版本</span><code>{buildVersion}</code></div>
            <div><span>线上版本</span><code>{liveVersion}</code></div>
            <div><span>任务完成状态</span><strong>{selectedTask ? supStateLabel(selectedTask.state) : '未执行'}</strong></div>
            <div><span>PR 合并状态</span><strong>{supPrStatusLabel(selectedTask?.metadata?.pr_status)}</strong></div>
            <div><span>生产部署状态</span><strong>{activeDeployment ? supStateLabel(activeDeployment.status) : '未部署'}</strong></div>
          </div>
          <p className="sup-separation-note">
            状态说明：任务完成 ≠ PR 已合并 ≠ 已部署上线。缺失或未上报的阶段保持「未知」，不会推断为成功。
          </p>
        </div>
      </div>

      <details className="sup-tech-details sup-deploy-logs">
        <summary className="sup-tech-summary">
          <span>查看阶段日志与详情（可选展开）</span>
          <span className="sup-tech-meta">{stageLogItems.length ? `${stageLogItems.length} 个阶段记录` : '暂无记录'}</span>
        </summary>
        <div className="sup-tech-body">
          {stageLogItems.length === 0 ? (
            <p className="sup-muted-note">暂无阶段日志输出。</p>
          ) : (
            <ul className="sup-stage-log-list">
              {DEPLOY_STAGE_ORDER.map(stageMeta => {
                const info = resolveStageStatus(activeDeployment, stageMeta.key)
                if (info.status === 'unknown' && !info.detail && !info.started_at && !info.finished_at) return null
                return (
                  <li key={stageMeta.key} className="sup-stage-log-item">
                    <div className="sup-stage-log-head">
                      <strong>{stageMeta.label}（{stageMeta.key}）</strong>
                      <span>{supStateLabel(info.status)}</span>
                      {info.started_at && <time>{formatShortTime(info.started_at)}</time>}
                    </div>
                    {info.detail && <pre className="sup-exec-output">{info.detail}</pre>}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </details>
    </div>
  )
}

export function SupervisorWorkspace({
  token,
  active,
  coordinatorFallbackName,
  onOpenWorkers,
}: {
  token: string
  active: boolean
  coordinatorFallbackName?: string
  onOpenWorkers?: () => void
}) {
  const narrowViewport = useMediaQuery('(max-width: 900px)')
  const [capabilities, setCapabilities] = useState<SupervisorCapabilities | null>(null)
  const [capabilitiesError, setCapabilitiesError] = useState('')
  const [repositories, setRepositories] = useState<SupervisorRepo[]>([])
  const [reposError, setReposError] = useState('')
  const [initialLoading, setInitialLoading] = useState(true)

  const savedContext = useMemo(() => loadSavedContext(), [])
  const [selectedRepoId, setSelectedRepoId] = useState(savedContext.repoId)
  const [selectedWorktreeId, setSelectedWorktreeId] = useState(savedContext.worktreeId)
  const [expandedRepos, setExpandedRepos] = useState<Record<string, boolean>>({})
  const [searchQuery, setSearchQuery] = useState('')
  const [subTab, setSubTab] = useState<'goals' | 'workspace' | 'chat' | 'deploy'>('workspace')
  // The 目标 tab becomes the default once the selected worktree has a running / waiting goal;
  // any explicit tab click wins from then on (reset when the worktree context changes).
  const subTabTouchedRef = useRef(false)
  // 选中的工作目录上有没有正在跑的目标：合并入口在目标跑着的时候要藏起来。
  const [hasActiveGoalInWorktree, setHasActiveGoalInWorktree] = useState(false)
  // 讨论里点「设为目标」带去「目标」页的草稿（目标页填进输入框后就清掉）。
  const [goalDraft, setGoalDraft] = useState<string | null>(null)
  // 目标在跑、老板又不在「目标」页时，隔一会儿重新问一次，好让「设为目标」在目标结束后重新能点。
  const [goalProbeTick, setGoalProbeTick] = useState(0)
  // 任务卡片上「合并到主分支…」：点开才读合并预览（默认收起）。
  const [mergeOpen, setMergeOpen] = useState(false)
  const [mobileRepoDrawerOpen, setMobileRepoDrawerOpen] = useState(false)

  // Repository registration & worktree creation states
  const [registerBusy, setRegisterBusy] = useState(false)
  const [registerError, setRegisterError] = useState('')
  const [openCreateForRepoId, setOpenCreateForRepoId] = useState('')
  const [createWorktreeBusyRepoId, setCreateWorktreeBusyRepoId] = useState('')
  const [createWorktreeError, setCreateWorktreeError] = useState('')
  const [createProjectBusy, setCreateProjectBusy] = useState(false)
  const [createProjectError, setCreateProjectError] = useState('')
  const [retryPrepNote, setRetryPrepNote] = useState('')

  // Per-context isolated drafts and data snapshots
  const [draftsByContext, setDraftsByContext] = useState<Record<string, WorktreeDraft>>({})
  const [dataByContext, setDataByContext] = useState<Record<string, WorktreeDataSnapshot>>({})
  const [pendingByContext, setPendingByContext] = useState<Record<string, PendingDispatch | null>>({})

  // Action busy/error states scoped to current context
  const [dispatchBusy, setDispatchBusy] = useState(false)
  const [dispatchError, setDispatchError] = useState('')
  const [cancelBusyTaskId, setCancelBusyTaskId] = useState('')
  const [deployBusy, setDeployBusy] = useState(false)
  const [deployError, setDeployError] = useState('')

  const contextGenRef = useRef(0)
  const contextAbortRef = useRef<AbortController | null>(null)
  const pollInFlightRef = useRef(false)
  const goalInputRef = useRef<HTMLTextAreaElement>(null)
  // 「查看这一轮的任务」切回任务页后，把这一轮的任务卡片滚进可视区，
  // 免得老板一落地正对着上面的派发表单。
  const taskCardRef = useRef<HTMLElement>(null)
  const pendingTaskScrollRef = useRef(false)

  // N20: a one-paragraph intro at the top of 项目, only while there is no repository yet.
  // Once the owner closes it (or a repository exists) it stays out of the way.
  const [projectIntroDismissed, setProjectIntroDismissed] = useState(() => {
    try { return localStorage.getItem(PROJECT_INTRO_DISMISSED_KEY) === 'true' } catch { return false }
  })
  const dismissProjectIntro = useCallback(() => {
    setProjectIntroDismissed(true)
    try { localStorage.setItem(PROJECT_INTRO_DISMISSED_KEY, 'true') } catch { /* storage unavailable */ }
  }, [])
  const projectIntroOpen = !initialLoading && !projectIntroDismissed && repositories.length === 0

  const selectedRepo = useMemo(
    () => repositories.find(r => r.id === selectedRepoId) || null,
    [repositories, selectedRepoId],
  )
  const selectedWorktree = useMemo(
    () => (selectedRepo?.worktrees || []).find(wt => wt.id === selectedWorktreeId) || null,
    [selectedRepo, selectedWorktreeId],
  )
  const contextKey = selectedRepo && selectedWorktree ? `${selectedRepo.id}:${selectedWorktree.id}` : ''
  const activeContextKeyRef = useRef(contextKey)
  activeContextKeyRef.current = contextKey

  const apiCall = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const res = await fetch(path, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
        ...(init?.headers || {}),
      },
    })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      const err = new Error(body.error || body.message || `请求失败 (${res.status})`) as Error & { status?: number; code?: string }
      err.status = res.status
      err.code = body.code
      throw err
    }
    return body as T
  }, [token])

  // Choose sensible initial repo/worktree when repositories arrive
  const reconcileSelection = useCallback((repos: SupervisorRepo[], currentRepoId: string, currentWtId: string) => {
    if (!repos.length) return { repoId: '', worktreeId: '' }
    const repo = repos.find(r => r.id === currentRepoId) || repos[0]
    const wts = repo.worktrees || []
    const matchedWt = wts.find(w => w.id === currentWtId)
    if (matchedWt) return { repoId: repo.id, worktreeId: matchedWt.id }
    // Prefer a managed unlocked worktree first, else first worktree
    const preferred = wts.find(w => w.managed && !w.is_main && !w.locked)
      || wts.find(w => w.managed && !w.is_main)
      || wts[0]
    return { repoId: repo.id, worktreeId: preferred?.id || '' }
  }, [])

  const loadBootstrap = useCallback(async () => {
    if (!token) return
    const [capRes, repoRes] = await Promise.allSettled([
      apiCall<SupervisorCapabilities>('/api/supervisor/capabilities'),
      apiCall<{ repositories: SupervisorRepo[] }>('/api/supervisor/repositories'),
    ])
    if (capRes.status === 'fulfilled') {
      setCapabilities(capRes.value)
      setCapabilitiesError('')
    } else {
      setCapabilitiesError(capRes.reason instanceof Error ? capRes.reason.message : '无法读取项目能力配置')
    }

    if (repoRes.status === 'fulfilled') {
      const repos = Array.isArray(repoRes.value.repositories) ? repoRes.value.repositories : []
      setRepositories(repos)
      setReposError('')
      setSelectedRepoId(prevRepoId => {
        const next = reconcileSelection(repos, prevRepoId, selectedWorktreeId)
        if (next.worktreeId !== selectedWorktreeId) {
          setSelectedWorktreeId(next.worktreeId)
        }
        saveContext(next.repoId, next.worktreeId)
        return next.repoId
      })
    } else {
      setReposError(repoRes.reason instanceof Error ? repoRes.reason.message : '无法读取仓库列表')
    }
    setInitialLoading(false)
  }, [apiCall, reconcileSelection, selectedWorktreeId, token])

  // Re-read the repository list so validation_source badges stay honest after a save.
  const refreshRepositories = useCallback(async () => {
    try {
      const repoRes = await apiCall<{ repositories: SupervisorRepo[] }>('/api/supervisor/repositories')
      const repos = Array.isArray(repoRes.repositories) ? repoRes.repositories : []
      setRepositories(repos)
      setReposError('')
    } catch (err) {
      setReposError(err instanceof Error ? err.message : '无法读取仓库列表')
    }
  }, [apiCall])

  // Load tasks, task detail, and deployments for the active (repoId, worktreeId) context with race protection
  const loadContextData = useCallback(async (repo: SupervisorRepo, worktree: SupervisorWorktree, isPoll = false) => {
    if (!token) return
    if (isPoll && pollInFlightRef.current) return
    const targetKey = `${repo.id}:${worktree.id}`
    const gen = isPoll ? contextGenRef.current : ++contextGenRef.current
    if (!isPoll) {
      contextAbortRef.current?.abort()
    }
    const controller = new AbortController()
    if (!isPoll) contextAbortRef.current = controller
    if (isPoll) pollInFlightRef.current = true

    try {
      const query = `repo_id=${encodeURIComponent(repo.id)}&worktree_id=${encodeURIComponent(worktree.id)}`
      const taskRes = await apiCall<{ tasks: SupervisorTask[]; count?: number }>(
        `/api/supervisor/tasks?${query}`,
        { signal: controller.signal },
      )
      if (controller.signal.aborted || gen !== contextGenRef.current || activeContextKeyRef.current !== targetKey) return

      const rawTasks = Array.isArray(taskRes.tasks) ? taskRes.tasks : []
      // Ensure client-side context isolation even if a backend ignores query filters
      const scopedTasks = rawTasks.filter(t => isTaskInWorktreeContext(t, repo, worktree))

      const defaultTaskId =
        (worktree.active_task_id && scopedTasks.some(t => t.id === worktree.active_task_id) ? worktree.active_task_id : '')
        || scopedTasks[0]?.id
        || ''

      let targetTaskId = defaultTaskId
      setDataByContext(prev => {
        const existing = prev[targetKey]
        const keepUserSelection = Boolean(
          existing?.userSelected
          && existing.selectedTaskId
          && scopedTasks.some(t => t.id === existing.selectedTaskId),
        )
        const preferredTaskId = keepUserSelection ? existing!.selectedTaskId : defaultTaskId
        targetTaskId = preferredTaskId
        return {
          ...prev,
          [targetKey]: {
            tasks: scopedTasks,
            selectedTaskId: preferredTaskId,
            userSelected: keepUserSelection,
            taskDetail: existing?.taskDetail?.id === preferredTaskId ? existing.taskDetail : null,
            deployments: existing?.deployments || [],
            loaded: true,
            error: '',
          },
        }
      })

      const [detailRes, deployRes] = await Promise.allSettled([
        targetTaskId
          ? apiCall<SupervisorTaskDetail>(`/api/supervisor/tasks/${encodeURIComponent(targetTaskId)}`, { signal: controller.signal })
          : Promise.resolve(null),
        apiCall<{ deployments: SupervisorDeployment[] }>(
          `/api/supervisor/deployments${targetTaskId ? `?task_id=${encodeURIComponent(targetTaskId)}` : ''}`,
          { signal: controller.signal },
        ),
      ])

      if (controller.signal.aborted || gen !== contextGenRef.current || activeContextKeyRef.current !== targetKey) return

      setDataByContext(prev => {
        const current = prev[targetKey]
        if (!current) return prev
        return {
          ...prev,
          [targetKey]: {
            ...current,
            taskDetail: detailRes.status === 'fulfilled' && detailRes.value ? detailRes.value : current.taskDetail,
            deployments: deployRes.status === 'fulfilled' && Array.isArray(deployRes.value.deployments)
              ? deployRes.value.deployments
              : current.deployments,
          },
        }
      })
    } catch (err) {
      if (controller.signal.aborted || gen !== contextGenRef.current || activeContextKeyRef.current !== targetKey) return
      const msg = err instanceof Error ? err.message : '读取工作目录任务失败'
      setDataByContext(prev => ({
        ...prev,
        [targetKey]: {
          tasks: prev[targetKey]?.tasks || [],
          selectedTaskId: prev[targetKey]?.selectedTaskId || '',
          taskDetail: prev[targetKey]?.taskDetail || null,
          deployments: prev[targetKey]?.deployments || [],
          loaded: true,
          error: msg,
        },
      }))
    } finally {
      if (isPoll) pollInFlightRef.current = false
    }
  }, [apiCall, token])

  useEffect(() => {
    if (!active) return
    void loadBootstrap()
  }, [active, loadBootstrap])

  useEffect(() => {
    if (!active || !selectedRepo || !selectedWorktree) return
    const key = `${selectedRepo.id}:${selectedWorktree.id}`
    setPendingByContext(prev => (key in prev ? prev : { ...prev, [key]: loadPendingDispatch(key) }))
    void loadContextData(selectedRepo, selectedWorktree, false)
    return () => {
      contextAbortRef.current?.abort()
    }
  }, [active, loadContextData, selectedRepo, selectedWorktree])

  // Clean background polling while Code workspace is visible
  useEffect(() => {
    if (!active || !selectedRepo || !selectedWorktree) return
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      void loadContextData(selectedRepo, selectedWorktree, true)
    }, 4_000)
    return () => window.clearInterval(timer)
  }, [active, loadContextData, selectedRepo, selectedWorktree])

  const selectWorktreeContext = useCallback((repoId: string, worktreeId: string) => {
    contextGenRef.current += 1
    contextAbortRef.current?.abort()
    setDispatchError('')
    setDeployError('')
    setRetryPrepNote('')
    setMergeOpen(false)
    setHasActiveGoalInWorktree(false)
    setGoalDraft(null)
    subTabTouchedRef.current = false
    setSubTab('workspace')
    setSelectedRepoId(repoId)
    setSelectedWorktreeId(worktreeId)
    setExpandedRepos(prev => ({ ...prev, [repoId]: true }))
    saveContext(repoId, worktreeId)
    setMobileRepoDrawerOpen(false)
  }, [])

  const toggleRepo = useCallback((repoId: string) => {
    setExpandedRepos(prev => {
      const current = prev[repoId] ?? (repoId === selectedRepoId || repositories.length <= 3)
      return { ...prev, [repoId]: !current }
    })
    if (repoId !== selectedRepoId) {
      const repo = repositories.find(r => r.id === repoId)
      const firstWt = repo?.worktrees?.[0]
      if (repo && firstWt) {
        selectWorktreeContext(repo.id, firstWt.id)
      }
    }
  }, [repositories, selectWorktreeContext, selectedRepoId])

  const handleRegisterRepo = useCallback(async (path: string, name?: string): Promise<boolean> => {
    setRegisterBusy(true)
    setRegisterError('')
    try {
      const created = await apiCall<SupervisorRepo>('/api/supervisor/repositories', {
        method: 'POST',
        body: JSON.stringify({ path, ...(name ? { name } : {}) }),
      })
      setRepositories(prev => {
        const exists = prev.some(r => r.id === created.id)
        return exists ? prev.map(r => (r.id === created.id ? created : r)) : [...prev, created]
      })
      const firstWt = (created.worktrees || []).find(w => w.managed && !w.is_main) || created.worktrees?.[0]
      selectWorktreeContext(created.id, firstWt?.id || '')
      return true
    } catch (err) {
      setRegisterError(err instanceof Error ? err.message : '注册仓库失败')
      return false
    } finally {
      setRegisterBusy(false)
    }
  }, [apiCall, selectWorktreeContext])

  const handleCreateProject = useCallback(async (name: string): Promise<boolean> => {
    setCreateProjectBusy(true)
    setCreateProjectError('')
    try {
      const created = await apiCall<{ repository: SupervisorRepo; worktree: SupervisorWorktree }>(
        '/api/supervisor/projects',
        {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          body: JSON.stringify({ name }),
        },
      )
      const repo = created.repository
      const wt = created.worktree
      setRepositories(prev => {
        const exists = prev.some(r => r.id === repo.id)
        const merged = exists ? prev.map(r => (r.id === repo.id ? repo : r)) : [...prev, repo]
        if (!wt) return merged
        return merged.map(item => {
          if (item.id !== repo.id) return item
          const wts = item.worktrees || []
          if (wts.some(row => row.id === wt.id)) return item
          return { ...item, worktrees: [...wts, wt] }
        })
      })
      if (wt && wt.managed && !wt.is_main) {
        selectWorktreeContext(repo.id, wt.id)
        window.setTimeout(() => goalInputRef.current?.focus(), 50)
      }
      return true
    } catch (err) {
      setCreateProjectError(err instanceof Error ? err.message : '新建项目失败')
      return false
    } finally {
      setCreateProjectBusy(false)
    }
  }, [apiCall, selectWorktreeContext])

  const handleCreateWorktree = useCallback(async (repoId: string, branch?: string, baseRef?: string): Promise<boolean> => {
    setCreateWorktreeBusyRepoId(repoId)
    setCreateWorktreeError('')
    try {
      const createdWt = await apiCall<SupervisorWorktree>(
        `/api/supervisor/repositories/${encodeURIComponent(repoId)}/worktrees`,
        {
          method: 'POST',
          body: JSON.stringify({
            ...(branch ? { branch } : {}),
            ...(baseRef ? { base_ref: baseRef } : {}),
          }),
        },
      )
      setRepositories(prev =>
        prev.map(repo => {
          if (repo.id !== repoId) return repo
          const wts = repo.worktrees || []
          const exists = wts.some(w => w.id === createdWt.id)
          return {
            ...repo,
            worktrees: exists ? wts.map(w => (w.id === createdWt.id ? createdWt : w)) : [...wts, createdWt],
          }
        }),
      )
      selectWorktreeContext(repoId, createdWt.id)
      return true
    } catch (err) {
      setCreateWorktreeError(err instanceof Error ? err.message : '新建工作目录失败')
      return false
    } finally {
      setCreateWorktreeBusyRepoId('')
    }
  }, [apiCall, selectWorktreeContext])

  const currentDraft: WorktreeDraft = useMemo(() => {
    if (!contextKey) return { goal: '', acceptanceCriteria: '' }
    return draftsByContext[contextKey] || { goal: '', acceptanceCriteria: '' }
  }, [contextKey, draftsByContext])

  // N25: restore the dispatch draft (goal + 验收标准) for this worktree once, when the owner
  // lands on it, so a reload does not lose what was typed. In-memory entries always win.
  useEffect(() => {
    if (!contextKey || draftsByContext[contextKey]) return
    const stored = readDispatchDraft(contextKey)
    if (!stored.goal && !stored.criteria) return
    setDraftsByContext(prev => (prev[contextKey]
      ? prev
      : { ...prev, [contextKey]: { goal: stored.goal, acceptanceCriteria: stored.criteria } }))
  }, [contextKey, draftsByContext])

  // N25: every change is saved as a draft (an empty one is dropped), so a successful
  // 开始执行 — which clears the draft — clears the stored copy too.
  useEffect(() => {
    if (!contextKey || !draftsByContext[contextKey]) return
    writeDispatchDraft(contextKey, { goal: currentDraft.goal, criteria: currentDraft.acceptanceCriteria })
  }, [contextKey, currentDraft, draftsByContext])

  const updateCurrentDraft = useCallback((patch: Partial<WorktreeDraft>) => {
    if (!contextKey) return
    setDraftsByContext(prev => ({
      ...prev,
      [contextKey]: {
        ...(prev[contextKey] || { goal: '', acceptanceCriteria: '' }),
        ...patch,
      },
    }))
  }, [contextKey])

  // Effective roles for the current worktree (combining capability defaults + per-worktree overrides, NEVER silently falling back to a different provider)
  const effectiveRoles: RoleOverrides = useMemo(() => {
    const overrides = currentDraft.roleOverrides || {}
    const buildRole = (key: SupervisorRoleKey): RoleOverrideEntry => {
      const override = overrides[key]
      const capRole = capabilities?.roles?.[key]
      return {
        executor: override?.executor !== undefined ? override.executor : (capRole?.executor || ''),
        model: override?.model !== undefined ? override.model : (capRole?.model || ''),
      }
    }
    return {
      analyst: buildRole('analyst'),
      implementer: buildRole('implementer'),
      reviewer: buildRole('reviewer'),
    }
  }, [capabilities?.roles, currentDraft.roleOverrides])

  const handleRoleChange = useCallback((role: SupervisorRoleKey, patch: Partial<RoleOverrideEntry>) => {
    if (!contextKey) return
    setDraftsByContext(prev => {
      const cur = prev[contextKey] || { goal: '', acceptanceCriteria: '' }
      const prevRole = cur.roleOverrides?.[role] || effectiveRoles[role]
      return {
        ...prev,
        [contextKey]: {
          ...cur,
          roleOverrides: {
            ...(cur.roleOverrides || {}),
            [role]: { ...prevRole, ...patch },
          },
        },
      }
    })
  }, [contextKey, effectiveRoles])

  const currentSnapshot: WorktreeDataSnapshot = useMemo(() => {
    if (!contextKey) {
      return { tasks: [], selectedTaskId: '', taskDetail: null, deployments: [], loaded: false, error: '' }
    }
    return dataByContext[contextKey] || {
      tasks: [],
      selectedTaskId: '',
      taskDetail: null,
      deployments: [],
      loaded: false,
      error: '',
    }
  }, [contextKey, dataByContext])

  const selectedTask = useMemo(() => {
    const tasks = currentSnapshot.tasks
    return tasks.find(t => t.id === currentSnapshot.selectedTaskId) || tasks[0] || null
  }, [currentSnapshot.selectedTaskId, currentSnapshot.tasks])

  const selectedTaskDetail = useMemo(() => {
    if (!selectedTask) return null
    return currentSnapshot.taskDetail?.id === selectedTask.id ? currentSnapshot.taskDetail : null
  }, [currentSnapshot.taskDetail, selectedTask])

  // 「改动」：只有这一轮已经结束（完成 / 失败 / 在等你）时才值得看改了什么，
  // 选中任务时才去读差异（懒加载），读完按任务 id 缓存。
  const changesReady = Boolean(selectedTask) && DIFF_VISIBLE_STATES.includes((selectedTask?.state || '').trim().toLowerCase())
  const taskDiff = useSupervisorDiff(apiCall, 'task', selectedTask?.id || '', changesReady)
  const diffFileCount = taskDiff.status === 'ready'
    ? (taskDiff.data.files?.length || 0) + (taskDiff.data.untracked?.length || 0)
    : 0

  // 任务卡片上那一行短总结：状态 + 修了几轮 + 验收 + 改了几个文件（原始摘要进「技术细节」）。
  const taskSummary = useMemo(() => {
    if (!selectedTask) return null
    const acceptance = selectedTaskDetail?.acceptance
    const verdict = acceptance?.passed === true
      ? '验收通过'
      : acceptance?.passed === false
        ? '验收未通过'
        : '验收未验证'
    const parts = [supStateLabel(selectedTask.state)]
    if (typeof selectedTask.repair_round === 'number') {
      parts.push(selectedTask.repair_round > 0 ? `自动修了 ${selectedTask.repair_round} 轮` : '没有自动修复')
    }
    parts.push(verdict)
    parts.push(diffFileCount > 0 ? `改了 ${diffFileCount} 个文件` : '没有改动文件')
    const note = taskResultNote(selectedTaskDetail, selectedTask)
    return { line: parts.join(' · '), note }
  }, [diffFileCount, selectedTask, selectedTaskDetail])

  const handleSelectTask = useCallback(async (taskId: string) => {
    if (!contextKey || !selectedRepo || !selectedWorktree) return
    const targetKey = contextKey
    setDataByContext(prev => ({
      ...prev,
      [targetKey]: {
        ...(prev[targetKey] || { tasks: [], deployments: [], loaded: true, error: '', taskDetail: null }),
        selectedTaskId: taskId,
        userSelected: true,
      },
    }))
    try {
      const [detail, deployRes] = await Promise.all([
        apiCall<SupervisorTaskDetail>(`/api/supervisor/tasks/${encodeURIComponent(taskId)}`),
        apiCall<{ deployments: SupervisorDeployment[] }>(`/api/supervisor/deployments?task_id=${encodeURIComponent(taskId)}`).catch(() => ({ deployments: [] })),
      ])
      if (activeContextKeyRef.current !== targetKey) return
      setDataByContext(prev => {
        const cur = prev[targetKey]
        if (!cur) return prev
        return {
          ...prev,
          [targetKey]: {
            ...cur,
            taskDetail: detail,
            deployments: Array.isArray(deployRes.deployments) ? deployRes.deployments : cur.deployments,
          },
        }
      })
    } catch {
      // Keep summary row selected even if detail fetch fails
    }
  }, [apiCall, contextKey, selectedRepo, selectedWorktree])

  // Check if any selected role is marked unavailable
  const unavailableRoleReasons = useMemo(() => {
    if (!capabilities) return []
    const reasons: string[] = []
    for (const { key, label } of ROLE_META) {
      const chosen = effectiveRoles[key].executor
      const execInfo = (capabilities.executors || []).find(e => e.name === chosen)
      const capRole = capabilities.roles?.[key]
      const available = execInfo ? execInfo.available : (capRole?.executor === chosen ? capRole.available : true)
      const reason = execInfo?.reason || (capRole?.executor === chosen ? capRole.reason : undefined)
      if (!available) {
        reasons.push(`${label}（${chosen || '未指定'}）不可用${reason ? `：${reason}` : ''}`)
      }
    }
    return reasons
  }, [capabilities, effectiveRoles])

  const isReadOnlyWorktree = Boolean(selectedWorktree && (selectedWorktree.is_main || !selectedWorktree.managed))
  const hasRunningTaskInWorktree = useMemo(
    () =>
      currentSnapshot.tasks.some(t => {
        const st = (t.state || '').trim().toLowerCase()
        return !isTerminalTaskState(st) && st !== 'created' && st !== 'pending_dispatch'
      }),
    [currentSnapshot.tasks],
  )
  const isLockedWorktree = Boolean(selectedWorktree && (selectedWorktree.locked || hasRunningTaskInWorktree))
  // 「合并到主分支」入口：只给受托管的工作目录（主分支 / 未托管目录不能作为合并来源），
  // 这一轮已经跑完、而且没有任务或目标在跑的时候才出现。
  const latestTask = currentSnapshot.tasks[0] || null
  const latestTaskDone = Boolean(latestTask)
    && MERGE_DONE_STATES.includes((latestTask?.state || '').trim().toLowerCase())
  const mergeAvailable = Boolean(selectedWorktree)
    && !isReadOnlyWorktree
    && !isLockedWorktree
    && !hasActiveGoalInWorktree
    && latestTaskDone

  const currentPending = contextKey ? (pendingByContext[contextKey] || null) : null

  const handleCreateAndDispatch = useCallback(async (event: FormEvent) => {
    event.preventDefault()
    if (!selectedRepo || !selectedWorktree || !contextKey || dispatchBusy) return
    const goal = currentDraft.goal.trim()
    if (!goal || isReadOnlyWorktree || isLockedWorktree || unavailableRoleReasons.length > 0) return

    const criteria = currentDraft.acceptanceCriteria.trim()
    const sessionId = currentDraft.preparedSessionId || selectedWorktree.chat_session_id || undefined
    const rolesPayload: Record<SupervisorRoleKey, { executor: string; model?: string }> = {
      analyst: {
        executor: effectiveRoles.analyst.executor,
        ...(effectiveRoles.analyst.model.trim() ? { model: effectiveRoles.analyst.model.trim() } : {}),
      },
      implementer: {
        executor: effectiveRoles.implementer.executor,
        ...(effectiveRoles.implementer.model.trim() ? { model: effectiveRoles.implementer.model.trim() } : {}),
      },
      reviewer: {
        executor: effectiveRoles.reviewer.executor,
        ...(effectiveRoles.reviewer.model.trim() ? { model: effectiveRoles.reviewer.model.trim() } : {}),
      },
    }

    const fp = computeFingerprint({
      repoId: selectedRepo.id,
      worktreeId: selectedWorktree.id,
      goal,
      criteria,
      roles: rolesPayload,
    })

    const existingPending = pendingByContext[contextKey] || loadPendingDispatch(contextKey)
    let pending: PendingDispatch
    if (existingPending && existingPending.fingerprint === fp) {
      pending = { ...existingPending }
    } else {
      pending = { fingerprint: fp, requestId: generateRequestId() }
    }
    savePendingDispatch(contextKey, pending)
    setPendingByContext(prev => ({ ...prev, [contextKey]: pending }))

    setDispatchBusy(true)
    setDispatchError('')
    try {
      let taskId = pending.taskId
      if (!taskId) {
        const created = await apiCall<SupervisorTask>('/api/supervisor/tasks', {
          method: 'POST',
          body: JSON.stringify({
            goal,
            repo_id: selectedRepo.id,
            worktree_id: selectedWorktree.id,
            ...(criteria ? { acceptance_criteria: criteria } : {}),
            ...(sessionId ? { session_id: sessionId } : {}),
            request_id: pending.requestId,
          }),
        })
        taskId = created.id
        pending = { ...pending, taskId }
        savePendingDispatch(contextKey, pending)
        setPendingByContext(prev => ({ ...prev, [contextKey]: pending }))
      }

      const dispatched = await apiCall<SupervisorTask>(
        `/api/supervisor/tasks/${encodeURIComponent(taskId)}/dispatch`,
        {
          method: 'POST',
          body: JSON.stringify({
            ...(criteria ? { acceptance_criteria: criteria } : {}),
            roles: rolesPayload,
          }),
        },
      )

      // Clear pending record & goal draft on successful dispatch
      savePendingDispatch(contextKey, null)
      setPendingByContext(prev => ({ ...prev, [contextKey]: null }))
      updateCurrentDraft({ goal: '', acceptanceCriteria: '', fromSuggestion: false, preparedSessionId: undefined, handoffMode: undefined })
      setRetryPrepNote('')

      setDataByContext(prev => {
        const cur = prev[contextKey]
        const prevTasks = cur?.tasks || []
        const updatedTasks = [dispatched, ...prevTasks.filter(t => t.id !== dispatched.id)]
        return {
          ...prev,
          [contextKey]: {
            tasks: updatedTasks,
            selectedTaskId: dispatched.id,
            taskDetail: dispatched,
            deployments: cur?.deployments || [],
            loaded: true,
            error: '',
          },
        }
      })

      void loadContextData(selectedRepo, selectedWorktree, false)
    } catch (err) {
      setDispatchError(err instanceof Error ? err.message : '任务派发失败，请点击重试')
    } finally {
      setDispatchBusy(false)
    }
  }, [
    apiCall,
    contextKey,
    currentDraft.acceptanceCriteria,
    currentDraft.goal,
    currentDraft.preparedSessionId,
    dispatchBusy,
    effectiveRoles,
    isLockedWorktree,
    isReadOnlyWorktree,
    loadContextData,
    pendingByContext,
    selectedRepo,
    selectedWorktree,
    unavailableRoleReasons.length,
    updateCurrentDraft,
  ])

  const handleCancelTask = useCallback(async (task: SupervisorTask) => {
    if (!contextKey || cancelBusyTaskId) return
    setCancelBusyTaskId(task.id)
    setDispatchError('')
    try {
      // Show backend-returned actual state (e.g. cancelling or cancelled), NEVER optimistic done
      const updated = await apiCall<SupervisorTask>(
        `/api/supervisor/tasks/${encodeURIComponent(task.id)}/cancel`,
        {
          method: 'POST',
          body: JSON.stringify({ reason: '用户手动请求取消任务' }),
        },
      )
      setDataByContext(prev => {
        const cur = prev[contextKey]
        if (!cur) return prev
        return {
          ...prev,
          [contextKey]: {
            ...cur,
            tasks: cur.tasks.map(t => (t.id === updated.id ? { ...t, ...updated } : t)),
            taskDetail: cur.taskDetail?.id === updated.id ? { ...cur.taskDetail, ...updated } : cur.taskDetail,
          },
        }
      })
    } catch (err) {
      setDispatchError(err instanceof Error ? err.message : '取消任务请求失败')
    } finally {
      setCancelBusyTaskId('')
    }
  }, [apiCall, cancelBusyTaskId, contextKey])

  const handleStartDeploy = useCallback(async () => {
    if (!contextKey || !selectedTask || deployBusy) return
    if (!capabilities?.deployment?.configured || !capabilities?.deployment?.can_start) return
    setDeployBusy(true)
    setDeployError('')
    try {
      const created = await apiCall<SupervisorDeployment>('/api/supervisor/deployments', {
        method: 'POST',
        body: JSON.stringify({ task_id: selectedTask.id }),
      })
      setDataByContext(prev => {
        const cur = prev[contextKey]
        if (!cur) return prev
        return {
          ...prev,
          [contextKey]: {
            ...cur,
            deployments: [created, ...cur.deployments.filter(d => d.id !== created.id)],
          },
        }
      })
    } catch (err) {
      setDeployError(err instanceof Error ? err.message : '启动部署失败')
    } finally {
      setDeployBusy(false)
    }
  }, [apiCall, capabilities?.deployment?.can_start, capabilities?.deployment?.configured, contextKey, deployBusy, selectedTask])

  const handleSubTabChange = useCallback((tab: 'goals' | 'workspace' | 'chat' | 'deploy') => {
    subTabTouchedRef.current = true
    setSubTab(tab)
  }, [])

  // 目标 tab: an active (running / waiting) goal becomes the default sub-tab for this worktree.
  const handleActiveGoalChange = useCallback((hasActiveGoal: boolean) => {
    setHasActiveGoalInWorktree(hasActiveGoal)
    if (!hasActiveGoal || subTabTouchedRef.current) return
    setSubTab('goals')
  }, [])

  // 目标还在跑、但「目标」页没打开（它自己会轮询）：每 15 秒重新问一次有没有进行中的目标。
  useEffect(() => {
    if (!active || !hasActiveGoalInWorktree || subTab === 'goals') return
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      setGoalProbeTick(tick => tick + 1)
    }, 15_000)
    return () => window.clearInterval(timer)
  }, [active, hasActiveGoalInWorktree, subTab])

  // 讨论里的「设为目标」：切到「目标」页并把草稿填进「描述你想要的结果」，绝不自动开始。
  const handleMakeGoal = useCallback((draft: string) => {
    if (hasActiveGoalInWorktree || !draft.trim()) return
    setGoalDraft(draft)
    subTabTouchedRef.current = true
    setSubTab('goals')
    window.setTimeout(() => {
      const field = document.getElementById('sup-goal-outcome')
      if (field instanceof HTMLTextAreaElement && !field.disabled) {
        field.focus()
        field.setSelectionRange(field.value.length, field.value.length)
      }
    }, 50)
  }, [hasActiveGoalInWorktree])

  const handleGoalDraftUsed = useCallback(() => {
    setGoalDraft(null)
  }, [])

  // 「查看这一轮的任务」: jump to 任务与结果 with that task selected.
  const handleOpenGoalTask = useCallback((taskId: string) => {
    subTabTouchedRef.current = true
    setSubTab('workspace')
    void handleSelectTask(taskId)
    // 等这一页连任务卡片一起画出来，再把卡片滚到任务区顶部（见下面的 effect）。
    pendingTaskScrollRef.current = true
  }, [handleSelectTask])

  // 切页 + 选中都渲染完之后，把这一轮的任务卡片滚到任务区顶部：老板一落地看到的就是这一轮，
  // 而不是上面的派发表单。放在 effect 里是因为点按钮的那一刻任务卡片可能还没画出来。
  useEffect(() => {
    if (!pendingTaskScrollRef.current) return
    if (subTab !== 'workspace' || !selectedTask) return
    pendingTaskScrollRef.current = false
    const frame = window.requestAnimationFrame(() => {
      taskCardRef.current?.scrollIntoView({ block: 'start' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [subTab, selectedTask])

  // Task suggestion from ChatPanel fills reviewable goal without auto-dispatching
  const handlePrepareTask = useCallback((suggestion: TaskSuggestion) => {
    const applied = applyTaskSuggestion(suggestion)
    updateCurrentDraft({
      goal: applied.goal,
      acceptanceCriteria: applied.acceptanceCriteria,
      preparedSessionId: applied.preparedSessionId,
      fromSuggestion: applied.fromSuggestion,
      handoffMode: applied.handoffMode,
    })
    setRetryPrepNote('')
    if (narrowViewport) {
      subTabTouchedRef.current = true
      setSubTab('workspace')
    }
    window.setTimeout(() => {
      goalInputRef.current?.focus()
    }, 50)
  }, [narrowViewport, updateCurrentDraft])

  const handleRetryPreparation = useCallback(() => {
    if (!selectedTask) return
    const criteria = selectedTask.metadata?.acceptance_criteria
    const result = prepareRetryDraft({
      goal: selectedTask.goal,
      acceptanceCriteria: typeof criteria === 'string' ? criteria : '',
      locked: isLockedWorktree,
    })
    if (!result.ok) {
      setRetryPrepNote(result.reason === 'locked' ? RETRY_LOCKED_NOTE : RETRY_MISSING_NOTE)
      return
    }
    updateCurrentDraft({
      goal: result.goal,
      acceptanceCriteria: result.acceptanceCriteria,
      fromSuggestion: false,
      handoffMode: 'retry',
    })
    setRetryPrepNote(RETRY_READY_NOTE)
    window.setTimeout(() => {
      goalInputRef.current?.focus()
    }, 50)
  }, [isLockedWorktree, selectedTask, updateCurrentDraft])

  // 讨论 uses the coordinator chat model. The owner sees one plain Chinese line; the raw
  // executor code and model id stay in the 技术详情 disclosure (QA N29).
  const coordinatorPlain = useMemo(() => supCoordinatorLabel(capabilities?.coordinator?.executor), [capabilities?.coordinator])
  const coordinatorTech = useMemo(() => {
    const executor = capabilities?.coordinator?.executor || ''
    const model = capabilities?.coordinator?.model || ''
    return [executor, model].filter(Boolean).join(' · ') || coordinatorFallbackName || ''
  }, [capabilities?.coordinator, coordinatorFallbackName])

  const activeWorktreeBadge = selectedWorktree ? worktreeBadgeInfo(selectedWorktree) : null

  return (
    <div className="sup-workspace">
      <aside
        className={`sup-repo-sidebar ${mobileRepoDrawerOpen ? 'is-drawer-open' : ''}`}
        aria-label="仓库与工作目录"
      >
        <SupervisorRepoSidebar
          repositories={repositories}
          selectedRepoId={selectedRepoId}
          selectedWorktreeId={selectedWorktreeId}
          expandedRepos={expandedRepos}
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
          onToggleRepo={toggleRepo}
          onSelectWorktree={selectWorktreeContext}
          onRegisterRepo={handleRegisterRepo}
          onCreateProject={handleCreateProject}
          onCreateWorktree={handleCreateWorktree}
          registerBusy={registerBusy}
          registerError={registerError}
          createProjectBusy={createProjectBusy}
          createProjectError={createProjectError}
          createWorktreeBusyRepoId={createWorktreeBusyRepoId}
          createWorktreeError={createWorktreeError}
          openCreateForRepoId={openCreateForRepoId}
          onOpenCreateWorktree={setOpenCreateForRepoId}
          mobileCloseButton={
            narrowViewport ? (
              <button
                type="button"
                className="drawer-close-btn"
                aria-label="关闭仓库列表"
                onClick={() => setMobileRepoDrawerOpen(false)}
              >
                ×
              </button>
            ) : undefined
          }
        />
      </aside>

      {narrowViewport && mobileRepoDrawerOpen && (
        <div
          className="sup-mobile-backdrop"
          role="button"
          tabIndex={-1}
          aria-label="关闭仓库抽屉"
          onClick={() => setMobileRepoDrawerOpen(false)}
        />
      )}

      <div className="sup-main-column">
        {projectIntroOpen && (
          <section className="sup-intro" role="region" aria-label="项目是做什么的">
            <button type="button" className="sup-intro-close" aria-label="关闭说明" onClick={dismissProjectIntro}>×</button>
            <p>
              「项目」把你电脑上的 Git <strong>仓库</strong>（就是项目文件夹）登记进来，再为它建出一个或多个<strong>工作目录</strong>。
              每个工作目录是项目的一份独立副本，改动不会碰到主分支，确认后再合并。
            </p>
            <p>
              也可以只写一个名称，点「新建项目」。Conveyor 会在服务器托管的位置创建一个空的 Git 仓库，并准备一份可写的独立工作目录。不用填写服务器上的文件夹路径。
            </p>
            <p>
              要真的动手改代码，还需要一个<strong>可用的执行者</strong>（已经装好并登录）。
              {onOpenWorkers && <> <button type="button" className="sup-intro-link" onClick={onOpenWorkers}>去执行者页</button></>}
            </p>
          </section>
        )}
        <header className="sup-context-bar">
          <div className="sup-context-title-group">
            {narrowViewport && (
              <button
                type="button"
                className="shell-header-button sup-mobile-repo-btn"
                aria-label="选择仓库与工作目录"
                onClick={() => setMobileRepoDrawerOpen(true)}
              >
                <span>仓库</span>
              </button>
            )}
            <div className="sup-context-heading">
              <h3>
                {selectedRepo && selectedWorktree
                  ? `${selectedRepo.name} / ${selectedWorktree.branch || 'detached'}`
                  : selectedRepo
                    ? selectedRepo.name
                    : '请选择或注册 Git 仓库'}
              </h3>
              <div className="sup-context-sub">
                {selectedWorktree && activeWorktreeBadge && (
                  <span className={`sup-wt-badge sup-wt-badge--${activeWorktreeBadge.tone}`}>
                    {activeWorktreeBadge.label}
                  </span>
                )}
                {selectedWorktree?.head && <code>{shortSha(selectedWorktree.head)}</code>}
                {(selectedRepo?.source_machine || capabilities?.source_machine) && (
                  <span>主机：{selectedRepo?.source_machine || capabilities?.source_machine}</span>
                )}
              </div>
            </div>
          </div>

          <div className="sup-subnav" role="tablist" aria-label="项目视图">
            <button
              type="button"
              role="tab"
              aria-selected={subTab === 'goals'}
              className={`sup-subnav-tab ${subTab === 'goals' ? 'active' : ''}`}
              onClick={() => handleSubTabChange('goals')}
            >
              目标
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={subTab === 'workspace'}
              className={`sup-subnav-tab ${subTab === 'workspace' ? 'active' : ''}`}
              onClick={() => handleSubTabChange('workspace')}
            >
              任务与结果
            </button>
            {narrowViewport && (
              <button
                type="button"
                role="tab"
                aria-selected={subTab === 'chat'}
                className={`sup-subnav-tab ${subTab === 'chat' ? 'active' : ''}`}
                onClick={() => handleSubTabChange('chat')}
              >
                讨论
              </button>
            )}
            <button
              type="button"
              role="tab"
              aria-selected={subTab === 'deploy'}
              className={`sup-subnav-tab ${subTab === 'deploy' ? 'active' : ''}`}
              onClick={() => handleSubTabChange('deploy')}
            >
              部署
            </button>
          </div>
        </header>

        {(capabilitiesError || reposError) && (
          <div className="sup-banner sup-banner--warning" role="status">
            <strong>部分项目接口暂未就绪</strong>
            <span>{[capabilitiesError, reposError].filter(Boolean).join(' · ')}</span>
          </div>
        )}

        {selectedRepo && selectedWorktree && (
          <SupervisorGoalsProbe
            key={`probe:${contextKey}:${goalProbeTick}`}
            apiCall={apiCall}
            repo={selectedRepo}
            worktree={selectedWorktree}
            onActiveGoalChange={handleActiveGoalChange}
          />
        )}

        {!selectedRepo || !selectedWorktree ? (
          <div className="sup-empty-workspace">
            <div className="brand-mark">C</div>
            <h3>{initialLoading ? '正在加载仓库与工作目录…' : '选择或创建一个工作目录'}</h3>
            <p>
              {narrowViewport
                ? '打开「仓库」列表，选择一个工作目录，或先点「新建项目」。之后可以在「讨论」里梳理方案，在「任务与结果」里派发任务并查看结果。'
                : '在左侧选择仓库并进入一个工作目录，或先点「新建项目」。之后就可以在这里讨论方案、派发任务，并查看结果与部署状态。'}
            </p>
          </div>
        ) : subTab === 'deploy' ? (
          <div className="sup-deploy-scroll">
            <SupervisorDeployView
              repo={selectedRepo}
              worktree={selectedWorktree}
              selectedTask={selectedTask}
              capabilities={capabilities}
              deployments={currentSnapshot.deployments}
              deployBusy={deployBusy}
              deployError={deployError}
              onStartDeploy={() => void handleStartDeploy()}
            />
          </div>
        ) : subTab === 'goals' ? (
          <SupervisorGoals
            key={contextKey}
            apiCall={apiCall}
            repo={selectedRepo}
            worktree={selectedWorktree}
            visible={active && subTab === 'goals'}
            readOnly={isReadOnlyWorktree}
            locked={isLockedWorktree}
            capabilities={capabilities}
            effectiveRoles={effectiveRoles}
            onChangeRole={handleRoleChange}
            onOpenTask={handleOpenGoalTask}
            onActiveGoalChange={handleActiveGoalChange}
            onRefreshRepositories={() => void refreshRepositories()}
            draftOutcome={goalDraft}
            onDraftOutcomeUsed={handleGoalDraftUsed}
          />
        ) : (
          <div className="sup-workbench-split">
            <div
              className="sup-task-pane"
              style={narrowViewport && subTab === 'chat' ? { display: 'none' } : undefined}
            >
              {isReadOnlyWorktree && (
                <div className="sup-banner sup-banner--readonly" role="status">
                  <div className="sup-banner-text">
                    <strong>当前为只读工作目录（{selectedWorktree.is_main ? '主分支' : '未托管目录'}）</strong>
                    <span>主工作目录与未托管目录仅支持只读查看。请新建工作目录，在独立副本里派发任务。</span>
                  </div>
                  <button
                    type="button"
                    className="shell-button primary"
                    onClick={() => {
                      setExpandedRepos(prev => ({ ...prev, [selectedRepo.id]: true }))
                      setOpenCreateForRepoId(selectedRepo.id)
                      if (narrowViewport) setMobileRepoDrawerOpen(true)
                    }}
                  >
                    新建工作目录
                  </button>
                </div>
              )}

              {!isReadOnlyWorktree && isLockedWorktree && (
                <div className="sup-banner sup-banner--locked" role="status">
                  <strong>当前工作目录已锁定（任务执行中）</strong>
                  <span>同一个工作目录同一时间只允许一个任务运行，当前任务结束或取消后可以再派发新任务。</span>
                </div>
              )}

              <SupervisorChecks apiCall={apiCall} repo={selectedRepo} onSaved={() => void refreshRepositories()} />

              <ProjectNotesCard key={selectedRepo.id} token={token} repoId={selectedRepo.id} repoName={selectedRepo.name} />

              <section className="sup-card sup-dispatch-card" aria-label="任务目标与派发">
                <div className="sup-card-head">
                  <h4>当前任务目标</h4>
                  <span className="sup-card-hint">普通对话不会自动派发，确认目标后点击「开始执行」</span>
                </div>

                {currentDraft.fromSuggestion && (
                  <div className="sup-suggestion-notice" role="status">
                    {handoffNotice(currentDraft.handoffMode, narrowViewport)}
                  </div>
                )}

                {/* N25: while a draft exists the owner can see it is kept on this device. */}
                {(currentDraft.goal.trim() || currentDraft.acceptanceCriteria.trim()) && (
                  <p className="sup-draft-hint" role="status">草稿已自动保存，刷新页面也不会丢；点「开始执行」后清除。</p>
                )}

                <form className="composer sup-goal-composer" onSubmit={e => void handleCreateAndDispatch(e)}>
                  <label htmlFor="sup-goal-textarea" className="sr-only">任务目标</label>
                  <textarea
                    id="sup-goal-textarea"
                    ref={goalInputRef}
                    aria-label="任务目标"
                    placeholder={
                      isReadOnlyWorktree
                        ? '只读工作目录不能派发任务，请先新建工作目录…'
                        : '输入要在这个工作目录完成的目标（例如：为项目页补充仓库搜索与错误提示）…'
                    }
                    rows={3}
                    value={currentDraft.goal}
                    disabled={isReadOnlyWorktree || dispatchBusy}
                    onChange={e => updateCurrentDraft({ goal: e.target.value })}
                  />

                  <div className="sup-criteria-row">
                    <label htmlFor="sup-criteria-input">验收标准（可选）</label>
                    <input
                      id="sup-criteria-input"
                      aria-label="验收标准"
                      type="text"
                      placeholder="例如：通过单元测试与构建检查，不改动后端接口"
                      value={currentDraft.acceptanceCriteria}
                      disabled={isReadOnlyWorktree || dispatchBusy}
                      onChange={e => updateCurrentDraft({ acceptanceCriteria: e.target.value })}
                    />
                  </div>

                  <SupervisorRoleBar
                    capabilities={capabilities}
                    effectiveRoles={effectiveRoles}
                    onChangeRole={handleRoleChange}
                    onOpenWorkers={onOpenWorkers}
                    disabled={isReadOnlyWorktree || dispatchBusy}
                  />

                  {unavailableRoleReasons.length > 0 && (
                    <div className="sup-banner sup-banner--error" role="alert">
                      <strong>角色执行器不可用</strong>
                      <span>{unavailableRoleReasons.join('；')}</span>
                    </div>
                  )}

                  {dispatchError && (
                    <div className="sup-banner sup-banner--error" role="alert">
                      <strong>派发未完成</strong>
                      <span>{dispatchError}</span>
                      {currentPending && (
                        <small>已保留请求标识（{currentPending.taskId ? `任务 ${currentPending.taskId}` : `请求 ${currentPending.requestId.slice(0, 8)}`}），重试不会重复创建任务。</small>
                      )}
                    </div>
                  )}

                  <div className="sup-dispatch-footer">
                    <span className="sup-dispatch-context-tag">
                      在这里改：<code>{selectedRepo.name}</code> / <code>{selectedWorktree.branch}</code>
                    </span>
                    <button
                      type="submit"
                      className="send-button primary"
                      disabled={
                        !currentDraft.goal.trim()
                        || dispatchBusy
                        || isReadOnlyWorktree
                        || isLockedWorktree
                        || unavailableRoleReasons.length > 0
                      }
                    >
                      {dispatchBusy
                        ? '正在派发…'
                        : currentPending
                          ? '重试派发'
                          : '开始执行'}
                    </button>
                  </div>
                </form>
              </section>

              {selectedTask && (
                <section ref={taskCardRef} className="sup-card sup-active-task-card" aria-label="当前任务进度与结果">
                  <div className="sup-active-task-header">
                    <div className="sup-active-task-title">
                      <span className={`status-badge ${selectedTask.state}`}>
                        <i />
                        {supStateLabel(selectedTask.state)}
                      </span>
                      <h4>{goalRoundTaskTitle(selectedTask.goal) || selectedTask.goal}</h4>
                    </div>
                    {!isTerminalTaskState(selectedTask.state) && (
                      <button
                        type="button"
                        className="shell-button shell-button--danger"
                        disabled={cancelBusyTaskId === selectedTask.id}
                        onClick={() => void handleCancelTask(selectedTask)}
                      >
                        {cancelBusyTaskId === selectedTask.id ? '正在请求取消…' : '取消任务'}
                      </button>
                    )}
                  </div>

                  <div className="sup-task-progress-strip">
                    <div className="sup-progress-pill">
                      <span>当前阶段</span>
                      <strong>{supStageLabel(selectedTask.stage, selectedTask.state)}</strong>
                    </div>
                    {typeof selectedTask.repair_round === 'number' && typeof selectedTask.max_repair_rounds === 'number' && selectedTask.max_repair_rounds > 0 && (
                      <div className="sup-progress-pill">
                        <span>自动修复轮次</span>
                        <strong>{selectedTask.repair_round} / {selectedTask.max_repair_rounds}</strong>
                      </div>
                    )}
                    <div className="sup-progress-pill">
                      <span>任务编号</span>
                      <code>{selectedTask.id}</code>
                    </div>
                  </div>

                  <SupervisorTaskExecutors executions={selectedTaskDetail?.executions || []} />

                  {selectedTask.error && (
                    <div className="sup-banner sup-banner--error" role="alert">
                      <strong>受阻原因</strong>
                      <span>{supFriendlyTaskError(selectedTask.error)}</span>
                      <details className="sup-task-raw-error">
                        <summary>技术详情</summary>
                        <pre>{selectedTask.error}</pre>
                      </details>
                    </div>
                  )}

                  {(selectedTask.state === 'failed' || selectedTask.state === 'error') && (
                    <div className="sup-retry-prep">
                      <button
                        type="button"
                        className="shell-button"
                        disabled={isLockedWorktree}
                        onClick={handleRetryPreparation}
                      >
                        重新准备
                      </button>
                      <p className="sup-draft-hint" role="status">
                        {isLockedWorktree ? RETRY_LOCKED_NOTE : (retryPrepNote || '用原来的目标和验收标准重新填入草稿，不会自动开始执行。')}
                      </p>
                    </div>
                  )}

                  {changesReady && taskSummary && (
                    <div className="sup-task-summary">
                      <h5>这一轮的结果</h5>
                      <p>{taskSummary.line}</p>
                      {taskSummary.note && <p className="sup-task-summary-note">{taskSummary.note}</p>}
                    </div>
                  )}

                  {selectedTaskDetail?.acceptance && (
                    <div className="sup-acceptance-box">
                      <h5>
                        验收结果：
                        <strong>
                          {selectedTaskDetail.acceptance.passed === true
                            ? '已通过'
                            : selectedTaskDetail.acceptance.passed === false
                              ? '未通过'
                              : supStateLabel(selectedTaskDetail.acceptance.status)}
                        </strong>
                      </h5>
                      {selectedTaskDetail.acceptance.summary && <p>{selectedTaskDetail.acceptance.summary}</p>}
                    </div>
                  )}

                  {changesReady && (
                    <section className="sup-task-changes" aria-label="改动">
                      <div className="sup-card-head">
                        <h4>改动</h4>
                        <span className="sup-card-hint">改了哪些文件、每一行变成了什么</span>
                      </div>
                      <SupervisorDiffView diffState={taskDiff} />
                    </section>
                  )}

                  <p className="sup-separation-note">
                    状态边界：任务完成 ≠ PR 已合并 ≠ 已部署上线。需要部署时请切换至「部署」标签页操作。
                  </p>
                </section>
              )}

              {mergeAvailable && selectedWorktree && (
                <div className="sup-merge-entry">
                  <button
                    type="button"
                    className="shell-button sup-merge-toggle"
                    aria-expanded={mergeOpen}
                    onClick={() => setMergeOpen(prev => !prev)}
                  >
                    合并到主分支…
                  </button>
                  <span className="sup-merge-entry-hint">这一轮的改动还在这个工作目录里，确认后才合回主分支。</span>
                </div>
              )}
              {mergeOpen && mergeAvailable && selectedWorktree && (
                <SupervisorMerge apiCall={apiCall} worktree={selectedWorktree} />
              )}

              <SupervisorRunHistory
                worktreeId={selectedWorktree.id}
                tasks={currentSnapshot.tasks}
                selectedTaskId={selectedTask?.id || ''}
                onSelectTask={id => void handleSelectTask(id)}
              />

              <SupervisorTechnicalDetails task={selectedTask} taskDetail={selectedTaskDetail} />
            </div>

            <div
              className="sup-chat-pane"
              style={narrowViewport && subTab !== 'chat' ? { display: 'none' } : undefined}
              aria-label="讨论"
            >
              <div className="sup-chat-pane-header">
                <div>
                  <strong>讨论</strong>
                  <small>用来聊方案的模型：{coordinatorPlain}</small>
                  {coordinatorTech && (
                    <details className="sup-chat-tech">
                      <summary>技术详情</summary>
                      <span>{coordinatorTech}</span>
                    </details>
                  )}
                </div>
                <span className="sup-chat-pane-badge">讨论不自动执行</span>
              </div>

              {selectedWorktree.chat_session_id ? (
                <div className="sup-chat-pane-body">
                  <ChatPanel
                    key={selectedWorktree.chat_session_id}
                    token={token}
                    active={active && (!narrowViewport || subTab === 'chat')}
                    agentSessionId={selectedWorktree.chat_session_id}
                    onPrepareTask={handlePrepareTask}
                    onMakeGoal={handleMakeGoal}
                    makeGoalDisabledReason={hasActiveGoalInWorktree ? '这个工作目录已经有进行中的目标' : ''}
                    discussionOnly
                    discussionHandoffHint={narrowViewport ? NARROW_DISCUSSION_HINT : WIDE_DISCUSSION_HINT}
                  />
                </div>
              ) : (
                <div className="sup-chat-unbound" role="status">
                  <strong>当前工作目录尚未绑定对话会话</strong>
                  <p>
                    {narrowViewport
                      ? '后端还没有为这个工作目录分配对话会话。为避免伪造虚构会话，此处不显示模拟聊天；你仍可在「任务与结果」填写「当前任务目标」并点击「开始执行」。'
                      : '后端还没有为这个工作目录分配对话会话。为避免伪造虚构会话，此处不显示模拟聊天；你仍可在左侧直接填写「当前任务目标」并点击「开始执行」。'}
                  </p>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export default SupervisorWorkspace
