// Chinese wording and small parsers for the 任务 view and the context panel.
// Pure functions only, so the panel components stay presentational.
import type { JobEvent } from './jobEvents'

export type FileTone = 'added' | 'modified' | 'deleted' | 'renamed'

/** `git status --short` code (already trimmed, e.g. "M", "??", "AM", "R") → chip. */
export function fileStatusInfo(status?: string): { label: string; tone: FileTone } {
  const code = (typeof status === 'string' ? status : '').toUpperCase()
  if (code.includes('R')) return { label: '重命名', tone: 'renamed' }
  if (code.includes('D')) return { label: '删除', tone: 'deleted' }
  if (code === '??' || code.includes('A') || code.includes('C')) return { label: '新增', tone: 'added' }
  return { label: '修改', tone: 'modified' }
}

/** Splits "web/src/App.tsx" into name + folder; renames ("a -> b") keep the new path. */
export function splitPath(raw: string): { name: string; dir: string; from?: string } {
  const source = typeof raw === 'string' ? raw : ''
  const [from, to] = source.includes(' -> ') ? source.split(' -> ', 2) : [undefined, source]
  const path = (to || '').replace(/^"|"$/g, '')
  const clean = path.replace(/\/+$/, '')
  const cut = clean.lastIndexOf('/')
  return {
    name: cut >= 0 ? clean.slice(cut + 1) + (path.endsWith('/') ? '/' : '') : path,
    dir: cut >= 0 ? clean.slice(0, cut) : '',
    from: from?.replace(/^"|"$/g, ''),
  }
}

export function modeLabel(mode?: string) {
  if (mode === 'fix') return '改代码'
  if (mode === 'run') return '提问'
  return mode || '—'
}

const TOOL_NAMES: Record<string, string> = {
  bash: '运行命令', shell: '运行命令', sh: '运行命令', exec: '运行命令', command: '运行命令',
  command_execution: '运行命令', run_command: '运行命令', local_shell: '运行命令',
  read: '读取文件', read_file: '读取文件', view: '读取文件', cat: '读取文件', open_file: '读取文件',
  write: '写入文件', write_file: '写入文件', create: '新建文件', create_file: '新建文件',
  edit: '编辑文件', edit_file: '编辑文件', str_replace: '编辑文件', str_replace_editor: '编辑文件',
  multiedit: '编辑文件', multi_edit: '编辑文件', apply_patch: '修改文件', patch: '修改文件',
  ls: '查看目录', list: '查看目录', list_dir: '查看目录', list_files: '查看目录',
  grep: '搜索内容', rg: '搜索内容', search: '搜索内容', search_files: '搜索内容',
  find: '查找文件', glob: '查找文件',
  web_search: '搜索网页', websearch: '搜索网页', web_fetch: '读取网页', webfetch: '读取网页', fetch: '读取网页',
  browser: '使用浏览器', computer: '操作电脑', screenshot: '截屏',
  todo_write: '更新待办', todowrite: '更新待办', update_plan: '更新计划', task: '分派子任务',
}

/** Readable Chinese name for a tool; unknown tools keep their own name. */
export function toolDisplayName(name?: string) {
  const raw = (name || '').trim()
  if (!raw) return '工具'
  return TOOL_NAMES[raw.toLowerCase()] || raw
}

const EVENT_KINDS: Record<string, string> = {
  'task.created': '已创建', 'task.queued': '排队中', 'task.started': '开始运行', 'task.completed': '已完成',
  'task.failed': '失败', 'task.cancelled': '已取消', 'task.cancel_requested': '请求停止', 'task.interrupted': '已中断',
  'assistant.delta': '回复片段', 'assistant.completed': '回复完成',
  'tool.started': '工具开始', 'tool.output': '工具输出', 'tool.completed': '工具完成', 'tool.failed': '工具失败',
  'approval.required': '等待确认', 'approval.accepted': '已批准', 'approval.approved': '已批准',
  'approval.rejected': '已拒绝', 'approval.expired': '已过期',
  'apply.started': '开始应用', 'apply.completed': '应用结束', 'discard.started': '开始放弃', 'discard.completed': '放弃结束',
  'agent.error': '出错', 'system.status': '系统状态',
}

export function eventKindLabel(kind: string) {
  if (EVENT_KINDS[kind]) return EVENT_KINDS[kind]
  if (kind.startsWith('refinement.')) return '继续修改'
  return kind
}

export type ToolCall = {
  id: string
  name: string
  status: 'running' | 'completed' | 'failed'
  output: string
  error: string
  timestamp: string
}

