// Shared helpers for the knowledge pages: 记忆 (MemoryPanel), 收件箱 (InboxPanel)
// and the agent 资料 tab (AgentLibrary). Chinese labels, times and error text.

const pad2 = (value: number) => String(value).padStart(2, '0');
const isNumber = (value: string) => /^\d+$/.test(value);

export function parseTime(value?: string | number | null): Date | null {
  if (value === null || value === undefined || value === '') return null;
  const date = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function startOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** "11月3日 14:05" (with the year when it is not this year). */
export function formatDateTime(value?: string | number | null, now = Date.now()): string {
  const date = parseTime(value);
  if (!date) return '';
  const clock = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  const year = date.getFullYear() === new Date(now).getFullYear() ? '' : `${date.getFullYear()}年`;
  return `${year}${date.getMonth() + 1}月${date.getDate()}日 ${clock}`;
}

/** Past times: "刚刚", "5 分钟前", "今天 14:05", "昨天 14:05", "11月3日 14:05". */
export function formatWhen(value?: string | number | null, now = Date.now()): string {
  const date = parseTime(value);
  if (!date) return '';
  const diff = now - date.getTime();
  if (diff >= -60_000 && diff < 60_000) return '刚刚';
  if (diff > 0 && diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  const clock = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  const days = Math.round((startOfDay(new Date(now)) - startOfDay(date)) / 86_400_000);
  if (days === 0) return `今天 ${clock}`;
  if (days === 1) return `昨天 ${clock}`;
  return formatDateTime(date.getTime(), now);
}

/** Future times: "马上", "20 分钟后", "今天 21:00", "明天 09:00", "11月5日 09:00". */
export function formatUpcoming(value?: string | number | null, now = Date.now()): string {
  const date = parseTime(value);
  if (!date) return '';
  const diff = date.getTime() - now;
  if (diff < 60_000) return '马上';
  if (diff < 3_600_000) return `${Math.ceil(diff / 60_000)} 分钟后`;
  const clock = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  const days = Math.round((startOfDay(date) - startOfDay(new Date(now))) / 86_400_000);
  if (days === 0) return `今天 ${clock}`;
  if (days === 1) return `明天 ${clock}`;
  return formatDateTime(date.getTime(), now);
}

export function channelLabel(channel?: string | null): string {
  switch ((channel || '').toLowerCase()) {
    case 'web': return '网页';
    case 'telegram': return 'Telegram';
    case 'feishu': return '飞书';
    case 'agent': return '助手对话';
    default: return channel || '';
  }
}

export function jobStateLabel(state?: string | null): string {
  switch (state) {
    case 'queued': return '排队中';
    case 'running': return '运行中';
    case 'completed': return '已完成';
    case 'failed': return '失败';
    case 'cancelled': return '已取消';
    case 'interrupted': return '已中断';
    default: return state ? state.replace(/_/g, ' ') : '';
  }
}

// ---- Errors ----------------------------------------------------------------

export type FriendlyError = {
  /** Short Chinese sentence shown as the main message. */
  message: string;
  /** Muted technical detail (English server text without config hints), may be empty. */
  detail: string;
  /** The untouched server text, for a tooltip. */
  raw: string;
};

const HAS_CJK = /[㐀-鿿]/;

/** Remove "(set CONVEYOR_…)" style configuration hints from server text. */
export function stripConfigHint(text: string): string {
  return text
    .replace(/\s*\((?:set|requires?)\s+CONVEYOR_[^)]*\)/gi, '')
    .replace(/\s*(?:set|requires?)\s+CONVEYOR_[A-Z0-9_]+(?:=\S+)?(?:\s+(?:or|and)\s+CONVEYOR_[A-Z0-9_]+(?:=\S+)?)*/gi, '')
    .trim();
}

const KNOWN_ERRORS: Array<[RegExp, string]> = [
  [/routines are disabled/i, '定时任务功能未开启'],
  [/memory is disabled/i, '记忆功能未开启'],
  [/webhooks are disabled/i, 'Webhook 功能未开启'],
  [/agents are disabled/i, '助手功能未开启'],
  [/memory editing requires/i, '当前的记忆版本不支持编辑'],
  [/agent not found/i, '找不到这个助手，请刷新页面后重试'],
  [/memory not found/i, '这条记忆已经不存在了，请刷新'],
  [/routine #?\d* ?not found/i, '这个定时任务已经不存在了，请刷新'],
  [/hook not found/i, '这个 Webhook 已经不存在了'],
  [/name must be 1-80/i, '名称需要 1–80 个字'],
  [/prompt must be 1-2000/i, '要做的事需要 1–2000 个字'],
  [/maximum of \d+ routines/i, '定时任务已达上限（最多 50 个），请先删除不用的'],
  [/unsupported delivery channel|deliver must be/i, '不支持这个发送渠道'],
  [/cron|field|day-of-week|day-of-month|invalid (?:step|range|element|expression|empty item)|out of range|range start|no matching fire time/i,
    '运行时间的格式不对，请从常用时间里选，或检查 cron 表达式'],
  [/secrets and credentials/i, '不能保存密码、密钥等敏感信息'],
  [/untrusted sources cannot create profile/i, '外部来源的内容不能存入常用档案'],
  [/log entry must contain/i, '日志需要 1–4000 个字'],
  [/profile exceeds/i, '常用档案已满，请改存为“事实”'],
  [/tier must be/i, '请选择记忆类型'],
  [/text is required|numeric memory id and text/i, '请先写下要记住的内容'],
  [/internal error/i, '服务器出错了，请稍后重试'],
  [/token rejected|unauthori[sz]ed/i, '登录已失效，请重新解锁控制台'],
  [/failed to fetch|networkerror|load failed|network request failed/i, '网络连接失败，请检查网络后重试'],
];

/**
 * Turn a server or network error into a short Chinese sentence.
 * Chinese server text (memory refusals) is already readable and kept as is.
 */
export function friendlyError(reason: unknown, fallback: string): FriendlyError {
  const raw = (reason instanceof Error ? reason.message : typeof reason === 'string' ? reason : '').trim();
  const stripped = stripConfigHint(raw);
  if (stripped && HAS_CJK.test(stripped)) return { message: stripped, detail: '', raw };
  const known = KNOWN_ERRORS.find(([pattern]) => pattern.test(raw));
  const message = known ? known[1] : fallback;
  return { message, detail: stripped && stripped !== message ? stripped : '', raw };
}

/** Read `{error}` from a failed response, falling back to "HTTP <status>". */
export async function responseError(res: Response): Promise<Error> {
  const body = await res.json().catch(() => ({} as { error?: unknown }));
  const text = typeof body?.error === 'string' && body.error ? body.error : `HTTP ${res.status}`;
  return new Error(text);
}

export function ErrorNote({ error, onDismiss, onRetry, className = '' }: {
  error: FriendlyError; onDismiss?: () => void; onRetry?: () => void; className?: string;
}) {
  return (
    <div className={`kn-error ${className}`} role="alert" title={error.raw || undefined}>
      <div className="kn-error-text">
        <strong>{error.message}</strong>
        {error.detail && <small>{error.detail}</small>}
      </div>
      {(onRetry || onDismiss) && <div className="kn-error-actions">
        {onRetry && <button type="button" className="kn-btn" onClick={onRetry}>重试</button>}
        {onDismiss && <button type="button" className="kn-btn kn-btn-ghost" aria-label="关闭提示" onClick={onDismiss}>×</button>}
      </div>}
    </div>
  );
}

// ---- Schedules (5-field cron, evaluated by the server) -----------------------

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];

export function splitCron(expr: string): string[] {
  return expr.trim().split(/\s+/).filter(Boolean);
}

/** Plain-Chinese reading of a cron expression, or null when it is not a common shape. */
export function describeCron(expr?: string | null): string | null {
  const parts = splitCron(expr || '');
  if (parts.length !== 5) return null;
  const [minute, hour, dom, month, dow] = parts;
  if (month !== '*') return null;

  let clock: string | null = null;
  if (isNumber(minute) && Number(minute) <= 59) {
    if (isNumber(hour) && Number(hour) <= 23) clock = `${pad2(Number(hour))}:${pad2(Number(minute))}`;
    else if (/^\d+(,\d+)+$/.test(hour)) {
      const hours = hour.split(',').map(Number);
      if (hours.every(value => value <= 23)) clock = hours.map(value => `${pad2(value)}:${pad2(Number(minute))}`).join('、');
    }
  }

  if (dom === '*' && dow === '*') {
    if (minute === '*' && hour === '*') return '每分钟';
    const everyMinutes = /^\*\/(\d+)$/.exec(minute);
    if (everyMinutes && hour === '*') return `每 ${Number(everyMinutes[1])} 分钟`;
    if (isNumber(minute) && Number(minute) <= 59 && hour === '*') {
      return Number(minute) === 0 ? '每小时整点' : `每小时第 ${Number(minute)} 分`;
    }
    const everyHours = /^\*\/(\d+)$/.exec(hour);
    if (isNumber(minute) && Number(minute) <= 59 && everyHours) {
      return `每 ${Number(everyHours[1])} 小时${Number(minute) === 0 ? '（整点）' : `（第 ${Number(minute)} 分）`}`;
    }
    return clock ? `每天 ${clock}` : null;
  }
  if (!clock) return null;
  if (dom === '*') {
    if (dow === '1-5') return `工作日 ${clock}`;
    if (['0,6', '6,0', '6,7', '6-7'].includes(dow)) return `周末 ${clock}`;
    if (/^[0-7](,[0-7])*$/.test(dow)) {
      const names = [...new Set(dow.split(',').map(value => WEEKDAYS[Number(value)]))];
      return `每${names.join('、')} ${clock}`;
    }
    const range = /^([0-7])-([0-7])$/.exec(dow);
    if (range && Number(range[1]) < Number(range[2])) return `每${WEEKDAYS[Number(range[1])]}至${WEEKDAYS[Number(range[2])]} ${clock}`;
    return null;
  }
  if (dow === '*' && /^\d+(,\d+)*$/.test(dom)) {
    const days = dom.split(',').map(Number);
    if (days.every(value => value >= 1 && value <= 31)) return `每月 ${days.join('、')} 号 ${clock}`;
  }
  return null;
}

/** "每天 09:00", or the raw expression labelled as custom. */
export function scheduleLabel(expr?: string | null): string {
  return describeCron(expr) || `自定义时间（${(expr || '').trim() || '未设置'}）`;
}

export type SchedulePreset = { id: string; label: string; timed: boolean; build: (hour: number, minute: number) => string };

export const SCHEDULE_PRESETS: SchedulePreset[] = [
  { id: 'daily', label: '每天', timed: true, build: (h, m) => `${m} ${h} * * *` },
  { id: 'workdays', label: '工作日', timed: true, build: (h, m) => `${m} ${h} * * 1-5` },
  { id: 'weekly', label: '每周一', timed: true, build: (h, m) => `${m} ${h} * * 1` },
  { id: 'monthly', label: '每月 1 号', timed: true, build: (h, m) => `${m} ${h} 1 * *` },
  { id: 'hourly', label: '每小时', timed: false, build: () => '0 * * * *' },
];

/** Which preset (if any) produced this expression. */
export function presetFor(expr: string): string | null {
  const parts = splitCron(expr);
  if (parts.length !== 5) return null;
  const line = parts.join(' ');
  if (line === '0 * * * *') return 'hourly';
  const rest = parts.slice(2);
  if (!cronClock(line)) return null;
  const tail = rest.join(' ');
  return tail === '* * *' ? 'daily' : tail === '* * 1-5' ? 'workdays' : tail === '* * 1' ? 'weekly' : tail === '1 * *' ? 'monthly' : null;
}

/** "HH:MM" when the expression runs at one fixed clock time, else null. */
export function cronClock(expr: string): string | null {
  const parts = splitCron(expr);
  if (parts.length !== 5 || !isNumber(parts[0]) || !isNumber(parts[1])) return null;
  const minute = Number(parts[0]);
  const hour = Number(parts[1]);
  return minute <= 59 && hour <= 23 ? `${pad2(hour)}:${pad2(minute)}` : null;
}

/** Replace the clock time of a fixed-time expression. */
export function withClock(expr: string, clock: string): string {
  const match = /^(\d{1,2}):(\d{2})$/.exec(clock);
  const parts = splitCron(expr);
  if (!match || parts.length !== 5) return expr;
  parts[0] = String(Number(match[2]));
  parts[1] = String(Number(match[1]));
  return parts.join(' ');
}
