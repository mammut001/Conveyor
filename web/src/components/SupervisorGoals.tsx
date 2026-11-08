import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  RoleOverrideEntry,
  RoleOverrides,
  SupervisorCapabilities,
  SupervisorRepo,
  SupervisorRoleKey,
  SupervisorWorktree,
} from './SupervisorWorkspace'
import SupervisorChecks, { type SupervisorChecksSummary } from './SupervisorChecks'
import { SupervisorRoleBar, supStateLabel } from './SupervisorWorkspace'
import SupervisorMerge from './SupervisorMerge'
import { SupervisorDiffView, useSupervisorDiff } from './SupervisorDiff'

// ---------------------------------------------------------------------------
// 目标（goal-driven loop）contract types — mirrors supervisor_goals.py payloads.
// All owner-facing copy is Simplified Chinese; raw backend codes never render.
// ---------------------------------------------------------------------------

export type GoalState = 'running' | 'waiting_user' | 'achieved' | 'stopped' | 'exhausted' | 'failed'

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
}

export type GoalDetail = Goal & {
  iterations: Iteration[]
  iterations_used: number
}

const GOAL_STATE_LABELS: Record<GoalState, string> = {
  running: '进行中',
  waiting_user: '等你回复',
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

const GOAL_ACTIVE_STATES: GoalState[] = ['running', 'waiting_user']

const ROLE_KEYS: SupervisorRoleKey[] = ['analyst', 'implementer', 'reviewer']

const DEFAULT_MAX_ROUNDS = 5
const MIN_MAX_ROUNDS = 1
const MAX_MAX_ROUNDS = 20
const POLL_INTERVAL_MS = 3000

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
        main: reason || `已经按你的要求停止，${kept}`,
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
  if (key === 'waiting_user') return 'waiting'
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
  const [outcome, setOutcome] = useState('')
  const [maxRounds, setMaxRounds] = useState(String(DEFAULT_MAX_ROUNDS))
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
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

  // Poll the running goal every 3s while this tab is visible; stop otherwise.
  useEffect(() => {
    if (!visible || !hasActiveGoal || activeGoal?.state !== 'running') return
    const timer = window.setInterval(() => {
      void refresh()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [visible, hasActiveGoal, activeGoal?.state, refresh])

  const activeGoalKey = activeGoal?.id || ''
  useEffect(() => {
    onActiveGoalChange(hasActiveGoal)
  }, [hasActiveGoal, onActiveGoalChange, activeGoalKey])

  const handleStart = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const text = outcome.trim()
      if (!text || creating || readOnly || locked) return
      const roleOverrides: Record<string, { executor: string; model?: string }> = {}
      for (const key of ROLE_KEYS) {
        const role = effectiveRoles[key]
        if (!role.executor) continue
        const entry: { executor: string; model?: string } = { executor: role.executor }
        const model = role.model.trim()
        if (model) entry.model = model
        roleOverrides[key] = entry
      }
      setCreating(true)
      setCreateError('')
      try {
        await apiCall<Goal>('/api/supervisor/goals', {
          method: 'POST',
          body: JSON.stringify({
            outcome: text,
            max_iterations: clampRounds(maxRounds),
            repo_id: repo.id,
            worktree_id: worktree.id,
            ...(Object.keys(roleOverrides).length > 0 ? { role_overrides: roleOverrides } : {}),
          }),
        })
        setOutcome('')
        setFormRequested(false)
        await refresh()
      } catch (err) {
        setCreateError(goalChineseError(err))
      } finally {
        setCreating(false)
      }
    },
    [apiCall, creating, effectiveRoles, locked, maxRounds, outcome, readOnly, refresh, repo.id, worktree.id],
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
  // 跑完之后的结果卡文案（标题 + 白话结论 + 补充说明）。
  const resultCopy = goalFinished && shownGoal ? goalResultCopy(shownGoal, shownDetail) : null
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
                  第 {roundsUsed} / {roundsTotal} 轮
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
                  <button type="button" className="shell-button primary sup-goal-new-button" onClick={handleStartNew}>
                    开始新目标
                  </button>
                </div>
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
              <div className="sup-goal-form-row">
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
              </div>

              <SupervisorRoleBar
                capabilities={capabilities}
                effectiveRoles={effectiveRoles}
                onChangeRole={onChangeRole}
                disabled={readOnly || locked || creating}
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