const text = (value: unknown) => typeof value === 'string' && value.trim() ? value : ''

/** One entry per tool call: started/output/completed events share a tool_call_id. */
export function groupToolCalls(events: JobEvent[]): ToolCall[] {
  const calls: ToolCall[] = []
  const byId = new Map<string, ToolCall>()
  for (const event of events) {
    if (!event.kind.startsWith('tool.')) continue
    const key = event.tool_call_id || event.event_id
    let call = byId.get(key)
    if (!call) {
      call = { id: key, name: '', status: 'running', output: '', error: '', timestamp: event.timestamp }
      byId.set(key, call)
      calls.push(call)
    }
    const payload = event.payload || {}
    if (!call.name) call.name = text(payload.name)
    const output = text(payload.output) || text(payload.text) || text(payload.result)
    if (output) call.output = output
    if (text(payload.error)) call.error = text(payload.error)
    if (event.kind === 'tool.failed') call.status = 'failed'
    else if (event.kind === 'tool.completed' && call.status !== 'failed') call.status = 'completed'
  }
  return calls
}

export function toolStatusLabel(status: ToolCall['status'], jobFinished: boolean) {
  if (status === 'failed') return '失败'
  if (status === 'completed') return '完成'
  return jobFinished ? '已结束' : '运行中'
}

export type DiffLine = { kind: 'file' | 'meta' | 'hunk' | 'add' | 'del' | 'context'; text: string }

const DIFF_NOISE = /^(index |--- |\+\+\+ |similarity index|dissimilarity index|old mode|new mode|rename to |copy to )/

/**
 * Readable unified diff: a file line shows just the path, hunk headers become
 * "第 N 行", and git bookkeeping (index, ---/+++ headers) is dropped. The
 * server's text may start with a status/stat preamble before "Unified diff:".
 */
export function diffLines(raw: string, limit = 4000): { lines: DiffLine[]; truncated: boolean; empty: boolean } {
  let body = raw || ''
  const marker = body.indexOf('Unified diff:\n')
  if (marker >= 0) body = body.slice(marker + 'Unified diff:\n'.length)
  if (!body.includes('diff --git') && !/^[+-]/m.test(body)) return { lines: [], truncated: false, empty: true }
  const all = body.replace(/\n$/, '').split('\n')
  const lines: DiffLine[] = []
  let state: 'outside' | 'header' | 'hunk' = 'outside'
  let wholeFile = false // new or deleted file: its single hunk header says nothing useful
  for (const line of all.slice(0, limit)) {
    if (line.startsWith('diff --git ')) {
      state = 'header'
      wholeFile = false
      const paths = line.match(/^diff --git a\/(.*) b\/(.*)$/)
      lines.push({ kind: 'file', text: paths ? paths[2] : line.slice('diff --git '.length) })
      continue
    }
    if (line.startsWith('@@')) {
      state = 'hunk'
      if (wholeFile) continue
      const range = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$/)
      const start = range ? (range[2] === '0' ? range[1] : range[2]) : ''
      lines.push({ kind: 'hunk', text: range ? `第 ${start} 行${range[3] ? ` · ${range[3]}` : ''}` : line })
      continue
    }
    if (state === 'hunk') {
      if (line.startsWith('\\')) continue // "\ No newline at end of file"
      lines.push({ kind: line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : 'context', text: line })
      continue
    }
    if (state === 'header') {
      if (DIFF_NOISE.test(line)) continue
      if (line.startsWith('new file mode')) { wholeFile = true; lines.push({ kind: 'meta', text: '新文件' }) }
      else if (line.startsWith('deleted file mode')) { wholeFile = true; lines.push({ kind: 'meta', text: '已删除的文件' }) }
      else if (line.startsWith('rename from ')) lines.push({ kind: 'meta', text: `由 ${line.slice('rename from '.length)} 重命名而来` })
      else if (line.startsWith('copy from ')) lines.push({ kind: 'meta', text: `复制自 ${line.slice('copy from '.length)}` })
      else if (line.startsWith('Binary files ')) lines.push({ kind: 'meta', text: '二进制文件，不显示内容' })
      else lines.push({ kind: line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : 'meta', text: line })
      continue
    }
    lines.push({ kind: line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : 'context', text: line })
  }
  return { lines, truncated: all.length > limit, empty: false }
}

export type ChangesDiff = {
  jobId: string; text: string; loading: boolean; failed: boolean
  additions: number; deletions: number; files: Record<string, { additions: number; deletions: number }>
}
export type DiffResponse = {
  diff?: string; stats?: { additions?: number; deletions?: number }
  files?: { path: string; additions?: number; deletions?: number }[]
}
export const emptyChangesDiff: ChangesDiff = { jobId: '', text: '', loading: false, failed: false, additions: 0, deletions: 0, files: {} }

