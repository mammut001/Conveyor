// Pure helpers for moving a discussion draft into the reviewable task form.
// Nothing here starts a task or talks to the network.

export type TaskSuggestion = {
  prompt: string
  session_id: string
  goal?: string
  acceptance_criteria?: string
  source?: string
  execution_authorized?: boolean
  context?: string
}

export type HandoffMode = 'review' | 'read_only' | 'retry'

export type AppliedTaskDraft = {
  goal: string
  acceptanceCriteria: string
  preparedSessionId?: string
  fromSuggestion: boolean
  handoffMode: HandoffMode
}

export type RetryDraftResult =
  | { ok: true; goal: string; acceptanceCriteria: string }
  | { ok: false; reason: 'locked' | 'missing_goal' }

export const WIDE_REVIEW_NOTICE = '已从右侧对话填入建议目标，请检查或修改后点击「开始执行」。'
export const NARROW_REVIEW_NOTICE = '已从「讨论」填入建议目标，请在「任务与结果」检查或修改后点击「开始执行」。'
export const READ_ONLY_NOTICE = '这次讨论没有把原来的请求当成可以执行的授权。请在「任务与结果」里检查并修改目标和验收标准。讨论本身不会开始任务。'
export const PREPARE_BUTTON_LABEL = '准备执行任务 →'
export const DRAFT_BUTTON_LABEL = '整理成任务草稿'
export const WIDE_DISCUSSION_HINT = '确认方案后，可填入左侧「当前任务目标」并点击「开始执行」。'
export const NARROW_DISCUSSION_HINT = '确认方案后，可在「任务与结果」检查「当前任务目标」，再点击「开始执行」。'
export const RETRY_READY_NOTE = '已用原来的目标和验收标准填好草稿。这只是重新准备，不会开始执行。'
export const RETRY_LOCKED_NOTE = '这个工作目录正在执行任务，已锁定。草稿没有改动，也不会开始新的任务。'
export const RETRY_MISSING_NOTE = '原来的任务没有目标，没有填入草稿，也不会开始执行。'

const READ_ONLY_RE = /先不要执行|不要执行|先别执行|不要改代码|先不要改|别改代码|不要修改代码|只讨论|仅讨论|只读|do not execute|don't execute|do not change (?:the )?code|don't change (?:the )?code|read-only/i

function textOf(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** A short string that only refuses execution, not a plan that happens to mention those words. */
export function isOnlyProhibition(goal: string): boolean {
  const compact = textOf(goal).replace(/\s+/g, '')
  if (!compact || [...compact].length > 40) return false
  return READ_ONLY_RE.test(goal)
}

export function isReadOnlySuggestion(suggestion: TaskSuggestion): boolean {
  return suggestion.execution_authorized === false || suggestion.source === 'read_only_fallback'
}

export function prepareButtonLabel(suggestion: TaskSuggestion): string {
  return suggestion.execution_authorized === false ? DRAFT_BUTTON_LABEL : PREPARE_BUTTON_LABEL
}

export function handoffNotice(mode: HandoffMode | undefined, narrow: boolean): string {
  if (mode === 'read_only') return READ_ONLY_NOTICE
  return narrow ? NARROW_REVIEW_NOTICE : WIDE_REVIEW_NOTICE
}

/**
 * Fill the review form from a persisted or live suggestion.
 * A read-only request contributes context, never a prohibition as the goal.
 */
export function applyTaskSuggestion(suggestion: TaskSuggestion): AppliedTaskDraft {
  const readOnly = isReadOnlySuggestion(suggestion)
  const structured = textOf(suggestion.goal)
  const prompt = textOf(suggestion.prompt)
  const context = textOf(suggestion.context)
  let goal = ''
  if (readOnly) {
    if (structured && !isOnlyProhibition(structured)) goal = structured
    else if (context && !isOnlyProhibition(context)) goal = context
    else goal = ''
  } else if (structured) {
    goal = structured
  } else {
    goal = prompt
  }
  return {
    goal,
    acceptanceCriteria: textOf(suggestion.acceptance_criteria),
    preparedSessionId: suggestion.session_id,
    fromSuggestion: true,
    handoffMode: readOnly ? 'read_only' : 'review',
  }
}

/** Copy a failed task back into the draft. A lock or a missing goal changes nothing. */
export function prepareRetryDraft(input: {
  goal?: string
  acceptanceCriteria?: string
  locked?: boolean
}): RetryDraftResult {
  if (input.locked) return { ok: false, reason: 'locked' }
  const goal = textOf(input.goal)
  if (!goal) return { ok: false, reason: 'missing_goal' }
  return { ok: true, goal, acceptanceCriteria: textOf(input.acceptanceCriteria) }
}
