// N25: what the owner types in 项目 is a draft until they press 开始 / 开始执行. The draft is
// kept in localStorage, per worktree, so a reload (or a switch to another page and back) never
// loses it. Every access is wrapped in try/catch: a browser with storage disabled (private
// mode, quota, blocked cookies) must not break the page — it just loses the draft.

const GOAL_PREFIX = 'conveyor-project-goal-draft-'
const DISPATCH_PREFIX = 'conveyor-project-dispatch-draft-'

export type GoalDraft = { outcome: string }
export type DispatchDraft = { goal: string; criteria: string }

function readRaw(key: string): string {
  try {
    return localStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

function writeRaw(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage unavailable: the draft simply does not survive a reload */
  }
}

function dropRaw(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    /* storage unavailable */
  }
}

/** 目标 tab: the 「描述你想要的结果」 text, for one worktree. */
export function readGoalDraft(contextKey: string): string {
  if (!contextKey) return ''
  const raw = readRaw(GOAL_PREFIX + contextKey)
  if (!raw) return ''
  try {
    const parsed = JSON.parse(raw) as Partial<GoalDraft> | null
    return typeof parsed?.outcome === 'string' ? parsed.outcome : ''
  } catch {
    return ''
  }
}

export function writeGoalDraft(contextKey: string, outcome: string): void {
  if (!contextKey) return
  const key = GOAL_PREFIX + contextKey
  if (!outcome.trim()) {
    dropRaw(key)
    return
  }
  writeRaw(key, JSON.stringify({ outcome } satisfies GoalDraft))
}

/** 任务与结果 tab: the dispatch textarea plus its 验收标准, for one worktree. */
export function readDispatchDraft(contextKey: string): DispatchDraft {
  if (!contextKey) return { goal: '', criteria: '' }
  const raw = readRaw(DISPATCH_PREFIX + contextKey)
  if (!raw) return { goal: '', criteria: '' }
  try {
    const parsed = JSON.parse(raw) as Partial<DispatchDraft> | null
    return {
      goal: typeof parsed?.goal === 'string' ? parsed.goal : '',
      criteria: typeof parsed?.criteria === 'string' ? parsed.criteria : '',
    }
  } catch {
    return { goal: '', criteria: '' }
  }
}

export function writeDispatchDraft(contextKey: string, draft: DispatchDraft): void {
  if (!contextKey) return
  const key = DISPATCH_PREFIX + contextKey
  if (!draft.goal.trim() && !draft.criteria.trim()) {
    dropRaw(key)
    return
  }
  writeRaw(key, JSON.stringify({ goal: draft.goal, criteria: draft.criteria } satisfies DispatchDraft))
}