export function changesDiffFrom(jobId: string, data: DiffResponse): ChangesDiff {
  const files: ChangesDiff['files'] = {}
  for (const file of data.files || []) files[file.path] = { additions: file.additions || 0, deletions: file.deletions || 0 }
  return {
    jobId, text: data.diff || '', loading: false, failed: false,
    additions: data.stats?.additions || 0, deletions: data.stats?.deletions || 0, files,
  }
}

/** "5 分钟后自动失效" from an epoch-seconds expiry. */
export function expiryText(expiresAt?: number, now = Date.now()) {
  if (!expiresAt) return '超时未处理会自动失效'
  const minutes = Math.ceil((expiresAt * 1000 - now) / 60_000)
  if (minutes <= 0) return '即将自动失效'
  return `${minutes} 分钟后自动失效`
}

export type ChangesOutcome = { tone: 'success' | 'warning' | 'muted'; text: string; detail: string; applied: boolean }

/** Server results of an approved Apply/Discard (English, technical) → one Chinese sentence. */
export function changesOutcome(action: string, status: string, result: string): ChangesOutcome | null {
  const detail = (result || '').split('\n')[0].trim()
  if (status === 'rejected') return null
  if (status === 'expired') return { tone: 'warning', text: '这次确认已过期，请重新操作。', detail: '', applied: false }
  if (/^Applied\b/.test(result)) return { tone: 'success', text: '改动已应用到项目文件夹。提交前请在项目里检查一遍。', detail: '', applied: true }
  if (/^Discarded\b/.test(result)) return { tone: 'muted', text: '改动已放弃。', detail: '', applied: false }
  if (/uncommitted changes/i.test(result)) return { tone: 'warning', text: '项目文件夹里有未提交的改动，为了安全没有应用。先提交或清理后再试。', detail, applied: false }
  if (/high-risk paths/i.test(result)) return { tone: 'warning', text: '改动涉及高风险文件，已拒绝应用。', detail, applied: false }
  if (/still running/i.test(result)) return { tone: 'warning', text: '还有运行没结束，结束后再试。', detail, applied: false }
  if (/no changes to apply/i.test(result)) return { tone: 'muted', text: '没有可应用的改动。', detail: '', applied: false }
  if (/No job worktree/i.test(result)) return { tone: 'muted', text: '隔离副本已经不存在。', detail: '', applied: false }
  if (/^Refused to apply/i.test(result)) return { tone: 'warning', text: '安全检查没有通过，改动没有应用。', detail, applied: false }
  if (/^Apply failed/i.test(result)) return { tone: 'warning', text: '应用失败，已尝试撤回。', detail, applied: false }
  if (!result) return null
  return { tone: 'muted', text: action === 'discard' ? '放弃操作已结束。' : '应用操作已结束。', detail, applied: false }
}

/** Live-screen server reasons/errors (English) → Chinese sentence. */
export function screenMessage(reason?: string | null, fallback = '画面暂时不可用') {
  const value = reason || ''
  if (!value) return fallback
  if (/Live screen is disabled/i.test(value)) return '实时画面未开启'
  if (/control is disabled/i.test(value)) return '未开启接管操作'
  if (/ImageMagick|`import`/i.test(value)) return '主机缺少截屏组件'
  if (/xdotool/i.test(value)) return '主机缺少输入组件，暂时不能接管'
  if (/graphical session/i.test(value)) return '主机上没有图形界面'
  if (/capture failed/i.test(value)) return '截屏失败'
  if (/takeover is already open/i.test(value)) return '已有人工接管在进行'
  if (/still finishing/i.test(value)) return '助手还在完成上一步，请稍后再试'
  if (/Take control before/i.test(value)) return '请先接管再操作'
  if (/Input injection failed/i.test(value)) return '输入没有送达主机'
  return fallback
}

export function pausedByLabel(owner?: string | null) {
  if (!owner) return ''
  if (owner === 'human-takeover') return '人工接管中'
  if (owner === 'live-screen') return '有人正在实时画面里操作'
  return `已被 ${owner} 暂停`
}

export function nodeStatusLabel(status: string) {
  return status === 'online' ? '在线' : status === 'offline' ? '离线' : status === 'unknown' ? '状态未知' : status
}

export function nodeTypeLabel(type: string) {
  return type === 'desktop' ? '电脑' : type === 'vps' ? '服务器' : type
}

