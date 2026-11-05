import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react';

export type SentryAlert = {
  source: string;
  severity: 'info' | 'warning' | 'critical';
  title: string;
  summary: string;
  fingerprint: string;
  suggested_actions?: { label: string; command: string }[];
  created_at?: string;
};

export type TeammateStatus = {
  enabled: boolean;
  is_paused: boolean;
  paused_until: string | null;
  muted_sources: string[];
  interval_seconds: number;
  cooldown_seconds: number;
  last_patrol_at: string | null;
  total_alerts_count: number;
  recent_alerts: SentryAlert[];
  monitored_services: string[];
  thresholds: {
    disk_pct: number;
    disk_gb: number;
    load_ratio: number;
    error_burst: number;
  };
  status_text: string;
};

export type PatrolResult = {
  ok: boolean;
  is_healthy: boolean;
  alerts: SentryAlert[];
  suppressed: SentryAlert[];
  timestamp: string;
};

export type TeammatePanelProps = {
  token: string;
  onSendToChat?: (command: string) => void;
};

type Problem = { message: string; detail?: string };

const SOURCE_LABELS: Record<string, string> = {
  'host.disk': '磁盘空间',
  'host.cpu': 'CPU 负载',
  'host.service': '系统服务',
  'host.logs': '错误日志',
  'repo.git': 'Git 工作区',
  'repo.ci': 'GitHub CI',
};

const SERVICE_LABELS: Record<string, string> = {
  'conveyor-telegram-bot': 'Telegram 机器人',
  'conveyor-feishu-bot': '飞书机器人',
  'conveyor-vps-computer': '远程电脑',
  'conveyor-scheduler': '定时任务',
};

const SEVERITY_LABELS: Record<SentryAlert['severity'], string> = {
  critical: '严重',
  warning: '警告',
  info: '提示',
};

const SEND_HINT = '填入对话输入框，确认后发送';

const sourceLabel = (source: string) => SOURCE_LABELS[source] || source;

const pad = (value: number) => String(value).padStart(2, '0');

function parseTime(iso?: string | null): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "11月3日 14:05"; the year only when it differs from this year. */
function absoluteTime(iso?: string | null): string {
  const date = parseTime(iso);
  if (!date) return '';
  const year = date.getFullYear() === new Date().getFullYear() ? '' : `${date.getFullYear()}年`;
  return `${year}${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** "刚刚" / "5 分钟前" / "3 小时前" for recent times, otherwise "11月3日 14:05". */
function relativeTime(iso?: string | null): string {
  const date = parseTime(iso);
  if (!date) return '';
  const elapsed = Date.now() - date.getTime();
  if (elapsed < 0) return absoluteTime(iso);
  const minutes = Math.floor(elapsed / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return absoluteTime(iso);
}

/** 300 → "5 分钟", 7200 → "2 小时". */
function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Number((seconds / 3600).toFixed(1))} 小时`;
  return `${Number((seconds / 86400).toFixed(1))} 天`;
}

/** Alert text marks paths and states with backticks; show those as inline code. */
function inlineCode(text: string): ReactNode[] {
  return text.split('`').map((part, index) => (index % 2 ? <code key={index}>{part}</code> : part));
}

async function failure(res: Response, message: string): Promise<Problem> {
  const body = await res.json().catch(() => null) as { error?: unknown } | null;
  return { message, detail: typeof body?.error === 'string' ? body.error : `HTTP ${res.status}` };
}

function AlertItem({ alert, muted, busy, onMute, onSendToChat }: {
  alert: SentryAlert;
  muted: boolean;
  busy: boolean;
  onMute: (source: string) => void;
  onSendToChat?: (command: string) => void;
}) {
  const severity: SentryAlert['severity'] = alert.severity in SEVERITY_LABELS ? alert.severity : 'info';
  const summary = alert.summary || '';
  const headline = summary.split('\n')[0].trim();
  const hasDetails = summary.includes('\n') || headline.length > 90;
  return (
    <li className={`guard-alert is-${severity}`}>
      <div className="guard-alert-head">
        <span className={`guard-badge guard-sev-${severity}`}>{SEVERITY_LABELS[severity]}</span>
        <strong className="guard-alert-title">{alert.title}</strong>
        <span className="guard-alert-meta">
          <span>{sourceLabel(alert.source)}</span>
          {alert.created_at && (
            <time dateTime={alert.created_at} title={absoluteTime(alert.created_at)}>{relativeTime(alert.created_at)}</time>
          )}
          {muted ? (
            <span className="guard-badge guard-badge-muted">已静音</span>
          ) : (
            <button
              type="button"
              className="guard-btn guard-btn-small guard-btn-quiet guard-alert-mute"
              disabled={busy}
              title={`以后不再推送「${sourceLabel(alert.source)}」类告警`}
              aria-label={`静音「${sourceLabel(alert.source)}」类告警`}
              onClick={() => onMute(alert.source)}
            >
              静音此类
            </button>
          )}
        </span>
      </div>
      {headline && <p className="guard-alert-summary">{inlineCode(headline)}</p>}
      {hasDetails && (
        <details className="guard-alert-details">
          <summary>查看详情</summary>
          <pre>{summary}</pre>
        </details>
      )}
      {alert.suggested_actions && alert.suggested_actions.length > 0 && (
        <div className="guard-alert-actions">
          {alert.suggested_actions.map((act) => (
            <button
              key={act.command}
              type="button"
              className="guard-btn guard-btn-small"
              title={SEND_HINT}
              onClick={() => onSendToChat?.(act.command)}
            >
              {act.label}{' '}
              <code className="guard-cmd">{act.command}</code>
            </button>
          ))}
        </div>
      )}
    </li>
  );
}

