import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  RoleOverrideEntry,
  RoleOverrides,
  SupervisorCapabilities,
  SupervisorRepo,
  SupervisorRoleKey,
  SupervisorTaskDetail,
  SupervisorWorktree,
  SupervisorWorkersData,
} from './SupervisorWorkspace'
import SupervisorChecks, { type SupervisorChecksSummary } from './SupervisorChecks'
import { SupervisorRoleBar, supExecutorLabel, supStageLabel, supStateLabel } from './SupervisorWorkspace'
import SupervisorMerge from './SupervisorMerge'
import { SupervisorDiffView, useSupervisorDiff } from './SupervisorDiff'
import { readGoalDraft, writeGoalDraft } from './supDrafts'

// ---------------------------------------------------------------------------
// 目标（goal-driven loop）contract types — mirrors supervisor_goals.py payloads.
// All owner-facing copy is Simplified Chinese; raw backend codes never render.
// ---------------------------------------------------------------------------

export type GoalState = 'running' | 'waiting_user' | 'waiting_quota' | 'achieved' | 'stopped' | 'exhausted' | 'failed'
export type GoalMode = 'outcome' | 'continuous'

export type Goal = {
  id: string
  repo_id: string
  worktree_id: string
  title: string
  outcome: string
  max_iterations: number
  role_overrides: Record<string, unknown>
  state: GoalState
  created_at: string
  updated_at: string
  question: string
  summary: string
  stop_reason: string
  last_next_prompt: string
  iterations_used?: number
  deadline_at?: string | null
  mode?: GoalMode
  resume_at?: string | null
  rounds_passed?: number
  rounds_total?: number
  remaining_seconds?: number | null
  /** 这个目标已经花掉的模型用量；老服务器不会带这个字段，读的时候按没有处理。 */
  usage?: GoalUsage | null
}

/** 模型用量：后端 conveyor/agents/agent_supervisor.py 里 usage 字典的原样拷贝。 */
export type GoalUsage = {
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
  runs_with_usage: number
  runs_without_usage: number
  /** 开始记录用量之前就跑完的执行轮次；老服务器不带这个字段，读的时候按 0 处理。 */
  runs_before_recording?: number
}

export type Iteration = {
  n: number
  task_id: string
  prompt: string
  decision: '' | 'continue' | 'achieved' | 'ask_user' | 'give_up'
  decision_reason: string
  started_at: string
  finished_at: string
  task_final_state: string
  task_state: string
  passed?: boolean
  round_title?: string
}

export type GoalDetail = Goal & {
  iterations: Iteration[]
  iterations_used: number
}

const GOAL_STATE_LABELS: Record<GoalState, string> = {
  running: '进行中',
  waiting_user: '等你回复',
  waiting_quota: '等待额度恢复',
  achieved: '已达成',
  stopped: '已停止',
  exhausted: '已用完轮数',
  failed: '失败',
}

const GOAL_DECISION_LABELS: Record<string, string> = {
  continue: '继续下一轮',
  achieved: '已达成',
  ask_user: '需要你决定',
  give_up: '放弃',
}

const GOAL_ACTIVE_STATES: GoalState[] = ['running', 'waiting_user', 'waiting_quota']

const ROLE_KEYS: SupervisorRoleKey[] = ['analyst', 'implementer', 'reviewer']

const DEFAULT_MAX_ROUNDS = 5
const MIN_MAX_ROUNDS = 1
const MAX_MAX_ROUNDS = 20
const MIN_DURATION_MINUTES = 30
const MAX_DURATION_MINUTES = 1440
const COUNTDOWN_TICK_MS = 30000
const DURATION_PRESETS: Array<{ label: string; minutes: 60 | 180 | 600 }> = [
  { label: '1 小时', minutes: 60 },
  { label: '3 小时', minutes: 180 },
  { label: '10 小时', minutes: 600 },
]
const POLL_INTERVAL_MS = 3000
/** 当前这一轮的任务详情最多每 5 秒读一次（只在页面看得见时）。 */
const ROUND_TASK_POLL_MS = 5000

export function goalStateLabel(state?: string | null): string {
  const key = (state || '').trim().toLowerCase() as GoalState
  return GOAL_STATE_LABELS[key] || state || '未知'
}

export function goalDecisionLabel(decision?: string | null): string {
  const key = (decision || '').trim().toLowerCase()
  if (!key) return ''
  return GOAL_DECISION_LABELS[key] || ''
}

export function isActiveGoalState(state?: string | null): boolean {
  return GOAL_ACTIVE_STATES.includes((state || '').trim().toLowerCase() as GoalState)
}

// 目标标题：第一行、空白叠成一个空格、最长 40 个字再加「…」。后端老数据可能存着
// `[truncated]` 英文标记（或者干脆是空的），这时按同样规则从 outcome 现算一个——
// 老板只看中文，永远不该看到那种标记。
const TITLE_MAX_CHARS = 40
const TITLE_ELLIPSIS = '…'

export function goalDisplayTitle(goal?: { title?: string; outcome?: string } | null): string {
  const stored = (goal?.title || '').trim()
  const source = stored && !stored.includes('[truncated]') ? stored : goal?.outcome || ''
  const firstLine = source
    .split('\n')
    .map(line => line.trim())
    .find(line => line.length > 0) || ''
  const collapsed = firstLine.replace(/\s+/g, ' ').trim()
  if (!collapsed) return '新目标'
  return collapsed.length > TITLE_MAX_CHARS ? `${collapsed.slice(0, TITLE_MAX_CHARS)}${TITLE_ELLIPSIS}` : collapsed
}

// 任务状态里这些算「已经结束」：任务停了但评估结论还没写出来时，才提示「正在判断下一步…」。
const TASK_TERMINAL_STATES = ['completed', 'failed', 'cancelled', 'interrupted', 'missing']

function isTerminalTaskState(state?: string | null): boolean {
  return TASK_TERMINAL_STATES.includes((state || '').trim().toLowerCase())
}

function formatRemainingDuration(totalSeconds?: number | null): string {
  if (totalSeconds == null || !Number.isFinite(totalSeconds)) return ''
  const clamped = Math.max(0, Math.floor(totalSeconds))
  const totalMinutes = Math.floor(clamped / 60)
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  if (hours > 0) return `还剩 ${hours} 小时 ${minutes} 分`
  if (totalMinutes > 0) return `还剩 ${minutes} 分`
  return clamped > 0 ? '还剩不到 1 分钟' : '已到截止时间'
}

function formatQuotaResumeClock(resumeAt?: string | null): string {
  const raw = (resumeAt || '').trim()
  if (!raw) return '模型额度暂时用完，稍后会自动继续'
  const parsed = new Date(raw)
  if (Number.isNaN(parsed.getTime())) return '模型额度暂时用完，稍后会自动继续'
  const hhmm = parsed.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  return `模型额度暂时用完，会在 ${hhmm} 自动继续`
}

// 结果卡片怎么说这一步是怎么结束的：全部白话中文。
function goalResultCopy(
  goal: Goal,
  detail?: GoalDetail | null,
): { headline: string; main: string; extra: string } {
  const state = (goal.state || '').trim().toLowerCase()
  const summary = (detail?.summary || goal.summary || '').trim()
  const reason = (detail?.stop_reason || goal.stop_reason || '').trim()
  const kept = '已做的改动都留在这个工作目录里。'
  switch (state) {
    case 'achieved':
      return {
        headline: '已达成',
        main: summary || reason || `目标已经达成，${kept}`,
        extra: summary && reason && summary !== reason ? reason : '',
      }
    case 'exhausted':
      if (reason === 'deadline') {
        return {
          headline: '时间到',
          main: summary || `时间到了，${kept}`,
          extra: '',
        }
      }
      if (reason === 'quota') {
        return {
          headline: '额度用完',
          main: summary || `模型额度用完了，下次可用时间已经超过截止时间，先停在这里。`,
          extra: '',
        }
      }
      return {
        headline: '轮数用完了，还没有达成',
        main: reason || `已经跑完 ${goal.max_iterations} 轮，目标还没有达成，${kept}`,
        extra: summary,
      }
    case 'failed':
      return {
        headline: '这个目标没有跑成功',
        main: reason || `这一轮没有跑成功，目标已经停止，${kept}`,
        extra: summary,
      }
    case 'stopped':
      return {
        headline: '已经停止',
        main: reason ? `你停止了这个目标（${reason}），${kept}` : `你停止了这个目标，${kept}`,
        extra: summary,
      }
    default:
      return { headline: '目标已结束', main: summary || reason || '目标已结束。', extra: '' }
  }
}