export function durationText(seconds: number) {
  const value = Math.max(0, Math.round(seconds))
  const minutes = Math.floor(value / 60)
  const rest = value % 60
  if (minutes >= 60) return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
  if (minutes > 0) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
  return `${rest} 秒`
}

// ---------------------------------------------------------------------------
// 目标循环创建的任务标题（supervisor_goals._continuation_prompt / _first_iteration_prompt）
//
//   第 N 轮：`这是目标「<title>」的第 N 轮。目标：<outcome>\n\n本轮要做：<instruction>`
//   第 1 轮：`<outcome>\n\n这是目标的第 1 轮。`
//
// 老板只看中文短标题，所以解析成「目标第 N 轮：<这一轮要做的事>」，解析不了就返回空串
// （调用方退回显示原始 goal 文本）。纯函数，方便单测。
// ---------------------------------------------------------------------------

const GOAL_ROUND_TITLE_MAX_CHARS = 40
const GOAL_ROUND_MARKER = '本轮要做：'

export type GoalRoundTaskInfo = {
  round: number
  /** 目标标题（第 1 轮的任务文本里没有，只有 outcome）。 */
  goalTitle: string
  /** 这一轮围绕的目标结果，用来把同一个目标的多轮任务认成一组。 */
  outcome: string
  /** 这一轮要做的事（可能多行，取第一行）。 */
  instruction: string
}

function firstMeaningfulLine(text: string): string {
  return text
    .split('\n')
    .map(line => line.trim())
    .find(line => line.length > 0) || ''
}

/** 解析目标循环写下的任务目标文本；不是目标循环创建的任务返回 null。 */
export function parseGoalRoundTask(goal?: string | null): GoalRoundTaskInfo | null {
  const raw = typeof goal === 'string' ? goal.trim() : ''
  if (!raw) return null

  // 第 2 轮及以后：开头就是「这是目标「…」的第 N 轮。目标：…」
  const later = raw.match(/^这是目标「([^」]*)」的第 (\d+) 轮。目标：([^\n]*)/)
  if (later) {
    const round = Number.parseInt(later[2], 10)
    if (!Number.isFinite(round) || round < 1) return null
    const rest = raw.slice(later[0].length)
    const marker = rest.indexOf(GOAL_ROUND_MARKER)
    const source = marker >= 0 ? rest.slice(marker + GOAL_ROUND_MARKER.length) : rest
    return {
      round,
      goalTitle: (later[1] || '').trim(),
      outcome: (later[3] || '').trim(),
      instruction: firstMeaningfulLine(source),
    }
  }

  // 第 1 轮：`<outcome>\n\n这是目标的第 1 轮。`
  const first = raw.match(/这是目标的第 (\d+) 轮。/)
  if (first && typeof first.index === 'number') {
    const round = Number.parseInt(first[1], 10)
    if (!Number.isFinite(round) || round < 1) return null
    const outcome = firstMeaningfulLine(raw.slice(0, first.index))
    return { round, goalTitle: '', outcome, instruction: outcome }
  }
  return null
}

/** 同一个目标的多轮任务靠 outcome（第 1 轮没有标题）认成一组。 */
export function goalRoundKey(info: GoalRoundTaskInfo): string {
  return info.outcome || info.goalTitle || `round-${info.round}`
}

/** 任务标题里这一行短话：`目标第 N 轮：<这一轮要做的事>`，最长 40 个字再加「…」。 */
export function goalRoundTaskTitle(goal?: string | null): string {
  const info = parseGoalRoundTask(goal)
  if (!info) return ''
  const collapsed = info.instruction.replace(/\s+/g, ' ').trim()
  const cut = collapsed.length > GOAL_ROUND_TITLE_MAX_CHARS
    ? `${collapsed.slice(0, GOAL_ROUND_TITLE_MAX_CHARS)}…`
    : collapsed
  return cut ? `目标第 ${info.round} 轮：${cut}` : `目标第 ${info.round} 轮`
}

/** 按 `diff --git` 把一段 unified diff 切成每个文件一块（每块保留自己的文件头，
 *  这样 diffLines 仍然认识它）。文件头之前的统计前言直接丢掉。 */
export function splitDiffByFile(diff?: string | null): { path: string; text: string }[] {
  const raw = typeof diff === 'string' ? diff : ''
  if (!raw.trim()) return []
  const chunks: { path: string; text: string }[] = []
  let current: string[] | null = null
  for (const line of raw.split('\n')) {
    if (line.startsWith('diff --git ')) {
      if (current && current.length > 0) chunks.push({ path: diffChunkPath(current), text: current.join('\n') })
      current = [line]
      continue
    }
    if (current) current.push(line)
  }
  if (current && current.length > 0) chunks.push({ path: diffChunkPath(current), text: current.join('\n') })
  return chunks
}