export function TeammatePanel({ token, onSendToChat }: TeammatePanelProps) {
  const [status, setStatus] = useState<TeammateStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [notice, setNotice] = useState('');
  const [patrolling, setPatrolling] = useState(false);
  const [patrolResult, setPatrolResult] = useState<PatrolResult | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [newMuteSource, setNewMuteSource] = useState('');

  const loadStatus = useCallback(async () => {
    if (!token) return;
    try {
      setLoading(true);
      const res = await fetch('/api/teammate/status', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        setProblem(await failure(res, '无法读取值守状态，请稍后重试。'));
        return;
      }
      const data = (await res.json()) as TeammateStatus;
      setStatus(data);
      setProblem(null);
    } catch {
      setProblem({ message: '无法连接服务器，值守状态暂时读取不到。' });
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 8000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const handleRunPatrol = async () => {
    if (patrolling || !token) return;
    try {
      setPatrolling(true);
      setNotice('');
      const res = await fetch('/api/teammate/patrol', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ force: true }),
      });
      if (!res.ok) {
        setProblem(await failure(res, '巡检没有完成，请稍后重试。'));
        return;
      }
      const data = (await res.json()) as PatrolResult;
      setPatrolResult(data);
      setProblem(null);
      void loadStatus();
    } catch {
      setProblem({ message: '无法连接服务器，巡检没有完成。' });
    } finally {
      setPatrolling(false);
    }
  };

  const handleAction = async (action: 'pause' | 'resume' | 'mute' | 'unmute', value?: unknown) => {
    if (actionBusy || !token) return;
    try {
      setActionBusy(true);
      setNotice('');
      const res = await fetch('/api/teammate/action', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ action, value }),
      });
      if (!res.ok) {
        setProblem(await failure(res, '操作没有完成，请稍后重试。'));
        return;
      }
      if (action === 'mute') setNewMuteSource('');
      setProblem(null);
      const label = typeof value === 'string' ? sourceLabel(value) : '';
      setNotice(
        action === 'pause' ? `已暂停 ${value} 小时，期间不会推送提醒。`
          : action === 'resume' ? '已恢复值守。'
          : action === 'mute' ? `已静音「${label}」，这类问题不会再推送提醒。`
          : `已取消静音「${label}」。`,
      );
      await loadStatus();
    } catch {
      setProblem({ message: '无法连接服务器，操作没有完成。' });
    } finally {
      setActionBusy(false);
    }
  };

  const muted = status?.muted_sources ?? [];
  const submitMute = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const typed = newMuteSource.trim();
    // Accept either the source code (host.disk) or its Chinese name (磁盘空间).
    const known = Object.entries(SOURCE_LABELS).find(([, label]) => label === typed);
    const source = (known ? known[0] : typed).toLowerCase();
    if (source) void handleAction('mute', source);
  };

  const thresholds = status?.thresholds;
  const services = status?.monitored_services ?? [];
  const modules = [
    {
      key: 'disk', title: '磁盘空间', sources: ['host.disk'], command: '/clean',
      description: `使用率超过 ${thresholds?.disk_pct ?? 90}% 或剩余空间少于 ${thresholds?.disk_gb ?? 3} GB 时提醒。`,
    },
    {
      key: 'cpu', title: 'CPU 负载', sources: ['host.cpu'], command: '/ps',
      description: `1 分钟平均负载超过 CPU 核数的 ${thresholds?.load_ratio ?? 2} 倍时提醒，并列出最耗资源的进程。`,
    },
    {
      key: 'services', title: '系统服务', sources: ['host.service'], command: '/status',
      description: '下面这些后台服务停止运行时立即提醒。',
    },
    {
      key: 'logs', title: '错误日志', sources: ['host.logs'], command: '/diagnose',
      description: `10 分钟内出现 ${thresholds?.error_burst ?? 5} 条以上错误日志时提醒。`,
    },
    {
      key: 'repo', title: 'Git 与 GitHub CI', sources: ['repo.git', 'repo.ci'], command: '/github_ci',
      description: '未提交的改动积压过多，或 main 分支的 GitHub CI 构建失败时提醒。',
    },
  ];

  const paused = Boolean(status?.is_paused);
  const enabled = status?.enabled !== false;
  const stateLabel = !status ? (loading ? '正在读取值守状态…' : '值守状态未知')
    : !enabled ? '值守未开启'
    : paused ? '已暂停'
    : '值守中';
  const dot = !status || !enabled ? 'is-muted' : paused ? 'is-warn' : 'is-ok';
  const interval = duration(status?.interval_seconds ?? 300);
  const cooldown = duration(status?.cooldown_seconds ?? 7200);
  const lastPatrol = status?.last_patrol_at ? relativeTime(status.last_patrol_at) : '尚未巡检';
  const statusLine = !status ? ''
    : !enabled ? `不会自动巡检，可以手动点“立即巡检”。上次巡检：${lastPatrol}`
    : paused ? `${status.paused_until ? `暂停到 ${absoluteTime(status.paused_until)}` : '暂停中，直到你手动恢复'}，期间不会推送提醒 · 上次巡检：${lastPatrol}`
    : `每 ${interval}自动巡检一次 · 上次巡检：${lastPatrol}`;
  const alerts = status?.recent_alerts ?? [];
  const unmutedKnown = Object.entries(SOURCE_LABELS).filter(([code]) => !muted.includes(code));

  return (
    <div className="guard-page guard-teammate">
      <div className="guard-stack">
        <p className="guard-intro">值守会定时检查服务器和代码仓库，发现问题时主动提醒你。</p>
        <section className={`guard-card guard-status-card${paused ? ' is-warn' : ''}`} aria-labelledby="guard-teammate-title">
          <div className="guard-status-main">
            <h3 id="guard-teammate-title" className="guard-status-title">
              <span className={`guard-dot ${dot}`} aria-hidden="true" />
              {stateLabel}
            </h3>
            {statusLine && <p className="guard-muted">{statusLine}</p>}
          </div>
          <div className="guard-actions">
            <button
              type="button"
              className="guard-btn guard-btn-primary"
              disabled={patrolling || (loading && !status)}
              aria-busy={patrolling || undefined}
              onClick={() => void handleRunPatrol()}
            >
              {patrolling ? '巡检中…' : '立即巡检'}
            </button>
            {paused ? (
              <button type="button" className="guard-btn" disabled={actionBusy} onClick={() => void handleAction('resume')}>
                恢复值守
              </button>
            ) : (
              <>
                <button type="button" className="guard-btn" disabled={actionBusy || !status} onClick={() => void handleAction('pause', 4)}>
                  暂停 4 小时
                </button>
                <button type="button" className="guard-btn" disabled={actionBusy || !status} onClick={() => void handleAction('pause', 24)}>
                  暂停 24 小时
                </button>
              </>
            )}
            <button type="button" className="guard-btn" disabled={loading} onClick={() => void loadStatus()}>
              {loading && status ? '刷新中…' : '刷新'}
            </button>
          </div>
        </section>

        {(notice || problem) && (
          <div className="guard-messages" aria-live="polite">
            {notice && <p role="status" className="guard-feedback guard-feedback-ok">{notice}</p>}
            {problem && (
              <p role="alert" className="guard-feedback guard-feedback-error">
                {problem.message}
                {problem.detail && <span className="guard-tech">{problem.detail}</span>}
              </p>
            )}
          </div>
        )}

        {patrolResult && (
          <section
            className={`guard-card guard-patrol ${patrolResult.is_healthy ? 'is-ok' : 'is-warn'}`}
            aria-labelledby="guard-patrol-title"
            aria-live="polite"
          >
            <div className="guard-card-head">
              <h3 id="guard-patrol-title" className="guard-card-title">
                {patrolResult.is_healthy ? '巡检完成：一切正常' : `巡检发现 ${patrolResult.alerts.length} 个问题`}
              </h3>
              <span className="guard-card-head-side">
                <time className="guard-muted" dateTime={patrolResult.timestamp} title={absoluteTime(patrolResult.timestamp)}>
                  {relativeTime(patrolResult.timestamp)}
                </time>
                <button type="button" className="guard-btn guard-btn-small guard-btn-quiet" onClick={() => setPatrolResult(null)}>
                  关闭
                </button>
              </span>
            </div>
            {patrolResult.alerts.length > 0 && (
              <ul className="guard-alert-list">
                {patrolResult.alerts.map((alert, idx) => (
                  <AlertItem
                    key={alert.fingerprint || idx}
                    alert={alert}
                    muted={muted.includes(alert.source)}
                    busy={actionBusy}
                    onMute={(source) => void handleAction('mute', source)}
                    onSendToChat={onSendToChat}
                  />
                ))}
              </ul>
            )}
            {patrolResult.suppressed.length > 0 && (
              <p className="guard-muted">另有 {patrolResult.suppressed.length} 个问题来自已静音的来源，没有列出。</p>
            )}
          </section>
        )}

        <section className="guard-card" aria-labelledby="guard-teammate-alerts-title">
          <div className="guard-card-head">
            <h3 id="guard-teammate-alerts-title" className="guard-card-title">最近告警</h3>
            {(status?.total_alerts_count ?? 0) > 0 && <span className="guard-count">累计 {status?.total_alerts_count} 次</span>}
          </div>
          {!status ? (
            <p className="guard-empty">{loading ? '加载中…' : '暂时读取不到告警记录。'}</p>
          ) : alerts.length > 0 ? (
            <ul className="guard-alert-list">
              {alerts.map((alert, idx) => (
                <AlertItem
                  key={`${alert.fingerprint}-${alert.created_at ?? idx}`}
                  alert={alert}
                  muted={muted.includes(alert.source)}
                  busy={actionBusy}
                  onMute={(source) => void handleAction('mute', source)}
                  onSendToChat={onSendToChat}
                />
              ))}
            </ul>
          ) : (
            <p className="guard-empty">最近没有告警，一切正常。</p>
          )}
        </section>

        <section className="guard-section" aria-labelledby="guard-teammate-modules-title">
          <h3 id="guard-teammate-modules-title" className="guard-section-title">巡检项目</h3>
          <div className="guard-module-grid">
            {modules.map((module) => {
              const moduleMuted = module.sources.every((source) => muted.includes(source));
              return (
                <article key={module.key} className="guard-card guard-module">
                  <div className="guard-module-head">
                    <h4 className="guard-module-title">{module.title}</h4>
                    {moduleMuted && <span className="guard-badge guard-badge-muted">已静音</span>}
                  </div>
                  <p className="guard-module-desc">{module.description}</p>
                  {module.key === 'services' && services.length > 0 && (
                    <ul className="guard-chips" aria-label="监测的服务">
                      {services.map((service) => (
                        <li key={service} className="guard-chip" title={service}>{SERVICE_LABELS[service] || service}</li>
                      ))}
                    </ul>
                  )}
                  <button
                    type="button"
                    className="guard-btn guard-btn-small guard-module-cmd"
                    title={SEND_HINT}
                    onClick={() => onSendToChat?.(module.command)}
                  >
                    在对话中执行 <code className="guard-cmd">{module.command}</code>
                  </button>
                </article>
              );
            })}
          </div>
        </section>

        <section className="guard-card" aria-labelledby="guard-teammate-mute-title">
          <h3 id="guard-teammate-mute-title" className="guard-card-title">静音的告警来源</h3>
          <p className="guard-card-desc">
            静音后，这类问题不会再推送提醒。没有静音的问题，同一个问题 {cooldown}内也只提醒一次。
          </p>
          {muted.length > 0 ? (
            <ul className="guard-chips">
              {muted.map((source) => (
                <li key={source} className="guard-chip guard-chip-removable">
                  <span>{sourceLabel(source)}</span>
                  {SOURCE_LABELS[source] && <code>{source}</code>}
                  <button
                    type="button"
                    className="guard-chip-remove"
                    disabled={actionBusy}
                    aria-label={`取消静音 ${sourceLabel(source)}`}
                    title="取消静音"
                    onClick={() => void handleAction('unmute', source)}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="guard-muted">还没有静音任何来源。</p>
          )}
          <form className="guard-inline-form" onSubmit={submitMute}>
            <label htmlFor="guard-mute-source" className="guard-visually-hidden">要静音的告警来源</label>
            <input
              id="guard-mute-source"
              className="guard-input"
              list="guard-mute-sources"
              placeholder="输入告警来源，例如 host.disk"
              value={newMuteSource}
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              onChange={(e) => setNewMuteSource(e.target.value)}
            />
            <datalist id="guard-mute-sources">
              {unmutedKnown.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
            </datalist>
            <button type="submit" className="guard-btn" disabled={!newMuteSource.trim() || actionBusy}>
              静音
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}