// Backend errors arrive as {error, code}; some messages are English. The owner only
// reads Chinese, so anything without Chinese characters becomes one generic line.
export function goalChineseError(err: unknown): string {
  const raw = err instanceof Error ? err.message.trim() : ''
  if (raw && /[\u4e00-\u9fff]/.test(raw)) return raw
  return '操作没有成功，请稍后再试'
}

function goalStateTone(state?: string | null): string {
  const key = (state || '').trim().toLowerCase()
  if (key === 'running') return 'running'
  if (key === 'waiting_user' || key === 'waiting_quota') return 'waiting'
  if (key === 'achieved') return 'achieved'
  if (key === 'failed' || key === 'exhausted') return 'failed'
  return 'finished'
}

function formatGoalTime(value?: string | null): string {
  const raw = (value || '').trim()
  if (!raw) return ''
  const parsed = new Date(raw)
  if (Number.isNaN(parsed.getTime())) return raw
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
}

function clampRounds(raw: string): number {
  const parsed = Number.parseInt(raw, 10)
  if (!Number.isFinite(parsed)) return DEFAULT_MAX_ROUNDS
  return Math.min(MAX_MAX_ROUNDS, Math.max(MIN_MAX_ROUNDS, parsed))
}

function GoalChip({ state }: { state?: string | null }) {
  return <span className={`sup-goal-chip sup-goal-chip--${goalStateTone(state)}`}>{goalStateLabel(state)}</span>
}

function GoalIterationRow({
  iteration,
  expanded,
  goalRunning,
  onToggleExpand,
  onOpenTask,
}: {
  iteration: Iteration
  expanded: boolean
  goalRunning: boolean
  onToggleExpand: () => void
  onOpenTask: (taskId: string) => void
}) {
  const taskState = iteration.task_state || iteration.task_final_state
  const prompt = iteration.prompt.trim()
  // 只显示任务状态；「继续下一轮 / 已达成 / 需要你决定 / 放弃」只在真的有结论时出现。
  // 任务已经结束但结论还没写出来时，才提示「正在判断下一步…」。
  const decision = (iteration.decision || '').trim()
  const deciding = !decision && goalRunning && isTerminalTaskState(taskState)
  return (
    <li className="sup-goal-iteration">
      <div className="sup-goal-iteration-head">
        <strong className="sup-goal-iteration-n">第 {iteration.n} 轮</strong>
        <span className="sup-goal-iteration-task-state">{taskState ? supStateLabel(taskState) : '未开始'}</span>
        {decision ? (
          <span className="sup-goal-iteration-decision">{goalDecisionLabel(decision)}</span>
        ) : deciding ? (
          <span className="sup-goal-iteration-decision sup-goal-iteration-decision--pending">正在判断下一步…</span>
        ) : null}
      </div>
      <div className={`sup-goal-iteration-prompt ${expanded ? 'is-expanded' : ''}`}>
        <p>{prompt || '这一轮没有留下指令记录。'}</p>
        {!expanded && (
          <button type="button" className="sup-goal-expand-button" onClick={onToggleExpand}>
            展开全部
          </button>
        )}
      </div>
      {iteration.decision_reason.trim() && (
        <p className="sup-goal-iteration-reason">判断：{iteration.decision_reason.trim()}</p>
      )}
      <div className="sup-goal-iteration-foot">
        <span className="sup-goal-iteration-time">
          {formatGoalTime(iteration.started_at)}{iteration.finished_at ? ` → ${formatGoalTime(iteration.finished_at)}` : ''}
        </span>
        {iteration.task_id && (
          <button type="button" className="sup-goal-link-button" onClick={() => onOpenTask(iteration.task_id)}>
            查看这一轮的任务
          </button>
        )}
      </div>
    </li>
  )
}

// ---------------------------------------------------------------------------
// 当前这一轮的实时进度：一行白话「在做哪一步 · 谁在做 · 已经多久」。
// 步骤来自任务的 state / stage（GET /api/supervisor/tasks/<id>），读不到详情时
// 只用目标轮询里带的 task_state；已经过去的时间每秒在本地走，不额外发请求。
// ---------------------------------------------------------------------------

/** 任务详情里这一行用得到的字段（后端还会带上 started_at）。 */
export type GoalRoundTask = SupervisorTaskDetail & { started_at?: string | null }

// 这一轮的任务已经停下来（结束或在等人）：接下来是评估器判断下一步。
const ROUND_TASK_SETTLED_STATES = [
  ...TASK_TERMINAL_STATES,
  'succeeded',
  'waiting_user',
  'waiting_for_user',
  'waiting_for_human',
]

const HAS_CHINESE = /[\u4e00-\u9fff]/

function isRoundTaskSettled(state?: string | null): boolean {
  return ROUND_TASK_SETTLED_STATES.includes((state || '').trim().toLowerCase())
}

/** 这一轮现在在做哪一步：只返回白话中文。 */
export function goalRoundStepLabel(
  state?: string | null,
  stage?: string | null,
  repairRound?: number | null,
): string {
  const s = (state || '').trim().toLowerCase()
  // 后端阶段可能带「:轮次」后缀（例如 implementation:1），只看前半截。
  const st = (stage || '').trim().toLowerCase().split(':')[0]
  const repair = Math.max(0, Math.floor(Number(repairRound) || 0))
  if (!s) return '准备中'
  if (isRoundTaskSettled(s)) return '正在判断下一步…'
  if (s === 'repair_pending' || st === 'repair') return `自动修复第 ${Math.max(1, repair)} 轮`
  if (s === 'analyzing' || st === 'analysis' || st === 'analyzing') return '分析中'
  if (s === 'implementing' || st === 'implementation' || st === 'implementing') {
    return repair > 0 ? `自动修复第 ${repair} 轮` : '动手改'
  }
  if (st === 'validation') return '运行自动检查'
  if (s === 'reviewing' || st === 'review' || st === 'reviewing') return '检查中'
  if (s === 'dispatching' || s === 'pending_dispatch' || s === 'created') return '准备中'
  const stageLabel = st ? supStageLabel(st, s) : ''
  if (HAS_CHINESE.test(stageLabel)) return stageLabel
  const stateLabel = supStateLabel(s)
  if (HAS_CHINESE.test(stateLabel)) return stateLabel
  return '正在进行'
}

/** 已经花了多少 token：和后端 format_tokens 同一套舍入规则（先舍入，再选单位）。
 *  <10000 精确到百位并带千分位（约 8,500 token），>=10000 用「万」，
 *  不到 10 万保留一位小数（约 3.4 万 token，末尾的 .0 省略），10 万以上只写整数万（约 12 万 token）。 */
