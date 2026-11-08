import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  RoleOverrideEntry,
  RoleOverrides,
  SupervisorCapabilities,
  SupervisorRepo,
  SupervisorRoleKey,
  SupervisorWorktree,
} from './SupervisorWorkspace'
import { SupervisorRoleBar, supStateLabel } from './SupervisorWorkspace'

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
  if (!key) return '进行中'
  return GOAL_DECISION_LABELS[key] || '进行中'
}

export function isActiveGoalState(state?: string | null): boolean {
  return GOAL_ACTIVE_STATES.includes((state || '').trim().toLowerCase() as GoalState)
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
  onToggleExpand,
  onOpenTask,
}: {
  iteration: Iteration
  expanded: boolean
  onToggleExpand: () => void
  onOpenTask: (taskId: string) => void
}) {
  const taskState = iteration.task_state || iteration.task_final_state
  const prompt = iteration.prompt.trim()
  return (
    <li className="sup-goal-iteration">
      <div className="sup-goal-iteration-head">
        <strong className="sup-goal-iteration-n">第 {iteration.n} 轮</strong>
        <span className="sup-goal-iteration-task-state">{taskState ? supStateLabel(taskState) : '未开始'}</span>
        <span className="sup-goal-iteration-decision">{goalDecisionLabel(iteration.decision)}</span>
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
}) {
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
  const [pinnedGoalId, setPinnedGoalId] = useState('')
  const pinnedGoalIdRef = useRef('')
  pinnedGoalIdRef.current = pinnedGoalId

  // Keep the ref in sync immediately (actions read it right after pinning, before re-render).
  const pinGoal = useCallback((goalId: string) => {
    pinnedGoalIdRef.current = goalId
    setPinnedGoalId(goalId)
  }, [])

  const activeGoal = useMemo(() => goals.find(g => isActiveGoalState(g.state)) || null, [goals])
  const hasActiveGoal = Boolean(activeGoal)
  const shownGoal: Goal | null = activeGoal || (pinnedGoalId ? goals.find(g => g.id === pinnedGoalId) || null : null)
  const shownDetail: GoalDetail | null = detail && shownGoal && detail.id === shownGoal.id ? detail : null
  const finishedGoals = useMemo(() => goals.filter(g => !isActiveGoalState(g.state)), [goals])

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
      await loadDetail(currentActive.id)
      return
    }
    if (pinnedGoalIdRef.current && scoped.some(g => g.id === pinnedGoalIdRef.current)) {
      await loadDetail(pinnedGoalIdRef.current)
      return
    }
    setDetail(null)
  }, [apiCall, loadDetail, pinGoal, repo.id, worktree.id])

  // Initial load for this worktree (also decides the default sub-tab).
  useEffect(() => {
    setDetail(null)
    pinGoal('')
    setReplyMessage('')
    setConfirmStop(false)
    void refresh()
  }, [contextKey, pinGoal, refresh])

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
          setReplyMessage('')
        } else {
          pinGoal(res.id)
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

  const handleSelectFinished = useCallback(
    (goalId: string) => {
      pinGoal(goalId)
      void loadDetail(goalId)
    },
    [loadDetail, pinGoal],
  )

  const roundsUsed = shownDetail ? shownDetail.iterations_used ?? shownDetail.iterations.length : 0
  const roundsTotal = shownGoal?.max_iterations || DEFAULT_MAX_ROUNDS
  const canStop = Boolean(activeGoal) && !stopping
  const iterations = shownDetail?.iterations || []

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
              <span>主工作目录与未托管目录仅支持只读查看。请新建托管 Worktree 以在隔离目录中执行目标。</span>
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
                <h3>{shownGoal.title || shownGoal.outcome}</h3>
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

            {shownDetail?.summary.trim() && <p className="sup-goal-summary">{shownDetail.summary.trim()}</p>}
            {!shownDetail && <p className="sup-muted-note">正在读取这一目标…</p>}

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
              <span className="sup-card-hint">最新的一轮在最上面</span>
            </div>
            {iterations.length === 0 ? (
              <p className="sup-muted-note">正在准备第 1 轮，稍等几秒就会出现在这里。</p>
            ) : (
              <ol className="sup-goal-timeline">
                {iterations
                  .slice()
                  .sort((a, b) => b.n - a.n)
                  .map(iteration => (
                    <GoalIterationRow
                      key={iteration.n}
                      iteration={iteration}
                      expanded={expandedRound === iteration.n}
                      onToggleExpand={() => setExpandedRound(prev => (prev === iteration.n ? null : iteration.n))}
                      onOpenTask={onOpenTask}
                    />
                  ))}
              </ol>
            )}
          </section>
        )}

        {!hasActiveGoal && (
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

              {createError && (
                <div className="sup-banner sup-banner--error" role="alert">
                  <strong>没有开始成功</strong>
                  <span>{createError}</span>
                </div>
              )}

              <div className="sup-dispatch-footer">
                <span className="sup-dispatch-context-tag">
                  作用范围：<code>{repo.name}</code> / <code>{worktree.branch}</code>
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
                      <strong>{goal.title || goal.outcome}</strong>
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