function diffChunkPath(lines: string[]): string {
  const head = lines[0] || ''
  const match = head.match(/^diff --git a\/(.*) b\/(.*)$/)
  return match ? match[2] : head.slice('diff --git '.length)
}

export type JobErrorInfo = {
  message: string
  action?: 'workers' | 'settings'
  actionLabel?: string
  /** False for configuration errors: sending the same text again cannot fix them. */
  retryable?: boolean
}

export function describeJobError(error?: string | null): JobErrorInfo {
  const raw = (error || '').trim()
  if (!raw) {
    return { message: '任务没能启动，请稍后重试。' }
  }

  const isMissingWorkerPool = /No eligible worker available in worker pool/i.test(raw)
  const isMissingCli = /(?:No such file or directory|not found|ENOENT)[\s\S]*?\b(codex|claude|pi|agy|cmd)\b|\b(codex|claude|pi|agy|cmd)\b[\s\S]*?(?:No such file or directory|not found|ENOENT)/i.test(raw)

  if (isMissingWorkerPool || isMissingCli) {
    return {
      message: '还没有可用的执行者，先去「执行者」页安装并登录一个',
      action: 'workers',
      actionLabel: '去执行者',
    }
  }

  // The model answered, but not in a form the agent could finish: usually the wrong 接口类型 for this provider.
  if (/stream ended|finish_reason|terminal response event|Responses (API|stream)|invalid (JSON|response)/i.test(raw)) {
    return {
      message: '模型服务返回不完整，可以在模型设置里把接口类型换成 Chat Completions 再试',
      action: 'settings',
      actionLabel: '去设置',
    }
  }

  // Provider HTTP failures. Configuration problems (401/403/404) say what to change instead of "retype and resend".
  const providerStatus = /HTTP|status|provider|completions|responses/i.test(raw) ? raw.match(/\b(4\d\d|5\d\d)\b/)?.[1] : undefined
  if (providerStatus === '401' || providerStatus === '403') {
    return { message: `模型服务拒绝了这次请求（${providerStatus}），请在模型设置里检查 API 密钥`, action: 'settings', actionLabel: '去设置' }
  }
  if (providerStatus === '404') {
    return { message: '模型服务的接口地址不对（404），请在模型设置里检查接口地址', action: 'settings', actionLabel: '去设置' }
  }
  if (providerStatus === '429') {
    return { message: '模型服务请求太频繁或额度已用完（429），可以稍后再试，或在模型设置里换一个服务商', action: 'settings', actionLabel: '去设置' }
  }
  if (providerStatus && providerStatus.startsWith('5')) {
    return { message: `模型服务暂时出错（${providerStatus}），可以稍后再试` }
  }

  // Server setup problems (workspace folder, environment variables). Resending the same request cannot fix them.
  const isServerConfigError = /CODEX_WORKSPACE_ROOT|CODEX_TASK_ROOT|workspace root|git repo root|not a git (repo|repository)|is not a directory|does not exist or is not a directory/i.test(raw)
  const isMissingEnvVar = /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b[^\n]{0,40}\b(is not set|is required|must be set|missing)\b/.test(raw)
  if (isServerConfigError) {
    return {
      message: '项目文件夹没有设置好，任务无法在里面运行。请在服务器上检查项目文件夹设置，修好后再重新发起任务。',
      retryable: false,
    }
  }
  if (isMissingEnvVar) {
    return {
      message: '服务器配置缺少必要的设置，任务无法运行。请检查服务器配置，修好后再重新发起任务。',
      retryable: false,
    }
  }

  const isModelProviderError = /chat tier is disabled|no model configured|provider|api[-_]?key|OPENAI_API_KEY|ANTHROPIC_API_KEY|DEEPSEEK_API_KEY|model_providers/i.test(raw)

  if (isModelProviderError) {
    return {
      message: '还没有可用的模型，先在「设置」里配置模型',
      action: 'settings',
      actionLabel: '去设置',
    }
  }

  if (/No such file or directory|Errno|ENOENT|not found|spawn|Permission denied|Traceback|command/i.test(raw)) {
    return { message: '任务没能启动，请稍后重试。' }
  }

  return { message: '任务没能完成，可以补充说明后重新发送。' }
}

/** Raw job error (English, technical) → one short Chinese sentence for the failed-task card. */
export function jobErrorSummary(error?: string | null): string {
  return describeJobError(error).message
}