export function formatTokens(n?: number | null): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return '0 token'
  const value = Math.floor(n)
  // 先舍入到百位，再按舍入后的结果选单位：<1 万带千分位，>=1 万用「万」。
  const rounded = (Math.floor(value / 100) + (value % 100 >= 50 ? 1 : 0)) * 100
  // 1..49 舍入后是 0，但确实花了：说「不到 100 token」，而不是「0 token」。
  if (rounded <= 0) return '不到 100 token'
  if (rounded < 10000) return `约 ${rounded.toLocaleString('en-US')} token`
  // 万：不到 10 万保留一位小数（99999 先进位成 10 万，就不再带小数），以上只写整数万。
  const wan = rounded / 10000
  if (wan < 10) return `约 ${wan.toFixed(1).replace(/\.0$/, '')} 万 token`
  return `约 ${Math.floor(wan + 0.5)} 万 token`
}

/** 「已用约 3.4 万 token」+ 同样的备注；一条用量都没有时返回空串（不显示这一行）。 */
export function goalUsageLine(goal?: { usage?: GoalUsage | null } | null): string {
  // 老服务器可能只带一部分字段，读的时候一律按「没有」处理，不抛错。
  const usage = goal?.usage as Partial<GoalUsage> | null | undefined
  if (!usage) return ''
  const runsWithUsage = usage.runs_with_usage ?? 0
  const runsWithoutUsage = usage.runs_without_usage ?? 0
  // 开始记录用量之前就跑掉的轮次：不是执行者不报告，是那时候还没有记录。
  const runsBeforeRecording = usage.runs_before_recording ?? 0
  // 一条用量都没有，但这些轮次都早于记录起点：没有 token 数可显示，改成说明一句。
  if (runsWithUsage <= 0) {
    return runsBeforeRecording > 0 ? '这个目标是在开始记录用量之前跑的' : ''
  }
  const total = (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0)
  let note = ''
  if (runsWithoutUsage > 0) note = '（部分执行者没有报告用量）'
  else if (runsBeforeRecording > 0) note = '（更早的轮次还没开始记录用量）'
  return `已用${formatTokens(total)}${note}`
}

// 每一步对应哪个角色：执行记录里找不到正在跑的那条时，用任务里登记的角色执行者。
function stepRole(state: string, stage: string): SupervisorRoleKey | '' {
  const s = state.trim().toLowerCase()
  const st = stage.trim().toLowerCase().split(':')[0]
  if (st === 'validation') return ''
  if (s === 'analyzing' || st === 'analysis') return 'analyst'
  if (s === 'implementing' || s === 'repair_pending' || st === 'implementation' || st === 'repair') return 'implementer'
  if (s === 'reviewing' || st === 'review') return 'reviewer'
  return ''
}

/** 谁在做：正在跑的执行记录优先；自动检查由服务器自己跑，不写执行者。 */
export function goalRoundExecutor(task: GoalRoundTask | null, state: string, stage: string): string {
  if (!task) return ''
  const role = stepRole(state, stage)
  if (!role) return ''
  const executions = Array.isArray(task.executions) ? task.executions : []
  for (let i = executions.length - 1; i >= 0; i -= 1) {
    const ex = executions[i]
    if (!ex || typeof ex !== 'object') continue
    const status = (ex.status || '').trim().toLowerCase()
    if (status !== 'running' || (ex.finished_at || '').trim()) continue
    const name = (ex.executor || '').trim()
    if (!name || name === 'operator-validation') return ''
    return supExecutorLabel(name)
  }
  const roles = task.metadata?.roles
  const entry = roles ? roles[role] : undefined
  const name = typeof entry === 'string' ? entry : entry?.executor || ''
  return name.trim() ? supExecutorLabel(name) : ''
}

/** 「已经 3 分 12 秒」：不到一分钟只说秒，一小时以上只说小时和分。 */
export function goalElapsedLabel(totalSeconds: number): string {
  const sec = Math.max(0, Math.floor(totalSeconds))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (h > 0) return `已经 ${h} 小时 ${m} 分`
  if (m > 0) return `已经 ${m} 分 ${s} 秒`
  return `已经 ${s} 秒`
}

function parseTimeMs(value?: string | null): number | null {
  const raw = (value || '').trim()
  if (!raw) return null
  const ms = new Date(raw).getTime()
  return Number.isNaN(ms) ? null : ms
}

function usePageVisible(): boolean {
  const [pageVisible, setPageVisible] = useState(() =>
    typeof document === 'undefined' ? true : document.visibilityState !== 'hidden',
  )
  useEffect(() => {
    const onChange = () => setPageVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', onChange)
    return () => document.removeEventListener('visibilitychange', onChange)
  }, [])
  return pageVisible
}

