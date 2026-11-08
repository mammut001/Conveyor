// Chinese time labels and human error messages for the conversation screens.

const pad = (value: number) => String(value).padStart(2, '0')
const DAY_MS = 86_400_000

/** Accepts ISO strings, epoch milliseconds, or epoch seconds (server REAL columns). */
export function parseChatTime(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null
  const date = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function dayDiff(date: Date, now: Date): number {
  const start = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  return Math.round((start(now) - start(date)) / DAY_MS)
}

/** "14:05" today, "昨天 14:05", "11月3日 14:05", "2025年11月3日 14:05". */
export function formatChatTime(value: string | number | null | undefined, now = new Date()): string {
  const date = parseChatTime(value)
  if (!date) return ''
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  const days = dayDiff(date, now)
  if (days === 0) return time
  if (days === 1) return `昨天 ${time}`
  if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${time}`
}

/** "11月3日 14:05" regardless of how recent it is. */
export function formatDayTime(date: Date): string {
  return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** "刚刚", "5 分钟前", "3 小时前", then the same labels as formatChatTime. */
export function formatRelativeTime(value: string | number | null | undefined, now = new Date()): string {
  const date = parseChatTime(value)
  if (!date) return ''
  const minutes = Math.floor((now.getTime() - date.getTime()) / 60_000)
  if (minutes >= 0 && minutes < 1) return '刚刚'
  if (minutes >= 1 && minutes < 60) return `${minutes} 分钟前`
  if (minutes >= 60 && minutes < 6 * 60 && dayDiff(date, now) === 0) return `${Math.floor(minutes / 60)} 小时前`
  return formatChatTime(date.getTime(), now)
}

/** Full timestamp for tooltips: "2026年11月3日 14:05:09". */
export function formatFullTime(value: string | number | null | undefined): string {
  const date = parseChatTime(value)
  if (!date) return ''
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export const CHANNEL_LABELS: Record<string, string> = { web: '网页', telegram: 'Telegram', feishu: '飞书' }

/** A failed request that carries the HTTP status and the server's own text. */
export class ChatRequestError extends Error {
  status?: number
  constructor(message: string, status?: number) {
    super(message)
    this.name = 'ChatRequestError'
    this.status = status
  }
}

/** Thrown when an answer stream stops before the server says it is done. */
export const STREAM_INTERRUPTED = 'stream-interrupted'

export type HumanError = {
  /** Short Chinese sentence shown to the owner. */
  message: string
  /** Original technical text, shown only on request. */
  detail?: string
  /** Whether trying the same request again can help. */
  retryable: boolean
}

const NETWORK = /failed to fetch|networkerror|load failed|network error|network request failed/i

const KNOWN: Array<[RegExp, string, boolean]> = [
  [/token rejected|unauthori[sz]ed/i, '访问令牌已失效，请重新解锁', false],
  [/No eligible worker available in worker pool|(?:No such file or directory|not found|ENOENT)[\s\S]*?\b(codex|claude|pi|agy|cmd)\b|\b(codex|claude|pi|agy|cmd)\b[\s\S]*?(?:No such file or directory|not found|ENOENT)/i, '还没有可用的执行者，先去「执行者」页安装并登录一个', false],
  [/no model configured|model_providers/i, '还没有可用的模型，先在「设置」里配置模型', false],
  [/chat tier is disabled/i, '对话模型还没有配置好，暂时无法回复。请在「设置」里检查模型配置。', true],
  [/voice messages are disabled/i, '语音输入还没有开启，请改用文字。', false],
  [/kill switch/i, '浏览器紧急停止开关已打开，需要先关闭它才能批准这个操作。', false],
  [/timed out|timeout/i, '回复超时了，可以重试', true],
  [/message must be 1-8000/i, '消息太长了，请控制在 8000 字以内', false],
  [/invalid (web )?session_id/i, '这个会话已失效，请新建一个会话再发', false],
  [/only supports web sessions/i, '这个会话只能在原来的入口继续，请新建一个网页会话', false],
  [/audio is empty|upload size limit/i, '录音为空或太长，请重新录制', false],
  [/voice processing failed|incomplete audio|chunked voice/i, '语音没有识别成功，可以重试', true],
  [/internal error/i, '服务器暂时出了问题，可以稍后重试', true],
]

type DescribeOptions = {
  /** Sentence for errors we do not recognise. */
  fallback?: string
  /** Sentence when the browser could not reach the server at all. */
  network?: string
}

/**
 * Turn a backend/browser error into one short Chinese sentence. The original
 * text (which may name env vars or HTTP codes) is kept only as `detail`.
 */
export function describeChatError(error: unknown, options: DescribeOptions = {}): HumanError {
  const fallback = options.fallback || '消息没有发送成功，可以重试'
  const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : ''
  const status = error instanceof ChatRequestError ? error.status : undefined
  if (raw === STREAM_INTERRUPTED) return { message: '连接中断了，回答可能不完整，可以重试', retryable: true }
  const detail = [raw.trim(), status ? `HTTP ${status}` : ''].filter(Boolean).join(' · ') || undefined
  if (status === 401) return { message: '访问令牌已失效，请重新解锁', detail, retryable: false }
  if (!status && NETWORK.test(raw)) {
    return { message: options.network || '网络中断，消息没有发出去，可以重试', detail, retryable: true }
  }
  for (const [pattern, message, retryable] of KNOWN) {
    if (pattern.test(raw)) return { message, detail, retryable }
  }
  if (status === 429) return { message: '请求太频繁了，请稍等一会儿再试', detail, retryable: true }
  if (status && status >= 500) return { message: '服务器暂时出了问题，可以稍后重试', detail, retryable: true }
  return { message: fallback, detail, retryable: true }
}

/** A "reply" that is really the backend's own exception or HTTP failure text. */
export function looksLikeRawError(text: string): boolean {
  const value = (text || '').trim()
  if (!value) return false
  if (value.startsWith('[Errno')) return true
  if (/^(HTTP \d{3}|Error:)/.test(value)) return true
  // A real traceback, not a reply that quotes one inside a code block.
  return /Traceback \(most recent call last\)/.test(value) && !value.includes('```')
}

const DEEP_HINT = /\s*(?:发\s*\/deep\s*让\s*Codex\s*深入查证|想更深入地查证，可以让执行者再查一遍)。?\s*$/

/** The backend closes some replies with a "dig deeper" line the web owner cannot act on; it is dropped here. */
export function stripDeepHint(text: string): string {
  return (text || '').replace(DEEP_HINT, '').replace(/\n\s*[ℹ️⚠️️]+\s*$/u, '').trimEnd()
}

const TOOL_LABELS: Record<string, string> = {
  'pi.destructive_task': '删除类任务',
  'email.send': '发送邮件',
  'github.comment': '发表 GitHub 评论',
  'github.create_issue': '创建 GitHub 议题',
  'reminders.create': '创建提醒',
}
const RISK_LABELS: Record<string, string> = {
  destructive: '高', high: '高', critical: '高', medium: '中', moderate: '中', low: '低',
}

/** Plain Chinese for an approval prompt; the raw tool id stays available as `raw`. */
export function describeApproval(toolName: string, text = ''): { action: string; risk: string; raw: string } {
  const key = (toolName || '').trim()
  const parsed = /风险[:：]\s*([A-Za-z]+)/.exec(text)?.[1]?.toLowerCase()
  const action = TOOL_LABELS[key] || (key ? '一项需要确认的操作' : '操作确认')
  const risk = RISK_LABELS[parsed || ''] || (key === 'pi.destructive_task' ? '高' : '')
  return { action, risk, raw: key }
}