function GoalRoundProgress({
  apiCall,
  iteration,
  visible,
}: {
  apiCall: <T>(path: string, init?: RequestInit) => Promise<T>
  iteration: Iteration
  visible: boolean
}) {
  const pageVisible = usePageVisible()
  const active = visible && pageVisible
  const taskId = (iteration.task_id || '').trim()
  const iterationState = (iteration.task_state || '').trim()
  const settled = isRoundTaskSettled(iterationState)
  const [task, setTask] = useState<GoalRoundTask | null>(null)
  const [nowMs, setNowMs] = useState(() => Date.now())
  // 上一次开始读任务详情的时间：页面切走再切回来也保证两次请求至少隔 5 秒。
  const lastFetchRef = useRef(0)

  useEffect(() => {
    setTask(null)
    lastFetchRef.current = 0
  }, [taskId])

  // 任务详情：只在页面看得见、这一轮的任务还在跑时读，最多每 5 秒一次。
  useEffect(() => {
    if (!active || !taskId || settled) return
    let cancelled = false
    let timer: number | undefined
    const run = async () => {
      lastFetchRef.current = Date.now()
      try {
        const res = await apiCall<GoalRoundTask>(`/api/supervisor/tasks/${encodeURIComponent(taskId)}`)
        if (!cancelled && res && (!res.id || res.id === taskId)) setTask(res)
      } catch {
        // 读不到详情就先用目标轮询里的状态，不打扰老板。
      }
      if (!cancelled) timer = window.setTimeout(() => void run(), ROUND_TASK_POLL_MS)
    }
    const wait = Math.max(0, ROUND_TASK_POLL_MS - (Date.now() - lastFetchRef.current))
    timer = window.setTimeout(() => void run(), wait)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [active, apiCall, taskId, settled])

  // 已经过去的时间每秒在本地走一下，不发请求。
  useEffect(() => {
    if (!active) return
    setNowMs(Date.now())
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])

  const matchedTask = task && (!task.id || task.id === taskId) ? task : null
  const state = iterationState || matchedTask?.state || ''
  // 详情比目标轮询旧的时候，阶段可能已经过时：状态对得上才用它的阶段。
  const stage = matchedTask && (matchedTask.state || '').trim().toLowerCase() === state.toLowerCase()
    ? matchedTask.stage || ''
    : ''
  const step = goalRoundStepLabel(state, stage, matchedTask?.repair_round)
  const executor = settled ? '' : goalRoundExecutor(matchedTask, state, stage)
  const startedMs = parseTimeMs(iteration.started_at) ?? parseTimeMs(matchedTask?.started_at)
  const elapsed = startedMs === null ? '' : goalElapsedLabel((nowMs - startedMs) / 1000)

  return (
    <p className="sup-goal-live">
      <span className="sup-goal-live-dot" aria-hidden="true" />
      <span className="sup-goal-live-step">{step}</span>
      {executor && <span className="sup-goal-live-executor">{executor} 在做</span>}
      {elapsed && <span className="sup-goal-live-elapsed">{elapsed}</span>}
    </p>
  )
}

// Renders nothing. Lets the workspace learn whether the selected worktree has a running /
// waiting goal (so 目标 can become the default sub-tab) without mounting the full UI.
export function SupervisorGoalsProbe({
  apiCall,
  repo,
  worktree,
  onActiveGoalChange,
}: {
  apiCall: <T>(path: string, init?: RequestInit) => Promise<T>
  repo: SupervisorRepo
  worktree: SupervisorWorktree
  onActiveGoalChange: (hasActiveGoal: boolean) => void
}) {
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const res = await apiCall<{ goals?: Goal[] }>('/api/supervisor/goals')
        if (cancelled) return
        const all = Array.isArray(res.goals) ? res.goals : []
        const hasActive = all.some(
          g => g.repo_id === repo.id && g.worktree_id === worktree.id && isActiveGoalState(g.state),
        )
        onActiveGoalChange(hasActive)
      } catch {
        if (!cancelled) onActiveGoalChange(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [apiCall, onActiveGoalChange, repo.id, worktree.id])
  return null
}

export default function SupervisorGoals({
  apiCall,
  repo,
  worktree,
  visible,
  readOnly,
  locked,
  capabilities,
  effectiveRoles,
  onChangeRole,
  onOpenTask,
  onActiveGoalChange,
  onRefreshRepositories,
  draftOutcome,
  onDraftOutcomeUsed,
  onOpenWorkers,
  workersData,
}: {
  apiCall: <T>(path: string, init?: RequestInit) => Promise<T>
  repo: SupervisorRepo
  worktree: SupervisorWorktree
  visible: boolean
  readOnly: boolean
  locked: boolean
  capabilities: SupervisorCapabilities | null
  effectiveRoles: RoleOverrides
  onChangeRole: (role: SupervisorRoleKey, patch: Partial<RoleOverrideEntry>) => void
  onOpenTask: (taskId: string) => void
  onActiveGoalChange: (hasActiveGoal: boolean) => void
  onRefreshRepositories?: () => void
  /** 从「讨论」里点「设为目标」带来的草稿：只填进输入框，绝不自动开始。 */
  draftOutcome?: string | null
  onDraftOutcomeUsed?: () => void
  /** Jump to the 执行者 page (used when a start is rejected for a failed executor check). */
  onOpenWorkers?: () => void
  /** Live workers payload from App.tsx: the Pi/pool role badges read Pi's last 测试. */
  workersData?: SupervisorWorkersData
}) {
  // 自动检查状态（来自 SupervisorChecks 的汇报）：用来在「开始」上方提醒还没有检查命令。
  const [checksSummary, setChecksSummary] = useState<SupervisorChecksSummary | null>(null)
  useEffect(() => {
    setChecksSummary(null)
  }, [repo.id])
  const handleChecksSummary = useCallback((summary: SupervisorChecksSummary) => {
    setChecksSummary(summary)
  }, [])
  // 服务器已配置（env）时不算「未设置」；否则只要勾选数为 0 就提醒。详情还没读到时，先按仓库列表
  // 自带的标记判断：标记说「没有」或没有标记时，先按没有检查提醒（绝不挡住「开始」）。
  const checksMissing = checksSummary
    ? checksSummary.source !== 'env' && checksSummary.selectedCount === 0
    : repo.validation_source !== 'env' && repo.validation_source !== 'repo'
  const contextKey = `${repo.id}:${worktree.id}`
  const [goals, setGoals] = useState<Goal[]>([])
  const [detail, setDetail] = useState<GoalDetail | null>(null)
  const [loadError, setLoadError] = useState('')
  const [outcome, setOutcome] = useState(() => readGoalDraft(contextKey))
  // N25: the typed goal is a draft on this device until 开始; an empty draft is dropped, so a
  // successful 开始 clears the stored copy together with the box.
  useEffect(() => {
    writeGoalDraft(contextKey, outcome)
  }, [contextKey, outcome])
  const [maxRounds, setMaxRounds] = useState(String(DEFAULT_MAX_ROUNDS))
  const [budgetKind, setBudgetKind] = useState<'rounds' | 'duration'>('rounds')
  const [durationPreset, setDurationPreset] = useState<60 | 180 | 600 | 'custom'>(600)
  const [customMinutes, setCustomMinutes] = useState('120')
  const [continuousMode, setContinuousMode] = useState(false)
  const [countdownNowMs, setCountdownNowMs] = useState(() => Date.now())
  const countdownAnchorRef = useRef<{ key: string; baseSeconds: number; anchoredAtMs: number } | null>(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  // When the start is rejected because the executor failed its pre-flight check, remember the
  // code so the error area can offer a shortcut to the 执行者 page.
  const [createErrorCode, setCreateErrorCode] = useState('')
  const [replyMessage, setReplyMessage] = useState('')
  const [replying, setReplying] = useState(false)
  const [replyError, setReplyError] = useState('')
  const [confirmStop, setConfirmStop] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState('')
  const [expandedRound, setExpandedRound] = useState<number | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  // 结果卡上的「查看改动」：就地展开这个目标的改动（文件列表 + 逐行差异），不跳页。
  const [changesOpen, setChangesOpen] = useState(false)
  // 结果卡上的「合并到主分支…」：点开才读合并预览，确认后才真的合并。
  const [mergeOpen, setMergeOpen] = useState(false)
  const [pinnedGoalId, setPinnedGoalId] = useState('')
  // 老板点了「开始新目标」：表单要一直露出来，别再自动把刚跑完的目标钉回最上面。
  const [formRequested, setFormRequested] = useState(false)
  const [timelineOpen, setTimelineOpen] = useState(false)
  const pinnedGoalIdRef = useRef('')
  const formRequestedRef = useRef(false)
  pinnedGoalIdRef.current = pinnedGoalId
  formRequestedRef.current = formRequested

  // Keep the ref in sync immediately (actions read it right after pinning, before re-render).
  const pinGoal = useCallback((goalId: string) => {
    pinnedGoalIdRef.current = goalId
    setPinnedGoalId(goalId)
  }, [])

  const activeGoal = useMemo(() => goals.find(g => isActiveGoalState(g.state)) || null, [goals])
  const hasActiveGoal = Boolean(activeGoal)
  const shownGoal: Goal | null = activeGoal || (pinnedGoalId ? goals.find(g => g.id === pinnedGoalId) || null : null)
  const shownDetail: GoalDetail | null = detail && shownGoal && detail.id === shownGoal.id ? detail : null
  // 最上面结果卡里那个目标不再重复出现在「以前的目标」里。
  const finishedGoals = useMemo(() => goals.filter(g => !isActiveGoalState(g.state) && g.id !== shownGoal?.id), [goals, shownGoal])
  // 目标跑完之后（达成 / 用完轮数 / 失败 / 停止）结果卡继续挂在最上面，表单藏起来。
  const goalFinished = shownGoal ? !isActiveGoalState(shownGoal.state) : false
  const showForm = !hasActiveGoal && !shownGoal
  const showTimeline = !goalFinished || timelineOpen

  const loadDetail = useCallback(
    async (goalId: string) => {
      try {
        const res = await apiCall<GoalDetail>(`/api/supervisor/goals/${encodeURIComponent(goalId)}`)
        setDetail(res)
        setLoadError('')
      } catch (err) {
        setLoadError(goalChineseError(err))
      }
    },
    [apiCall],
  )

  // List + (when relevant) detail for the goal currently on screen.
  const refresh = useCallback(async () => {
    let scoped: Goal[]
    try {
      const res = await apiCall<{ goals?: Goal[] }>('/api/supervisor/goals')
      const all = Array.isArray(res.goals) ? res.goals : []
      scoped = all.filter(g => g.repo_id === repo.id && g.worktree_id === worktree.id)
      setGoals(scoped)
      setLoadError('')
    } catch (err) {
      setLoadError(goalChineseError(err))
      return
    }
    const currentActive = scoped.find(g => isActiveGoalState(g.state)) || null
    if (currentActive) {
      pinGoal('')
      setFormRequested(false)
      await loadDetail(currentActive.id)
      return
    }
    if (pinnedGoalIdRef.current && scoped.some(g => g.id === pinnedGoalIdRef.current)) {
      await loadDetail(pinnedGoalIdRef.current)
      return
    }
    if (formRequestedRef.current) {
      // 老板已经点了「开始新目标」：别把刚跑完的目标又钉回最上面。
      setDetail(null)
      return
    }
    // 没有在跑的目标：把这个 worktree 最近一个目标的结果卡留在最上面。
    const latest = scoped[0] || null
    if (latest) {
      pinGoal(latest.id)
      await loadDetail(latest.id)
      return
    }
    setDetail(null)
  }, [apiCall, loadDetail, pinGoal, repo.id, worktree.id])

  // Initial load for this worktree (also decides the default sub-tab).
  useEffect(() => {
    setDetail(null)
    pinGoal('')
    setFormRequested(false)
    setReplyMessage('')
    setConfirmStop(false)
    void refresh()
  }, [contextKey, pinGoal, refresh])

  // 「设为目标」带来的草稿：露出表单（像点了「开始新目标」）并填进去；必须排在上面的初次加载之后。
  useEffect(() => {
    if (!draftOutcome) return
    pinGoal('')
    formRequestedRef.current = true
    setFormRequested(true)
    setOutcome(draftOutcome)
    onDraftOutcomeUsed?.()
  }, [draftOutcome, onDraftOutcomeUsed, pinGoal])

  // 换一个目标看的时候，每一轮默认收起（跑着的时候才默认展开），单轮展开也重置。
  const shownGoalId = shownGoal?.id || ''
  useEffect(() => {
    setTimelineOpen(false)
    setExpandedRound(null)
    setChangesOpen(false)
    setMergeOpen(false)
  }, [shownGoalId])

  // Poll the running / waiting_quota goal every 3s while this tab is visible; stop otherwise.
  useEffect(() => {
    if (!visible || !hasActiveGoal || (activeGoal?.state !== 'running' && activeGoal?.state !== 'waiting_quota')) return
    const timer = window.setInterval(() => {
      void refresh()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [visible, hasActiveGoal, activeGoal?.state, refresh])

  const shownServerRemaining = shownGoal?.remaining_seconds ?? null
  useEffect(() => {
    if (!shownGoal?.id || shownServerRemaining == null) {
      countdownAnchorRef.current = null
      return
    }
    const nowMs = Date.now()
    const key = `${shownGoal.id}:${shownGoal.updated_at}:${shownServerRemaining}`
    if (countdownAnchorRef.current?.key !== key) {
      countdownAnchorRef.current = { key, baseSeconds: shownServerRemaining, anchoredAtMs: nowMs }
      setCountdownNowMs(nowMs)
    }
  }, [shownGoal?.id, shownGoal?.updated_at, shownServerRemaining])

  // 本地每 30 秒刷新一次倒计时文本，不额外发网络请求。
  useEffect(() => {
    if (!visible || !shownGoal?.deadline_at || !isActiveGoalState(shownGoal.state) || shownServerRemaining == null) {
      return
    }
    const timer = window.setInterval(() => {
      setCountdownNowMs(Date.now())
    }, COUNTDOWN_TICK_MS)
    return () => window.clearInterval(timer)
  }, [visible, shownGoal?.deadline_at, shownGoal?.state, shownServerRemaining])

  const activeGoalKey = activeGoal?.id || ''
  useEffect(() => {
    onActiveGoalChange(hasActiveGoal)
  }, [hasActiveGoal, onActiveGoalChange, activeGoalKey])

  const handleStart = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const text = outcome.trim()
      if (!text || creating || readOnly || locked) return
      const payload: Record<string, unknown> = {
        outcome: text,
        repo_id: repo.id,
        worktree_id: worktree.id,
      }
      if (budgetKind === 'duration') {
        const rawDuration = durationPreset === 'custom' ? Number.parseInt(customMinutes, 10) : durationPreset
        if (!Number.isFinite(rawDuration) || rawDuration < MIN_DURATION_MINUTES || rawDuration > MAX_DURATION_MINUTES) {
          setCreateError(`持续时间请填 ${MIN_DURATION_MINUTES} 到 ${MAX_DURATION_MINUTES} 分钟之间的整数`)
          return
        }
        payload.duration_minutes = rawDuration
        payload.mode = continuousMode ? 'continuous' : 'outcome'
      } else {
        payload.max_iterations = clampRounds(maxRounds)
        if (continuousMode) {
          payload.mode = 'continuous'
        }
      }
      const roleOverrides: Record<string, { executor: string; model?: string }> = {}
      for (const key of ROLE_KEYS) {
        const role = effectiveRoles[key]
        if (!role.executor) continue
        const entry: { executor: string; model?: string } = { executor: role.executor }
        const model = role.model.trim()
        if (model) entry.model = model
        roleOverrides[key] = entry
      }
      if (Object.keys(roleOverrides).length > 0) {
        payload.role_overrides = roleOverrides
      }
      setCreating(true)
      setCreateError('')
      setCreateErrorCode('')
      try {
        await apiCall<Goal>('/api/supervisor/goals', {
          method: 'POST',
          body: JSON.stringify(payload),
        })
        setOutcome('')
        setFormRequested(false)
        await refresh()
      } catch (err) {
        const code = (err as { code?: string } | null)?.code || ''
        setCreateErrorCode(code === 'executor_check_failed' ? code : '')
        setCreateError(goalChineseError(err))
      } finally {
        setCreating(false)
      }
    },
    [
      apiCall,
      budgetKind,
      continuousMode,
      creating,
      customMinutes,
      durationPreset,
      effectiveRoles,
      locked,
      maxRounds,
      outcome,
      readOnly,
      refresh,
      repo.id,
      worktree.id,
    ],
  )

  const handleStop = useCallback(async () => {
    if (!activeGoal || stopping) return
    setStopping(true)
    setStopError('')
    try {
      const res = await apiCall<GoalDetail>(
        `/api/supervisor/goals/${encodeURIComponent(activeGoal.id)}/stop`,
        { method: 'POST', body: JSON.stringify({}) },
      )
      setDetail(res)
      pinGoal(res.id)
      setFormRequested(false)
      setConfirmStop(false)
      await refresh()
    } catch (err) {
      setStopError(goalChineseError(err))
    } finally {
      setStopping(false)
    }
  }, [activeGoal, apiCall, refresh, stopping])

  const handleReply = useCallback(
    async (achieved: boolean) => {
      if (!activeGoal || replying) return
      const message = replyMessage.trim()
      if (!achieved && !message) {
        setReplyError('请先写一句还要改什么，再点「继续」。')
        return
      }
      setReplying(true)
      setReplyError('')
      try {
        const res = await apiCall<GoalDetail>(
          `/api/supervisor/goals/${encodeURIComponent(activeGoal.id)}/reply`,
          {
            method: 'POST',
            body: JSON.stringify({ message, achieved }),
          },
        )
        setDetail(res)
        if (isActiveGoalState(res.state)) {
          pinGoal('')
          setFormRequested(false)
          setReplyMessage('')
        } else {
          pinGoal(res.id)
          setFormRequested(false)
        }
        await refresh()
      } catch (err) {
        setReplyError(goalChineseError(err))
      } finally {
        setReplying(false)
      }
    },
    [activeGoal, apiCall, refresh, replyMessage, replying],
  )

  // 结果卡上的「开始新目标」：收起结果卡，把表单露出来。
  const handleStartNew = useCallback(() => {
    pinGoal('')
    setFormRequested(true)
    setConfirmStop(false)
    setReplyMessage('')
    setStopError('')
  }, [pinGoal])

  const handleSelectFinished = useCallback(
    (goalId: string) => {
      setFormRequested(false)
      // 已经在看这个目标了就保持原样（尤其是「查看每一轮」别被收起）。
      if (pinnedGoalIdRef.current === goalId) return
      pinGoal(goalId)
      void loadDetail(goalId)
    },
    [loadDetail, pinGoal],
  )

  const roundsUsed = shownDetail ? shownDetail.iterations_used ?? shownDetail.iterations.length : 0
  const roundsTotal = shownGoal?.max_iterations || DEFAULT_MAX_ROUNDS
  const canStop = Boolean(activeGoal) && !stopping
  const iterations = shownDetail?.iterations || []
  // 「查看改动」要去的那一轮：最近一个真的跑出任务的一轮（和「查看这一轮的任务」同一套跳转）。
  const lastTaskId = useMemo(() => {
    const withTask = iterations.filter(round => round.task_id.trim())
    if (withTask.length === 0) return ''
    return withTask.reduce((latest, round) => (round.n > latest.n ? round : latest)).task_id
  }, [iterations])
  // 正在跑的目标的当前这一轮（编号最大的那一轮），还没有结论时才显示实时进度。
  const currentRound = useMemo(
    () => (iterations.length > 0 ? iterations.reduce((latest, round) => (round.n > latest.n ? round : latest)) : null),
    [iterations],
  )
  const showRoundProgress = shownGoal?.state === 'running'
    && shownDetail?.state === 'running'
    && Boolean(currentRound)
    && !(currentRound?.decision || '').trim()
  // 跑完之后的结果卡文案（标题 + 白话结论 + 补充说明）。
  const resultCopy = goalFinished && shownGoal ? goalResultCopy(shownGoal, shownDetail) : null
  // 「已用约 3.4 万 token」：一条用量都没有时这一行不出现。
  const usageLine = goalUsageLine(shownGoal)
  // 「查看改动」就地读这个目标的差异：只有点开了才请求。
  const goalDiff = useSupervisorDiff(apiCall, 'goal', shownGoal?.id || '', changesOpen && goalFinished)
  // 只有达成目标才给「合并到主分支」：主工作目录 / 未托管目录没有合并入口，还有任务或
  // 目标在跑的时候也不给（合并不能和正在写的改动撞上）。
  const canOfferMerge = Boolean(resultCopy)
    && shownGoal?.state === 'achieved'
    && !worktree.is_main
    && worktree.managed
    && !locked
    && !hasActiveGoal
  const isTimeboxed = Boolean(shownGoal?.deadline_at)
  const roundsExecutedCount =
    shownGoal?.rounds_total
    ?? iterations.filter(round => Boolean(round.task_id || round.decision || round.task_final_state)).length
  const roundsPassedCount =
    shownGoal?.rounds_passed
    ?? iterations.filter(round => Boolean(round.passed) || round.decision === 'achieved').length
  const passedIterations = iterations.filter(round => Boolean(round.passed) || round.decision === 'achieved')
  const countdownAnchor = countdownAnchorRef.current
  const displayRemainingSeconds =
    shownServerRemaining == null
      ? null
      : countdownAnchor && countdownAnchor.key.startsWith(`${shownGoal?.id || ''}:`)
        ? Math.max(
            0,
            countdownAnchor.baseSeconds - Math.floor(Math.max(0, countdownNowMs - countdownAnchor.anchoredAtMs) / 1000),
          )
        : shownServerRemaining
  // 「再试几轮」：已用完轮数或失败的目标（按轮数）可以增加 1–10 轮（默认 3 轮）继续跑。
  const canOfferExtend = Boolean(resultCopy)
    && (shownGoal?.state === 'exhausted' || shownGoal?.state === 'failed')
    && !shownGoal?.deadline_at
    && !readOnly
  const [extendOpen, setExtendOpen] = useState(false)
  const [extendRounds, setExtendRounds] = useState('3')
  const [extendMessage, setExtendMessage] = useState('')
  const [extending, setExtending] = useState(false)
  const [extendError, setExtendError] = useState('')
  useEffect(() => {
    setExtendOpen(false)
    setExtendRounds('3')
    setExtendMessage('')
    setExtendError('')
  }, [shownGoalId, contextKey])
  const handleExtend = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      if (!shownGoal || extending || readOnly) return
      const parsedRounds = Number.parseInt(extendRounds, 10)
      const rounds = Number.isFinite(parsedRounds) ? Math.min(10, Math.max(1, parsedRounds)) : 3
      const message = extendMessage.trim()
      setExtending(true)
      setExtendError('')
      try {
        const res = await apiCall<GoalDetail>(
          `/api/supervisor/goals/${encodeURIComponent(shownGoal.id)}/extend`,
          {
            method: 'POST',
            body: JSON.stringify({
              rounds,
              ...(message ? { message } : {}),
            }),
          },
        )
        setDetail(res)
        pinGoal('')
        setFormRequested(false)
        setExtendOpen(false)
        setExtendRounds('3')
        setExtendMessage('')
        await refresh()
      } catch (err) {
        setExtendError(goalChineseError(err))
      } finally {
        setExtending(false)
      }
    },
    [apiCall, extendMessage, extendRounds, extending, pinGoal, readOnly, refresh, shownGoal],
  )

  return (
    <div className="sup-goals-scroll">
      <div className="sup-goals-view">
        {loadError && (
          <div className="sup-banner sup-banner--error" role="alert">
            <strong>没有读到目标</strong>
            <span>{loadError}</span>
          </div>
        )}

        {readOnly && (
          <div className="sup-banner sup-banner--readonly" role="status">
            <div className="sup-banner-text">
              <strong>当前为只读工作目录（{worktree.is_main ? '主分支' : '未托管目录'}）</strong>
              <span>主工作目录与未托管目录仅支持只读查看。请新建工作目录，在独立副本里执行目标。</span>
            </div>
          </div>
        )}

        {!readOnly && locked && !hasActiveGoal && (
          <div className="sup-banner sup-banner--locked" role="status">
            <div className="sup-banner-text">
              <strong>当前工作目录已锁定（有任务正在执行）</strong>
              <span>同一时间只允许一个写入任务运行，当前任务结束或取消后可以再开始新目标。</span>
            </div>
          </div>
        )}

        {shownGoal && (
          <section className="sup-card sup-goal-card" aria-label="目标进度">
            <header className="sup-goal-header">
              <div className="sup-goal-header-main">
                <GoalChip state={shownGoal.state} />
                <h3>{goalDisplayTitle(shownGoal)}</h3>
              </div>
              <div className="sup-goal-header-side">
                <span className="sup-goal-rounds">
                  {isTimeboxed
                    ? `已跑 ${roundsExecutedCount} 轮 · 通过 ${roundsPassedCount} 轮`
                    : `第 ${roundsUsed} / ${roundsTotal} 轮`}
                </span>
                {canStop && !confirmStop && (
                  <button
                    type="button"
                    className="shell-button shell-button--danger sup-goal-stop-button"
                    onClick={() => setConfirmStop(true)}
                  >
                    停止
                  </button>
                )}
              </div>
            </header>

            {isTimeboxed && !goalFinished && (
              <div className="sup-goal-timebox-bar">
                {displayRemainingSeconds != null && (
                  <strong className="sup-goal-countdown" aria-live="polite">
                    {formatRemainingDuration(displayRemainingSeconds)}
                  </strong>
                )}
                <span className="sup-goal-mode-note">
                  {shownGoal.mode === 'continuous'
                    ? '持续改进模式 · 到点自动停止'
                    : '达标即停 · 到点自动停止'}
                </span>
              </div>
            )}

            {usageLine && <p className="sup-goal-usage">{usageLine}</p>}

            {shownGoal.state === 'waiting_quota' && (
              <div className="sup-banner sup-banner--waiting-quota" role="status">
                <div className="sup-banner-text">
                  <strong>模型额度暂时用完，先停下来等恢复</strong>
                  <span>{formatQuotaResumeClock(shownGoal.resume_at)}</span>
                </div>
              </div>
            )}

            {isTimeboxed && passedIterations.length > 0 && (
              <div className="sup-goal-passed-list" aria-label="已完成的改进">
                <strong>已完成的改进（{passedIterations.length}）</strong>
                <ul>
                  {passedIterations.map(round => (
                    <li key={round.n}>
                      <span className="sup-goal-passed-round">第 {round.n} 轮</span>
                      <span className="sup-goal-passed-title">
                        {round.round_title?.trim() || round.decision_reason?.trim() || '已完成本轮改进'}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {showRoundProgress && currentRound && (
              <GoalRoundProgress
                key={`${shownGoal.id}:${currentRound.n}`}
                apiCall={apiCall}
                iteration={currentRound}
                visible={visible}
              />
            )}

            {shownGoal.outcome.trim() && shownGoal.outcome.trim() !== shownGoal.title && (
              <p className="sup-goal-outcome-text">想要的结果：{shownGoal.outcome.trim()}</p>
            )}

            {!shownDetail && <p className="sup-muted-note">正在读取这一目标…</p>}

            {resultCopy ? (
              // 跑完了：结果卡说清楚这一步是怎么结束的、改动在哪，并给出去看改动 / 开始新目标的出口。
              <div className="sup-goal-result">
                <strong>{resultCopy.headline}</strong>
                <p>{resultCopy.main}</p>
                {resultCopy.extra && <p className="sup-goal-result-note">{resultCopy.extra}</p>}
                {lastTaskId && (
                  <p className="sup-goal-result-where">
                    改动在工作目录 <code>{worktree.branch}</code> 里{worktree.is_main ? '。' : '（还没有合并到主分支）。'}
                  </p>
                )}
                <div className="sup-goal-result-actions">
                  {lastTaskId && (
                    <button
                      type="button"
                      className="shell-button"
                      aria-expanded={changesOpen}
                      onClick={() => setChangesOpen(prev => !prev)}
                    >
                      查看改动
                    </button>
                  )}
                  {canOfferExtend && (
                    <button
                      type="button"
                      className="shell-button sup-goal-extend-button"
                      aria-expanded={extendOpen}
                      onClick={() => {
                        setExtendOpen(prev => !prev)
                        setExtendError('')
                      }}
                    >
                      再试几轮
                    </button>
                  )}
                  <button type="button" className="shell-button primary sup-goal-new-button" onClick={handleStartNew}>
                    开始新目标
                  </button>
                </div>
                {extendOpen && canOfferExtend && (
                  <form
                    className="sup-goal-extend"
                    aria-label="再试几轮"
                    onSubmit={event => {
                      void handleExtend(event)
                    }}
                  >
                    <div className="sup-goal-form-row">
                      <div className="sup-form-field sup-goal-rounds-field">
                        <label htmlFor="sup-goal-extend-rounds">再试几轮</label>
                        <input
                          id="sup-goal-extend-rounds"
                          aria-label="再试几轮"
                          type="number"
                          min={1}
                          max={10}
                          step={1}
                          value={extendRounds}
                          disabled={extending || readOnly}
                          onChange={e => setExtendRounds(e.target.value)}
                        />
                      </div>
                    </div>
                    <div className="sup-form-field">
                      <label htmlFor="sup-goal-extend-message">补充说明</label>
                      <textarea
                        id="sup-goal-extend-message"
                        aria-label="补充说明"
                        placeholder="补充说明（可选），例如：换个思路或重点检查哪里"
                        rows={3}
                        maxLength={2000}
                        value={extendMessage}
                        disabled={extending || readOnly}
                        onChange={e => setExtendMessage(e.target.value)}
                      />
                    </div>
                    {extendError && (
                      <div className="sup-banner sup-banner--error" role="alert">
                        <strong>没有继续成功</strong>
                        <span>{extendError}</span>
                      </div>
                    )}
                    <div className="sup-goal-extend-actions">
                      <button
                        type="submit"
                        className="shell-button primary"
                        disabled={extending || readOnly}
                      >
                        {extending ? '正在继续…' : '继续'}
                      </button>
                    </div>
                  </form>
                )}
                {changesOpen && <SupervisorDiffView diffState={goalDiff} />}
                {canOfferMerge && (
                  <button
                    type="button"
                    className="shell-button sup-goal-merge-button"
                    aria-expanded={mergeOpen}
                    onClick={() => setMergeOpen(prev => !prev)}
                  >
                    合并到主分支…
                  </button>
                )}
                {mergeOpen && canOfferMerge && <SupervisorMerge apiCall={apiCall} worktree={worktree} />}
              </div>
            ) : (
              shownDetail?.summary.trim() && <p className="sup-goal-summary">{shownDetail.summary.trim()}</p>
            )}

            {stopError && (
              <div className="sup-banner sup-banner--error" role="alert">
                <strong>没有停止成功</strong>
                <span>{stopError}</span>
              </div>
            )}

            {confirmStop && canStop && (
              <div className="sup-banner sup-banner--warning sup-goal-confirm" role="alert">
                <div className="sup-banner-text">
                  <strong>确认停止这个目标？</strong>
                  <span>停止后当前这一轮会被取消，已做的改动保留在工作目录里。</span>
                </div>
                <div className="sup-goal-confirm-actions">
                  <button
                    type="button"
                    className="shell-button"
                    disabled={stopping}
                    onClick={() => setConfirmStop(false)}
                  >
                    继续跑
                  </button>
                  <button
                    type="button"
                    className="shell-button shell-button--danger"
                    disabled={stopping}
                    onClick={() => void handleStop()}
                  >
                    {stopping ? '正在停止…' : '确认停止'}
                  </button>
                </div>
              </div>
            )}

            {shownDetail?.state === 'waiting_user' && shownDetail.question.trim() && (
              <div className="sup-banner sup-banner--warning sup-goal-question" role="status">
                <div className="sup-banner-text">
                  <strong>需要你决定</strong>
                  <span>{shownDetail.question.trim()}</span>
                </div>
              </div>
            )}

            {shownDetail?.state === 'waiting_user' && (
              <form
                className="sup-goal-reply"
                onSubmit={event => {
                  event.preventDefault()
                  void handleReply(false)
                }}
              >
                <label htmlFor="sup-goal-reply-input" className="sr-only">补充说明或回答</label>
                <textarea
                  id="sup-goal-reply-input"
                  aria-label="补充说明或回答"
                  placeholder="补充说明或回答"
                  rows={3}
                  value={replyMessage}
                  disabled={replying}
                  onChange={e => setReplyMessage(e.target.value)}
                />
                {replyError && (
                  <div className="sup-banner sup-banner--error" role="alert">
                    <span>{replyError}</span>
                  </div>
                )}
                <div className="sup-goal-reply-actions">
                  <button
                    type="submit"
                    className="shell-button primary"
                    disabled={replying || !replyMessage.trim()}
                  >
                    {replying ? '正在提交…' : '继续'}
                  </button>
                  <button
                    type="button"
                    className="shell-button"
                    disabled={replying}
                    onClick={() => void handleReply(true)}
                  >
                    已经达成了
                  </button>
                </div>
              </form>
            )}

            <div className="sup-goal-timeline-head">
              <h4>每一轮在做什么</h4>
              {goalFinished ? (
                <button
                  type="button"
                  className="sup-goal-expand-button"
                  aria-expanded={showTimeline}
                  onClick={() => setTimelineOpen(prev => !prev)}
                >
                  {showTimeline ? '收起每一轮' : '查看每一轮'}
                </button>
              ) : (
                <span className="sup-card-hint">最新的一轮在最上面</span>
              )}
            </div>
            {!goalFinished && iterations.length === 0 && (
              <p className="sup-muted-note">正在准备第 1 轮，稍等几秒就会出现在这里。</p>
            )}
            {goalFinished && !showTimeline && iterations.length > 0 && (
              <p className="sup-muted-note">一共跑了 {iterations.length} 轮，点「查看每一轮」可以回看每一轮做了什么。</p>
            )}
            {showTimeline && iterations.length > 0 && (
              <ol className="sup-goal-timeline">
                {iterations
                  .slice()
                  .sort((a, b) => b.n - a.n)
                  .map(iteration => (
                    <GoalIterationRow
                      key={iteration.n}
                      iteration={iteration}
                      expanded={expandedRound === iteration.n}
                      goalRunning={shownGoal?.state === 'running'}
                      onToggleExpand={() => setExpandedRound(prev => (prev === iteration.n ? null : iteration.n))}
                      onOpenTask={onOpenTask}
                    />
                  ))}
              </ol>
            )}
          </section>
        )}

        {showForm && (
          <section className="sup-card sup-goal-form-card" aria-label="描述你想要的结果">
            <div className="sup-card-head">
              <h4>描述你想要的结果</h4>
              <span className="sup-card-hint">会一轮一轮地改、验证，直到达成</span>
            </div>
            <form
              className="sup-goal-form"
              onSubmit={event => {
                void handleStart(event)
              }}
            >
              <label htmlFor="sup-goal-outcome" className="sr-only">描述你想要的结果</label>
              <textarea
                id="sup-goal-outcome"
                aria-label="描述你想要的结果"
                placeholder="例如：首页加载时间降到 1 秒以内，并且所有测试通过"
                rows={4}
                value={outcome}
                disabled={readOnly || locked || creating}
                onChange={e => setOutcome(e.target.value)}
              />
              {/* N25: while a draft exists the owner can see it is kept on this device. */}
              {outcome.trim() && (
                <p className="sup-draft-hint" role="status">草稿已自动保存，刷新页面也不会丢；点「开始」后清除。</p>
              )}
              <div className="sup-goal-form-row">
                <div className="sup-goal-budget-row" role="group" aria-label="运行方式">
                  <div className="sup-goal-budget-tabs" role="radiogroup" aria-label="按轮数或按时间">
                    <button
                      type="button"
                      role="radio"
                      aria-checked={budgetKind === 'rounds'}
                      className={`sup-goal-segment-btn ${budgetKind === 'rounds' ? 'is-active' : ''}`}
                      disabled={readOnly || locked || creating}
                      onClick={() => setBudgetKind('rounds')}
                    >
                      按轮数
                    </button>
                    <button
                      type="button"
                      role="radio"
                      aria-checked={budgetKind === 'duration'}
                      className={`sup-goal-segment-btn ${budgetKind === 'duration' ? 'is-active' : ''}`}
                      disabled={readOnly || locked || creating}
                      onClick={() => setBudgetKind('duration')}
                    >
                      按时间
                    </button>
                  </div>

                  {budgetKind === 'rounds' ? (
                    <div className="sup-form-field sup-goal-rounds-field">
                      <label htmlFor="sup-goal-max-rounds">最多尝试几轮</label>
                      <input
                        id="sup-goal-max-rounds"
                        aria-label="最多尝试几轮"
                        type="number"
                        min={MIN_MAX_ROUNDS}
                        max={MAX_MAX_ROUNDS}
                        step={1}
                        value={maxRounds}
                        disabled={readOnly || locked || creating}
                        onChange={e => setMaxRounds(e.target.value)}
                      />
                    </div>
                  ) : (
                    <div className="sup-goal-duration-picker">
                      <div className="sup-goal-duration-presets" role="group" aria-label="运行时长">
                        {DURATION_PRESETS.map(preset => (
                          <button
                            key={preset.minutes}
                            type="button"
                            aria-pressed={durationPreset === preset.minutes}
                            className={`sup-goal-preset-btn ${durationPreset === preset.minutes ? 'is-active' : ''}`}
                            disabled={readOnly || locked || creating}
                            onClick={() => setDurationPreset(preset.minutes)}
                          >
                            {preset.label}
                          </button>
                        ))}
                        <button
                          type="button"
                          aria-pressed={durationPreset === 'custom'}
                          className={`sup-goal-preset-btn ${durationPreset === 'custom' ? 'is-active' : ''}`}
                          disabled={readOnly || locked || creating}
                          onClick={() => setDurationPreset('custom')}
                        >
                          自定义（分钟）
                        </button>
                      </div>
                      {durationPreset === 'custom' && (
                        <div className="sup-form-field sup-goal-custom-minutes">
                          <label htmlFor="sup-goal-duration-minutes">自定义时长（分钟）</label>
                          <input
                            id="sup-goal-duration-minutes"
                            aria-label="自定义时长（分钟）"
                            type="number"
                            min={MIN_DURATION_MINUTES}
                            max={MAX_DURATION_MINUTES}
                            step={1}
                            value={customMinutes}
                            disabled={readOnly || locked || creating}
                            onChange={e => setCustomMinutes(e.target.value)}
                          />
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>

              <label className="sup-goal-continuous-check">
                <input
                  type="checkbox"
                  checked={continuousMode}
                  disabled={readOnly || locked || creating}
                  onChange={e => setContinuousMode(e.target.checked)}
                />
                <span>达成后继续找下一个改进（持续改进）</span>
              </label>

              <SupervisorRoleBar
                capabilities={capabilities}
                effectiveRoles={effectiveRoles}
                onChangeRole={onChangeRole}
                disabled={readOnly || locked || creating}
                apiCall={apiCall}
                workersData={workersData}
              />

              <SupervisorChecks
                apiCall={apiCall}
                repo={repo}
                onSummaryChange={handleChecksSummary}
                onSaved={onRefreshRepositories}
              />

              {checksMissing && (
                <p className="sup-checks-warning" role="status">
                  还没有设置自动检查，每一轮做完都会停下来请你检查。
                </p>
              )}

              {createError && (
                <div className="sup-banner sup-banner--error" role="alert">
                  <strong>没有开始成功</strong>
                  <span>{createError}</span>
                  {createErrorCode === 'executor_check_failed' && onOpenWorkers && (
                    <button type="button" className="sup-role-guidance-link" onClick={onOpenWorkers}>
                      去执行者页面测试
                    </button>
                  )}
                </div>
              )}

              <div className="sup-dispatch-footer">
                <span className="sup-dispatch-context-tag">
                  在这里改：<code>{repo.name}</code> / <code>{worktree.branch}</code>
                </span>
                <button
                  type="submit"
                  className="shell-button primary"
                  disabled={!outcome.trim() || creating || readOnly || locked}
                >
                  {creating ? '正在开始…' : '开始'}
                </button>
              </div>
              <p className="sup-goal-form-hint">会一轮一轮地改、验证，直到达成；需要你判断时会停下来问你。</p>
            </form>
          </section>
        )}

        {finishedGoals.length > 0 && (
          <section className={`sup-run-history sup-goal-history ${historyOpen ? 'is-expanded' : 'is-collapsed'}`} aria-label="以前的目标">
            <button
              type="button"
              className="sup-run-history-bar"
              aria-expanded={historyOpen}
              onClick={() => setHistoryOpen(prev => !prev)}
            >
              <span className="sup-run-history-chevron" aria-hidden="true">{historyOpen ? '▾' : '▸'}</span>
              <strong>以前的目标 · {finishedGoals.length} 个</strong>
              <span className="sup-run-history-hint">点开可以回看每一轮</span>
            </button>
            {historyOpen && (
              <div className="sup-goal-history-list" role="group" aria-label="以前的目标列表">
                {finishedGoals.map(goal => (
                  <button
                    key={goal.id}
                    type="button"
                    className={`sup-goal-history-row ${pinnedGoalId === goal.id ? 'selected' : ''}`}
                    onClick={() => handleSelectFinished(goal.id)}
                  >
                    <div className="sup-goal-history-row-main">
                      <GoalChip state={goal.state} />
                      <strong>{goalDisplayTitle(goal)}</strong>
                    </div>
                    <span className="sup-goal-history-row-meta">
                      第 {goal.iterations_used ?? '—'} / {goal.max_iterations} 轮 · {formatGoalTime(goal.updated_at || goal.created_at)}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  )
}
